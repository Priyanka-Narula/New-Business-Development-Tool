from __future__ import annotations

import pytest

from aambridge.db.session import create_database_engine, create_session_factory, initialise_database
from aambridge.db.storage import ScanStore
from run_pipeline import EmptyDatabaseError, database_has_records, decide_scrape, parse_args


def test_decide_scrape_auto_skips_when_database_has_rows() -> None:
    assert decide_scrape(force_scrape=False, skip_scrape=False, has_records=True) is False


def test_decide_scrape_auto_runs_when_database_is_empty() -> None:
    assert decide_scrape(force_scrape=False, skip_scrape=False, has_records=False) is True


def test_decide_scrape_force_and_skip_are_respected() -> None:
    assert decide_scrape(force_scrape=True, skip_scrape=False, has_records=True) is True
    assert decide_scrape(force_scrape=False, skip_scrape=True, has_records=True) is False


def test_skip_scrape_on_empty_database_raises() -> None:
    with pytest.raises(EmptyDatabaseError):
        decide_scrape(force_scrape=False, skip_scrape=True, has_records=False)


def test_conflicting_scrape_flags_raise() -> None:
    with pytest.raises(ValueError):
        decide_scrape(force_scrape=True, skip_scrape=True, has_records=True)


def test_parse_args_forwards_unknown_scrape_flags() -> None:
    args, scrape_args = parse_args(
        ["--skip-scrape", "--no-dashboard", "--mode", "jobs", "--max-per-query", "10"]
    )
    assert args.skip_scrape is True
    assert args.no_dashboard is True
    assert scrape_args == ["--mode", "jobs", "--max-per-query", "10"]


def test_parse_args_accepts_enrich_contacts_flag() -> None:
    args, scrape_args = parse_args(
        ["--skip-scrape", "--no-dashboard", "--enrich-contacts", "--enrich-limit", "5"]
    )
    assert args.enrich_contacts is True
    assert args.enrich_limit == 5
    assert scrape_args == []


def test_database_has_records_detects_saved_jobs(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'pipe.db').as_posix()}")
    initialise_database(engine)
    sessions = create_session_factory(engine)
    assert database_has_records(sessions) is False

    scan = ScanStore.start(sessions, mode="jobs", args={})
    scan.save_job(
        {
            "job_title": "HR Manager",
            "company_name": "Example LLC",
            "job_location": "Riyadh, Saudi Arabia",
            "job_url": "https://www.linkedin.com/jobs/view/123456",
            "description": "Hiring an HR Manager.",
        }
    )
    scan.finish("completed")
    assert database_has_records(sessions) is True
