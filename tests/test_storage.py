from __future__ import annotations

import csv
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, inspect, select

from aambridge.db.models import (
    Job,
    JobObservation,
    Post,
    QueryResultCount,
    ScanRun,
    SourceRecord,
)
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
    reset_database,
)
from aambridge.db.storage import ScanStore, export_csv
from aambridge.sources.base import NormalisedRecord
import run_scraper


def _database(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    initialise_database(engine)
    return engine, create_session_factory(engine)


def _job() -> dict[str, str]:
    return {
        "job_title": "HR Manager",
        "company_name": "Example LLC",
        "job_location": "Riyadh, Saudi Arabia",
        "job_url": "https://www.linkedin.com/jobs/view/123456?tracking=abc",
        "description": "Hiring an HR Manager.",
    }


def _post() -> dict[str, str]:
    return {
        "post_text": "We are hiring a Finance Manager in Dubai.",
        "post_url": (
            "https://www.linkedin.com/feed/update/"
            "urn:li:activity:987654?tracking=abc"
        ),
        "job_title": "Finance Manager",
    }


def _normalised_post_record(activity_id: str) -> NormalisedRecord:
    text = "Hiring an Operations Manager in Dubai."
    url = (
        "https://www.linkedin.com/feed/update/"
        f"urn:li:activity:{activity_id}"
    )
    return NormalisedRecord(
        source="linkedin_post",
        source_key=activity_id,
        source_url=url,
        observed_at=datetime.now(UTC),
        raw_text=text,
        raw_fields={
            "source": "post",
            "source_key": activity_id,
            "industry": "healthcare",
            "query": "hiring",
            "query_set": "hiring",
            "search_location": "United Arab Emirates",
            "job_title": "Operations Manager",
            "has_contact_email": False,
            "post_text": text,
            "post_url": url,
            "author_name": "",
            "author_url": "",
            "author_headline": "",
            "author_type": "unknown",
            "author_is_agency": False,
            "agency_evidence": "",
            "post_date": "",
            "post_date_precision": "unknown",
            "post_date_raw": "",
            "post_category": "hiring",
            "location_hint": "Dubai",
            "seniority_hint": "manager",
        },
    )


def test_job_upsert_adds_one_observation_per_scan(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    first = ScanStore.start(sessions, mode="jobs", args={"max_per_query": 25})
    first.page_loaded(3)
    first.card_parsed()

    assert first.save_job(_job())
    first.card_parsed()
    assert not first.save_job(_job())
    first.finish("completed")

    second = ScanStore.start(sessions, mode="jobs", args={})
    assert second.save_job(_job())
    second.finish("completed")

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 1
        assert (
            session.scalar(select(func.count()).select_from(JobObservation)) == 2
        )
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 2
        job = session.scalar(select(Job))
        assert job is not None
        assert job.linkedin_job_id == "123456"
        assert job.first_seen_at <= job.last_seen_at

    assert first.stats() == {
        "pages_loaded": 1,
        "cards_seen": 3,
        "cards_parsed": 2,
        "parse_failures_by_selector": {},
        "rows_saved": 1,
    }


def test_post_seen_in_two_scans_is_upserted_with_two_source_observations(
    tmp_path,
) -> None:
    _engine, sessions = _database(tmp_path)
    first = ScanStore.start(sessions, mode="posts", args={})
    assert first.save_post(_post())
    first.finish("completed")

    second = ScanStore.start(sessions, mode="posts", args={})
    assert second.save_post(_post())
    second.finish("completed")

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Post)) == 1
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 2
        post = session.scalar(select(Post))
        assert post is not None
        assert post.first_seen_at <= post.last_seen_at


