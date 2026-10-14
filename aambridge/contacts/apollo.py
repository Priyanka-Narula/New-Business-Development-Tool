"""Apollo.io people search (free) and email disclose (paid)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from aambridge.db.models import Company, DecisionMaker, Job
from aambridge.decision_makers import add_decision_maker
from aambridge.settings import Settings

log = logging.getLogger(__name__)
SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"
PERSON_TITLES = (
    "HR",
    "Talent Acquisition",
    "Recruiter",
    "People",
    "Recruitment",
)
PER_PAGE = 25
TIMEOUT = 30
PostJson = Callable[[str, dict[str, Any]], dict[str, Any]]


class ApolloError(RuntimeError):
    """Raised when Apollo is not configured or a request cannot be used."""


@dataclass(frozen=True)
class ApolloPerson:
    apollo_id: str
    name: str
    title: str
    linkedin_url: str
    email_status: str
    location: str
    email: str = ""


def require_api_key(settings: Settings | None = None) -> str:
    key = (settings or Settings()).apollo_api_key.strip()
    if not key:
        raise ApolloError(
            "APOLLO_API_KEY is not set. Add it to .env to search or disclose contacts."
        )
    return key


def default_post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "x-api-key": require_api_key(),
        },
        timeout=TIMEOUT,
    )
    if response.status_code == 401:
        raise ApolloError("Apollo rejected the API key.")
    if response.status_code == 429:
        raise ApolloError("Apollo rate limit reached. Try again in a minute.")
    if response.status_code >= 400:
        detail = (response.text or "").strip()[:300]
        raise ApolloError(
            f"Apollo request failed ({response.status_code})"
            + (f": {detail}" if detail else ".")
        )
    body = response.json()
    if not isinstance(body, dict):
        raise ApolloError("Apollo returned an unexpected response.")
    return body


def search_people(
    *,
    organization_name: str,
    locations: Sequence[str] = (),
    per_page: int = PER_PAGE,
    post_json: PostJson | None = None,
) -> list[ApolloPerson]:
    name = (organization_name or "").strip()
    if not name:
        raise ApolloError("Company name is required for an Apollo search.")
    places = [item.strip() for item in locations if item and item.strip()]
    payload: dict[str, Any] = {
        "q_organization_name": name,
        "organization_names": [name],
        "person_titles": list(PERSON_TITLES),
        "page": 1,
        "per_page": per_page,
    }
    if places:
        payload["person_locations"] = places
    body = (post_json or default_post_json)(SEARCH_URL, payload)
    rows = body.get("people") or body.get("contacts") or []
    people: list[ApolloPerson] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        person = _parse_person(row)
        if person is None or person.apollo_id in seen:
            continue
        seen.add(person.apollo_id)
        people.append(person)
    return people


def match_person(
    apollo_id: str,
    *,
    post_json: PostJson | None = None,
) -> ApolloPerson:
    ident = (apollo_id or "").strip()
    if not ident:
        raise ApolloError("Apollo person id is required to disclose a contact.")
    body = (post_json or default_post_json)(
        MATCH_URL,
        {
            "id": ident,
            "reveal_personal_emails": True,
            "reveal_phone_number": False,
        },
    )
    row = body.get("person")
    if not isinstance(row, dict):
        raise ApolloError("Apollo found no match for this person.")
    person = _parse_person(row)
    if person is None:
        raise ApolloError("Apollo found no match for this person.")
    return person


def company_search_locations(session: Session, company: Company) -> list[str]:
    places: list[str] = []
    city_rows = session.execute(
        select(Job.city, func.count(Job.id))
        .where(Job.company_id == company.id, Job.city.is_not(None), Job.city != "")
        .group_by(Job.city)
        .order_by(func.count(Job.id).desc(), Job.city)
    ).all()
    if city_rows:
        city = city_rows[0][0]
        if city:
            places.append(city)
    country = (company.hq_country or "").strip()
    if country and country not in places:
        places.append(country)
    return places


def search_company_people(
    session_factory: sessionmaker[Session],
    company_id: int,
    *,
    post_json: PostJson | None = None,
) -> dict[str, Any]:
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            raise ValueError(f"Company {company_id} not found")
        name = company.canonical_name
        locations = company_search_locations(session, company)
    people = search_people(
        organization_name=name,
        locations=locations,
        post_json=post_json,
    )
    stored = 0
    for person in people:
        add_decision_maker(
            session_factory,
            company_id=company_id,
            name=person.name,
            designation=person.title,
            source="apollo",
            profile_url=person.linkedin_url,
            notes=person.location,
            apollo_person_id=person.apollo_id,
            email_status=person.email_status,
        )
        stored += 1
    stats = {
        "company_id": company_id,
        "company": name,
        "locations": locations,
        "found": len(people),
        "stored": stored,
    }
    log.info("Apollo search: %s", stats)
    return stats


def disclose_person(
    session_factory: sessionmaker[Session],
    person_id: int,
    *,
    verified_by: str = "",
    post_json: PostJson | None = None,
) -> dict[str, Any]:
    with session_factory() as session:
        record = session.get(DecisionMaker, person_id)
        if record is None:
            raise ValueError(f"Decision-maker {person_id} not found")
        company_id = record.company_id
        apollo_id = (record.apollo_person_id or "").strip()
        already = record.disclosed_at is not None
        existing_email = record.email or ""
        existing_name = record.name
        existing_title = record.designation
    if not apollo_id:
        raise ApolloError("This person has no Apollo id. Search Apollo first.")
    if already:
        log.info("Apollo disclose skipped; person %s already disclosed.", person_id)
        return {
            "id": person_id,
            "apollo_person_id": apollo_id,
            "email": existing_email,
            "disclosed": True,
            "skipped": True,
        }
    person = match_person(apollo_id, post_json=post_json)
    add_decision_maker(
        session_factory,
        company_id=company_id,
        name=person.name or existing_name,
        designation=person.title or existing_title,
        source="apollo",
        profile_url=person.linkedin_url,
        email=person.email,
        notes=person.location,
        verified_by=verified_by,
        apollo_person_id=person.apollo_id,
        email_status=person.email_status,
        disclosed_at=datetime.now(UTC),
    )
    result = {
        "id": person_id,
        "apollo_person_id": person.apollo_id,
        "email": person.email,
        "email_status": person.email_status,
        "disclosed": True,
        "skipped": False,
    }
    log.info("Apollo disclose: %s", {**result, "email": bool(person.email)})
    return result


def _parse_person(row: dict[str, Any]) -> ApolloPerson | None:
    apollo_id = str(row.get("id") or "").strip()
    name = " ".join(
        part
        for part in (
            str(row.get("first_name") or "").strip(),
            str(row.get("last_name") or "").strip(),
        )
        if part
    ) or str(row.get("name") or "").strip()
    title = str(row.get("title") or "").strip()
    if not apollo_id or not name or not title:
        return None
    location = ", ".join(
        part
        for part in (
            str(row.get("city") or "").strip(),
            str(row.get("state") or "").strip(),
            str(row.get("country") or "").strip(),
        )
        if part
    )
    return ApolloPerson(
        apollo_id=apollo_id,
        name=name,
        title=title,
        linkedin_url=str(row.get("linkedin_url") or "").strip(),
        email_status=str(row.get("email_status") or "unknown").strip() or "unknown",
        location=location,
        email=str(row.get("email") or "").strip(),
    )
