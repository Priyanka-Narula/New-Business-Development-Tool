from __future__ import annotations

import pytest
from sqlalchemy import func, select

from aambridge.db.models import Company, LeadScore
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.db.storage import ScanStore
from aambridge.resolve.companies import resolve_all
from aambridge.scoring.bands import (
    band_for_percentile,
    market_score,
    percentile_map,
)
from aambridge.scoring.engine import score_companies, score_context
from aambridge.scoring.factors import CompanyScoreContext

CAPABILITY_INDUSTRIES = frozenset(
    {"healthcare", "retail", "construction_infrastructure"}
)
CAPABILITY_FUNCTIONS = frozenset(
    {"clinical", "hr_talent", "retail_ops", "engineering", "project_management"}
)


@pytest.mark.parametrize(
    ("context", "expected_total", "expected_priority"),
    [
        (
            CompanyScoreContext(
                active_jobs=10,
                new_jobs=5,
                target_jobs=8,
                junior_jobs=2,
                distinct_locations=2,
                signal_types=frozenset(
                    {
                        "hiring_acceleration",
                        "repeated_vacancy",
                        "new_location",
                        "expansion",
                        "localisation_pressure",
                    }
                ),
                inference_signals=frozenset({"localisation_pressure"}),
                industry="healthcare",
                industry_opportunity=85,
                market_source_note="Fixture market input",
                size_band="250-999",
                has_group=True,
                functions=frozenset({"clinical", "hr_talent"}),
                capability_industries=CAPABILITY_INDUSTRIES,
                capability_functions=CAPABILITY_FUNCTIONS,
                high_applicant_senior_jobs=2,
                nationality_restricted_jobs=2,
                specialist_jobs=2,
                decision_maker_rank=1,
            ),
            88,
            "HIGH",
        ),
        (
            CompanyScoreContext(
                active_jobs=10,
                new_jobs=1,
                target_jobs=1,
                junior_jobs=9,
                distinct_locations=1,
                industry="retail",
                industry_opportunity=82,
                market_source_note="Fixture market input",
                size_band="1-49",
                functions=frozenset({"retail_ops"}),
                capability_industries=CAPABILITY_INDUSTRIES,
                capability_functions=CAPABILITY_FUNCTIONS,
            ),
            34,
            "LOW",
        ),
        (
            CompanyScoreContext(
                active_jobs=1,
                distinct_locations=1,
                capability_industries=CAPABILITY_INDUSTRIES,
                capability_functions=CAPABILITY_FUNCTIONS,
            ),
            0,
            "LOW",
        ),
        (
            CompanyScoreContext(
                active_jobs=5,
                new_jobs=2,
                target_jobs=4,
                junior_jobs=1,
                distinct_locations=1,
                signal_types=frozenset({"repeated_vacancy"}),
                industry="construction_infrastructure",
                industry_opportunity=95,
                market_source_note="Fixture market input",
                size_band="50-249",
                functions=frozenset({"engineering", "project_management"}),
                capability_industries=CAPABILITY_INDUSTRIES,
                capability_functions=CAPABILITY_FUNCTIONS,
                specialist_jobs=1,
            ),
            61,
            "MEDIUM",
        ),
        (
            CompanyScoreContext(
                active_jobs=10,
                new_jobs=5,
                target_jobs=8,
                junior_jobs=2,
                distinct_locations=2,
                signal_types=frozenset(
                    {"hiring_acceleration", "repeated_vacancy", "expansion"}
                ),
                industry="healthcare",
                industry_opportunity=85,
                market_source_note="Fixture market input",
                size_band="250-999",
                functions=frozenset({"clinical"}),
                capability_industries=CAPABILITY_INDUSTRIES,
                capability_functions=CAPABILITY_FUNCTIONS,
                high_applicant_senior_jobs=2,
                nationality_restricted_jobs=2,
                specialist_jobs=2,
            ),
            78,
            "MEDIUM",
        ),
    ],
)
def test_five_hand_built_company_scores(
    context: CompanyScoreContext,
    expected_total: int,
    expected_priority: str,
) -> None:
    total, priority, factors = score_context(context)

    assert total == expected_total
    assert priority == expected_priority
    assert set(factors) == {
        "hiring_activity",
        "industry_opportunity",
        "seniority_match",
        "company_potential",
        "aambridge_fit",
        "external_agency_potential",
        "decision_maker_accessibility",
    }


def test_database_scoring_persists_explanation_and_skips_agencies(tmp_path) -> None:
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'score.db').as_posix()}")
    initialise_database(engine)
    sessions = create_session_factory(engine)
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
            "job_url": "https://linkedin.com/jobs/view/6001",
            "seniority_hint": "manager",
        }
    )
    assert scan.save_job(
        {
            "job_title": "Finance Director",
            "company_name": "Agency Recruitment",
            "company_linkedin_url": "https://linkedin.com/company/agency-recruitment",
            "job_location": "Dubai, United Arab Emirates",
            "city": "Dubai",
            "country": "United Arab Emirates",
            "industry": "banking_financial_services",
            "job_url": "https://linkedin.com/jobs/view/6002",
            "posted_by_agency": True,
        }
    )
    scan.finish("completed")
    resolve_all(sessions)

    stats = score_companies(sessions, scan_run_id=scan.scan_run_id)

    assert stats == {"scored": 1, "agencies_skipped": 1}
    with sessions() as session:
        score = session.scalar(select(LeadScore))
        assert score is not None
        assert "Client Hospital scored" in score.explanation_text
        assert set(score.factor_results_json) == {
            "hiring_activity",
            "industry_opportunity",
            "seniority_match",
            "company_potential",
            "aambridge_fit",
            "external_agency_potential",
            "decision_maker_accessibility",
        }
        assert session.scalar(select(func.count()).select_from(LeadScore)) == 1
        client = session.scalar(
            select(Company).where(Company.canonical_name == "Client Hospital")
        )
        agency = session.scalar(
            select(Company).where(Company.canonical_name == "Agency Recruitment")
        )
        assert client is not None and agency is not None
        assert client.lead_score_percentile == 50
        assert client.acquisition_band == "Medium"
        assert agency.lead_score_percentile is None
        assert agency.acquisition_band is None


def test_percentile_tertiles_and_ties() -> None:
    distinct = list(range(1, 10))
    ranked = percentile_map(distinct)
    bands = [band_for_percentile(ranked[score]) for score in distinct]
    assert bands == [
        "Low",
        "Low",
        "Low",
        "Medium",
        "Medium",
        "Medium",
        "High",
        "High",
        "High",
    ]

    tied = percentile_map([10, 90, 90])
    assert [band_for_percentile(tied[score]) for score in (10, 90, 90)] == [
        "Low",
        "High",
        "High",
    ]


def test_market_score_weights_high_companies_above_volume() -> None:
    assert market_score(8, 4, 10) == 28
    assert market_score(3, 20, 10) == 29
    assert market_score(0, 0, 12) == 0
