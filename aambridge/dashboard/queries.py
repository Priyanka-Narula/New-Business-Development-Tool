"""Deterministic dashboard queries. No live LinkedIn access."""

from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from aambridge.contacts.enrich import best_organizational_channel
from aambridge.db.models import (
    Company,
    CompanyContact,
    DecisionMaker,
    Evidence,
    Job,
    LeadScore,
    Post,
    Signal,
    SourceRecord,
)
from aambridge.extract.taxonomies import load_yaml
from aambridge.privacy import redact_personal_contacts
from aambridge.scoring.bands import market_score
from aambridge.signals.detect import _scan_ids, _seniority, what_changed
from config import SECTOR_KEYWORDS

OUTREACH_STATUSES = (
    "new",
    "researching",
    "contacted",
    "meeting",
    "client",
    "not a fit",
)
TABLE_COLUMNS = (
    "priority",
    "company",
    "website",
    "industry",
    "country_city",
    "hiring_activity",
    "roles_hiring",
    "seniority",
    "key_contact",
    "designation",
    "company_phone",
    "company_email",
    "lead_score",
    "status",
    "last_updated",
)
UAE_NAMES = {"united arab emirates", "uae"}
BAND_ORDER = {"High": 0, "Medium": 1, "Low": 2}
FIRST_CALL_STATUSES = frozenset({"new", "researching"})
GRAPH_EXCLUDED_FACTORS = frozenset(
    {
        "company_potential",
        "external_agency_potential",
        "decision_maker_accessibility",
    }
)
ROLLING_DAYS = 30
SEARCH_SECTORS = tuple(SECTOR_KEYWORDS)
TAXONOMY_TO_SECTOR = {
    "renewable_energy": "green_energy",
    "hospitality_tourism": "hospitality",
    "ai_data": "ai",
}


@dataclass(frozen=True)
class PipelineFilters:
    countries: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    industries: tuple[str, ...] = ()
    seniorities: tuple[str, ...] = ()
    hiring_bands: tuple[str, ...] = ()
    size_bands: tuple[str, ...] = ()
    score_min: int = 0
    score_max: int = 100
    outreach_statuses: tuple[str, ...] = ()
    hide_agencies: bool = True
    preset: str | None = None
    limit: int | None = None
    acquisition_bands: tuple[str, ...] = ()


@dataclass
class _JobSummary:
    titles: list[str] = field(default_factory=list)
    seniorities: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    active_count: int = 0
    nationality_restricted: bool = False
    new_this_scan: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)


def hiring_band(score_0_100: float) -> str:
    if score_0_100 >= 60:
        return "high"
    if score_0_100 >= 30:
        return "medium"
    return "low"


def size_at_least_50(size_band: str | None) -> bool:
    match = re.match(r"(\d+)", size_band or "")
    return bool(match and int(match.group(1)) >= 50)


