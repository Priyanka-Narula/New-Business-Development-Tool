#!/usr/bin/env python3
"""Command-line entry point for Middle East LinkedIn collection."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.db.storage import ScanStore, export_csv
from aambridge.logging_config import configure_logging
from aambridge.settings import Settings
from config import (
    SECTOR_KEYWORDS,
    build_post_search_specs,
    build_search_specs,
    normalize_sectors,
)
from linkedin_jobs_scraper import experience_level_choices, scrape_jobs
from linkedin_posts_scraper import scrape_posts
from scraper_common import automatic_login, create_driver, manual_login

ROOT = Path(__file__).resolve().parent


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect KSA/UAE recruitment market signals from LinkedIn."
    )
    parser.add_argument(
        "--mode", choices=("posts", "jobs", "both"), default="both"
    )
    parser.add_argument(
        "--sectors",
        nargs="+",
        metavar="SECTOR",
        help=(
            "Space-separated sectors. Defaults to all. Choices: "
            + ", ".join(SECTOR_KEYWORDS)
        ),
    )
    parser.add_argument(
        "--max-per-query", type=int, default=25, help="Maximum accepted rows per query."
    )
    parser.add_argument(
        "--time-filter",
        choices=("any", "day", "week", "month"),
        default="week",
    )
    parser.add_argument(
        "--gcc",
        action="store_true",
        help="Also search Qatar, Bahrain, Oman, and Kuwait.",
    )
    parser.add_argument(
        "--descriptions",
        action="store_true",
        help="Open each Jobs result to collect its description (much slower).",
    )
    parser.add_argument(
        "--experience-levels",
        nargs="+",
        choices=("all", *experience_level_choices()),
        default=("all",),
        help=(
            "LinkedIn Jobs experience filters. Defaults to all levels so junior "
            "volume remains available for later ratios."
        ),
    )
    parser.add_argument(
        "--query-set",
        choices=("hiring", "expansion", "all"),
        default="hiring",
        help="LinkedIn Posts query set. Jobs searches are unaffected.",
    )
    parser.add_argument(
        "--export-csv",
        type=Path,
        metavar="DIR",
        help="Export current database records to CSV files after the scan.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Deprecated alias for --export-csv.",
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for this run.",
    )
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help="Do not fill saved credentials; sign in yourself in Chrome.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings_kwargs = (
        {"database_url": args.database_url} if args.database_url else {}
    )
    settings = Settings(**settings_kwargs)
    configure_logging(settings)
    if args.max_per_query < 1:
        raise SystemExit("--max-per-query must be at least 1")
    try:
        sectors = normalize_sectors(args.sectors)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    job_specs = build_search_specs(sectors, include_gcc=args.gcc)
    post_specs = build_post_search_specs(
        sectors,
        include_gcc=args.gcc,
        query_set=args.query_set,
    )
    log = logging.getLogger("middle-east-scraper")
    log.info(
        "Prepared %s post and %s job searches across %s sector(s).",
        len(post_specs),
        len(job_specs),
        len(sectors),
    )

    engine = create_database_engine(
        settings.database_url, echo=settings.database_echo
    )
    initialise_database(engine)
    session_factory = create_session_factory(engine)
    scan_args = vars(args).copy()
    if scan_args.get("database_url"):
        scan_args["database_url"] = "<configured>"
    scan = ScanStore.start(
        session_factory,
        mode=args.mode,
        args=scan_args,
    )

    driver = None
    counts = {"posts": 0, "jobs": 0}
    try:
        driver = create_driver()
        if args.manual_login:
            manual_login(driver)
        else:
            automatic_login(driver)
        if args.mode in ("posts", "both"):
            counts["posts"] = scrape_posts(
                driver=driver,
                specs=post_specs,
                max_per_query=args.max_per_query,
                time_filter=args.time_filter,
                include_gcc=args.gcc,
                on_record=lambda record: scan.save_post(record.storage_fields()),
                progress=scan,
            )
        if args.mode in ("jobs", "both"):
            counts["jobs"] = scrape_jobs(
                driver=driver,
                specs=job_specs,
                max_per_query=args.max_per_query,
                time_filter=args.time_filter,
                include_gcc=args.gcc,
                include_description=args.descriptions,
                experience_levels=args.experience_levels,
                on_record=lambda record: scan.save_job(record.storage_fields()),
                progress=scan,
            )
    except KeyboardInterrupt:
        scan.finish("interrupted")
        log.warning("Stopped by user; all parsed records were retained.")
        return_code = 130
    except Exception:
        scan.finish("failed")
        log.exception("Scan failed; all parsed records were retained.")
        raise
    else:
        scan.finish("completed")
        return_code = 0
    finally:
        if driver is not None:
            driver.quit()

    export_dir = args.export_csv or args.output_dir
    if export_dir is not None:
        export_csv(session_factory, export_dir)
        log.info("Exported database records to %s.", export_dir)
    stats = scan.stats()
    log.info("Finished: %s posts, %s jobs.", counts["posts"], counts["jobs"])
    log.info("Scan %s statistics: %s", scan.scan_run_id, stats)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
