from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select

from aambridge import __main__ as cli
from aambridge.db.models import (
    Company,
    CompanyAlias,
    DecisionMaker,
    LeadScore,
    ScanRun,
)
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.db.storage import ScanStore
from aambridge.decision_makers import (
    add_decision_maker,
    import_decision_makers,
    list_decision_makers,
    missing_contact_tasks,
    rank_designation,
    suggested_task,
)
from aambridge.resolve.companies import normalise_company_name, resolve_all
from aambridge.scoring.engine import score_companies


def _database(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'dm.db').as_posix()}")
    initialise_database(engine)
    return engine, create_session_factory(engine)


def _company(
    session,
    name: str,
    *,
    is_agency: bool = False,
) -> Company:
    company = Company(canonical_name=name, is_agency=is_agency)
    session.add(company)
    session.flush()
    session.add(
        CompanyAlias(
            company_id=company.id,
            alias=name,
            alias_normalised=normalise_company_name(name),
            source="manual",
        )
    )
    return company


def _company_id(sessions, name: str) -> int:
    with sessions() as session:
        company = session.scalar(select(Company).where(Company.canonical_name == name))
        assert company is not None
        return company.id


def _score(
    session,
    company: Company,
    scan_run_id: int,
    *,
    total: int,
    priority: str,
) -> None:
    session.add(
        LeadScore(
            company_id=company.id,
            scan_run_id=scan_run_id,
            total_score=total,
            priority=priority,
            factor_results_json={},
            explanation_json={},
            explanation_text=f"{company.canonical_name} scored {total}",
            created_at=datetime.now(UTC),
        )
    )


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("CHRO", 1),
        ("Chief Human Resources Officer", 1),
        ("Chief People Officer", 2),
        ("VP Talent Acquisition", 4),
        ("Head of Talent Acquisition", 5),
        ("Head of TA", 5),
        ("Director of HR", 7),
        ("Talent Acquisition Manager", 8),
        ("Vendor Manager", 13),
        ("CEO", 99),
        ("Software Engineer", 99),
        ("", 99),
    ],
)
def test_designation_ranking_prefers_partnership_control(
    title: str,
    expected: int,
) -> None:
    assert rank_designation(title) == expected
    assert rank_designation("Head of Talent Acquisition") < rank_designation("CEO")


def test_suggested_task_uses_taxonomy_default() -> None:
    assert suggested_task() == "Find: Head of Talent Acquisition or equivalent"
    assert suggested_task("CHRO") == "Find: CHRO or equivalent"


def test_missing_contact_tasks_only_cover_high_medium_without_verified_contact(
    tmp_path,
) -> None:
    _engine, sessions = _database(tmp_path)
    with sessions.begin() as session:
        scan = ScanRun(
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            mode="jobs",
            args_json={},
            status="completed",
            stats_json={},
        )
        session.add(scan)
        session.flush()
        high = _company(session, "High Hospital")
        medium = _company(session, "Medium Retail")
        low = _company(session, "Low Clinic")
        agency = _company(session, "Agency Desk", is_agency=True)
        verified = _company(session, "Verified Energy")
        _score(session, high, scan.id, total=88, priority="HIGH")
        _score(session, medium, scan.id, total=61, priority="MEDIUM")
        _score(session, low, scan.id, total=20, priority="LOW")
        _score(session, agency, scan.id, total=90, priority="HIGH")
        _score(session, verified, scan.id, total=80, priority="HIGH")
        high_id = high.id
        medium_id = medium.id

    add_decision_maker(
        sessions,
        company_id=_company_id(sessions, "Verified Energy"),
        name="Noura Ali",
        designation="CHRO",
        source="manual",
        verified_by="recruiter",
    )
    add_decision_maker(
        sessions,
        company_id=high_id,
        name="Unverified Name",
        designation="HR Manager",
        source="manual",
    )

    tasks = missing_contact_tasks(sessions)
    assert [item["company_id"] for item in tasks] == [high_id, medium_id]
    assert tasks[0]["priority"] == "HIGH"
    assert tasks[0]["task"] == "Find: Head of Talent Acquisition or equivalent"
    assert all(item["priority"] in {"HIGH", "MEDIUM"} for item in tasks)


