"""Database context building and deterministic weighted lead scoring."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.db.models import (
    Company,
    DecisionMaker,
    Evidence,
    Job,
    LeadScore,
    ScanRun,
    Signal,
    SourceRecord,
)
from aambridge.extract.taxonomies import load_yaml, target_seniority_keys
from aambridge.scoring.bands import assign_acquisition_bands
from aambridge.scoring.factors import (
    CompanyScoreContext,
    FactorResult,
    aambridge_fit,
    company_potential,
    decision_maker_accessibility,
    external_agency_potential,
    hiring_activity,
    industry_opportunity,
    seniority_match,
)
from aambridge.signals.detect import _seniority

FACTOR_FUNCTIONS = {
    "hiring_activity": hiring_activity,
    "industry_opportunity": industry_opportunity,
    "seniority_match": seniority_match,
    "company_potential": company_potential,
    "aambridge_fit": aambridge_fit,
    "external_agency_potential": external_agency_potential,
    "decision_maker_accessibility": decision_maker_accessibility,
}
INDUSTRY_ALIASES = {
    "green_energy": "renewable_energy",
    "hospitality": "hospitality_tourism",
    "ai": "ai_data",
}


def _latest_scan_id(session: Session) -> int:
    scan_id = session.scalar(
        select(ScanRun.id)
        .where(ScanRun.status == "completed")
        .order_by(ScanRun.started_at.desc())
    )
    if scan_id is None:
        raise ValueError("No completed scan is available for scoring")
    return scan_id


def _function_keys(text: str) -> frozenset[str]:
    values = load_yaml("taxonomies/functions.yaml")["functions"]
    matched = {
        key
        for key, keywords in values.items()
        if any(
            re.search(
                rf"(?<!\w){re.escape(str(keyword))}(?!\w)",
                text,
                re.IGNORECASE,
            )
            for keyword in keywords
        )
    }
    return frozenset(matched)


def _ensure_evidence(
    session: Session,
    source: SourceRecord,
    quote: str,
) -> int:
    existing = session.scalar(
        select(Evidence).where(
            Evidence.signal_id.is_(None),
            Evidence.source_record_id == source.id,
            Evidence.quoted_text == quote,
        )
    )
    if existing is not None:
        return existing.id
    evidence = Evidence(
        signal_id=None,
        source_record_id=source.id,
        quoted_text=quote,
        source_url=source.source_url,
        observed_at=source.observed_at,
        basis="fact",
    )
    session.add(evidence)
    session.flush()
    return evidence.id


def _market_input(country: str, industry: str) -> tuple[float | None, str]:
    markets = load_yaml("taxonomies/market_model.yaml")["markets"]
    country_values = markets.get(country)
    if not country_values:
        return None, ""
    value = country_values.get(industry) or country_values.get("other")
    if not value:
        return None, ""
    return float(value["score"]), (
        f"{value['source_note']} (as of {value['as_of']})"
    )


def build_context(
    session: Session,
    company: Company,
    scan_run_id: int,
) -> CompanyScoreContext:
    jobs = list(session.scalars(select(Job).where(Job.company_id == company.id)))
    target_levels = target_seniority_keys()
    target_jobs = 0
    senior_jobs = 0
    junior_jobs = 0
    new_jobs = 0
    specialist_jobs = 0
    high_applicant_senior = 0
    nationality_restricted = 0
    locations: set[str] = set()
    functions: set[str] = set()
    industries: list[str] = []
    countries: list[str] = []
    evidence_ids: set[int] = set()
    active_jobs = 0

    for job in jobs:
        observation = next(
            (
                item
                for item in job.observations
                if item.scan_run_id == scan_run_id
            ),
            None,
        )
        if observation is None:
            continue
        active_jobs += 1
        if len(job.observations) == 1:
            new_jobs += 1
        raw = observation.raw_listing_json
        text = f"{job.title} {raw.get('description', '')} {raw.get('seniority_hint', '')}"
        level = _seniority(text)
        if level in target_levels:
            target_jobs += 1
        if level in {"director", "vp", "c_level"}:
            senior_jobs += 1
            if (observation.applicant_count or 0) >= 100:
                high_applicant_senior += 1
        if level in {"intern", "entry", "junior"}:
            junior_jobs += 1
        if level in {"specialist", "functional_expert"}:
            specialist_jobs += 1
        if raw.get("nationality_restriction"):
            nationality_restricted += 1
        if job.city or job.location_raw:
            locations.add(job.city or job.location_raw)
        functions.update(_function_keys(text))
        industry = str(raw.get("industry") or "")
        if industry:
            industries.append(INDUSTRY_ALIASES.get(industry, industry))
        if job.country:
            countries.append(job.country)
        source = session.scalar(
            select(SourceRecord).where(
                SourceRecord.scan_run_id == scan_run_id,
                SourceRecord.source == "linkedin_job",
                SourceRecord.source_key == job.source_key,
            )
        )
        if source is not None:
            evidence_ids.add(_ensure_evidence(session, source, job.title))

    signals = list(
        session.scalars(
            select(Signal).where(
                Signal.company_id == company.id,
                Signal.scan_run_id == scan_run_id,
            )
        )
    )
    signal_types = frozenset(signal.signal_type for signal in signals)
    inference_signals = frozenset(
        signal.signal_type for signal in signals if signal.basis == "inference"
    )
    signal_ids = [signal.id for signal in signals]
    if signal_ids:
        evidence_ids.update(
            session.scalars(
                select(Evidence.id).where(Evidence.signal_id.in_(signal_ids))
            )
        )

    industry = company.industry_key or (industries[0] if industries else "")
    country = company.hq_country or (countries[0] if countries else "")
    opportunity, source_note = _market_input(country, industry)
    config = load_yaml("config/scoring.yaml")
    capabilities = config["aambridge_capabilities"]
    decision_rank = session.scalar(
        select(DecisionMaker.relevance_rank)
        .where(
            DecisionMaker.company_id == company.id,
            DecisionMaker.verified_at.is_not(None),
        )
        .order_by(DecisionMaker.relevance_rank)
    )
    return CompanyScoreContext(
        active_jobs=active_jobs,
        new_jobs=new_jobs,
        target_jobs=target_jobs,
        senior_jobs=senior_jobs,
        junior_jobs=junior_jobs,
        distinct_locations=len(locations),
        signal_types=signal_types,
        inference_signals=inference_signals,
        industry=industry,
        industry_opportunity=opportunity,
        market_source_note=source_note,
        size_band=company.size_band or "",
        has_group=company.group_id is not None,
        functions=frozenset(functions),
        capability_industries=frozenset(capabilities["industries"]),
        capability_functions=frozenset(capabilities["functions"]),
        high_applicant_senior_jobs=high_applicant_senior,
        nationality_restricted_jobs=nationality_restricted,
        specialist_jobs=specialist_jobs,
        decision_maker_rank=decision_rank,
        evidence_ids=tuple(sorted(evidence_ids)),
    )


def score_context(
    context: CompanyScoreContext,
) -> tuple[int, str, dict[str, FactorResult]]:
    config = load_yaml("config/scoring.yaml")
    weights = config["weights"]
    factors = {
        name: function(context) for name, function in FACTOR_FUNCTIONS.items()
    }
    total = round(
        sum(
            factors[name].score_0_100 * float(weights[name]) / 100
            for name in FACTOR_FUNCTIONS
        )
    )
    priority_rules = config["priority"]
    high = (
        total >= int(priority_rules["high_min_score"])
        and factors["hiring_activity"].score_0_100
        >= float(priority_rules["high_min_hiring_activity"])
        and factors["seniority_match"].score_0_100
        >= float(priority_rules["high_min_seniority_match"])
        and factors["external_agency_potential"].score_0_100
        >= float(priority_rules["high_min_agency_potential"])
        and (
            context.industry in context.capability_industries
            or context.industry_opportunity is not None
        )
        and (
            context.decision_maker_rank is not None
            or not priority_rules["require_decision_maker_for_high"]
        )
    )
    priority = (
        "HIGH"
        if high
        else "MEDIUM"
        if total >= int(priority_rules["medium_min_score"])
        else "LOW"
    )
    return total, priority, factors


def _explanation(
    session: Session,
    company: Company,
    total: int,
    factors: dict[str, FactorResult],
) -> str:
    reasons: list[str] = []
    for name, result in sorted(
        factors.items(),
        key=lambda item: item[1].score_0_100,
        reverse=True,
    ):
        if result.score_0_100 and result.reasons:
            reasons.append(
                f"{name.replace('_', ' ').title()} "
                f"{result.score_0_100:.0f}/100: {result.reasons[0]}"
            )
    evidence_ids = sorted(
        {item for result in factors.values() for item in result.evidence_ids}
    )
    evidence_note = ""
    if evidence_ids:
        evidence = session.get(Evidence, evidence_ids[0])
        if evidence:
            evidence_note = (
                f" Evidence observed {evidence.observed_at.date().isoformat()}"
                f" from {evidence.source_url or 'stored source'}"
                f" [{evidence.basis.upper()}]."
            )
    return (
        f"{company.canonical_name} scored {total}/100 because: "
        + "; ".join(reasons[:4])
        + "."
        + evidence_note
    )


def score_companies(
    session_factory: sessionmaker[Session],
    *,
    scan_run_id: int | None = None,
) -> dict[str, int]:
    stats = {"scored": 0, "agencies_skipped": 0}
    with session_factory.begin() as session:
        current_id = scan_run_id or _latest_scan_id(session)
        for company in session.scalars(select(Company).order_by(Company.id)):
            if company.is_agency:
                stats["agencies_skipped"] += 1
                continue
            context = build_context(session, company, current_id)
            if context.active_jobs == 0 and "expansion" not in context.signal_types:
                continue
            total, priority, factors = score_context(context)
            factor_json = {
                name: asdict(result) for name, result in factors.items()
            }
            explanation = _explanation(session, company, total, factors)
            existing = session.scalar(
                select(LeadScore).where(
                    LeadScore.company_id == company.id,
                    LeadScore.scan_run_id == current_id,
                )
            )
            values: dict[str, Any] = {
                "total_score": total,
                "priority": priority,
                "factor_results_json": factor_json,
                "explanation_json": {
                    "factors": factor_json,
                    "missing_inputs": sorted(
                        {
                            missing
                            for result in factors.values()
                            for missing in result.missing_inputs
                        }
                    ),
                },
                "explanation_text": explanation,
                "created_at": datetime.now(UTC),
            }
            if existing is None:
                session.add(
                    LeadScore(
                        company_id=company.id,
                        scan_run_id=current_id,
                        **values,
                    )
                )
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            stats["scored"] += 1
        assign_acquisition_bands(session)
    return stats
