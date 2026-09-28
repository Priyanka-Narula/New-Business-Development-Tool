"""Manual, assisted decision-maker capture. No emails or phone numbers."""

from __future__ import annotations

import csv
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.db.models import Company, CompanyAlias, DecisionMaker, LeadScore
from aambridge.extract.taxonomies import load_yaml
from aambridge.privacy import privacy_safe, redact_personal_contacts
from aambridge.resolve.companies import normalise_company_name
from aambridge.signals.detect import _scan_ids

log = logging.getLogger(__name__)
ALLOWED_COLUMNS = {
    "company_id",
    "company_name",
    "name",
    "designation",
    "department",
    "profile_url",
    "source",
    "verified_by",
    "confidence",
    "notes",
}
FORBIDDEN_COLUMNS = {
    "email",
    "contact_email",
    "recruiter_email",
    "phone",
    "phone_number",
    "mobile",
    "telephone",
}


def _config() -> dict[str, Any]:
    return load_yaml("taxonomies/designations.yaml")


def _normalise_title(value: str) -> str:
    text = re.sub(r"[^\w\u0600-\u06ff]+", " ", (value or "").casefold())
    return re.sub(r"\s+", " ", text).strip()


def rank_designation(designation: str) -> int:
    """Map a title to partnership-control rank. Lower is more relevant."""

    config = _config()
    normalised = _normalise_title(designation)
    if not normalised:
        return int(config["unmatched_rank"])
    matches: list[tuple[int, int]] = []
    for item in config["designations"]:
        aliases = [str(item["label"]), *item.get("aliases", [])]
        for alias in aliases:
            alias_norm = _normalise_title(str(alias))
            if alias_norm and alias_norm in normalised:
                matches.append((len(alias_norm), int(item["rank"])))
    if not matches:
        return int(config["unmatched_rank"])
    return max(matches, key=lambda item: item[0])[1]


def suggested_task(designation: str | None = None) -> str:
    if designation:
        return f"Find: {designation} or equivalent"
    return str(_config()["default_task"])


def _latest_scores(session: Session) -> list[LeadScore]:
    scans = _scan_ids(session)
    if not scans:
        return []
    return list(
        session.scalars(
            select(LeadScore).where(LeadScore.scan_run_id == scans[-1])
        )
    )


def missing_contact_tasks(
    session_factory: sessionmaker[Session],
) -> list[dict[str, Any]]:
    """HIGH/MEDIUM companies that still need a verified decision-maker."""

    with session_factory() as session:
        tasks: list[dict[str, Any]] = []
        for score in _latest_scores(session):
            if score.priority not in {"HIGH", "MEDIUM"}:
                continue
            verified = session.scalar(
                select(DecisionMaker).where(
                    DecisionMaker.company_id == score.company_id,
                    DecisionMaker.verified_at.is_not(None),
                )
            )
            if verified is not None:
                continue
            company = session.get(Company, score.company_id)
            if company is None or company.is_agency:
                continue
            tasks.append(
                {
                    "company_id": company.id,
                    "company": company.canonical_name,
                    "priority": score.priority,
                    "lead_score": score.total_score,
                    "task": suggested_task(),
                }
            )
        return sorted(
            tasks,
            key=lambda item: (0 if item["priority"] == "HIGH" else 1, -item["lead_score"]),
        )