def test_csv_import_drops_forbidden_columns_and_redacts_notes(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    with sessions.begin() as session:
        company = _company(session, "Client Hospital")
        company_id = company.id

    path = tmp_path / "decision_makers.csv"
    path.write_text(
        "company_name,name,designation,email,phone,notes\n"
        "Client Hospital,Sara Khan,Head of TA,sara@example.com,+971 50 123 4567,"
        "Call sara@example.com or +971 50 123 4567\n",
        encoding="utf-8",
    )

    stats = import_decision_makers(sessions, path)
    assert stats == {"imported": 1, "skipped": 0}

    rows = list_decision_makers(sessions, company_id=company_id)
    assert len(rows) == 1
    stored = rows[0]
    assert stored["name"] == "Sara Khan"
    assert stored["designation"] == "Head of TA"
    assert stored["relevance_rank"] == 5
    assert stored["verified_at"] is None
    assert stored["email"] == "sara@example.com"
    assert stored["phone"] == "+971 50 123 4567"
    assert "sara@example.com" not in (stored["notes"] or "")
    assert "+971 50 123 4567" not in (stored["notes"] or "")
    assert "[email redacted]" in (stored["notes"] or "")
    assert "[phone redacted]" in (stored["notes"] or "")

    with sessions() as session:
        record = session.scalar(select(DecisionMaker))
        assert record is not None
        assert record.email == "sara@example.com"
        assert record.phone == "+971 50 123 4567"
        assert record.notes is not None
        assert "sara@example.com" not in record.notes


def test_decision_maker_schema_has_optional_contact_columns() -> None:
    columns = {column.name for column in inspect(DecisionMaker).columns}
    assert {"email", "phone", "apollo_person_id", "email_status", "disclosed_at"} <= columns
    assert columns.isdisjoint(
        {
            "contact_email",
            "recruiter_email",
            "phone_number",
            "mobile",
            "telephone",
        }
    )


def test_add_decision_maker_redacts_contact_details_in_text_fields(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    with sessions.begin() as session:
        company_id = _company(session, "Client Hospital").id

    record_id = add_decision_maker(
        sessions,
        company_id=company_id,
        name="Sara sara@example.com",
        designation="Head of TA",
        source="manual",
        notes="Reach +971 50 123 4567",
    )
    with sessions() as session:
        record = session.get(DecisionMaker, record_id)
        assert record is not None
        assert "sara@example.com" not in record.name
        assert record.notes is not None
        assert "+971 50 123 4567" not in record.notes


def test_add_decision_maker_stores_public_email_and_phone(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    with sessions.begin() as session:
        company_id = _company(session, "Client Hospital").id

    record_id = add_decision_maker(
        sessions,
        company_id=company_id,
        name="Sara Khan",
        designation="Head of TA",
        source="website_leadership",
        email="sara@example.com",
        phone="+971 50 123 4567",
    )
    with sessions() as session:
        record = session.get(DecisionMaker, record_id)
        assert record is not None
        assert record.email == "sara@example.com"
        assert record.phone == "+971 50 123 4567"
        assert record.verified_at is None


def test_verified_rank_is_used_by_scoring(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        {
            "job_title": "Healthcare Operations Manager",
            "company_name": "Client Hospital",
            "company_linkedin_url": "https://linkedin.com/company/client-hospital",
            "job_location": "Riyadh, Saudi Arabia",
            "city": "Riyadh",
            "country": "Saudi Arabia",
            "industry": "healthcare",
            "job_url": "https://linkedin.com/jobs/view/9001",
            "seniority_hint": "manager",
        }
    )
    scan.finish("completed")
    resolve_all(sessions)

    first = score_companies(sessions, scan_run_id=scan.scan_run_id)
    assert first["scored"] == 1
    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None
        company_id = company.id
        before = session.scalar(select(LeadScore))
        assert before is not None
        before_total = before.total_score
        before_factor = before.factor_results_json["decision_maker_accessibility"]
    assert before_factor["score_0_100"] == 0
    assert before_factor["missing_inputs"] == ["verified_decision_maker"]

    add_decision_maker(
        sessions,
        company_id=company_id,
        name="Aisha Rahman",
        designation="CHRO",
        source="manual",
        verified_by="recruiter",
        confidence=0.9,
    )
    add_decision_maker(
        sessions,
        company_id=company_id,
        name="Intern Name",
        designation="Software Engineer",
        source="manual",
    )

    second = score_companies(sessions, scan_run_id=scan.scan_run_id)
    assert second["scored"] == 1
    with sessions() as session:
        after = session.scalar(select(LeadScore))
        assert after is not None
        factor = after.factor_results_json["decision_maker_accessibility"]
        assert after.total_score == before_total + 5
        assert factor["score_0_100"] == 100
        assert "relevance rank 1" in factor["reasons"][0]


def test_cli_lists_tasks_without_contact_fields(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    engine = create_database_engine(database_url)
    initialise_database(engine)
    sessions = create_session_factory(engine)
    with sessions.begin() as session:
        scan = ScanRun(
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            mode="jobs",
            args_json={},
            status="completed",
            stats_json={},
        )
        session.add(scan)
        session.flush()
        company = _company(session, "High Hospital")
        _score(session, company, scan.id, total=88, priority="HIGH")

    assert cli.main(["decision-maker-tasks", "--database-url", database_url]) == 0
    tasks = missing_contact_tasks(sessions)
    assert [item["company"] for item in tasks] == ["High Hospital"]
    assert all("email" not in item and "phone" not in item for item in tasks)

    assert (
        cli.main(
            [
                "add-decision-maker",
                "--database-url",
                database_url,
                "--company-id",
                "1",
                "--name",
                "Aisha Rahman",
                "--designation",
                "CHRO",
                "--verified-by",
                "recruiter",
            ]
        )
        == 0
    )
    rows = list_decision_makers(sessions)
    assert [item["name"] for item in rows] == ["Aisha Rahman"]
    assert rows[0]["designation"] == "CHRO"
    assert rows[0]["verified_at"] is not None
    assert rows[0]["email"] is None
    assert rows[0]["phone"] is None
    assert missing_contact_tasks(sessions) == []
