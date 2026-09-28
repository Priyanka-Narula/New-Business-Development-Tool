"""Deterministic company resolution with a fuzzy-review queue."""

from __future__ import annotations

import csv
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rapidfuzz.fuzz import ratio
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.db.models import (
    Company,
    CompanyAlias,
    CompanyGroup,
    CompanyMergeReview,
    Extraction,
    Job,
    Post,
    SourceRecord,
)
from aambridge.extract.taxonomies import load_yaml
from aambridge.sources.linkedin_jobs import detect_agency


def canonical_linkedin_url(value: str) -> str:
    return (value or "").split("?")[0].rstrip("/").casefold()


def normalise_company_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = re.sub(r"[-_/.,()]+", " ", text)
    suffixes = load_yaml("taxonomies/legal_suffixes.yaml")["suffixes"]
    suffix_patterns = sorted(
        (
            re.escape(
                re.sub(
                    r"\s+",
                    " ",
                    re.sub(r"[-_/.,()]+", " ", str(suffix).casefold()),
                ).strip()
            )
            for suffix in suffixes
        ),
        key=len,
        reverse=True,
    )
    text = re.sub(r"\s+", " ", text).strip()
    changed = True
    while changed:
        changed = False
        for suffix in suffix_patterns:
            updated = re.sub(rf"(?:\s+|^){suffix}\s*$", "", text).strip()
            if updated != text:
                text = updated
                changed = True
    text = re.sub(r"^(?:شركة|مؤسسة)\s+", "", text)
    text = re.sub(r"\b(?:al|el)\s+([\w\u0600-\u06ff]+)", r"al\1", text)
    return re.sub(r"[^\w\u0600-\u06ff]+", "", text)


def _thresholds() -> tuple[float, float]:
    config = load_yaml("config/resolution.yaml")
    return float(config["fuzzy_auto_accept"]), float(config["fuzzy_review"])


def _add_alias(
    session: Session,
    company: Company,
    alias: str,
    source: str,
) -> None:
    normalised = normalise_company_name(alias)
    if not normalised:
        return
    existing = session.scalar(
        select(CompanyAlias).where(
            CompanyAlias.alias_normalised == normalised,
            CompanyAlias.source == source,
        )
    )
    if existing is None:
        session.add(
            CompanyAlias(
                company_id=company.id,
                alias=alias,
                alias_normalised=normalised,
                source=source,
            )
        )


def _new_company(
    session: Session,
    *,
    name: str,
    linkedin_url: str,
    country: str,
    industry: str,
    is_agency: bool,
    source: str,
) -> Company:
    company = Company(
        canonical_name=name,
        canonical_name_ar=None,
        linkedin_url=linkedin_url or None,
        website=None,
        industry_key=industry or None,
        hq_country=country or None,
        size_band=None,
        jurisdiction="unknown",
        is_agency=is_agency,
        group_id=None,
    )
    session.add(company)
    session.flush()
    _add_alias(session, company, name, source)
    return company


def resolve_company(
    session: Session,
    *,
    name: str,
    linkedin_url: str = "",
    country: str = "",
    industry: str = "",
    is_agency: bool = False,
    source: str = "linkedin",
) -> Company | None:
    if not name.strip() and not linkedin_url.strip():
        return None
    canonical_url = canonical_linkedin_url(linkedin_url)
    if canonical_url:
        companies = list(
            session.scalars(select(Company).where(Company.linkedin_url.is_not(None)))
        )
        for company in companies:
            if canonical_linkedin_url(company.linkedin_url or "") == canonical_url:
                company.is_agency = company.is_agency or is_agency
                _add_alias(session, company, name, source)
                return company

    normalised = normalise_company_name(name)
    alias = session.scalar(
        select(CompanyAlias).where(CompanyAlias.alias_normalised == normalised)
    )
    if alias is not None:
        company = session.get(Company, alias.company_id)
        if company is not None:
            company.is_agency = company.is_agency or is_agency
        return company

    companies = list(session.scalars(select(Company)))
    for company in companies:
        if normalise_company_name(company.canonical_name) == normalised:
            _add_alias(session, company, name, source)
            company.is_agency = company.is_agency or is_agency
            return company

    same_country = [
        company
        for company in companies
        if country
        and company.hq_country
        and company.hq_country.casefold() == country.casefold()
    ]
    scored = sorted(
        (
            (ratio(normalised, normalise_company_name(company.canonical_name)), company)
            for company in same_country
        ),
        reverse=True,
        key=lambda item: item[0],
    )
    auto, review = _thresholds()
    if scored and scored[0][0] >= auto:
        company = scored[0][1]
        _add_alias(session, company, name, source)
        company.is_agency = company.is_agency or is_agency
        return company

    company = _new_company(
        session,
        name=name or linkedin_url,
        linkedin_url=canonical_url,
        country=country,
        industry=industry,
        is_agency=is_agency,
        source=source,
    )
    if scored and scored[0][0] >= review:
        session.add(
            CompanyMergeReview(
                source_company_id=company.id,
                candidate_company_id=scored[0][1].id,
                similarity=scored[0][0],
                status="pending",
                created_at=datetime.now(UTC),
                reviewed_at=None,
            )
        )
    return company


def _field(payload: dict[str, Any], name: str) -> Any:
    value = payload.get(name, {})
    return value.get("value") if isinstance(value, dict) else None


