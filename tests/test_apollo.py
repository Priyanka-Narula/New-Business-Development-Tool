from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, select

from aambridge import __main__ as cli
from aambridge.contacts.apollo import (
    MATCH_URL,
    SEARCH_URL,
    ApolloError,
    disclose_person,
    require_api_key,
    search_company_people,
    search_people,
)
from aambridge.dashboard.queries import company_detail
from aambridge.db.models import Company, DecisionMaker, Job
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)


def _database(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'apollo.db').as_posix()}")
    initialise_database(engine)
    return engine, create_session_factory(engine)


def _company(session, name: str = "Client Hospital") -> Company:
    company = Company(
        canonical_name=name,
        hq_country="United Arab Emirates",
    )
    session.add(company)
    session.flush()
    now = datetime.now(UTC)
    session.add(
        Job(
            source_key=f"job-{company.id}",
            title="Healthcare Operations Manager",
            company_name_raw=name,
            company_id=company.id,
            location_raw="Dubai, United Arab Emirates",
            city="Dubai",
            country="United Arab Emirates",
            first_seen_at=now,
            last_seen_at=now,
        )
    )
    return company


def _search_payload() -> dict:
    return {
        "people": [
            {
                "id": "abc123",
                "first_name": "Aisha",
                "last_name": "Rahman",
                "title": "Head of Talent Acquisition",
                "linkedin_url": "https://www.linkedin.com/in/aisha-rahman",
                "email_status": "verified",
                "city": "Dubai",
                "country": "United Arab Emirates",
            },
            {
                "id": "skip-me",
                "first_name": "No",
                "last_name": "Title",
            },
        ]
    }


def _match_payload() -> dict:
    return {
        "person": {
            "id": "abc123",
            "name": "Aisha Rahman",
            "title": "Head of Talent Acquisition",
            "email": "aisha.rahman@clienthospital.ae",
            "email_status": "verified",
            "linkedin_url": "https://www.linkedin.com/in/aisha-rahman",
            "city": "Dubai",
            "country": "United Arab Emirates",
        }
    }


def test_require_api_key_explains_missing_config(monkeypatch) -> None:
    monkeypatch.setattr(
        "aambridge.contacts.apollo.Settings",
        lambda: SimpleNamespace(apollo_api_key=""),
    )
    with pytest.raises(ApolloError, match="APOLLO_API_KEY"):
        require_api_key()


def test_search_people_parses_name_title_without_email() -> None:
    def post_json(url: str, payload: dict) -> dict:
        assert url == SEARCH_URL
        assert payload["q_organization_name"] == "Client Hospital"
        assert payload["person_locations"] == ["Dubai", "United Arab Emirates"]
        assert "Talent Acquisition" in payload["person_titles"]
        return _search_payload()

    people = search_people(
        organization_name="Client Hospital",
        locations=("Dubai", "United Arab Emirates"),
        post_json=post_json,
    )
    assert [item.apollo_id for item in people] == ["abc123"]
    assert people[0].name == "Aisha Rahman"
    assert people[0].email == ""
    assert people[0].email_status == "verified"
    assert people[0].location == "Dubai, United Arab Emirates"


