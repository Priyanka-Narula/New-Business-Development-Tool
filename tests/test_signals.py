from __future__ import annotations

from sqlalchemy import func, select

from aambridge.db.models import Evidence, Job, Signal
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.db.storage import ScanStore
from aambridge.resolve.companies import resolve_all
from aambridge.signals.detect import detect_signals, what_changed


def _database(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'signals.db').as_posix()}")
    initialise_database(engine)
    return create_session_factory(engine)


def _job(index: int, *, city: str = "Riyadh") -> dict:
    title = "Project Director" if index == 24 else f"Operations Manager {index}"
    return {
        "job_title": title,
        "company_name": "Growth Company",
        "company_linkedin_url": "https://linkedin.com/company/growth-company",
        "job_location": f"{city}, Saudi Arabia",
        "city": city,
        "region": "Riyadh" if city == "Riyadh" else "Dubai",
        "country": "Saudi Arabia" if city == "Riyadh" else "United Arab Emirates",
        "job_url": f"https://linkedin.com/jobs/view/{9000 + index}",
        "seniority_hint": "director" if index == 24 else "manager",
        "is_reposted": index == 23,
        "nationality_restriction_evidence": (
            "Saudi nationals only" if index == 22 else ""
        ),
    }


def test_acceleration_and_evidence_backed_signals_are_idempotent(tmp_path) -> None:
    sessions = _database(tmp_path)
    first = ScanStore.start(sessions, mode="jobs", args={})
    for index in range(5):
        assert first.save_job(_job(index))
    first.finish("completed")

    second = ScanStore.start(sessions, mode="both", args={})
    for index in range(25):
        values = _job(index, city="Dubai" if index == 24 else "Riyadh")
        assert second.save_job(values)
    assert second.save_post(
        {
            "source_key": "expansion-post",
            "post_url": "https://linkedin.com/feed/update/urn:li:activity:888",
            "post_text": "We are expanding with a new office in Riyadh.",
            "author_name": "Growth Company",
            "author_url": "https://linkedin.com/company/growth-company",
            "author_type": "company",
            "post_category": "expansion",
        }
    )
    second.finish("completed")
    resolve_all(sessions)

    stats = detect_signals(sessions, scan_run_id=second.scan_run_id)
    assert stats["signals"] >= 5
    with sessions() as session:
        types = set(
            session.scalars(
                select(Signal.signal_type).where(
                    Signal.scan_run_id == second.scan_run_id
                )
            )
        )
        assert {
            "hiring_acceleration",
            "new_senior_vacancy",
            "repeated_vacancy",
            "new_location",
            "localisation_pressure",
            "expansion",
        } <= types
        assert session.scalar(select(func.count()).select_from(Evidence)) >= 6
        first_count = session.scalar(select(func.count()).select_from(Signal))

    detect_signals(sessions, scan_run_id=second.scan_run_id)
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Signal)) == first_count

    changes = what_changed(sessions, scan_run_id=second.scan_run_id)
    assert "Growth Company" in changes["new_companies"]
    assert changes["new_signals"]


def test_job_closes_after_configured_consecutive_missing_scans(tmp_path) -> None:
    sessions = _database(tmp_path)
    first = ScanStore.start(sessions, mode="jobs", args={})
    assert first.save_job(_job(1))
    first.finish("completed")
    resolve_all(sessions)

    second = ScanStore.start(sessions, mode="jobs", args={})
    second.finish("completed")
    third = ScanStore.start(sessions, mode="jobs", args={})
    third.finish("completed")

    stats = detect_signals(sessions, scan_run_id=third.scan_run_id)

    assert stats["closed_jobs"] == 1
    with sessions() as session:
        job = session.scalar(select(Job))
        assert job is not None and not job.is_active
