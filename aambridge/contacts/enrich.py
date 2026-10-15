"""Enrich resolved companies with public website and LinkedIn People contacts."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.contacts.linkedin import (
    LinkedInBrowser,
    discover_website_with_browser,
    list_people_with_browser,
    parse_linkedin_about_website,
)
from aambridge.contacts.website import (
    ExtractedChannel,
    crawl_company_site,
    discover_official_website,
    extract_channels,
    extract_people,
    is_official_website,
    normalise_channel_value,
)
from aambridge.db.models import Company, CompanyContact, Job, LeadScore
from aambridge.decision_makers import add_decision_maker
from aambridge.signals.detect import _scan_ids

log = logging.getLogger(__name__)
EMAIL_PRIORITY = {
    "careers": 0,
    "hr": 1,
    "info": 2,
    "contact": 3,
    "switchboard": 4,
    "personal": 8,
}
PHONE_PRIORITY = {"hr": 0, "careers": 1, "switchboard": 2}


def _latest_priority(session: Session, company_id: int) -> str | None:
    scans = _scan_ids(session)
    if not scans:
        return None
    score = session.scalar(
        select(LeadScore)
        .where(
            LeadScore.company_id == company_id,
            LeadScore.scan_run_id == scans[-1],
        )
    )
    return score.priority if score is not None else None


def companies_to_enrich(
    session: Session,
    *,
    limit: int,
    priorities: Sequence[str],
) -> list[Company]:
    wanted = {item.upper() for item in priorities}
    companies = list(
        session.scalars(
            select(Company)
            .where(Company.is_agency.is_(False))
            .order_by(Company.id)
        )
    )
    ranked: list[tuple[int, int, Company]] = []
    for company in companies:
        has_job = session.scalar(
            select(Job.id).where(Job.company_id == company.id).limit(1)
        )
        if has_job is None and not company.linkedin_url:
            continue
        priority = _latest_priority(session, company.id)
        if wanted and priority and priority not in wanted:
            continue
        if wanted and priority is None and has_job is None:
            continue
        bucket = 0 if priority == "HIGH" else 1 if priority == "MEDIUM" else 2
        ranked.append((bucket, company.id, company))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked[:limit]]


def upsert_channel(
    session: Session,
    company_id: int,
    channel: ExtractedChannel,
    source_url: str,
) -> bool:
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
            source_url=source_url,
            source=channel.source,
            is_organizational=channel.is_organizational,
            captured_at=datetime.now(UTC),
        )
    )
    return True


def enrich_company_pages(
    session_factory: sessionmaker[Session],
    company_id: int,
    pages: Sequence[tuple[str, str]],
) -> dict[str, int]:
    stats = {"channels": 0, "people": 0}
    extracted: list[tuple[str, ExtractedChannel]] = []
    people = []
    for url, html in pages:
        extracted.extend((url, channel) for channel in extract_channels(html, url))
        people.extend(extract_people(html))
    with session_factory.begin() as session:
        for url, channel in extracted:
            if upsert_channel(session, company_id, channel, url):
                stats["channels"] += 1
    for person in people:
        add_decision_maker(
            session_factory,
            company_id=company_id,
            name=person.name,
            designation=person.designation,
            source="website_leadership",
            email=person.email,
            phone=person.phone,
        )
        stats["people"] += 1
    return stats


def _persist_website(
    session_factory: sessionmaker[Session],
    company_id: int,
    website: str,
) -> None:
    with session_factory.begin() as session:
        company = session.get(Company, company_id)
        if company is not None:
            company.website = website


def enrich_contacts(
    session_factory: sessionmaker[Session],
    *,
    limit: int = 50,
    priorities: Sequence[str] = ("HIGH", "MEDIUM"),
    skip_linkedin_people: bool = False,
    discover_website: Callable[[str], str] | None = None,
    fetch_page: Callable[[str], str] | None = None,
    fetch_people: Callable[[str], list[dict[str, str]]] | None = None,
    fetch_search: Callable[[str], str] | None = None,
    use_browser: bool = True,
) -> dict[str, Any]:
    """Fill company.website, company_contact, and unverified people."""

    stats = {
        "companies": 0,
        "websites": 0,
        "channels": 0,
        "people": 0,
        "skipped": 0,
        "errors": 0,
    }
    with session_factory() as session:
        targets = companies_to_enrich(
            session, limit=limit, priorities=priorities
        )
        snapshots = [
            (
                company.id,
                company.canonical_name or "",
                company.hq_country or "",
                company.linkedin_url or "",
                company.website or "",
            )
            for company in targets
        ]

    browser: LinkedInBrowser | None = None
    try:
        for company_id, name, country, linkedin_url, website in snapshots:
            try:
                stats["companies"] += 1
                if _needs_website_snapshot(website):
                    found = ""
                    if linkedin_url and discover_website is not None:
                        found = discover_website(linkedin_url)
                    elif linkedin_url and use_browser:
                        if browser is None:
                            browser = LinkedInBrowser()
                            browser.__enter__()
                        found = discover_website_with_browser(linkedin_url, browser)
                    if not found:
                        found = discover_official_website(
                            name,
                            country,
                            fetch_search=fetch_search,
                        )
                    if found:
                        _persist_website(session_factory, company_id, found)
                        website = found
                        stats["websites"] += 1
                    else:
                        log.info(
                            "No official website for company %s (%s)",
                            company_id,
                            name,
                        )
                        stats["skipped"] += 1

                if website:
                    pages = crawl_company_site(website, fetch_page=fetch_page)
                    page_stats = enrich_company_pages(session_factory, company_id, pages)
                    stats["channels"] += page_stats["channels"]
                    stats["people"] += page_stats["people"]

                if skip_linkedin_people or not linkedin_url:
                    continue
                people: list[dict[str, str]] = []
                if fetch_people is not None:
                    people = fetch_people(linkedin_url)
                elif use_browser:
                    if browser is None:
                        browser = LinkedInBrowser()
                        browser.__enter__()
                    people = list_people_with_browser(linkedin_url, browser)
                for person in people:
                    add_decision_maker(
                        session_factory,
                        company_id=company_id,
                        name=person["name"],
                        designation=person["designation"],
                        source="linkedin_people",
                        profile_url=person.get("profile_url") or "",
                    )
                    stats["people"] += 1
            except Exception:
                log.exception("Contact enrichment failed for company %s", company_id)
                stats["errors"] += 1
    finally:
        if browser is not None:
            browser.__exit__(None, None, None)

    log.info("Contact enrichment: %s", stats)
    return stats


def _needs_website_snapshot(website: str) -> bool:
    return not is_official_website(website)


def best_organizational_channel(
    channels: Sequence[CompanyContact],
    kind: str,
) -> str:
    ranked: list[tuple[int, CompanyContact]] = []
    for item in channels:
        if item.kind != kind:
            continue
        if kind == "email":
            rank = EMAIL_PRIORITY.get(item.label or "", 5)
            if not item.is_organizational:
                rank += 10
        else:
            rank = PHONE_PRIORITY.get(item.label or "", 5)
        ranked.append((rank, item))
    ranked.sort(key=lambda pair: (pair[0], pair[1].id))
    return ranked[0][1].value if ranked else ""


def parse_about_website(html: str) -> str:
    return parse_linkedin_about_website(html)