def resolve_all(session_factory: sessionmaker[Session]) -> dict[str, int]:
    stats = {"jobs": 0, "posts": 0, "companies_created": 0}
    with session_factory.begin() as session:
        before = len(list(session.scalars(select(Company))))
        jobs = list(session.scalars(select(Job).order_by(Job.id)))
        for job in jobs:
            observation = job.observations[-1] if job.observations else None
            raw = observation.raw_listing_json if observation else {}
            is_agency = bool(raw.get("posted_by_agency"))
            if not is_agency:
                is_agency, _evidence = detect_agency(job.company_name_raw)
            company = resolve_company(
                session,
                name=job.company_name_raw,
                linkedin_url=job.company_linkedin_url or "",
                country=job.country or "",
                industry=str(raw.get("industry") or ""),
                is_agency=is_agency,
                source="linkedin_job",
            )
            if company is not None:
                job.company_id = company.id
                stats["jobs"] += 1

        posts = list(session.scalars(select(Post).order_by(Post.id)))
        for post in posts:
            name = post.author_name or ""
            url = post.author_url or ""
            country = ""
            industry = ""
            if post.author_type != "company":
                source_record = session.scalar(
                    select(SourceRecord)
                    .where(
                        SourceRecord.source == "linkedin_post",
                        SourceRecord.source_key == post.source_key,
                    )
                    .order_by(SourceRecord.observed_at.desc())
                )
                if source_record is not None:
                    extraction = session.scalar(
                        select(Extraction)
                        .where(
                            Extraction.source_record_id == source_record.id,
                            Extraction.status == "completed",
                        )
                        .order_by(Extraction.created_at.desc())
                    )
                    if extraction is not None:
                        name = str(
                            _field(extraction.result_json, "company_name_clean")
                            or name
                        )
                        country = str(_field(extraction.result_json, "country") or "")
                        industry = str(
                            _field(extraction.result_json, "industry") or ""
                        )
            if not name:
                continue
            company = resolve_company(
                session,
                name=name,
                linkedin_url=url if post.author_type == "company" else "",
                country=country,
                industry=industry,
                is_agency=post.author_is_agency,
                source="linkedin_post",
            )
            if company is not None:
                post.company_id = company.id
                stats["posts"] += 1
        after = len(list(session.scalars(select(Company))))
        stats["companies_created"] = after - before
    return stats


def list_reviews(session_factory: sessionmaker[Session]) -> list[dict[str, Any]]:
    with session_factory() as session:
        reviews = list(
            session.scalars(
                select(CompanyMergeReview)
                .where(CompanyMergeReview.status == "pending")
                .order_by(CompanyMergeReview.similarity.desc())
            )
        )
        return [
            {
                "id": review.id,
                "source": session.get(Company, review.source_company_id).canonical_name,
                "candidate": session.get(
                    Company, review.candidate_company_id
                ).canonical_name,
                "similarity": review.similarity,
            }
            for review in reviews
        ]


def decide_review(
    session_factory: sessionmaker[Session],
    review_id: int,
    *,
    accept: bool,
) -> None:
    with session_factory.begin() as session:
        review = session.get(CompanyMergeReview, review_id)
        if review is None or review.status != "pending":
            raise ValueError(f"Pending review {review_id} not found")
        review.status = "accepted" if accept else "rejected"
        review.reviewed_at = datetime.now(UTC)
        if not accept:
            return
        source = session.get(Company, review.source_company_id)
        candidate = session.get(Company, review.candidate_company_id)
        if source is None or candidate is None:
            raise ValueError("Review companies no longer exist")
        for job in session.scalars(select(Job).where(Job.company_id == source.id)):
            job.company_id = candidate.id
        for post in session.scalars(select(Post).where(Post.company_id == source.id)):
            post.company_id = candidate.id
        for alias in session.scalars(
            select(CompanyAlias).where(CompanyAlias.company_id == source.id)
        ):
            existing = session.scalar(
                select(CompanyAlias).where(
                    CompanyAlias.company_id == candidate.id,
                    CompanyAlias.alias_normalised == alias.alias_normalised,
                )
            )
            if existing is None:
                alias.company_id = candidate.id
            else:
                session.delete(alias)
        candidate.is_agency = candidate.is_agency or source.is_agency
        if not candidate.linkedin_url:
            candidate.linkedin_url = source.linkedin_url


def import_company_groups(
    session_factory: sessionmaker[Session],
    path: Path,
) -> int:
    count = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with session_factory.begin() as session:
        for row in rows:
            normalised = normalise_company_name(row["company_name"])
            alias = session.scalar(
                select(CompanyAlias).where(
                    CompanyAlias.alias_normalised == normalised
                )
            )
            if alias is None:
                continue
            company = session.get(Company, alias.company_id)
            group = session.scalar(
                select(CompanyGroup).where(CompanyGroup.name == row["group_name"])
            )
            if group is None:
                group = CompanyGroup(name=row["group_name"])
                session.add(group)
                session.flush()
            company.group_id = group.id
            count += 1
    return count


def import_company_overrides(
    session_factory: sessionmaker[Session],
    path: Path,
) -> int:
    count = 0
    allowed = {"size_band", "jurisdiction", "hq_country", "industry_key"}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with session_factory.begin() as session:
        for row in rows:
            alias = session.scalar(
                select(CompanyAlias).where(
                    CompanyAlias.alias_normalised
                    == normalise_company_name(row["company_name"])
                )
            )
            if alias is None:
                continue
            company = session.get(Company, alias.company_id)
            for field in allowed:
                if row.get(field):
                    setattr(company, field, row[field])
            count += 1
    return count