def add_decision_maker(
    session_factory: sessionmaker[Session],
    *,
    company_id: int,
    name: str,
    designation: str,
    source: str,
    department: str = "",
    profile_url: str = "",
    verified_by: str = "",
    confidence: float | None = None,
    notes: str = "",
) -> int:
    safe_name = redact_personal_contacts(name).strip()
    safe_designation = redact_personal_contacts(designation).strip()
    safe_source = redact_personal_contacts(source).strip()
    safe_department = redact_personal_contacts(department).strip()
    safe_profile_url = redact_personal_contacts(profile_url).strip()
    safe_verified_by = redact_personal_contacts(verified_by).strip()
    safe_notes = redact_personal_contacts(notes).strip()
    if not safe_name or not safe_designation or not safe_source:
        raise ValueError("name, designation, and source are required")
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")

    with session_factory.begin() as session:
        company = session.get(Company, company_id)
        if company is None:
            raise ValueError(f"Company {company_id} not found")
        existing = session.scalar(
            select(DecisionMaker).where(
                DecisionMaker.company_id == company_id,
                DecisionMaker.name == safe_name,
                DecisionMaker.designation == safe_designation,
            )
        )
        now = datetime.now(UTC) if safe_verified_by else None
        values = {
            "department": safe_department or None,
            "relevance_rank": rank_designation(safe_designation),
            "profile_url": safe_profile_url or None,
            "source": safe_source,
            "verified_by": safe_verified_by or None,
            "verified_at": now,
            "confidence": confidence,
            "notes": safe_notes or None,
        }
        if existing is None:
            record = DecisionMaker(
                company_id=company_id,
                name=safe_name,
                designation=safe_designation,
                **values,
            )
            session.add(record)
            session.flush()
            return record.id
        for key, value in values.items():
            setattr(existing, key, value)
        return existing.id


def _company_id_from_row(session: Session, row: dict[str, str]) -> int | None:
    if row.get("company_id"):
        return int(row["company_id"])
    name = row.get("company_name", "")
    alias = session.scalar(
        select(CompanyAlias).where(
            CompanyAlias.alias_normalised == normalise_company_name(name)
        )
    )
    return alias.company_id if alias is not None else None


def import_decision_makers(
    session_factory: sessionmaker[Session],
    path: Path,
) -> dict[str, int]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [name or "" for name in (reader.fieldnames or [])]
        forbidden = [
            name for name in fieldnames if name.casefold() in FORBIDDEN_COLUMNS
        ]
        if forbidden:
            log.warning(
                "Ignoring personal-contact columns in %s: %s",
                path,
                ", ".join(forbidden),
            )
        rows = list(reader)

    stats = {"imported": 0, "skipped": 0}
    for row in rows:
        safe_row = {
            key: value
            for key, value in privacy_safe(row).items()
            if key.casefold() in ALLOWED_COLUMNS
        }
        try:
            with session_factory() as lookup:
                company_id = _company_id_from_row(lookup, safe_row)
            if company_id is None:
                stats["skipped"] += 1
                continue
            confidence = safe_row.get("confidence") or None
            add_decision_maker(
                session_factory,
                company_id=company_id,
                name=str(safe_row.get("name") or ""),
                designation=str(safe_row.get("designation") or ""),
                source=str(safe_row.get("source") or "manual"),
                department=str(safe_row.get("department") or ""),
                profile_url=str(safe_row.get("profile_url") or ""),
                verified_by=str(safe_row.get("verified_by") or ""),
                confidence=float(confidence) if confidence not in {None, ""} else None,
                notes=str(safe_row.get("notes") or ""),
            )
            stats["imported"] += 1
        except (TypeError, ValueError) as exc:
            log.warning("Skipped decision-maker row: %s", exc)
            stats["skipped"] += 1
    return stats


def list_decision_makers(
    session_factory: sessionmaker[Session],
    *,
    company_id: int | None = None,
) -> list[dict[str, Any]]:
    with session_factory() as session:
        statement = select(DecisionMaker).order_by(
            DecisionMaker.relevance_rank, DecisionMaker.id
        )
        if company_id is not None:
            statement = statement.where(DecisionMaker.company_id == company_id)
        rows = []
        for item in session.scalars(statement):
            company = session.get(Company, item.company_id)
            rows.append(
                {
                    "id": item.id,
                    "company_id": item.company_id,
                    "company": company.canonical_name if company else "",
                    "name": item.name,
                    "designation": item.designation,
                    "department": item.department,
                    "relevance_rank": item.relevance_rank,
                    "profile_url": item.profile_url,
                    "source": item.source,
                    "verified_by": item.verified_by,
                    "verified_at": (
                        item.verified_at.isoformat() if item.verified_at else None
                    ),
                    "confidence": item.confidence,
                    "notes": item.notes,
                }
            )
        return rows
