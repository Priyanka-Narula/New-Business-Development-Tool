"""Deterministic company signal and change detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from aambridge.db.models import (
    Company,
    Evidence,
    Job,
    JobObservation,
    LeadScore,
    Post,
    ScanRun,
    Signal,
    SourceRecord,
)
from aambridge.extract.taxonomies import load_yaml, target_seniority_keys


@dataclass(frozen=True)
class CompanyMetrics:
    active_jobs: int
    new_jobs: int
    reposted_jobs: int
    target_jobs: int
    senior_jobs: int
    locations: frozenset[str]


def _seniority(text: str) -> str:
    levels = load_yaml("taxonomies/seniority.yaml")["levels"]
    matches: list[tuple[int, int, str]] = []
    for key, details in levels.items():
        for keyword in details.get("keywords", []):
            match = re.search(
                rf"(?<!\w){re.escape(str(keyword))}(?!\w)",
                text,
                re.IGNORECASE,
            )
            if match:
                matches.append(
                    (int(details.get("order", 99)), len(match.group(0)), key)
                )
    if not matches:
        return "unknown"
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def _senior(level: str) -> bool:
    return level in {"director", "vp", "c_level"}


def _scan_ids(session: Session) -> list[int]:
    return list(
        session.scalars(
            select(ScanRun.id)
            .where(ScanRun.status == "completed")
            .order_by(ScanRun.started_at)
        )
    )


def _job_observation(
    job: Job,
    scan_run_id: int,
) -> JobObservation | None:
    return next(
        (item for item in job.observations if item.scan_run_id == scan_run_id),
        None,
    )


def company_metrics(
    session: Session,
    company: Company,
    scan_run_id: int,
) -> CompanyMetrics:
    jobs = list(
        session.scalars(select(Job).where(Job.company_id == company.id))
    )
    observed: list[tuple[Job, JobObservation]] = []
    for job in jobs:
        observation = _job_observation(job, scan_run_id)
        if observation is not None:
            observed.append((job, observation))
    target = target_seniority_keys()
    levels = [
        _seniority(
            f"{job.title} {observation.raw_listing_json.get('seniority_hint', '')} "
            f"{observation.raw_listing_json.get('description', '')}"
        )
        for job, observation in observed
    ]
    return CompanyMetrics(
        active_jobs=len(observed),
        new_jobs=sum(1 for job, _observation in observed if len(job.observations) == 1),
        reposted_jobs=sum(
            1 for _job, observation in observed if observation.is_reposted
        ),
        target_jobs=sum(1 for level in levels if level in target),
        senior_jobs=sum(1 for level in levels if _senior(level)),
        locations=frozenset(
            job.city or job.location_raw for job, _observation in observed
        ),
    )


def _source_for(
    session: Session,
    *,
    scan_run_id: int,
    source: str,
    source_key: str,
) -> SourceRecord | None:
    return session.scalar(
        select(SourceRecord).where(
            SourceRecord.scan_run_id == scan_run_id,
            SourceRecord.source == source,
            SourceRecord.source_key == source_key,
        )
    )


def _company_job_evidence(
    session: Session,
    company_id: int,
    scan_run_id: int,
) -> list[tuple[SourceRecord, str, str]]:
    evidence: list[tuple[SourceRecord, str, str]] = []
    for job in session.scalars(select(Job).where(Job.company_id == company_id)):
        if _job_observation(job, scan_run_id) is None:
            continue
        source = _source_for(
            session,
            scan_run_id=scan_run_id,
            source="linkedin_job",
            source_key=job.source_key,
        )
        if source is not None:
            evidence.append((source, job.title, "fact"))
    return evidence


def _add_signal(
    session: Session,
    *,
    company_id: int,
    scan_run_id: int,
    signal_type: str,
    strength: float,
    window: str,
    basis: str,
    evidence: list[tuple[SourceRecord, str, str]],
) -> Signal:
    signal = Signal(
        company_id=company_id,
        scan_run_id=scan_run_id,
        signal_type=signal_type,
        strength=max(0.0, min(1.0, strength)),
        window=window,
        basis=basis,
        created_at=datetime.now(UTC),
    )
    session.add(signal)
    session.flush()
    for source, quote, evidence_basis in evidence:
        session.add(
            Evidence(
                signal_id=signal.id,
                source_record_id=source.id,
                quoted_text=quote,
                source_url=source.source_url,
                observed_at=source.observed_at,
                basis=evidence_basis,
            )
        )
    return signal


def _close_missing_jobs(
    session: Session,
    scan_ids: list[int],
    missing_scans: int,
) -> int:
    if len(scan_ids) < missing_scans:
        return 0
    recent = set(scan_ids[-missing_scans:])
    closed = 0
    for job in session.scalars(select(Job).where(Job.is_active.is_(True))):
        observed = {item.scan_run_id for item in job.observations}
        if not observed.intersection(recent):
            job.is_active = False
            closed += 1
    return closed


def detect_signals(
    session_factory: sessionmaker[Session],
    *,
    scan_run_id: int | None = None,
) -> dict[str, int]:
    thresholds = load_yaml("config/scoring.yaml")["signals"]
    stats = {"companies": 0, "signals": 0, "closed_jobs": 0}
    with session_factory.begin() as session:
        scans = _scan_ids(session)
        if not scans:
            return stats
        current_id = scan_run_id or scans[-1]
        if current_id not in scans:
            raise ValueError(f"Completed scan {current_id} not found")
        current_index = scans.index(current_id)
        previous_id = scans[current_index - 1] if current_index > 0 else None

        old_signal_ids = list(
            session.scalars(
                select(Signal.id).where(Signal.scan_run_id == current_id)
            )
        )
        if old_signal_ids:
            session.execute(delete(Evidence).where(Evidence.signal_id.in_(old_signal_ids)))
            session.execute(delete(Signal).where(Signal.id.in_(old_signal_ids)))

        stats["closed_jobs"] = _close_missing_jobs(
            session,
            scans[: current_index + 1],
            int(thresholds["closed_after_missing_scans"]),
        )
        companies = list(
            session.scalars(select(Company).where(Company.is_agency.is_(False)))
        )
        for company in companies:
            current = company_metrics(session, company, current_id)
            previous = (
                company_metrics(session, company, previous_id)
                if previous_id is not None
                else CompanyMetrics(0, 0, 0, 0, 0, frozenset())
            )
            if not current.active_jobs and not current.new_jobs:
                current_posts = list(
                    session.scalars(
                        select(Post).where(Post.company_id == company.id)
                    )
                )
                if not any(
                    _source_for(
                        session,
                        scan_run_id=current_id,
                        source="linkedin_post",
                        source_key=post.source_key,
                    )
                    for post in current_posts
                ):
                    continue
            stats["companies"] += 1
            delta = current.target_jobs - previous.target_jobs
            percent = (
                (delta / previous.target_jobs) * 100
                if previous.target_jobs
                else (100.0 if current.target_jobs else 0.0)
            )
            if (
                delta >= int(thresholds["acceleration_min_jobs"])
                and percent >= float(thresholds["acceleration_min_percent"])
            ):
                signal = _add_signal(
                    session,
                    company_id=company.id,
                    scan_run_id=current_id,
                    signal_type="hiring_acceleration",
                    strength=min(1.0, 0.5 + delta / 20),
                    window="previous_scan",
                    basis="fact",
                    evidence=_company_job_evidence(
                        session, company.id, current_id
                    ),
                )
                stats["signals"] += bool(signal)
            elif (
                previous.target_jobs
                and -delta >= int(thresholds["acceleration_min_jobs"])
                and (-delta / previous.target_jobs) * 100
                >= float(thresholds["slowdown_min_percent"])
            ):
                _add_signal(
                    session,
                    company_id=company.id,
                    scan_run_id=current_id,
                    signal_type="hiring_slowdown",
                    strength=min(1.0, -delta / max(previous.target_jobs, 1)),
                    window="previous_scan",
                    basis="fact",
                    evidence=_company_job_evidence(
                        session, company.id, previous_id
                    ),
                )
                stats["signals"] += 1

            jobs = list(
                session.scalars(select(Job).where(Job.company_id == company.id))
            )
            current_jobs = [
                (job, _job_observation(job, current_id))
                for job in jobs
                if _job_observation(job, current_id) is not None
            ]
            senior_evidence: list[tuple[SourceRecord, str, str]] = []
            repeated_evidence: list[tuple[SourceRecord, str, str]] = []
            localisation_evidence: list[tuple[SourceRecord, str, str]] = []
            for job, observation in current_jobs:
                raw = observation.raw_listing_json
                source = _source_for(
                    session,
                    scan_run_id=current_id,
                    source="linkedin_job",
                    source_key=job.source_key,
                )
                level = _seniority(
                    f"{job.title} {raw.get('seniority_hint', '')} "
                    f"{raw.get('description', '')}"
                )
                if (
                    source is not None
                    and len(job.observations) == 1
                    and _senior(level)
                ):
                    senior_evidence.append((source, job.title, "fact"))
                first_seen = job.first_seen_at
                if first_seen.tzinfo is None:
                    first_seen = first_seen.replace(tzinfo=UTC)
                age_days = (datetime.now(UTC) - first_seen).days
                if source is not None and (
                    observation.is_reposted
                    or age_days >= int(thresholds["repeated_vacancy_days"])
                ):
                    repeated_evidence.append((source, job.title, "fact"))
                restriction = str(
                    raw.get("nationality_restriction_evidence") or ""
                )
                if source is not None and restriction:
                    localisation_evidence.append((source, restriction, "fact"))

            if senior_evidence:
                _add_signal(
                    session,
                    company_id=company.id,
                    scan_run_id=current_id,
                    signal_type="new_senior_vacancy",
                    strength=min(1.0, len(senior_evidence) / 3),
                    window="current_scan",
                    basis="fact",
                    evidence=senior_evidence,
                )
                stats["signals"] += 1
            if repeated_evidence:
                _add_signal(
                    session,
                    company_id=company.id,
                    scan_run_id=current_id,
                    signal_type="repeated_vacancy",
                    strength=min(1.0, len(repeated_evidence) / 3),
                    window=f"{thresholds['repeated_vacancy_days']}d",
                    basis="fact",
                    evidence=repeated_evidence,
                )
                stats["signals"] += 1
            new_locations = current.locations - previous.locations
            if previous_id is not None and new_locations:
                evidence: list[tuple[SourceRecord, str, str]] = []
                for job, _observation in current_jobs:
                    if (job.city or job.location_raw) in new_locations:
                        source = _source_for(
                            session,
                            scan_run_id=current_id,
                            source="linkedin_job",
                            source_key=job.source_key,
                        )
                        if source:
                            evidence.append(
                                (source, job.city or job.location_raw, "fact")
                            )
                _add_signal(
                    session,
                    company_id=company.id,
                    scan_run_id=current_id,
                    signal_type="new_location",
                    strength=min(1.0, len(new_locations) / 2),
                    window="all_prior_scans",
                    basis="fact",
                    evidence=evidence,
                )
                stats["signals"] += 1
            if localisation_evidence:
                _add_signal(
                    session,
                    company_id=company.id,
                    scan_run_id=current_id,
                    signal_type="localisation_pressure",
                    strength=min(1.0, 0.5 + len(localisation_evidence) / 10),
                    window="current_scan",
                    basis="inference",
                    evidence=localisation_evidence,
                )
                stats["signals"] += 1

            expansion_evidence: list[tuple[SourceRecord, str, str]] = []
            for post in session.scalars(
                select(Post).where(
                    Post.company_id == company.id,
                    Post.post_category.in_(["expansion", "project_award"]),
                )
            ):
                source = _source_for(
                    session,
                    scan_run_id=current_id,
                    source="linkedin_post",
                    source_key=post.source_key,
                )
                if source:
                    expansion_evidence.append(
                        (source, post.text[:300], "fact")
                    )
            if expansion_evidence:
                _add_signal(
                    session,
                    company_id=company.id,
                    scan_run_id=current_id,
                    signal_type="expansion",
                    strength=min(1.0, 0.5 + len(expansion_evidence) / 5),
                    window="current_scan",
                    basis="fact",
                    evidence=expansion_evidence,
                )
                stats["signals"] += 1
    return stats


def what_changed(
    session_factory: sessionmaker[Session],
    *,
    scan_run_id: int | None = None,
) -> dict[str, Any]:
    with session_factory() as session:
        scans = _scan_ids(session)
        if not scans:
            return {"new_companies": [], "new_signals": [], "score_changes": []}
        current = scan_run_id or scans[-1]
        previous = scans[scans.index(current) - 1] if scans.index(current) > 0 else None
        signals = list(
            session.scalars(select(Signal).where(Signal.scan_run_id == current))
        )
        jobs = session.scalars(select(Job).options(selectinload(Job.observations)))
        new_company_ids = {
            job.company_id
            for job in jobs
            if job.company_id
            and _job_observation(job, current) is not None
            and len(job.observations) == 1
        }
        score_changes: list[dict[str, Any]] = []
        if previous is not None:
            current_scores = {
                item.company_id: item.total_score
                for item in session.scalars(
                    select(LeadScore).where(LeadScore.scan_run_id == current)
                )
            }
            previous_scores = {
                item.company_id: item.total_score
                for item in session.scalars(
                    select(LeadScore).where(LeadScore.scan_run_id == previous)
                )
            }
            score_changes = [
                {
                    "company_id": company_id,
                    "previous": previous_scores.get(company_id, 0),
                    "current": score,
                    "change": score - previous_scores.get(company_id, 0),
                }
                for company_id, score in current_scores.items()
                if score != previous_scores.get(company_id, 0)
            ]
        companies = {
            company.id: company.canonical_name
            for company in session.scalars(
                select(Company).where(Company.id.in_(sorted(new_company_ids)))
            )
        } if new_company_ids else {}
        return {
            "new_companies": [
                companies[company_id]
                for company_id in sorted(new_company_ids)
                if company_id in companies
            ],
            "new_signals": [
                {
                    "company_id": signal.company_id,
                    "signal_type": signal.signal_type,
                    "strength": signal.strength,
                    "basis": signal.basis,
                }
                for signal in signals
            ],
            "score_changes": sorted(
                score_changes, key=lambda item: abs(item["change"]), reverse=True
            ),
        }
