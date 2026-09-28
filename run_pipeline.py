#!/usr/bin/env python3
"""One-command AAMBRIDGE run with live stage banners."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.db.models import Job, Post, SourceRecord
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.logging_config import configure_logging
from aambridge.settings import Settings

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("run_pipeline")

STAGES = (
    "SCRAPE",
    "CLEAN",
    "RESOLVE",
    "SIGNALS",
    "SCORE",
    "EXPORT",
    "DASHBOARD",
)


class EmptyDatabaseError(RuntimeError):
    """Raised when --skip-scrape is set and the database has no collected rows."""


def database_has_records(session_factory: sessionmaker[Session]) -> bool:
    """Return True when any source, job, or post row already exists."""

    with session_factory() as session:
        for model in (SourceRecord, Job, Post):
            count = session.scalar(select(func.count()).select_from(model))
            if count:
                return True
    return False


def decide_scrape(
    *,
    force_scrape: bool,
    skip_scrape: bool,
    has_records: bool,
) -> bool:
    """Choose whether to scrape. Auto mode scrapes only when the database is empty."""

    if force_scrape and skip_scrape:
        raise ValueError("Use either --scrape or --skip-scrape, not both.")
    if skip_scrape:
        if not has_records:
            raise EmptyDatabaseError(
                "Database has no source_record, job, or post rows. "
                "Run without --skip-scrape, or pass --scrape to collect first."
            )
        return False
    if force_scrape:
        return True
    return not has_records


def _banner(index: int, name: str, *, module: str, file: str, extra: str = "") -> None:
    log.info("%s", "")
    log.info("========== [%s/%s] %s ==========", index, len(STAGES), name)
    log.info("module: %s", module)
    log.info("file:   %s", file)
    if extra:
        log.info("%s", extra)


def _summarise(label: str, stats: Any) -> None:
    log.info("%s: %s", label, stats)


def parse_args(argv: Sequence[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="run_pipeline.py",
        allow_abbrev=False,
        description=(
            "Run clean through score, export CSVs, and open the dashboard. "
            "Scrapes LinkedIn only when the database is empty, unless "
            "--scrape or --skip-scrape is set."
        ),
    )
    scrape_mode = parser.add_mutually_exclusive_group()
    scrape_mode.add_argument(
        "--scrape",
        action="store_true",
        help="Always scrape LinkedIn before downstream stages.",
    )
    scrape_mode.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Never scrape. Fails if the database has no collected rows.",
    )
    parser.add_argument("--database-url", help="Override DATABASE_URL for this run.")
    parser.add_argument("--model", default="llama3.1")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int, help="Limit extraction to N source records.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output",
        help="Directory for jobs, posts, and Top 50 CSV exports.",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Export files and exit without starting Streamlit.",
    )
    parser.add_argument("--port", type=int, default=8501)
    return parser.parse_known_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args, scrape_args = parse_args(argv)
    settings = Settings(
        **({"database_url": args.database_url} if args.database_url else {})
    )
    configure_logging(settings)

    engine = create_database_engine(settings.database_url)
    initialise_database(engine)
    sessions = create_session_factory(engine)
    has_records = database_has_records(sessions)
    try:
        will_scrape = decide_scrape(
            force_scrape=args.scrape,
            skip_scrape=args.skip_scrape,
            has_records=has_records,
        )
    except (ValueError, EmptyDatabaseError) as exc:
        log.error("%s", exc)
        return 2

    log.info("Pipeline starting. database=%s", settings.database_url)
    log.info(
        "Scrape decision: %s (force=%s skip=%s existing_records=%s)",
        "run" if will_scrape else "skip",
        args.scrape,
        args.skip_scrape,
        has_records,
    )

    from aambridge.dashboard.queries import PipelineFilters, export_top50_csv
    from aambridge.db.storage import export_csv
    from aambridge.extract.service import process_database
    from aambridge.resolve.companies import resolve_all
    from aambridge.scoring.engine import score_companies
    from aambridge.signals.detect import detect_signals
    from run_scraper import main as scrape_main

    if will_scrape:
        _banner(
            1,
            "SCRAPE",
            module="run_scraper.main",
            file="run_scraper.py",
        )
        forwarded = list(scrape_args)
        if args.database_url:
            forwarded.extend(["--database-url", args.database_url])
        scrape_result = scrape_main(forwarded)
        if scrape_result not in {0, None}:
            log.error("Scrape stopped with exit code %s.", scrape_result)
            return int(scrape_result)
        _summarise("Scrape", "completed")
    else:
        _banner(
            1,
            "SCRAPE",
            module="run_scraper.main",
            file="run_scraper.py",
            extra=(
                "status: skipped (database already has records)"
                if has_records
                else "status: skipped"
            ),
        )

    _banner(
        2,
        "CLEAN",
        module="aambridge.extract.service.process_database",
        file="aambridge/extract/service.py",
    )
    extraction_stats = process_database(
        sessions,
        model=args.model,
        workers=args.workers,
        max_chars=settings.extraction_max_chars,
        limit=args.limit,
        retry_failed=True,
        max_attempts=settings.extraction_max_attempts,
    )
    _summarise("Extraction", extraction_stats)

    _banner(
        3,
        "RESOLVE",
        module="aambridge.resolve.companies.resolve_all",
        file="aambridge/resolve/companies.py",
    )
    resolution_stats = resolve_all(sessions)
    _summarise("Resolution", resolution_stats)

    _banner(
        4,
        "SIGNALS",
        module="aambridge.signals.detect.detect_signals",
        file="aambridge/signals/detect.py",
    )
    signal_stats = detect_signals(sessions)
    _summarise("Signals", signal_stats)

    _banner(
        5,
        "SCORE",
        module="aambridge.scoring.engine.score_companies",
        file="aambridge/scoring/engine.py",
    )
    score_stats = score_companies(sessions)
    _summarise("Scoring", score_stats)

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    top50_path = output_dir / "aambridge_top50.csv"
    _banner(
        6,
        "EXPORT",
        module="aambridge.db.storage.export_csv / aambridge.dashboard.queries.export_top50_csv",
        file="aambridge/db/storage.py, aambridge/dashboard/queries.py",
    )
    export_csv(sessions, output_dir)
    export_top50_csv(
        sessions,
        top50_path,
        PipelineFilters(hide_agencies=True, limit=50),
    )
    jobs_csv = output_dir / "linkedin_me_jobs.csv"
    posts_csv = output_dir / "linkedin_me_posts.csv"
    _summarise(
        "Exports",
        f"{jobs_csv} | {posts_csv} | {top50_path}",
    )

    dashboard_url = f"http://localhost:{args.port}"
    if args.no_dashboard:
        _banner(
            7,
            "DASHBOARD",
            module="aambridge.dashboard.app",
            file="aambridge/dashboard/app.py",
            extra="status: skipped (--no-dashboard)",
        )
    else:
        _banner(
            7,
            "DASHBOARD",
            module="aambridge.dashboard.app",
            file="aambridge/dashboard/app.py",
            extra=f"url: {dashboard_url}",
        )

    log.info("%s", "")
    log.info("Wrote:")
    log.info("  %s", jobs_csv)
    log.info("  %s", posts_csv)
    log.info("  %s", top50_path)
    log.info("Live stage log: %s", settings.log_dir / "aambridge.log")
    if args.no_dashboard:
        log.info("Dashboard was not started.")
    else:
        log.info("Opening dashboard at %s", dashboard_url)

    if extraction_stats["failed"]:
        log.error("Extraction reported %s failed record(s).", extraction_stats["failed"])
        if args.no_dashboard:
            return 1

    if args.no_dashboard:
        return 0

    from aambridge.__main__ import _run_dashboard

    dashboard_args = ["--port", str(args.port)]
    if args.database_url:
        dashboard_args.extend(["--database-url", args.database_url])
    dashboard_code = _run_dashboard(dashboard_args)
    if extraction_stats["failed"]:
        return 1
    return dashboard_code


if __name__ == "__main__":
    raise SystemExit(main())
