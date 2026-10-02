"""Relative acquisition tiers from lead scores, and market ranking weights."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.db.models import Company, LeadScore
from aambridge.extract.taxonomies import load_yaml

HIGH_MIN_PERCENTILE = 200 / 3
MEDIUM_MIN_PERCENTILE = 100 / 3
ACQUISITION_BANDS = ("High", "Medium", "Low")


def percentile_map(scores: Sequence[int]) -> dict[int, float]:
    """Mid-rank percentile for each distinct score. Ties share one value."""

    total = len(scores)
    if total == 0:
        return {}
    counts = Counter(scores)
    lower = 0
    ranked: dict[int, float] = {}
    for score in sorted(counts):
        same = counts[score]
        ranked[score] = (lower + 0.5 * same) / total * 100
        lower += same
    return ranked


def band_for_percentile(percentile: float) -> str:
    """Top third High, middle third Medium, remainder Low."""

    # Mid-rank percentiles can sit a hair below the exact third because of
    # binary floats. The tolerance keeps a score on the boundary in the
    # higher band, and identical scores still share one band.
    if percentile + 1e-6 >= HIGH_MIN_PERCENTILE:
        return "High"
    if percentile + 1e-6 >= MEDIUM_MIN_PERCENTILE:
        return "Medium"
    return "Low"


def market_weights() -> dict[str, float]:
    raw = load_yaml("config/scoring.yaml").get("market_potential") or {}
    return {
        "high": float(raw.get("high", 3)),
        "medium": float(raw.get("medium", 1)),
        "low": float(raw.get("low", 0)),
    }


def market_score(high: int, medium: int, low: int = 0) -> int:
    """Quality-weighted count. Low companies stay visible but do not raise rank."""

    weights = market_weights()
    return round(
        weights["high"] * high
        + weights["medium"] * medium
        + weights["low"] * low
    )


def assign_acquisition_bands(session: Session) -> int:
    """Persist percentile and band for every scored non-agency company."""

    latest: dict[int, LeadScore] = {}
    for score in session.scalars(
        select(LeadScore).order_by(LeadScore.created_at.desc(), LeadScore.id.desc())
    ):
        latest.setdefault(score.company_id, score)

    population: list[tuple[Company, int]] = []
    for company in session.scalars(select(Company)):
        score = latest.get(company.id)
        if company.is_agency or score is None:
            company.lead_score_percentile = None
            company.acquisition_band = None
            continue
        population.append((company, int(score.total_score)))

    ranked = percentile_map([total for _company, total in population])
    for company, total in population:
        percentile = ranked[total]
        company.lead_score_percentile = round(percentile, 2)
        company.acquisition_band = band_for_percentile(percentile)
    return len(population)


def refresh_acquisition_bands(session_factory: sessionmaker[Session]) -> int:
    """Recompute tiers for an existing database without rescoring."""

    with session_factory.begin() as session:
        return assign_acquisition_bands(session)
