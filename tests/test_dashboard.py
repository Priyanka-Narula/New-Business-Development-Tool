from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import inspect, select

from aambridge.dashboard.queries import (
    EMPTY_SCAN_HINT,
    GRAPH_EXCLUDED_FACTORS,
    PULSE_DAYS,
    TABLE_COLUMNS,
    PipelineFilters,
    company_detail,
    executive_summary,
    export_top50_csv,
    export_top50_rows,
    focus_snapshot,
    graph_potential,
    industry_rankings,
    latest_completed_scan,
    pipeline_rows,
    potential_breakdown,
    region_rankings,
    rolling_city_chart,
    rolling_country_chart,
    rolling_hiring,
    rolling_industry_chart,
    rolling_region_chart,
    set_outreach_status,
)
from aambridge.db.models import Company, Job, LeadScore
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.db.storage import ScanStore
from aambridge.decision_makers import add_decision_maker
from aambridge.extract.service import process_database
from aambridge.extract.taxonomies import load_yaml
from aambridge.resolve.companies import resolve_all
from aambridge.scoring.engine import score_companies
from aambridge.signals.detect import detect_signals


def _database(tmp_path):
    engine = create_database_engine(
        f"sqlite:///{(tmp_path / 'dashboard.db').as_posix()}"
    )
    initialise_database(engine)
    return engine, create_session_factory(engine)


def _empty_llm(_prompt: str, _model: str, _timeout: int) -> dict:
    return {}


def _seed_pipeline(sessions) -> int:
    scan = ScanStore.start(sessions, mode="both", args={})
    assert scan.save_job(
        {
            "job_title": "Healthcare Operations Manager",
            "company_name": "Client Hospital",
            "company_linkedin_url": "https://linkedin.com/company/client-hospital",
            "job_location": "Dubai, United Arab Emirates",
            "city": "Dubai",
            "region": "Dubai",
            "country": "United Arab Emirates",
            "industry": "healthcare",
            "job_url": "https://linkedin.com/jobs/view/7101",
            "seniority_hint": "manager",
            "nationality_restriction": True,
            "nationality_restriction_evidence": "UAE nationals preferred",
        }
    )
    assert scan.save_job(
        {
            "job_title": "Finance Director",
            "company_name": "Agency Recruitment",
            "company_linkedin_url": "https://linkedin.com/company/agency-recruitment",
            "job_location": "Riyadh, Saudi Arabia",
            "city": "Riyadh",
            "country": "Saudi Arabia",
            "industry": "banking_financial_services",
            "job_url": "https://linkedin.com/jobs/view/7102",
            "posted_by_agency": True,
        }
    )
    assert scan.save_post(
        {
            "source_key": "hospital-expansion",
            "post_url": "https://linkedin.com/feed/update/urn:li:activity:7101",
            "post_text": "Client Hospital is expanding with a new clinic in Dubai.",
            "author_name": "Client Hospital",
            "author_url": "https://linkedin.com/company/client-hospital",
            "author_type": "company",
            "post_category": "expansion",
        }
    )
    scan.finish("completed")
    resolve_all(sessions)
    process_database(
        sessions,
        model="fixture",
        workers=1,
        timeout=1,
        max_chars=2000,
        call=_empty_llm,
    )
    detect_signals(sessions, scan_run_id=scan.scan_run_id)
    with sessions.begin() as session:
        client = session.scalar(
            select(Company).where(Company.canonical_name == "Client Hospital")
        )
        assert client is not None
        client.industry_key = "healthcare"
        client.hq_country = "United Arab Emirates"
        client.size_band = "50-249"
        client.jurisdiction = "mainland"
        client_id = client.id
    add_decision_maker(
        sessions,
        company_id=client_id,
        name="Aisha Rahman",
        designation="Head of Talent Acquisition",
        source="manual",
        verified_by="recruiter",
    )
    score_companies(sessions, scan_run_id=scan.scan_run_id)
    return client_id


def test_outreach_status_column_is_added(tmp_path) -> None:
    engine, _sessions = _database(tmp_path)
    columns = {column["name"] for column in inspect(engine).get_columns("company")}
    assert "outreach_status" in columns
    assert "lead_score_percentile" in columns
    assert "acquisition_band" in columns