def test_rich_job_observation_and_query_count_are_persisted(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    values = {
        **_job(),
        "source_key": "123456",
        "linkedin_job_id": "123456",
        "company_linkedin_url": "https://www.linkedin.com/company/example",
        "city": "Riyadh",
        "region": "Riyadh",
        "country": "Saudi Arabia",
        "posted_date": "2026-09-20",
        "posted_date_precision": "exact",
        "is_reposted": True,
        "is_promoted": True,
        "applicant_count": 123,
        "easy_apply": True,
    }
    assert scan.save_job(values)
    scan.query_result_count(
        source="linkedin_job",
        keywords="healthcare",
        location="Saudi Arabia",
        time_filter="week",
        total_results=245,
    )
    scan.finish("completed")

    with sessions() as session:
        job = session.scalar(select(Job))
        observation = session.scalar(select(JobObservation))
        count = session.scalar(select(QueryResultCount))
        assert job is not None
        assert job.company_linkedin_url == (
            "https://www.linkedin.com/company/example"
        )
        assert (job.city, job.region, job.country) == (
            "Riyadh",
            "Riyadh",
            "Saudi Arabia",
        )
        assert observation is not None
        assert observation.posted_date.isoformat() == "2026-09-20"
        assert observation.posted_date_precision == "exact"
        assert observation.is_reposted and observation.is_promoted
        assert observation.applicant_count == 123
        assert observation.raw_listing_json["easy_apply"] is True
        assert count is not None
        assert count.total_results == 245


def test_post_upsert_and_interrupted_scan_status(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="posts", args={})

    assert scan.save_post(_post())
    assert not scan.save_post(_post())
    scan.parse_failure("post.permalink")
    scan.finish("interrupted")

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Post)) == 1
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 1
        run = session.get(ScanRun, scan.scan_run_id)
        assert run is not None
        assert run.status == "interrupted"
        assert run.finished_at is not None
        assert run.stats_json["parse_failures_by_selector"] == {
            "post.permalink": 1
        }


def test_storage_boundary_never_persists_contact_details(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="posts", args={})
    values = {
        **_post(),
        "post_text": (
            "Contact personal@example.com or +971 50 123 4567 about this role."
        ),
        "recruiter_email": "personal@example.com",
        "has_contact_email": True,
    }

    assert scan.save_post(values)
    scan.finish("completed")

    with sessions() as session:
        source = session.scalar(select(SourceRecord))
        post = session.scalar(select(Post))
        assert source is not None and post is not None
        assert "recruiter_email" not in source.raw_fields_json
        assert source.raw_fields_json["has_contact_email"] is True
        assert "personal@example.com" not in source.raw_text
        assert "+971 50 123 4567" not in source.raw_text
        assert "personal@example.com" not in post.text


def test_database_initialisation_scrubs_legacy_contact_details(tmp_path) -> None:
    engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="posts", args={})
    observed_at = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            SourceRecord(
                scan_run_id=scan.scan_run_id,
                source="linkedin_post",
                source_key="legacy-contact",
                source_url=None,
                observed_at=observed_at,
                raw_text="Email legacy@example.com or call +971 50 123 4567",
                raw_fields_json={
                    "recruiter_email": "legacy@example.com",
                    "post_text": "Email legacy@example.com",
                },
                content_hash="0" * 64,
                processing_status="new",
                extraction_attempts=0,
            )
        )

    initialise_database(engine)

    with sessions() as session:
        source = session.scalar(
            select(SourceRecord).where(SourceRecord.source_key == "legacy-contact")
        )
        assert source is not None
        assert "legacy@example.com" not in source.raw_text
        assert "+971 50 123 4567" not in source.raw_text
        assert "recruiter_email" not in source.raw_fields_json
        assert source.content_hash != "0" * 64


