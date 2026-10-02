from __future__ import annotations

from sqlalchemy import inspect, select

from aambridge.dashboard.queries import (
    GRAPH_EXCLUDED_FACTORS,
    TABLE_COLUMNS,
    PipelineFilters,
    company_detail,
    executive_summary,
    export_top50_csv,
    export_top50_rows,
    focus_snapshot,
    graph_potential,
    industry_rankings,
    pipeline_rows,
    potential_breakdown,
    region_rankings,
    set_outreach_status,
)
from aambridge.db.models import Company, LeadScore
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
    assert rows[0]["status"] == "new"
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

    emiratisation = pipeline_rows(
        sessions, PipelineFilters(preset="emiratisation", hide_agencies=True)
    )
    assert [row["company"] for row in emiratisation] == ["Client Hospital"]

    uae = pipeline_rows(
        sessions, PipelineFilters(preset="uae_riyadh", hide_agencies=True)
    )
    assert [row["company"] for row in uae] == ["Client Hospital"]

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
