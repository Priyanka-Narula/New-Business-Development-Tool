"""Pure lead-scoring factor functions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Basis = Literal["fact", "inference", "unknown"]


@dataclass(frozen=True)
class FactorResult:
    score_0_100: float
    reasons: list[str] = field(default_factory=list)
    evidence_ids: list[int] = field(default_factory=list)
    basis: Basis = "unknown"
    missing_inputs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompanyScoreContext:
    active_jobs: int = 0
    new_jobs: int = 0
    target_jobs: int = 0
    senior_jobs: int = 0
    junior_jobs: int = 0
    distinct_locations: int = 0
    signal_types: frozenset[str] = frozenset()
    inference_signals: frozenset[str] = frozenset()
    industry: str = ""
    industry_opportunity: float | None = None
    market_source_note: str = ""
    size_band: str = ""
    has_group: bool = False
    functions: frozenset[str] = frozenset()
    capability_industries: frozenset[str] = frozenset()
    capability_functions: frozenset[str] = frozenset()
    high_applicant_senior_jobs: int = 0
    nationality_restricted_jobs: int = 0
    specialist_jobs: int = 0
    decision_maker_rank: int | None = None
    evidence_ids: tuple[int, ...] = ()


def hiring_activity(context: CompanyScoreContext) -> FactorResult:
    if context.active_jobs == 0:
        return FactorResult(
            0,
            ["No active jobs observed in this scan"],
            list(context.evidence_ids),
            "unknown",
            ["active_jobs"],
        )
    score = min(40.0, context.target_jobs * 4.0)
    score += min(20.0, context.new_jobs * 4.0)
    reasons = [
        f"{context.active_jobs} active jobs",
        f"{context.target_jobs} target-seniority jobs",
        f"{context.new_jobs} newly observed jobs",
    ]
    additions = {
        "hiring_acceleration": (20, "Target-seniority hiring accelerated"),
        "repeated_vacancy": (10, "Repeated or long-open vacancies observed"),
        "new_location": (5, "Hiring appeared in a new location"),
        "expansion": (5, "Expansion or project-award evidence observed"),
    }
    for signal, (points, reason) in additions.items():
        if signal in context.signal_types:
            score += points
            reasons.append(reason)
    basis: Basis = (
        "inference"
        if context.inference_signals.intersection(additions)
        else "fact"
    )
    return FactorResult(
        min(100.0, score),
        reasons,
        list(context.evidence_ids),
        basis,
        [],
    )


def industry_opportunity(context: CompanyScoreContext) -> FactorResult:
    if context.industry_opportunity is None:
        return FactorResult(
            0,
            ["No country-industry market input is configured"],
            list(context.evidence_ids),
            "unknown",
            ["industry_opportunity"],
        )
    return FactorResult(
        context.industry_opportunity,
        [
            f"{context.industry} market opportunity: "
            f"{context.industry_opportunity:.0f}/100",
            context.market_source_note,
        ],
        list(context.evidence_ids),
        "inference",
        [],
    )


def seniority_match(context: CompanyScoreContext) -> FactorResult:
    total = context.target_jobs + context.junior_jobs
    if total == 0:
        return FactorResult(
            0,
            ["No classified seniority evidence"],
            list(context.evidence_ids),
            "unknown",
            ["classified_seniority"],
        )
    share = context.target_jobs / total
    return FactorResult(
        share * 100,
        [
            f"{context.target_jobs} of {total} classified jobs "
            "are AAMBRIDGE target seniority"
        ],
        list(context.evidence_ids),
        "fact",
        [],
    )


def company_potential(context: CompanyScoreContext) -> FactorResult:
    size_scores = {
        "1-49": 20,
        "50-249": 60,
        "250-999": 80,
        "1000+": 100,
    }
    if context.size_band not in size_scores:
        return FactorResult(
            0,
            ["Company size band is unknown"],
            list(context.evidence_ids),
            "unknown",
            ["size_band"],
        )
    missing: list[str] = []
    size = size_scores.get(context.size_band, 0)
    score = size * 0.6 + min(30, context.distinct_locations * 10)
    score += 10 if context.has_group else 0
    reasons = [f"Hiring across {context.distinct_locations} location(s)"]
    if context.size_band:
        reasons.append(f"Company size band: {context.size_band}")
    if context.has_group:
        reasons.append("Company belongs to a known group")
    return FactorResult(
        min(100.0, score),
        reasons,
        list(context.evidence_ids),
        "fact" if not missing else "unknown",
        missing,
    )


def aambridge_fit(context: CompanyScoreContext) -> FactorResult:
    if not context.industry:
        return FactorResult(
            0,
            ["Company industry is unknown"],
            list(context.evidence_ids),
            "unknown",
            ["industry"],
        )
    industry_fit = context.industry in context.capability_industries
    function_overlap = context.functions.intersection(context.capability_functions)
    score = (70 if industry_fit else 20) + min(30, len(function_overlap) * 10)
    reasons = [
        (
            f"{context.industry} is an AAMBRIDGE capability industry"
            if industry_fit
            else f"{context.industry} is outside the primary capability list"
        )
    ]
    if function_overlap:
        reasons.append(
            "Relevant functions: " + ", ".join(sorted(function_overlap))
        )
    return FactorResult(
        min(100.0, score),
        reasons,
        list(context.evidence_ids),
        "fact",
        [],
    )


def external_agency_potential(context: CompanyScoreContext) -> FactorResult:
    score = 0.0
    reasons: list[str] = []
    if "repeated_vacancy" in context.signal_types:
        score += 25
        reasons.append("Repeated or long-open vacancies")
    if context.high_applicant_senior_jobs:
        score += min(25, context.high_applicant_senior_jobs * 10)
        reasons.append(
            f"{context.high_applicant_senior_jobs} senior role(s) with high applicants"
        )
    if context.nationality_restricted_jobs:
        score += min(25, context.nationality_restricted_jobs * 15)
        reasons.append("Nationality-restricted hiring observed")
    if "localisation_pressure" in context.signal_types:
        score += 15
        reasons.append("Likely localisation pressure")
    if context.specialist_jobs:
        score += min(10, context.specialist_jobs * 5)
        reasons.append("Specialist or functional-expert demand")
    if not reasons:
        return FactorResult(
            0,
            ["No external-agency demand indicators observed"],
            list(context.evidence_ids),
            "unknown",
            ["agency_demand_indicators"],
        )
    basis: Basis = (
        "inference"
        if "localisation_pressure" in context.signal_types
        else "fact"
    )
    return FactorResult(
        min(100.0, score),
        reasons,
        list(context.evidence_ids),
        basis,
        [],
    )


def decision_maker_accessibility(context: CompanyScoreContext) -> FactorResult:
    if context.decision_maker_rank is None:
        return FactorResult(
            0,
            ["No verified decision-maker is recorded"],
            [],
            "unknown",
            ["verified_decision_maker"],
        )
    score = max(30.0, 100.0 - (context.decision_maker_rank - 1) * 5)
    return FactorResult(
        score,
        [f"Verified decision-maker relevance rank {context.decision_maker_rank}"],
        [],
        "fact",
        [],
    )