def test_acquisition_columns_are_added_to_existing_databases(tmp_path) -> None:
    engine = create_database_engine(
        f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE company ("
            "id INTEGER PRIMARY KEY, "
            "canonical_name VARCHAR(255) NOT NULL, "
            "is_agency BOOLEAN NOT NULL DEFAULT 0, "
            "outreach_status VARCHAR(32) NOT NULL DEFAULT 'new'"
            ")"
        )
    initialise_database(engine, scrub_personal_contacts=False)
    columns = {column["name"] for column in inspect(engine).get_columns("company")}
    assert "lead_score_percentile" in columns
    assert "acquisition_band" in columns


def test_end_to_end_fixture_pipeline_feeds_dashboard(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    company_id = _seed_pipeline(sessions)

    rows = pipeline_rows(sessions, PipelineFilters(hide_agencies=True))
    assert [row["company"] for row in rows] == ["Client Hospital"]
    assert rows[0]["key_contact"] == "Aisha Rahman"
    assert rows[0]["designation"] == "Head of Talent Acquisition"
    assert rows[0]["website"] == ""
    assert rows[0]["company_phone"] == ""
    assert rows[0]["company_email"] == ""
    assert rows[0]["status"] == "new"
    assert rows[0]["new_requirement"] is True
    assert rows[0]["industry"] == "healthcare"
    assert rows[0]["acquisition_band"] == "Medium"
    assert rows[0]["lead_score_percentile"] == 50
    assert "email" not in rows[0]
    assert "phone" not in rows[0]

    agencies = pipeline_rows(sessions, PipelineFilters(hide_agencies=False))
    assert {row["company"] for row in agencies} == {"Client Hospital"}
    with sessions() as session:
        assert session.scalar(select(LeadScore)) is not None
        assert session.scalar(
            select(Company).where(Company.canonical_name == "Agency Recruitment")
        ).is_agency

    detail = company_detail(sessions, company_id)
    assert detail is not None
    assert detail["company"] == "Client Hospital"
    assert detail["factors"]
    assert detail["active_jobs"]
    assert any(
        item["basis"] in {"FACT", "INFERENCE", "UNKNOWN"} for item in detail["evidence"]
    )
    assert "verified_decision_maker" not in detail["missing_inputs"]

    set_outreach_status(sessions, company_id, "contacted")
    assert company_detail(sessions, company_id)["outreach_status"] == "contacted"

    exported = export_top50_rows(sessions)
    assert list(exported[0]) == [*TABLE_COLUMNS, "evidence_summary"]
    assert exported[0]["company"] == "Client Hospital"
    assert exported[0]["key_contact"] == "Aisha Rahman"
    dumped = " ".join(exported[0].values()).casefold()
    assert "email" not in dumped
    assert "@" not in dumped

    csv_text = export_top50_csv(sessions)
    assert "Priority" not in csv_text.splitlines()[0]
    assert "company,industry" in csv_text.splitlines()[0] or "priority,company" in csv_text.splitlines()[0]

    summary = executive_summary(sessions)
    assert summary["scored_companies"] == 1
    assert summary["decision_makers_identified"] == 1
    assert summary["totals_by_priority"]["HIGH"] + summary["totals_by_priority"][
        "MEDIUM"
    ] + summary["totals_by_priority"]["LOW"] == 1

    breakdown = potential_breakdown(sessions)
    assert breakdown["by_region_industry"] == [
        {
            "region": "Dubai",
            "industry": "healthcare",
            "avg_score": rows[0]["graph_potential"],
            "company_count": 1,
        }
    ]
    assert breakdown["by_country_city"] == [
        {
            "country": "United Arab Emirates",
            "city": "Dubai",
            "avg_score": rows[0]["graph_potential"],
            "company_count": 1,
        }
    ]
    assert breakdown["top_city_by_country"][0]["city"] == "Dubai"


def test_graph_potential_rescales_kept_weights_to_100() -> None:
    weights = load_yaml("config/scoring.yaml")["weights"]
    kept = {
        name: {"score_0_100": 100}
        for name in weights
        if name not in GRAPH_EXCLUDED_FACTORS
    }
    ignored = {name: {"score_0_100": 0} for name in GRAPH_EXCLUDED_FACTORS}
    assert set(kept) == {
        "hiring_activity",
        "industry_opportunity",
        "seniority_match",
        "aambridge_fit",
    }
    assert "company_potential" not in weights
    assert graph_potential({**kept, **ignored}) == 100
    assert graph_potential({**kept, **{name: {"score_0_100": 100} for name in GRAPH_EXCLUDED_FACTORS}}) == 100
    assert graph_potential({name: {"score_0_100": 0} for name in kept} | ignored) == 0


def _company(name: str, band: str, industry: str, region: str, score: int = 50) -> dict:
    return {
        "company": name,
        "acquisition_band": band,
        "industry": industry,
        "region": region,
        "lead_score": score,
        "lead_score_percentile": score,
        "status": "new",
    }


def test_industry_and_region_rankings_follow_market_score() -> None:
    rows = [
        *[_company(f"Bank {index}", "High", "banking", "Dubai") for index in range(8)],
        *[_company(f"Bank mid {index}", "Medium", "banking", "Abu Dhabi") for index in range(4)],
        *[_company(f"Bank low {index}", "Low", "banking", "Dubai", score=10) for index in range(10)],
        *[_company(f"Retail {index}", "High", "retail", "Riyadh") for index in range(3)],
        *[_company(f"Retail mid {index}", "Medium", "retail", "Riyadh") for index in range(20)],
        *[_company(f"Retail low {index}", "Low", "retail", "Jeddah", score=5) for index in range(10)],
        _company("Unplaced", "High", "", "Dubai"),
    ]
    industries = industry_rankings(rows)
    assert industries["excluded"] == 1
    assert [item["industry"] for item in industries["rankings"]] == ["retail", "banking"]
    assert industries["rankings"][0]["market_score"] == 29
    assert industries["rankings"][1]["market_score"] == 28

    retail_regions = region_rankings(rows, "retail")
    assert [item["region"] for item in retail_regions["rankings"]] == ["Riyadh", "Jeddah"]
    assert retail_regions["rankings"][0]["market_score"] == 29
    assert retail_regions["rankings"][1]["market_score"] == 0
    assert retail_regions["excluded"] == 0

    snapshot = focus_snapshot(rows)
    assert snapshot["leading_industry"] == "retail"
    assert snapshot["high_uncontacted"] == 12
    assert snapshot["first_calls"][0]["acquisition_band"] == "High"


def _job_payload(
    *,
    title: str,
    company: str,
    industry: str,
    country: str,
    url: str,
    observed_at: datetime,
    city: str = "",
    region: str = "",
    posted_by_agency: bool = False,
) -> dict:
    return {
        "job_title": title,
        "company_name": company,
        "company_linkedin_url": f"https://linkedin.com/company/{company.lower().replace(' ', '-')}",
        "job_location": f"{city or country}",
        "city": city,
        "region": region,
        "country": country,
        "industry": industry,
        "job_url": url,
        "observed_at": observed_at,
        "posted_by_agency": posted_by_agency,
    }


def test_rolling_hiring_counts_a_job_once_across_scans_in_the_window(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    payload = _job_payload(
        title="Solar Engineer",
        company="Sun Co",
        industry="green_energy",
        country="United Arab Emirates",
        city="Dubai",
        region="Dubai",
        url="https://linkedin.com/jobs/view/8801",
        observed_at=now - timedelta(days=10),
    )
    first = ScanStore.start(sessions, mode="jobs", args={})
    assert first.save_job(payload)
    first.finish("completed")
    second = ScanStore.start(sessions, mode="jobs", args={})
    payload["observed_at"] = now
    assert second.save_job(payload)
    second.finish("completed")

    facts = rolling_hiring(sessions, days=30, hide_agencies=False, now=now)
    assert len(facts) == 1
    assert facts[0]["industry"] == "green_energy"
    chart = rolling_industry_chart(facts)
    green = next(item for item in chart["rankings"] if item["industry"] == "green_energy")
    assert green["job_count"] == 1
    assert green["company_count"] == 0


def test_rolling_hiring_excludes_jobs_older_than_the_window(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        _job_payload(
            title="Stale Nurse",
            company="Old Clinic",
            industry="healthcare",
            country="United Arab Emirates",
            url="https://linkedin.com/jobs/view/8802",
            observed_at=now - timedelta(days=31),
        )
    )
    assert scan.save_job(
        _job_payload(
            title="Current Nurse",
            company="New Clinic",
            industry="healthcare",
            country="United Arab Emirates",
            url="https://linkedin.com/jobs/view/8803",
            observed_at=now - timedelta(days=2),
        )
    )
    scan.finish("completed")
    facts = rolling_hiring(sessions, days=30, hide_agencies=False, now=now)
    assert len(facts) == 1
    assert facts[0]["job_id"]


def test_rolling_country_filter_lists_every_search_sector(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        _job_payload(
            title="Solar Engineer",
            company="Sun Co",
            industry="green_energy",
            country="United Arab Emirates",
            city="Abu Dhabi",
            region="Abu Dhabi",
            url="https://linkedin.com/jobs/view/8804",
            observed_at=now - timedelta(days=5),
        )
    )
    assert scan.save_job(
        _job_payload(
            title="Hospital Director",
            company="Riyadh Health",
            industry="healthcare",
            country="Saudi Arabia",
            city="Riyadh",
            region="Riyadh",
            url="https://linkedin.com/jobs/view/8805",
            observed_at=now - timedelta(days=5),
        )
    )
    assert scan.save_job(
        _job_payload(
            title="Agency Recruiter",
            company="Hire Fast",
            industry="healthcare",
            country="United Arab Emirates",
            city="Dubai",
            region="Dubai",
            url="https://linkedin.com/jobs/view/8806",
            observed_at=now - timedelta(days=1),
            posted_by_agency=True,
        )
    )
    scan.finish("completed")
    with sessions.begin() as session:
        jobs = {job.title: job for job in session.scalars(select(Job))}
        sun = Company(
            canonical_name="Sun Co",
            industry_key="renewable_energy",
            is_agency=False,
            jurisdiction="unknown",
        )
        agency = Company(
            canonical_name="Hire Fast",
            industry_key="healthcare",
            is_agency=True,
            jurisdiction="unknown",
        )
        session.add_all([sun, agency])
        session.flush()
        jobs["Solar Engineer"].company_id = sun.id
        jobs["Agency Recruiter"].company_id = agency.id

    facts = rolling_hiring(
        sessions,
        days=30,
        countries=("United Arab Emirates",),
        hide_agencies=True,
        now=now,
    )
    assert len(facts) == 1
    assert facts[0]["industry"] == "green_energy"
    chart = rolling_industry_chart(facts)
    by_industry = {item["industry"]: item for item in chart["rankings"]}
    assert "healthcare" not in by_industry
    assert by_industry["green_energy"]["job_count"] == 1
    assert by_industry["green_energy"]["company_count"] == 1
    filled = rolling_industry_chart(facts, fill_missing=True)
    assert {item["industry"] for item in filled["rankings"]} == {"green_energy"}
    regions = rolling_region_chart(facts, "green_energy")
    assert regions["rankings"][0]["region"] == "Abu Dhabi"
    assert regions["rankings"][0]["job_count"] == 1


def test_rolling_country_then_city_ranks_saudi_healthcare(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    now = datetime(2026, 10, 14, tzinfo=UTC)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        _job_payload(
            title="Hospital Director",
            company="Riyadh Health",
            industry="healthcare",
            country="Saudi Arabia",
            city="Riyadh",
            region="Riyadh",
            url="https://linkedin.com/jobs/view/8901",
            observed_at=now - timedelta(days=3),
        )
    )
    assert scan.save_job(
        _job_payload(
            title="Clinic Manager",
            company="Jeddah Clinic",
            industry="healthcare",
            country="Saudi Arabia",
            city="Jeddah",
            region="Makkah",
            url="https://linkedin.com/jobs/view/8902",
            observed_at=now - timedelta(days=4),
        )
    )
    assert scan.save_job(
        _job_payload(
            title="Ward Manager",
            company="Riyadh Ward",
            industry="healthcare",
            country="Saudi Arabia",
            city="Riyadh",
            region="Riyadh",
            url="https://linkedin.com/jobs/view/8903",
            observed_at=now - timedelta(days=2),
        )
    )
    assert scan.save_job(
        _job_payload(
            title="Solar Engineer",
            company="Sun Co",
            industry="green_energy",
            country="United Arab Emirates",
            city="Abu Dhabi",
            region="Abu Dhabi",
            url="https://linkedin.com/jobs/view/8904",
            observed_at=now - timedelta(days=1),
        )
    )
    assert scan.save_job(
        _job_payload(
            title="Remote Nurse",
            company="KSA Remote",
            industry="healthcare",
            country="Saudi Arabia",
            url="https://linkedin.com/jobs/view/8905",
            observed_at=now - timedelta(days=1),
        )
    )
    scan.finish("completed")

    facts = rolling_hiring(sessions, days=30, hide_agencies=False, now=now)
    countries = {
        item["country"]: item for item in rolling_country_chart(facts)["rankings"]
    }
    assert countries["Saudi Arabia"]["job_count"] == 4
    assert countries["United Arab Emirates"]["job_count"] == 1
    assert [item["country"] for item in rolling_country_chart(facts)["rankings"]][:2] == [
        "Saudi Arabia",
        "United Arab Emirates",
    ]

    saudi = [item for item in facts if item["country"] == "Saudi Arabia"]
    industries = {
        item["industry"]: item for item in rolling_industry_chart(saudi)["rankings"]
    }
    assert industries["healthcare"]["job_count"] == 4
    assert "green_energy" not in industries

    cities = rolling_city_chart(
        facts, country="Saudi Arabia", industry="healthcare"
    )["rankings"]
    assert [item["city"] for item in cities] == ["Riyadh", "Jeddah"]
    assert cities[0]["job_count"] == 2
    assert cities[1]["job_count"] == 1


def test_latest_completed_scan_label_and_empty_hint(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    assert latest_completed_scan(sessions) is None
    assert PULSE_DAYS == 15
    assert "python -m aambridge scrape" in EMPTY_SCAN_HINT
    scan = ScanStore.start(sessions, mode="jobs", args={})
    scan.finish("completed")
    info = latest_completed_scan(sessions)
    assert info is not None
    assert info["label"]
