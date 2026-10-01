"""Single command-line entry point for AAMBRIDGE pipeline stages."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
    reset_database,
)
from aambridge.db.storage import export_csv
from aambridge.logging_config import configure_logging
from aambridge.settings import Settings

log = logging.getLogger(__name__)


def _settings(database_url: str | None) -> Settings:
    return Settings(**({"database_url": database_url} if database_url else {}))


def _database_parser(command: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"python -m aambridge {command}")
    parser.description = description
    parser.add_argument("--database-url", help="Override DATABASE_URL.")
    return parser


def _run_migrate_sqlite(argv: Sequence[str]) -> int:
    from aambridge.db.migrate import migrate_sqlite
    from aambridge.settings import PROJECT_ROOT

    parser = _database_parser(
        "migrate-sqlite",
        "Copy a local SQLite file into DATABASE_URL (Neon or other Postgres).",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=PROJECT_ROOT / "aambridge.db",
        help="Local SQLite file to copy. Default: aambridge.db in this folder.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Drop destination tables and reload if they already have rows.",
    )
    args = parser.parse_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    engine = create_database_engine(settings.database_url)
    counts = migrate_sqlite(args.source, engine, replace=args.replace)
    copied = sum(counts.values())
    log.info(
        "Copied %s rows from %s across %s tables.",
        copied,
        args.source,
        len(counts),
    )
    return 0


def _run_database_command(command: str, argv: Sequence[str]) -> int:
    if command == "init-db":
        parser = _database_parser(command, "Create missing database tables.")
        args = parser.parse_args(argv)
        settings = _settings(args.database_url)
        configure_logging(settings)
        initialise_database(create_database_engine(settings.database_url))
        log.info("Database tables are ready.")
        return 0

    if command == "reset-db":
        parser = _database_parser(
            command, "Drop and recreate all pilot database tables."
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm destructive deletion of all database data.",
        )
        args = parser.parse_args(argv)
        if not args.yes:
            parser.error("reset-db is destructive; pass --yes to confirm")
        settings = _settings(args.database_url)
        configure_logging(settings)
        reset_database(create_database_engine(settings.database_url))
        log.info("Database reset completed.")
        return 0

    parser = _database_parser(command, "Export current database rows to CSV.")
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    engine = create_database_engine(settings.database_url)
    initialise_database(engine)
    export_csv(create_session_factory(engine), args.output_dir)
    log.info("Exported database records to %s.", args.output_dir)
    return 0


def _run_clean(argv: Sequence[str]) -> int:
    from aambridge.extract.service import clean_csv, process_database

    parser = argparse.ArgumentParser(prog="python -m aambridge clean")
    parser.add_argument("input_csv", nargs="?", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--database-url")
    parser.add_argument("--model", default="llama3.1")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    if args.input_csv:
        output = args.output or args.input_csv.with_name(
            f"{args.input_csv.stem}_extracted.csv"
        )
        count = clean_csv(
            args.input_csv,
            output,
            model=args.model,
            timeout=args.timeout,
            max_chars=settings.extraction_max_chars,
            limit=args.limit,
        )
        log.info("Extracted %s CSV rows to %s.", count, output)
        return 0
    engine = create_database_engine(settings.database_url)
    initialise_database(engine)
    stats = process_database(
        create_session_factory(engine),
        model=args.model,
        workers=args.workers,
        timeout=args.timeout,
        max_chars=settings.extraction_max_chars,
        limit=args.limit,
        retry_failed=args.retry_failed,
        max_attempts=args.max_attempts,
    )
    log.info("Extraction statistics: %s", stats)
    return 0 if stats["failed"] == 0 else 1


def _run_eval(argv: Sequence[str]) -> int:
    from aambridge.extract.service import evaluate_labelled_csv

    parser = argparse.ArgumentParser(prog="python -m aambridge eval-extraction")
    parser.add_argument("labelled_csv", type=Path)
    parser.add_argument("--model", default="llama3.1")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    settings = Settings()
    configure_logging(settings)
    report = evaluate_labelled_csv(
        args.labelled_csv,
        model=args.model,
        timeout=args.timeout,
        max_chars=settings.extraction_max_chars,
    )
    log.info("Extraction evaluation:\n%s", json.dumps(report, indent=2))
    return 0


def _run_resolve(command: str, argv: Sequence[str]) -> int:
    from aambridge.resolve.companies import (
        decide_review,
        import_company_groups,
        import_company_overrides,
        list_reviews,
        resolve_all,
    )

    parser = argparse.ArgumentParser(prog=f"python -m aambridge {command}")
    parser.add_argument("--database-url")
    if command == "resolve-review":
        action = parser.add_mutually_exclusive_group()
        action.add_argument("--accept", type=int, metavar="ID")
        action.add_argument("--reject", type=int, metavar="ID")
    elif command in {"import-company-groups", "import-company-overrides"}:
        parser.add_argument("csv_file", type=Path)
    args = parser.parse_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    engine = create_database_engine(settings.database_url)
    initialise_database(engine)
    sessions = create_session_factory(engine)
    if command == "resolve":
        log.info("Resolution statistics: %s", resolve_all(sessions))
    elif command == "resolve-review":
        if args.accept is not None:
            decide_review(sessions, args.accept, accept=True)
        elif args.reject is not None:
            decide_review(sessions, args.reject, accept=False)
        else:
            log.info("Pending merge reviews:\n%s", json.dumps(list_reviews(sessions), indent=2))
    elif command == "import-company-groups":
        log.info("Imported %s group mappings.", import_company_groups(sessions, args.csv_file))
    else:
        log.info(
            "Imported %s company overrides.",
            import_company_overrides(sessions, args.csv_file),
        )
    return 0


def _run_signals(command: str, argv: Sequence[str]) -> int:
    from aambridge.signals.detect import detect_signals, what_changed

    parser = argparse.ArgumentParser(prog=f"python -m aambridge {command}")
    parser.add_argument("--database-url")
    parser.add_argument("--scan-run-id", type=int)
    args = parser.parse_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    engine = create_database_engine(settings.database_url)
    initialise_database(engine)
    sessions = create_session_factory(engine)
    if command == "signals":
        log.info(
            "Signal statistics: %s",
            detect_signals(sessions, scan_run_id=args.scan_run_id),
        )
    else:
        log.info(
            "Changes:\n%s",
            json.dumps(
                what_changed(sessions, scan_run_id=args.scan_run_id),
                indent=2,
            ),
        )
    return 0


def _run_score(argv: Sequence[str]) -> int:
    from aambridge.scoring.engine import score_companies

    parser = argparse.ArgumentParser(prog="python -m aambridge score")
    parser.add_argument("--database-url")
    parser.add_argument("--scan-run-id", type=int)
    args = parser.parse_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    engine = create_database_engine(settings.database_url)
    initialise_database(engine)
    stats = score_companies(
        create_session_factory(engine),
        scan_run_id=args.scan_run_id,
    )
    log.info("Scoring statistics: %s", stats)
    return 0


def _run_decision_makers(command: str, argv: Sequence[str]) -> int:
    from aambridge.decision_makers import (
        add_decision_maker,
        import_decision_makers,
        list_decision_makers,
        missing_contact_tasks,
    )

    parser = argparse.ArgumentParser(prog=f"python -m aambridge {command}")
    parser.add_argument("--database-url")
    if command == "add-decision-maker":
        parser.add_argument("--company-id", type=int, required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--designation", required=True)
        parser.add_argument("--source", default="manual")
        parser.add_argument("--department", default="")
        parser.add_argument("--profile-url", default="")
        parser.add_argument("--verified-by", default="")
        parser.add_argument("--confidence", type=float)
        parser.add_argument("--notes", default="")
    elif command == "import-decision-makers":
        parser.add_argument("csv_file", type=Path)
    elif command == "decision-makers":
        parser.add_argument("--company-id", type=int)
    args = parser.parse_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    engine = create_database_engine(settings.database_url)
    initialise_database(engine)
    sessions = create_session_factory(engine)
    if command == "decision-maker-tasks":
        log.info(
            "Decision-maker tasks:\n%s",
            json.dumps(missing_contact_tasks(sessions), indent=2),
        )
    elif command == "decision-makers":
        log.info(
            "Decision-makers:\n%s",
            json.dumps(
                list_decision_makers(sessions, company_id=args.company_id),
                indent=2,
            ),
        )
    elif command == "import-decision-makers":
        log.info(
            "Import statistics: %s",
            import_decision_makers(sessions, args.csv_file),
        )
    else:
        record_id = add_decision_maker(
            sessions,
            company_id=args.company_id,
            name=args.name,
            designation=args.designation,
            source=args.source,
            department=args.department,
            profile_url=args.profile_url,
            verified_by=args.verified_by,
            confidence=args.confidence,
            notes=args.notes,
        )
        log.info("Saved decision-maker %s.", record_id)
    return 0


def _run_dashboard(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m aambridge dashboard")
    parser.add_argument("--database-url")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    app = Path(__file__).resolve().parent / "dashboard" / "app.py"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.port",
        str(args.port),
        "--server.headless",
        "true",
        "--",
        "--database-url",
        settings.database_url,
    ]
    log.info("Starting local dashboard at http://localhost:%s", args.port)
    from subprocess import call

    return int(call(command))


def _run_export_top50(argv: Sequence[str]) -> int:
    from aambridge.dashboard.queries import PipelineFilters, export_top50_csv

    parser = argparse.ArgumentParser(prog="python -m aambridge export-top-50")
    parser.add_argument("output_csv", type=Path, nargs="?", default=Path("output/aambridge_top50.csv"))
    parser.add_argument("--database-url")
    parser.add_argument(
        "--preset",
        choices=("uae_riyadh", "emiratisation"),
        help="Optional saved view used by the dashboard.",
    )
    args = parser.parse_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    engine = create_database_engine(settings.database_url)
    initialise_database(engine)
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    export_top50_csv(
        create_session_factory(engine),
        args.output_csv,
        PipelineFilters(hide_agencies=True, preset=args.preset, limit=50),
    )
    log.info("Wrote Top 50 CSV to %s.", args.output_csv)
    return 0


def _run_all(argv: Sequence[str]) -> int:
    from aambridge.extract.service import process_database
    from aambridge.resolve.companies import resolve_all
    from aambridge.scoring.engine import score_companies
    from aambridge.signals.detect import detect_signals
    from run_scraper import main as scrape_main

    parser = argparse.ArgumentParser(prog="python -m aambridge run-all")
    parser.add_argument("--database-url")
    parser.add_argument("--model", default="llama3.1")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Run clean through score using records already in the database.",
    )
    args, scrape_args = parser.parse_known_args(argv)
    settings = _settings(args.database_url)
    configure_logging(settings)
    if not args.skip_scrape:
        forwarded = list(scrape_args)
        if args.database_url:
            forwarded.extend(["--database-url", args.database_url])
        scrape_result = scrape_main(forwarded)
        if scrape_result not in {0, None}:
            return int(scrape_result)
    engine = create_database_engine(settings.database_url)
    initialise_database(engine)
    sessions = create_session_factory(engine)
    extraction_stats = process_database(
        sessions,
        model=args.model,
        workers=args.workers,
        max_chars=settings.extraction_max_chars,
        limit=args.limit,
        retry_failed=True,
        max_attempts=settings.extraction_max_attempts,
    )
    resolution_stats = resolve_all(sessions)
    signal_stats = detect_signals(sessions)
    score_stats = score_companies(sessions)
    log.info(
        "Run-all completed: extraction=%s resolution=%s signals=%s scoring=%s",
        extraction_stats,
        resolution_stats,
        signal_stats,
        score_stats,
    )
    return 0 if extraction_stats["failed"] == 0 else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch independently runnable pipeline and database commands."""

    arguments = list(argv) if argv is not None else sys.argv[1:]
    # Dispatch scrape before the parent parser handles --help, so
    # `python -m aambridge scrape --help` shows scrape-specific options.
    if arguments and arguments[0] == "scrape":
        from run_scraper import main as scrape_main

        return scrape_main(arguments[1:])
    if arguments and arguments[0] == "clean":
        return _run_clean(arguments[1:])
    if arguments and arguments[0] == "eval-extraction":
        return _run_eval(arguments[1:])
    if arguments and arguments[0] in {
        "resolve",
        "resolve-review",
        "import-company-groups",
        "import-company-overrides",
    }:
        return _run_resolve(arguments[0], arguments[1:])
    if arguments and arguments[0] in {"signals", "changes"}:
        return _run_signals(arguments[0], arguments[1:])
    if arguments and arguments[0] == "score":
        return _run_score(arguments[1:])
    if arguments and arguments[0] in {
        "decision-maker-tasks",
        "decision-makers",
        "add-decision-maker",
        "import-decision-makers",
    }:
        return _run_decision_makers(arguments[0], arguments[1:])
    if arguments and arguments[0] == "dashboard":
        return _run_dashboard(arguments[1:])
    if arguments and arguments[0] == "export-top-50":
        return _run_export_top50(arguments[1:])
    if arguments and arguments[0] == "run-all":
        return _run_all(arguments[1:])
    if arguments and arguments[0] == "migrate-sqlite":
        return _run_migrate_sqlite(arguments[1:])

    parser = argparse.ArgumentParser(prog="python -m aambridge")
    parser.add_argument(
        "command",
        choices=(
            "scrape",
            "clean",
            "eval-extraction",
            "resolve",
            "resolve-review",
            "import-company-groups",
            "import-company-overrides",
            "signals",
            "changes",
            "score",
            "decision-maker-tasks",
            "decision-makers",
            "add-decision-maker",
            "import-decision-makers",
            "dashboard",
            "export-top-50",
            "run-all",
            "init-db",
            "reset-db",
            "migrate-sqlite",
            "export-csv",
        ),
    )
    args, remaining = parser.parse_known_args(arguments)
    return _run_database_command(args.command, remaining)


if __name__ == "__main__":
    raise SystemExit(main())