def test_search_stores_people_without_email(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    with sessions.begin() as session:
        company_id = _company(session).id

    calls: list[str] = []

    def post_json(url: str, payload: dict) -> dict:
        calls.append(url)
        assert payload["q_organization_name"] == "Client Hospital"
        assert payload["person_locations"][0] == "Dubai"
        return _search_payload()

    stats = search_company_people(sessions, company_id, post_json=post_json)
    assert stats["found"] == 1
    assert stats["stored"] == 1
    assert stats["locations"] == ["Dubai", "United Arab Emirates"]
    assert calls == [SEARCH_URL]

    with sessions() as session:
        person = session.scalar(select(DecisionMaker))
        assert person is not None
        person_id = person.id
        assert person.email is None
        assert person.phone is None
        assert person.apollo_person_id == "abc123"
        assert person.email_status == "verified"
        assert person.disclosed_at is None
        assert person.source == "apollo"
        assert person.notes == "Dubai, United Arab Emirates"

    detail = company_detail(sessions, company_id)
    assert detail is not None
    payload = detail["people"][0]
    assert payload["id"] == person_id
    assert payload["apollo_person_id"] == "abc123"
    assert payload["disclosed"] is False
    assert payload["email"] == ""
    assert payload["location"] == "Dubai, United Arab Emirates"


def test_disclose_fills_email_and_second_call_is_noop(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    with sessions.begin() as session:
        company_id = _company(session).id

    search_company_people(
        sessions,
        company_id,
        post_json=lambda url, payload: _search_payload(),
    )
    with sessions() as session:
        person_id = session.scalar(select(DecisionMaker.id))
        assert person_id is not None

    calls: list[str] = []

    def post_json(url: str, payload: dict) -> dict:
        calls.append(url)
        assert payload["id"] == "abc123"
        assert payload["reveal_personal_emails"] is True
        assert payload["reveal_phone_number"] is False
        return _match_payload()

    first = disclose_person(sessions, person_id, post_json=post_json)
    assert first["skipped"] is False
    assert first["email"] == "aisha.rahman@clienthospital.ae"
    second = disclose_person(sessions, person_id, post_json=post_json)
    assert second["skipped"] is True
    assert second["email"] == "aisha.rahman@clienthospital.ae"
    assert calls == [MATCH_URL]

    with sessions() as session:
        person = session.get(DecisionMaker, person_id)
        assert person is not None
        assert person.email == "aisha.rahman@clienthospital.ae"
        assert person.disclosed_at is not None
        assert person.verified_at is None


def test_search_does_not_duplicate_or_wipe_disclosed_email(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    with sessions.begin() as session:
        company_id = _company(session).id
    search_company_people(
        sessions, company_id, post_json=lambda url, payload: _search_payload()
    )
    with sessions() as session:
        person_id = session.scalar(select(DecisionMaker.id))
    disclose_person(
        sessions, person_id, post_json=lambda url, payload: _match_payload()
    )
    search_company_people(
        sessions, company_id, post_json=lambda url, payload: _search_payload()
    )
    with sessions() as session:
        rows = list(session.scalars(select(DecisionMaker)))
        assert len(rows) == 1
        assert rows[0].email == "aisha.rahman@clienthospital.ae"
        assert rows[0].disclosed_at is not None


def test_cli_apollo_search_and_disclose(tmp_path, monkeypatch) -> None:
    engine, sessions = _database(tmp_path)
    with sessions.begin() as session:
        company_id = _company(session).id
    database_url = str(engine.url)

    def fake_search(session_factory, company_id: int, **_kwargs):
        return {
            "company_id": company_id,
            "company": "Client Hospital",
            "locations": ["Dubai"],
            "found": 1,
            "stored": 1,
        }

    monkeypatch.setattr(
        "aambridge.contacts.apollo.search_company_people", fake_search
    )
    assert (
        cli.main(
            [
                "apollo-search",
                "--database-url",
                database_url,
                "--company-id",
                str(company_id),
            ]
        )
        == 0
    )

    disclosed = []

    def fake_disclose(session_factory, person_id: int, **kwargs):
        disclosed.append((person_id, kwargs.get("verified_by")))
        return {
            "id": person_id,
            "apollo_person_id": "abc123",
            "email": "aisha.rahman@clienthospital.ae",
            "disclosed": True,
            "skipped": False,
        }

    monkeypatch.setattr("aambridge.contacts.apollo.disclose_person", fake_disclose)
    assert (
        cli.main(
            [
                "apollo-disclose",
                "--database-url",
                database_url,
                "--person-id",
                "12",
                "--verified-by",
                "recruiter",
            ]
        )
        == 0
    )
    assert disclosed == [(12, "recruiter")]


def test_cli_apollo_search_missing_company_returns_error(tmp_path) -> None:
    engine, _sessions = _database(tmp_path)
    assert (
        cli.main(
            [
                "apollo-search",
                "--database-url",
                str(engine.url),
                "--company-id",
                "99",
            ]
        )
        == 1
    )


def test_apollo_columns_are_added_to_existing_decision_maker_table(tmp_path) -> None:
    engine = create_database_engine(
        f"sqlite:///{(tmp_path / 'legacy-apollo.db').as_posix()}"
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE decision_maker ("
            "id INTEGER PRIMARY KEY, "
            "company_id INTEGER, "
            "name VARCHAR(255) NOT NULL, "
            "designation VARCHAR(255) NOT NULL, "
            "relevance_rank INTEGER, "
            "source VARCHAR(255)"
            ")"
        )
    initialise_database(engine, scrub_personal_contacts=False)
    columns = {
        column["name"] for column in inspect(engine).get_columns("decision_maker")
    }
    assert {"apollo_person_id", "email_status", "disclosed_at"} <= columns