def test_csv_export_reads_canonical_database_rows(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="both", args={})
    scan.save_job(_job())
    scan.save_post(_post())
    scan.finish("completed")

    output_dir = tmp_path / "export"
    export_csv(sessions, output_dir)

    with (output_dir / "linkedin_me_jobs.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        jobs = list(csv.DictReader(handle))
    with (output_dir / "linkedin_me_posts.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        posts = list(csv.DictReader(handle))

    assert [row["source_key"] for row in jobs] == ["123456"]
    assert [row["source_key"] for row in posts] == ["987654"]


def test_reset_recreates_schema_and_postgresql_url_is_supported(tmp_path) -> None:
    engine, _sessions = _database(tmp_path)
    reset_database(engine)
    assert {
        "scan_run",
        "source_record",
        "job",
        "job_observation",
        "post",
        "query_result_count",
        "company",
        "company_alias",
        "company_group",
        "extraction",
        "signal",
        "evidence",
        "decision_maker",
        "company_contact",
        "lead_score",
    } <= set(inspect(engine).get_table_names())

    postgres = create_database_engine(
        "postgresql://user:password@localhost/aambridge"
    )
    try:
        assert postgres.dialect.name == "postgresql"
        assert postgres.url.drivername == "postgresql+psycopg"
        assert postgres.url.query.get("sslmode") == "require"
    finally:
        postgres.dispose()

    neon = create_database_engine(
        "postgresql://user:password@ep-demo-pooler.region.aws.neon.tech/neondb"
        "?sslmode=require&channel_binding=require"
    )
    try:
        assert neon.url.query.get("sslmode") == "require"
        assert neon.url.query.get("channel_binding") == "require"
    finally:
        neon.dispose()


def test_keyboard_interrupt_keeps_parsed_rows(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'interrupted.db').as_posix()}"

    class Driver:
        def quit(self) -> None:
            pass

    def interrupted_posts(**kwargs) -> int:
        kwargs["on_record"](_normalised_post_record("42"))
        raise KeyboardInterrupt

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(run_scraper, "create_driver", Driver)
    monkeypatch.setattr(run_scraper, "manual_login", lambda _driver: None)
    monkeypatch.setattr(run_scraper, "scrape_posts", interrupted_posts)

    result = run_scraper.main(
        [
            "--mode",
            "posts",
            "--manual-login",
            "--database-url",
            database_url,
        ]
    )

    engine = create_database_engine(database_url)
    sessions = create_session_factory(engine)
    with sessions() as session:
        run = session.scalar(select(ScanRun))
        assert run is not None
        assert run.status == "interrupted"
        assert run.args_json["database_url"] == "<configured>"
        assert session.scalar(select(func.count()).select_from(Post)) == 1
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 1
    assert result == 130


def test_failed_scan_keeps_saved_rows_and_marks_run_failed(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'failed.db').as_posix()}"

    class Driver:
        def quit(self) -> None:
            pass

    def failed_posts(**kwargs) -> int:
        kwargs["on_record"](_normalised_post_record("84"))
        raise RuntimeError("fixture failure")

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(run_scraper, "create_driver", Driver)
    monkeypatch.setattr(run_scraper, "manual_login", lambda _driver: None)
    monkeypatch.setattr(run_scraper, "scrape_posts", failed_posts)

    with pytest.raises(RuntimeError, match="fixture failure"):
        run_scraper.main(
            [
                "--mode",
                "posts",
                "--manual-login",
                "--database-url",
                database_url,
            ]
        )

    engine = create_database_engine(database_url)
    sessions = create_session_factory(engine)
    with sessions() as session:
        run = session.scalar(select(ScanRun))
        assert run is not None
        assert run.status == "failed"
        assert run.finished_at is not None
        assert session.scalar(select(func.count()).select_from(Post)) == 1
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 1


def test_migrate_sqlite_copies_rows_and_refuses_overwrite(tmp_path) -> None:
    from aambridge.db.migrate import migrate_sqlite

    source_path = tmp_path / "source.db"
    dest_path = tmp_path / "dest.db"
    source_engine = create_database_engine(f"sqlite:///{source_path.as_posix()}")
    initialise_database(source_engine)
    with create_session_factory(source_engine).begin() as session:
        session.add(
            ScanRun(
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                mode="jobs",
                args_json={"mode": "jobs"},
                status="completed",
                stats_json={"jobs": 1},
            )
        )

    dest_engine = create_database_engine(f"sqlite:///{dest_path.as_posix()}")
    counts = migrate_sqlite(source_path, dest_engine)
    assert counts["scan_run"] == 1
    with create_session_factory(dest_engine)() as session:
        run = session.scalar(select(ScanRun))
        assert run is not None
        assert run.id == 1
        assert run.mode == "jobs"
        assert run.args_json == {"mode": "jobs"}

    with pytest.raises(RuntimeError, match="already has rows"):
        migrate_sqlite(source_path, dest_engine)

    assert migrate_sqlite(source_path, dest_engine, replace=True)["scan_run"] == 1

