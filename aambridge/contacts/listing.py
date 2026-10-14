"""Contacts taken from job listings. No company-page or website crawl."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from aambridge.db.models import Company, CompanyContact, Job
from aambridge.privacy import EMAIL_RE

GENERIC_CLIENT = {
    "a",
    "an",
    "the",
    "our",
    "client",
    "leading",
    "company",
    "organisation",
    "organization",
    "hospital",
    "school",
    "employer",
    "group",
    "firm",
}
EMAIL_RANK = {
    "careers": 0,
    "hr": 1,
    "info": 2,
    "contact": 3,
    "personal": 8,
}


def listing_emails(text: str) -> list[str]:
    """Copy unique emails from listing text, organisational inboxes first."""

    from aambridge.contacts.website import classify_email

    found: list[str] = []
    seen: set[str] = set()
    for match in EMAIL_RE.findall(text or ""):
        value = match.strip()
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(value)
    found.sort(key=lambda item: (EMAIL_RANK.get(classify_email(item)[0], 5), item.casefold()))
    return found


def parse_end_client_name(text: str) -> str:
    """Return a proper end-client name, or empty when only a generic phrase exists."""

    blob = " ".join((text or "").split())
    if not blob:
        return ""
    patterns = (
        r"(?:on behalf of|our client(?: is)?|for our client)\s+([^.,;\n]{2,80})",
    )
    for pattern in patterns:
        match = re.search(pattern, blob, re.IGNORECASE)
        if not match:
            continue
        candidate = match.group(1)
        candidate = re.split(
            r"\s+(?:in|at|for|looking|seeking|based)\s+",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        candidate = re.sub(
            r"^(?:a|an|the|our)\s+(?:leading\s+)?",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip(" ,.-")
        tokens = [part.casefold() for part in re.findall(r"[A-Za-z\u0600-\u06ff]+", candidate)]
        if not candidate or not tokens:
            continue
        if all(token in GENERIC_CLIENT for token in tokens):
            continue
        if len(candidate) < 3:
            continue
        return candidate
    return ""


def persist_job_listing_contacts(
    session: Session,
    company: Company,
    job: Job,
    raw: dict[str, Any],
    *,
    save_emails: bool,
) -> int:
    """Store job URL and, for employer postings, copied listing emails."""

    from aambridge.contacts.website import ExtractedChannel, classify_email

    added = 0
    job_url = str(raw.get("job_url") or raw.get("source_url") or "")
    if job_url:
        added += int(
            _upsert_channel(
                session,
                company.id,
                ExtractedChannel(
                    kind="page_url",
                    value=job_url,
                    label="job_listing",
                    is_organizational=True,
                    source="job_listing",
                ),
                job_url,
            )
        )
    if not save_emails:
        return added
    emails = raw.get("listing_emails")
    if not isinstance(emails, list):
        emails = listing_emails(str(raw.get("description") or job.title or ""))
    for address in emails:
        label, organisational = classify_email(str(address))
        added += int(
            _upsert_channel(
                session,
                company.id,
                ExtractedChannel(
                    kind="email",
                    value=str(address),
                    label=label,
                    is_organizational=organisational,
                    source="job_listing",
                ),
                job_url or (company.linkedin_url or ""),
            )
        )
    return added


def _upsert_channel(
    session: Session,
    company_id: int,
    channel: object,
    source_url: str,
) -> bool:
    from aambridge.contacts.website import ExtractedChannel, normalise_channel_value

    assert isinstance(channel, ExtractedChannel)
    normalised = normalise_channel_value(channel.kind, channel.value)
    if not normalised:
        return False
    existing = session.scalar(
        select(CompanyContact).where(
            CompanyContact.company_id == company_id,
            CompanyContact.kind == channel.kind,
            CompanyContact.value_normalised == normalised,
        )
    )
    if existing is not None:
        return False
    session.add(
        CompanyContact(
            company_id=company_id,
            kind=channel.kind,
            value=channel.value,
            value_normalised=normalised,
            label=channel.label,
            source_url=source_url or None,
            source=channel.source,
            is_organizational=channel.is_organizational,
            captured_at=datetime.now(UTC),
        )
    )
    return True
