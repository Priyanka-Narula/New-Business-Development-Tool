"""Deterministic dashboard queries. No live LinkedIn access."""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.db.models import (
    Company,
    DecisionMaker,
    Evidence,
    Job,
    LeadScore,
    Post,
    Signal,
    SourceRecord,
)
from aambridge.privacy import privacy_safe, redact_personal_contacts
from aambridge.signals.detect import _scan_ids, _seniority, what_changed

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
    "industry",
    "country_city",
    "hiring_activity",
    "roles_hiring",
    "seniority",
    "key_contact",
    "designation",
    "lead_score",
    "status",
    "last_updated",
)
UAE_NAMES = {"united arab emirates", "uae"}
PRIORITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}


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


@dataclass
class _JobSummary:
    titles: list[str] = field(default_factory=list)
    seniorities: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    active_count: int = 0
    nationality_restricted: bool = False
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


def _mode(values: list[str]) -> str:
    cleaned = [item for item in values if item]
    if not cleaned:
        return ""
    return Counter(cleaned).most_common(1)[0][0]


def _contact(session: Session, company_id: int) -> DecisionMaker | None:
    return session.scalar(
        select(DecisionMaker)
        .where(
            DecisionMaker.company_id == company_id,
            DecisionMaker.verified_at.is_not(None),
        )
        .order_by(DecisionMaker.relevance_rank, DecisionMaker.id)
    )


def _job_summary(session: Session, company_id: int) -> _JobSummary:
    summary = _JobSummary()
    for job in session.scalars(select(Job).where(Job.company_id == company_id)):
        raw = {}
        if job.observations:
            latest = max(job.observations, key=lambda item: item.observed_at)
            raw = latest.raw_listing_json or {}
        level = _seniority(" ".join(filter(None, [job.title, raw.get("job_description", "")])))
        if job.is_active:
            summary.active_count += 1
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


def _pipeline_row(
    session: Session,
    company: Company,
    score: LeadScore,
    jobs: _JobSummary,
) -> dict[str, Any]:
    contact = _contact(session, company.id)
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
        "key_contact": contact.name if contact else "",
        "designation": contact.designation if contact else "",
        "lead_score": score.total_score,
        "status": company.outreach_status or "new",
        "last_updated": score.created_at.isoformat() if score.created_at else "",
        "size_band": company.size_band or "",
        "jurisdiction": company.jurisdiction or "unknown",
        "is_agency": company.is_agency,
        "nationality_restricted": jobs.nationality_restricted,
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
        rows: list[dict[str, Any]] = []
        for score in session.scalars(
            select(LeadScore).where(LeadScore.scan_run_id == scan_id)
        ):
            company = session.get(Company, score.company_id)
            if company is None:
                continue
            row = _pipeline_row(session, company, score, _job_summary(session, company.id))
            if _matches(row, chosen):
                rows.append(row)
        rows.sort(
            key=lambda item: (
                PRIORITY_ORDER.get(item["priority"], 9),
                -int(item["lead_score"]),
                item["company"],
            )
        )
        if chosen.limit is not None:
            return rows[: chosen.limit]
        return rows


def filter_options(session_factory: sessionmaker[Session]) -> dict[str, list[str]]:
    rows = pipeline_rows(session_factory, PipelineFilters(hide_agencies=False))
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
    }


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
        evidence = []
        for item in session.scalars(select(Evidence).order_by(Evidence.observed_at)):
            source = session.get(SourceRecord, item.source_record_id)
            if source is None:
                continue
            linked = False
            if item.signal_id is not None:
                signal = session.get(Signal, item.signal_id)
                linked = signal is not None and signal.company_id == company_id
            if not linked and source.source == "linkedin_job":
                job = session.scalar(
                    select(Job).where(Job.source_key == source.source_key)
                )
                linked = job is not None and job.company_id == company_id
            if not linked and source.source == "linkedin_post":
                post = session.scalar(
                    select(Post).where(Post.source_key == source.source_key)
                )
                linked = post is not None and post.company_id == company_id
            if not linked:
                continue
            evidence.append(
                {
                    "quoted_text": redact_personal_contacts(item.quoted_text),
                    "source_url": source.source_url or item.source_url,
                    "observed_at": item.observed_at.isoformat() if item.observed_at else "",
                    "basis": item.basis.upper(),
                }
            )
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
            "key_contact": contact.name if contact else "",
            "designation": contact.designation if contact else "",
        }


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
        accelerating = {
            company.canonical_name
            for signal in session.scalars(
                select(Signal).where(Signal.signal_type == "hiring_acceleration")
            )
            if (company := session.get(Company, signal.company_id)) is not None
        }
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
        )
    exported: list[dict[str, str]] = []
    for row in pipeline_rows(session_factory, chosen):
        detail = company_detail(session_factory, row["company_id"]) or {}
        evidence = "; ".join(
            f"{item['basis']} {item['quoted_text'][:120]}"
            for item in detail.get("evidence") or []
        )
        exported.append(
            privacy_safe(
                {
                    "priority": row["priority"],
                    "company": row["company"],
                    "industry": row["industry"],
                    "country_city": row["country_city"],
                    "hiring_activity": row["hiring_activity_label"],
                    "roles_hiring": row["roles_hiring"],
                    "seniority": row["seniority"],
                    "key_contact": row["key_contact"],
                    "designation": row["designation"],
                    "lead_score": str(row["lead_score"]),
                    "status": row["status"],
                    "last_updated": row["last_updated"],
                    "evidence_summary": evidence,
                }
            )
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