def _latest_scan(session: Session) -> int | None:
    scans = _scan_ids(session)
    return scans[-1] if scans else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _normalise_sector(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if text in SECTOR_KEYWORDS:
        return text
    return TAXONOMY_TO_SECTOR.get(text, text)


def _job_sector(company: Company | None, raw: dict[str, Any]) -> str:
    if company is not None:
        from_company = _normalise_sector(company.industry_key or "")
        if from_company:
            return from_company
    return _normalise_sector(str(raw.get("industry") or ""))


def rolling_hiring(
    session_factory: sessionmaker[Session],
    *,
    days: int = ROLLING_DAYS,
    countries: tuple[str, ...] = (),
    regions: tuple[str, ...] = (),
    cities: tuple[str, ...] = (),
    industries: tuple[str, ...] = (),
    hide_agencies: bool = True,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One row per unique job observed in the rolling window."""

    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    with session_factory() as session:
        jobs = list(
            session.scalars(select(Job).options(selectinload(Job.observations)))
        )
        companies = {
            company.id: company for company in session.scalars(select(Company))
        }
    facts: list[dict[str, Any]] = []
    for job in jobs:
        windowed = [
            item
            for item in job.observations
            if _aware(item.observed_at) >= cutoff
        ]
        if not windowed:
            continue
        company = companies.get(job.company_id) if job.company_id else None
        if hide_agencies and company is not None and company.is_agency:
            continue
        latest = max(windowed, key=lambda item: _aware(item.observed_at))
        raw = latest.raw_listing_json or {}
        industry = _job_sector(company, raw)
        country = job.country or ""
        region = job.region or ""
        city = job.city or ""
        if countries and country not in countries:
            continue
        if regions and region not in regions:
            continue
        if cities and city not in cities:
            continue
        if industries and industry not in industries:
            continue
        facts.append(
            {
                "job_id": job.id,
                "company_id": job.company_id,
                "industry": industry,
                "country": country,
                "region": region,
                "city": city,
                "is_agency": bool(company is not None and company.is_agency),
            }
        )
    return facts


def _chart_row(label_key: str, name: str, job_ids: set[int], company_ids: set[int]) -> dict[str, Any]:
    job_count = len(job_ids)
    return {
        label_key: name,
        "job_count": job_count,
        "company_count": len(company_ids),
        "market_score": job_count,
        "high": 0,
        "medium": 0,
        "low": 0,
    }


def rolling_industry_chart(
    facts: Sequence[dict[str, Any]],
    *,
    fill_missing: bool = True,
) -> dict[str, Any]:
    grouped: dict[str, tuple[set[int], set[int]]] = defaultdict(lambda: (set(), set()))
    for fact in facts:
        industry = str(fact.get("industry") or "")
        jobs, companies = grouped[industry]
        jobs.add(int(fact["job_id"]))
        if fact.get("company_id"):
            companies.add(int(fact["company_id"]))
    names = list(SEARCH_SECTORS) if fill_missing else [
        name for name in grouped if name
    ]
    extra = [name for name in grouped if name and name not in names]
    rankings = [
        _chart_row("industry", name, *grouped[name])
        for name in [*names, *sorted(extra)]
    ]
    rankings.sort(key=lambda item: (-int(item["job_count"]), str(item["industry"])))
    return {"rankings": rankings, "excluded": 0}


def rolling_region_chart(
    facts: Sequence[dict[str, Any]],
    industry: str,
) -> dict[str, Any]:
    grouped: dict[str, tuple[set[int], set[int]]] = defaultdict(lambda: (set(), set()))
    for fact in facts:
        if fact.get("industry") != industry:
            continue
        region = str(fact.get("region") or fact.get("city") or "")
        if not region:
            continue
        jobs, companies = grouped[region]
        jobs.add(int(fact["job_id"]))
        if fact.get("company_id"):
            companies.add(int(fact["company_id"]))
    rankings = [
        _chart_row("region", name, jobs, companies)
        for name, (jobs, companies) in grouped.items()
    ]
    rankings.sort(key=lambda item: (-int(item["job_count"]), str(item["region"])))
    return {"rankings": rankings, "excluded": 0, "industry": industry}


def _mode(values: list[str]) -> str:
    cleaned = [item for item in values if item]
    if not cleaned:
        return ""
    return Counter(cleaned).most_common(1)[0][0]


def _contact(session: Session, company_id: int) -> DecisionMaker | None:
    return _contacts_by_company(session, (company_id,)).get(company_id)


def _contacts_by_company(
    session: Session,
    company_ids: Sequence[int],
) -> dict[int, DecisionMaker]:
    found: dict[int, DecisionMaker] = {}
    if not company_ids:
        return found
    for item in session.scalars(
        select(DecisionMaker)
        .where(
            DecisionMaker.company_id.in_(company_ids),
            DecisionMaker.verified_at.is_not(None),
        )
        .order_by(DecisionMaker.relevance_rank, DecisionMaker.id)
    ):
        found.setdefault(item.company_id, item)
    return found


def _channels_by_company(
    session: Session,
    company_ids: Sequence[int],
) -> dict[int, list[CompanyContact]]:
    grouped: dict[int, list[CompanyContact]] = defaultdict(list)
    if not company_ids:
        return grouped
    for item in session.scalars(
        select(CompanyContact)
        .where(CompanyContact.company_id.in_(company_ids))
        .order_by(CompanyContact.id)
    ):
        grouped[item.company_id].append(item)
    return grouped


def _people_by_company(
    session: Session,
    company_ids: Sequence[int],
) -> dict[int, list[DecisionMaker]]:
    grouped: dict[int, list[DecisionMaker]] = defaultdict(list)
    if not company_ids:
        return grouped
    for item in session.scalars(
        select(DecisionMaker)
        .where(DecisionMaker.company_id.in_(company_ids))
        .order_by(
            DecisionMaker.verified_at.is_(None),
            DecisionMaker.relevance_rank,
            DecisionMaker.id,
        )
    ):
        grouped[item.company_id].append(item)
    return grouped


def _channel_payload(item: CompanyContact) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "value": item.value,
        "label": item.label or "",
        "source": item.source,
        "source_url": item.source_url or "",
        "is_organizational": item.is_organizational,
    }


def _person_payload(item: DecisionMaker) -> dict[str, Any]:
    location = ""
    if item.source == "apollo":
        location = (item.notes or "").strip()
    return {
        "id": item.id,
        "name": item.name,
        "designation": item.designation,
        "email": item.email or "",
        "phone": item.phone or "",
        "profile_url": item.profile_url or "",
        "source": item.source,
        "verified": item.verified_at is not None,
        "apollo_person_id": item.apollo_person_id or "",
        "email_status": item.email_status or "",
        "disclosed": item.disclosed_at is not None,
        "location": location,
    }


def _jobs_by_company(
    session: Session,
    company_ids: Sequence[int],
) -> dict[int, list[Job]]:
    grouped: dict[int, list[Job]] = defaultdict(list)
    if not company_ids:
        return grouped
    for job in session.scalars(
        select(Job)
        .where(Job.company_id.in_(company_ids))
        .options(selectinload(Job.observations))
    ):
        grouped[job.company_id].append(job)
    return grouped


def _job_summary(session: Session, company_id: int) -> _JobSummary:
    return _job_summary_from_jobs(_jobs_by_company(session, (company_id,)).get(company_id, []))


def _job_summary_from_jobs(jobs: Sequence[Job]) -> _JobSummary:
    summary = _JobSummary()
    for job in jobs:
        raw = {}
        if job.observations:
            latest = max(job.observations, key=lambda item: item.observed_at)
            raw = latest.raw_listing_json or {}
        level = _seniority(" ".join(filter(None, [job.title, raw.get("job_description", "")])))
        if job.is_active:
            summary.active_count += 1
            if len(job.observations) <= 1:
                summary.new_this_scan = True
            summary.titles.append(job.title)
            summary.seniorities.append(level)
            if job.country:
                summary.countries.append(job.country)
            if job.region:
                summary.regions.append(job.region)
            if job.city:
                summary.cities.append(job.city)
            summary.rows.append(
                {
                    "title": job.title,
                    "location": ", ".join(
                        part for part in (job.city, job.country) if part
                    ),
                    "seniority": level,
                    "active": True,
                    "source_key": job.source_key,
                }
            )
        if raw.get("nationality_restriction") or raw.get(
            "nationality_restriction_evidence"
        ):
            summary.nationality_restricted = True
    return summary


def _factor_score(score: LeadScore, name: str) -> float:
    payload = (score.factor_results_json or {}).get(name) or {}
    return float(payload.get("score_0_100") or 0)


def graph_potential(factor_results: dict[str, Any] | None) -> int:
    """0-100 score from kept factors, with their weights rescaled to 100."""

    weights = load_yaml("config/scoring.yaml")["weights"]
    kept = {
        name: float(weight)
        for name, weight in weights.items()
        if name not in GRAPH_EXCLUDED_FACTORS
    }
    total_weight = sum(kept.values())
    if not total_weight:
        return 0
    factors = factor_results or {}
    weighted = 0.0
    for name, weight in kept.items():
        payload = factors.get(name) or {}
        weighted += float(payload.get("score_0_100") or 0) * weight
    return round(weighted / total_weight)


def _pipeline_row(
    company: Company,
    score: LeadScore,
    jobs: _JobSummary,
    contact: DecisionMaker | None,
    channels: Sequence[CompanyContact] = (),
) -> dict[str, Any]:
    country = company.hq_country or _mode(jobs.countries)
    city = _mode(jobs.cities)
    activity = _factor_score(score, "hiring_activity")
    return {
        "company_id": company.id,
        "priority": score.priority,
        "company": company.canonical_name,
        "industry": company.industry_key or "",
        "country": country,
        "region": _mode(jobs.regions),
        "city": city,
        "country_city": ", ".join(part for part in (city, country) if part),
        "hiring_activity": hiring_band(activity),
        "hiring_activity_score": activity,
        "hiring_activity_label": f"{hiring_band(activity)} ({jobs.active_count} jobs)",
        "roles_hiring": "; ".join(jobs.titles[:5]),
        "seniority": _mode(jobs.seniorities) or "unknown",
        "seniorities": tuple(sorted(set(jobs.seniorities))),
        "website": company.website or "",
        "key_contact": contact.name if contact else "",
        "designation": contact.designation if contact else "",
        "company_phone": best_organizational_channel(channels, "phone"),
        "company_email": best_organizational_channel(channels, "email"),
        "lead_score": score.total_score,
        "lead_score_percentile": company.lead_score_percentile,
        "acquisition_band": company.acquisition_band or "",
        "graph_potential": graph_potential(score.factor_results_json),
        "status": company.outreach_status or "new",
        "last_updated": score.created_at.isoformat() if score.created_at else "",
        "size_band": company.size_band or "",
        "jurisdiction": company.jurisdiction or "unknown",
        "is_agency": company.is_agency,
        "nationality_restricted": jobs.nationality_restricted,
        "new_requirement": jobs.new_this_scan,
        "explanation": score.explanation_text,
    }


def _matches(row: dict[str, Any], filters: PipelineFilters) -> bool:
    if filters.hide_agencies and row["is_agency"]:
        return False
    if not filters.score_min <= int(row["lead_score"]) <= filters.score_max:
        return False
    if filters.countries and row["country"] not in filters.countries:
        return False
    if filters.regions and row["region"] not in filters.regions:
        return False
    if filters.cities and row["city"] not in filters.cities:
        return False
    if filters.industries and row["industry"] not in filters.industries:
        return False
    if filters.seniorities and not set(filters.seniorities) & set(row["seniorities"]):
        return False
    if filters.hiring_bands and row["hiring_activity"] not in filters.hiring_bands:
        return False
    if filters.size_bands and row["size_band"] not in filters.size_bands:
        return False
    if filters.outreach_statuses and row["status"] not in filters.outreach_statuses:
        return False
    if filters.acquisition_bands and row.get("acquisition_band") not in filters.acquisition_bands:
        return False
    if filters.preset == "uae_riyadh":
        country = str(row["country"]).casefold()
        city = str(row["city"]).casefold()
        if country not in UAE_NAMES and city != "riyadh":
            return False
    if filters.preset == "emiratisation":
        country = str(row["country"]).casefold()
        if country not in UAE_NAMES:
            return False
        if row["jurisdiction"] != "mainland":
            return False
        if not size_at_least_50(row["size_band"]):
            return False
        if not row["nationality_restricted"]:
            return False
    return True


def pipeline_rows(
    session_factory: sessionmaker[Session],
    filters: PipelineFilters | None = None,
) -> list[dict[str, Any]]:
    chosen = filters or PipelineFilters()
    with session_factory() as session:
        scan_id = _latest_scan(session)
        if scan_id is None:
            return []
        scores = list(
            session.scalars(select(LeadScore).where(LeadScore.scan_run_id == scan_id))
        )
        company_ids = [score.company_id for score in scores]
        companies = {
            company.id: company
            for company in session.scalars(
                select(Company).where(Company.id.in_(company_ids))
            )
        } if company_ids else {}
        jobs_by_company = _jobs_by_company(session, company_ids)
        contacts = _contacts_by_company(session, company_ids)
        channels = _channels_by_company(session, company_ids)
        rows: list[dict[str, Any]] = []
        for score in scores:
            company = companies.get(score.company_id)
            if company is None:
                continue
            rows.append(
                _pipeline_row(
                    company,
                    score,
                    _job_summary_from_jobs(jobs_by_company.get(company.id, [])),
                    contacts.get(company.id),
                    channels.get(company.id, []),
                )
            )
        rows.sort(
            key=lambda item: (
                BAND_ORDER.get(item.get("acquisition_band") or "", 9),
                -(float(item["lead_score_percentile"] or 0)),
                -int(item["lead_score"]),
                item["company"],
            )
        )
        return apply_pipeline_filters(rows, chosen)


def apply_pipeline_filters(
    rows: list[dict[str, Any]],
    filters: PipelineFilters | None = None,
) -> list[dict[str, Any]]:
    chosen = filters or PipelineFilters()
    matched = [row for row in rows if _matches(row, chosen)]
    if chosen.limit is not None:
        return matched[: chosen.limit]
    return matched


def filter_options_from_rows(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    def unique(key: str) -> list[str]:
        return sorted({str(row[key]) for row in rows if row.get(key)})

    seniorities = sorted(
        {item for row in rows for item in row.get("seniorities") or () if item}
    )
    return {
        "countries": unique("country"),
        "regions": unique("region"),
        "cities": unique("city"),
        "industries": unique("industry"),
        "seniorities": seniorities,
        "hiring_bands": ["high", "medium", "low"],
        "size_bands": unique("size_band"),
        "outreach_statuses": list(OUTREACH_STATUSES),
        "acquisition_bands": ["High", "Medium", "Low"],
    }


def filter_options(session_factory: sessionmaker[Session]) -> dict[str, list[str]]:
    return filter_options_from_rows(
        pipeline_rows(session_factory, PipelineFilters(hide_agencies=False))
    )


def set_outreach_status(
    session_factory: sessionmaker[Session],
    company_id: int,
    status: str,
) -> None:
    if status not in OUTREACH_STATUSES:
        raise ValueError(f"Unknown outreach status: {status}")
    with session_factory.begin() as session:
        company = session.get(Company, company_id)
        if company is None:
            raise ValueError(f"Company {company_id} not found")
        company.outreach_status = status


def _evidence_payload(item: Evidence, source: SourceRecord) -> dict[str, Any]:
    return {
        "quoted_text": redact_personal_contacts(item.quoted_text),
        "source_url": source.source_url or item.source_url,
        "observed_at": item.observed_at.isoformat() if item.observed_at else "",
        "basis": item.basis.upper(),
    }


def _evidence_by_company(
    session: Session,
    company_ids: Sequence[int],
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if not company_ids:
        return grouped
    jobs = list(session.scalars(select(Job).where(Job.company_id.in_(company_ids))))
    posts = list(session.scalars(select(Post).where(Post.company_id.in_(company_ids))))
    signals = list(session.scalars(select(Signal).where(Signal.company_id.in_(company_ids))))
    job_company = {job.source_key: job.company_id for job in jobs}
    post_company = {post.source_key: post.company_id for post in posts}
    signal_company = {signal.id: signal.company_id for signal in signals}
    source_keys = set(job_company) | set(post_company)
    sources: dict[int, SourceRecord] = {}
    if source_keys:
        sources = {
            source.id: source
            for source in session.scalars(
                select(SourceRecord).where(SourceRecord.source_key.in_(source_keys))
            )
        }
    conditions = []
    if signal_company:
        conditions.append(Evidence.signal_id.in_(list(signal_company)))
    if sources:
        conditions.append(Evidence.source_record_id.in_(list(sources)))
    if not conditions:
        return grouped
    items = list(
        session.scalars(
            select(Evidence).where(or_(*conditions)).order_by(Evidence.observed_at)
        )
    )
    missing = {
        item.source_record_id for item in items if item.source_record_id not in sources
    }
    if missing:
        sources.update(
            {
                source.id: source
                for source in session.scalars(
                    select(SourceRecord).where(SourceRecord.id.in_(missing))
                )
            }
        )
    for item in items:
        source = sources.get(item.source_record_id)
        if source is None:
            continue
        company_id = None
        if item.signal_id is not None:
            company_id = signal_company.get(item.signal_id)
        if company_id is None and source.source == "linkedin_job":
            company_id = job_company.get(source.source_key)
        if company_id is None and source.source == "linkedin_post":
            company_id = post_company.get(source.source_key)
        if company_id is None:
            continue
        grouped[company_id].append(_evidence_payload(item, source))
    return grouped


def company_detail(
    session_factory: sessionmaker[Session],
    company_id: int,
) -> dict[str, Any] | None:
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            return None
        scores = list(
            session.scalars(
                select(LeadScore)
                .where(LeadScore.company_id == company_id)
                .order_by(LeadScore.created_at)
            )
        )
        latest = scores[-1] if scores else None
        jobs = _job_summary(session, company_id)
        signals = [
            {
                "signal_type": item.signal_type,
                "strength": item.strength,
                "window": item.window,
                "basis": item.basis,
                "created_at": item.created_at.isoformat() if item.created_at else "",
            }
            for item in session.scalars(
                select(Signal)
                .where(Signal.company_id == company_id)
                .order_by(Signal.created_at)
            )
        ]
        evidence = _evidence_by_company(session, (company_id,)).get(company_id, [])
        missing = list((latest.explanation_json or {}).get("missing_inputs", [])) if latest else []
        factors = []
        if latest:
            for name, payload in (latest.factor_results_json or {}).items():
                factors.append(
                    {
                        "factor": name,
                        "score_0_100": float(payload.get("score_0_100") or 0),
                        "reasons": list(payload.get("reasons") or []),
                        "basis": payload.get("basis", "unknown"),
                        "missing_inputs": list(payload.get("missing_inputs") or []),
                    }
                )
            factors.sort(key=lambda item: item["score_0_100"], reverse=True)
        contact = _contact(session, company_id)
        channels = _channels_by_company(session, (company_id,)).get(company_id, [])
        people = _people_by_company(session, (company_id,)).get(company_id, [])
        return {
            "company_id": company.id,
            "company": company.canonical_name,
            "industry": company.industry_key or "",
            "country": company.hq_country or _mode(jobs.countries),
            "size_band": company.size_band or "",
            "jurisdiction": company.jurisdiction,
            "is_agency": company.is_agency,
            "outreach_status": company.outreach_status or "new",
            "priority": latest.priority if latest else "",
            "lead_score": latest.total_score if latest else None,
            "lead_score_percentile": company.lead_score_percentile,
            "acquisition_band": company.acquisition_band or "",
            "explanation": latest.explanation_text if latest else "",
            "factors": factors,
            "missing_inputs": missing,
            "evidence": evidence,
            "active_jobs": jobs.rows,
            "signals": signals,
            "score_history": [
                {
                    "scan_run_id": item.scan_run_id,
                    "total_score": item.total_score,
                    "priority": item.priority,
                    "created_at": item.created_at.isoformat() if item.created_at else "",
                }
                for item in scores
            ],
            "website": company.website or "",
            "key_contact": contact.name if contact else "",
            "designation": contact.designation if contact else "",
            "company_phone": best_organizational_channel(channels, "phone"),
            "company_email": best_organizational_channel(channels, "email"),
            "public_contacts": [_channel_payload(item) for item in channels],
            "people": [_person_payload(item) for item in people],
        }


def _average_score(scores: list[int]) -> int:
    return round(sum(scores) / len(scores)) if scores else 0


def _rank_groups(rows: list[dict[str, Any]], key: str) -> tuple[list[dict[str, Any]], int]:
    """Rank a company slice by High/Medium/Low mix. Missing keys are excluded."""

    groups: dict[str, Counter[str]] = defaultdict(Counter)
    excluded = 0
    for row in rows:
        name = str(row.get(key) or "")
        band = str(row.get("acquisition_band") or "")
        if not name or band not in BAND_ORDER:
            excluded += 1
            continue
        groups[name][band] += 1
    ranked: list[dict[str, Any]] = []
    for name, counts in groups.items():
        high = counts["High"]
        medium = counts["Medium"]
        low = counts["Low"]
        ranked.append(
            {
                key: name,
                "market_score": market_score(high, medium, low),
                "high": high,
                "medium": medium,
                "low": low,
                "company_count": high + medium + low,
            }
        )
    ranked.sort(
        key=lambda item: (-int(item["market_score"]), -int(item["high"]), str(item[key]))
    )
    return ranked, excluded


def industry_rankings(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rankings, excluded = _rank_groups(rows, "industry")
    return {"rankings": rankings, "excluded": excluded}


def region_rankings(rows: list[dict[str, Any]], industry: str) -> dict[str, Any]:
    scoped = [row for row in rows if row.get("industry") == industry]
    rankings, excluded = _rank_groups(scoped, "region")
    return {"rankings": rankings, "excluded": excluded, "industry": industry}


def focus_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Where outreach time should go in the current filtered company set."""

    industries = industry_rankings(rows)["rankings"]
    first_calls = [
        row
        for row in rows
        if row.get("acquisition_band") == "High"
        and row.get("status") in FIRST_CALL_STATUSES
    ]
    first_calls.sort(
        key=lambda row: (
            -(float(row.get("lead_score_percentile") or 0)),
            -int(row["lead_score"]),
            row["company"],
        )
    )
    totals = Counter(row.get("acquisition_band") or "" for row in rows)
    leader = industries[0] if industries else None
    return {
        "high_uncontacted": len(first_calls),
        "leading_industry": leader["industry"] if leader else "",
        "leading_score": leader["market_score"] if leader else 0,
        "top_industries": industries[:5],
        "first_calls": first_calls,
        "totals_by_band": {name: totals[name] for name in ("High", "Medium", "Low")},
    }


def potential_from_rows(
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    by_region_industry: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_country_city: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in rows:
        score = int(row["graph_potential"])
        region = str(row.get("region") or "")
        industry = str(row.get("industry") or "")
        if region and industry:
            by_region_industry[(region, industry)].append(score)
        country = str(row.get("country") or "")
        city = str(row.get("city") or "")
        if country and city:
            by_country_city[(country, city)].append(score)

    region_industry = [
        {
            "region": region,
            "industry": industry,
            "avg_score": _average_score(scores),
            "company_count": len(scores),
        }
        for (region, industry), scores in by_region_industry.items()
    ]
    region_industry.sort(
        key=lambda item: (-item["avg_score"], item["region"], item["industry"])
    )
    country_city = [
        {
            "country": country,
            "city": city,
            "avg_score": _average_score(scores),
            "company_count": len(scores),
        }
        for (country, city), scores in by_country_city.items()
    ]
    country_city.sort(
        key=lambda item: (item["country"], -item["avg_score"], item["city"])
    )
    top_city_by_country: list[dict[str, Any]] = []
    seen_countries: set[str] = set()
    for item in country_city:
        if item["country"] in seen_countries:
            continue
        seen_countries.add(item["country"])
        top_city_by_country.append(item)
    return {
        "by_region_industry": region_industry,
        "by_country_city": country_city,
        "top_city_by_country": top_city_by_country,
    }


def potential_breakdown(
    session_factory: sessionmaker[Session],
    filters: PipelineFilters | None = None,
) -> dict[str, list[dict[str, Any]]]:
    chosen = filters or PipelineFilters(hide_agencies=True)
    return potential_from_rows(pipeline_rows(session_factory, chosen))


def executive_summary(
    session_factory: sessionmaker[Session],
) -> dict[str, Any]:
    rows = pipeline_rows(session_factory, PipelineFilters(hide_agencies=True))
    changes = what_changed(session_factory)
    with session_factory() as session:
        identified = len(
            set(
                session.scalars(
                    select(DecisionMaker.company_id).where(
                        DecisionMaker.verified_at.is_not(None)
                    )
                )
            )
        )
        accelerating = set(
            session.scalars(
                select(Company.canonical_name)
                .join(Signal, Signal.company_id == Company.id)
                .where(Signal.signal_type == "hiring_acceleration")
            )
        )
    return {
        "totals_by_priority": {
            name: sum(1 for row in rows if row["priority"] == name)
            for name in ("HIGH", "MEDIUM", "LOW")
        },
        "scored_companies": len(rows),
        "new_prospects": list(changes.get("new_companies") or []),
        "accelerating_companies": sorted(name for name in accelerating if name),
        "top_industries": [
            name for name, _count in Counter(row["industry"] for row in rows if row["industry"]).most_common(5)
        ],
        "top_cities": [
            name for name, _count in Counter(row["city"] for row in rows if row["city"]).most_common(5)
        ],
        "decision_makers_identified": identified,
    }


def export_top50_rows(
    session_factory: sessionmaker[Session],
    filters: PipelineFilters | None = None,
) -> list[dict[str, str]]:
    chosen = filters or PipelineFilters(hide_agencies=True, limit=50)
    if chosen.limit is None:
        chosen = PipelineFilters(
            countries=chosen.countries,
            regions=chosen.regions,
            cities=chosen.cities,
            industries=chosen.industries,
            seniorities=chosen.seniorities,
            hiring_bands=chosen.hiring_bands,
            size_bands=chosen.size_bands,
            score_min=chosen.score_min,
            score_max=chosen.score_max,
            outreach_statuses=chosen.outreach_statuses,
            hide_agencies=chosen.hide_agencies,
            preset=chosen.preset,
            limit=50,
            acquisition_bands=chosen.acquisition_bands,
        )
    rows = pipeline_rows(session_factory, chosen)
    with session_factory() as session:
        evidence_map = _evidence_by_company(
            session, [row["company_id"] for row in rows]
        )
    exported: list[dict[str, str]] = []
    for row in rows:
        evidence = "; ".join(
            f"{item['basis']} {item['quoted_text'][:120]}"
            for item in evidence_map.get(row["company_id"], [])
        )
        safe_evidence = redact_personal_contacts(evidence)
        exported.append(
            {
                "priority": row["priority"],
                "company": row["company"],
                "website": row.get("website") or "",
                "industry": row["industry"],
                "country_city": row["country_city"],
                "hiring_activity": row["hiring_activity_label"],
                "roles_hiring": row["roles_hiring"],
                "seniority": row["seniority"],
                "key_contact": row["key_contact"],
                "designation": row["designation"],
                "company_phone": row.get("company_phone") or "",
                "company_email": row.get("company_email") or "",
                "lead_score": str(row["lead_score"]),
                "status": row["status"],
                "last_updated": row["last_updated"],
                "evidence_summary": safe_evidence,
            }
        )
    return exported


def export_top50_csv(
    session_factory: sessionmaker[Session],
    path=None,
    filters: PipelineFilters | None = None,
) -> str:
    rows = export_top50_rows(session_factory, filters)
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[*TABLE_COLUMNS, "evidence_summary"],
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(rows)
    text = output.getvalue()
    if path is not None:
        path.write_text(text, encoding="utf-8-sig")
    return text
