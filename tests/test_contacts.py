from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import inspect, select

from aambridge.contacts.enrich import enrich_contacts
from aambridge.contacts.linkedin import parse_linkedin_about_website, parse_linkedin_people
from aambridge.contacts.website import (
    discover_official_website,
    extract_channels,
    extract_people,
    parse_ddg_instant_urls,
    parse_search_result_urls,
)
from aambridge.db.models import Company, CompanyContact, DecisionMaker, LeadScore
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.db.storage import ScanStore
from aambridge.decision_makers import add_decision_maker
from aambridge.resolve.companies import resolve_all
from aambridge.scoring.engine import score_companies

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _database(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'contacts.db').as_posix()}")
    initialise_database(engine)
    return engine, create_session_factory(engine)


def test_linkedin_about_reads_official_website_and_rejects_linkedin() -> None:
    assert (
        parse_linkedin_about_website(_html("linkedin_company_about.html"))
        == "https://www.clienthospital.ae"
    )
    assert parse_linkedin_about_website(_html("linkedin_company_about_linkedin_only.html")) == ""


def test_contact_page_extracts_switchboard_and_careers_email() -> None:
    channels = extract_channels(
        _html("company_contact_us.html"),
        "https://www.clienthospital.ae/contact-us",
    )
    emails = {item.value: item.label for item in channels if item.kind == "email"}
    phones = [item for item in channels if item.kind == "phone"]
    assert emails["careers@clienthospital.ae"] == "careers"
    assert emails["info@clienthospital.ae"] == "info"
    assert any(item.is_organizational for item in phones)
    assert any(item.kind == "address" for item in channels)


def test_form_only_page_has_no_phone_or_email() -> None:
    channels = extract_channels(
        _html("company_contact_form_only.html"),
        "https://www.clienthospital.ae/contact",
    )
    assert not [item for item in channels if item.kind in {"phone", "email"}]


def test_leadership_page_extracts_named_ta_head() -> None:
    people = extract_people(_html("company_leadership.html"))
    assert [(item.name, item.designation) for item in people] == [
        ("Aisha Rahman", "Head of Talent Acquisition")
    ]
    assert people[0].email == "aisha.rahman@clienthospital.ae"


def test_linkedin_people_returns_name_title_url_without_inventing_email() -> None:
    people = parse_linkedin_people(_html("linkedin_company_people.html"))
    assert [item["name"] for item in people] == ["Aisha Rahman", "Noura Ali", "Dev Person"]
    assert people[0]["profile_url"] == "https://www.linkedin.com/in/aisha-rahman"
    assert people[0]["designation"].startswith("Head of Talent Acquisition")
    assert all("email" not in item for item in people)
    hr_only = parse_linkedin_people(
        _html("linkedin_company_people.html"),
        keywords=("HR",),
    )
    assert [item["name"] for item in hr_only] == ["Noura Ali"]


def test_discover_official_website_picks_official_and_rejects_directories() -> None:
    html = _html("duckduckgo_search_client_hospital.html")
    urls = parse_search_result_urls(html)
    assert "https://www.clienthospital.ae" in urls
    assert not any(
        host in url
        for url in urls
        for host in ("linkedin.com", "crunchbase.com", "glassdoor.com")
    )
    found = discover_official_website(
        "Client Hospital",
        "United Arab Emirates",
        fetch_search=lambda _query: html,
    )
    assert found == "https://www.clienthospital.ae"


def test_parse_ddg_instant_urls_reads_official_site() -> None:
    payload = {
        "Results": [
            {"FirstURL": "https://www.mckinsey.com/", "Text": "Official site"},
            {"FirstURL": "https://www.crunchbase.com/organization/mckinsey", "Text": "Profile"},
        ],
        "Infobox": {
            "content": [
                {"label": "Official Website", "value": "https://www.mckinsey.com/"},
            ]
        },
    }
    urls = parse_ddg_instant_urls(payload)
    assert "https://www.mckinsey.com" in urls
    assert not any("crunchbase.com" in url for url in urls)


def test_discover_official_website_skips_low_confidence() -> None:
    html = (
        '<a href="https://www.crunchbase.com/organization/summit">Crunchbase</a>'
        '<a href="https://www.example.com">Unrelated</a>'
    )
    assert (
        discover_official_website(
            "Client Hospital",
            fetch_search=lambda _query: html,
        )
        == ""
    )


def test_enrich_contacts_from_name_search_without_linkedin_url(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        {
            "job_title": "Healthcare Operations Manager",
            "company_name": "Client Hospital",
            "job_location": "Dubai, United Arab Emirates",
            "city": "Dubai",
            "country": "United Arab Emirates",
            "industry": "healthcare",
            "job_url": "https://www.linkedin.com/jobs/view/8103",
        }
    )
    scan.finish("completed")
    resolve_all(sessions)

    pages = {
        "https://www.clienthospital.ae": (
            '<a href="/contact-us">Contact</a><a href="/leadership">Team</a>'
        ),
        "https://www.clienthospital.ae/contact-us": _html("company_contact_us.html"),
        "https://www.clienthospital.ae/leadership": _html("company_leadership.html"),
    }

    stats = enrich_contacts(
        sessions,
        limit=10,
        priorities=(),
        skip_linkedin_people=True,
        use_browser=False,
        fetch_search=lambda _query: _html("duckduckgo_search_client_hospital.html"),
        fetch_page=lambda url: pages.get(url, ""),
    )
    assert stats["websites"] == 1
    assert stats["channels"] >= 2
    assert stats["people"] >= 1

    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None
        assert company.linkedin_url is None
        assert company.website == "https://www.clienthospital.ae"
        channels = list(
            session.scalars(
                select(CompanyContact).where(CompanyContact.company_id == company.id)
            )
        )
        values = {item.value for item in channels}
        assert "careers@clienthospital.ae" in values
        people = list(session.scalars(select(DecisionMaker)))
        assert people
        assert all(item.source == "website_leadership" for item in people)
        assert all(item.verified_at is None for item in people)


def test_enrich_contacts_persists_website_channels_and_people(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        {
            "job_title": "Healthcare Operations Manager",
            "company_name": "Client Hospital",
            "company_linkedin_url": "https://www.linkedin.com/company/client-hospital",
            "job_location": "Dubai, United Arab Emirates",
            "city": "Dubai",
            "country": "United Arab Emirates",
            "industry": "healthcare",
            "job_url": "https://www.linkedin.com/jobs/view/8101",
        }
    )
    scan.finish("completed")
    resolve_all(sessions)

    pages = {
        "https://www.linkedin.com/company/client-hospital/about": _html(
            "linkedin_company_about.html"
        ),
        "https://www.clienthospital.ae": (
            '<a href="/contact-us">Contact</a><a href="/leadership">Team</a>'
        ),
        "https://www.clienthospital.ae/contact-us": _html("company_contact_us.html"),
        "https://www.clienthospital.ae/leadership": _html("company_leadership.html"),
    }

    stats = enrich_contacts(
        sessions,
        limit=10,
        priorities=(),
        skip_linkedin_people=False,
        use_browser=False,
        discover_website=lambda _url: parse_linkedin_about_website(
            pages["https://www.linkedin.com/company/client-hospital/about"]
        ),
        fetch_page=lambda url: pages.get(url, ""),
        fetch_people=lambda _url: parse_linkedin_people(
            _html("linkedin_company_people.html"),
            keywords=("HR", "Talent"),
        ),
    )
    assert stats["websites"] == 1
    assert stats["channels"] >= 2
    assert stats["people"] >= 2

    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None
        assert company.website == "https://www.clienthospital.ae"
        channels = list(
            session.scalars(
                select(CompanyContact).where(CompanyContact.company_id == company.id)
            )
        )
        values = {item.value for item in channels}
        assert "careers@clienthospital.ae" in values
        people = list(session.scalars(select(DecisionMaker)))
        assert any(item.source == "website_leadership" for item in people)
        assert any(item.source == "linkedin_people" for item in people)
        assert all(item.verified_at is None for item in people)


def test_unverified_enriched_people_do_not_unlock_high(tmp_path) -> None:
    _engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        {
            "job_title": "Healthcare Operations Manager",
            "company_name": "Client Hospital",
            "company_linkedin_url": "https://linkedin.com/company/client-hospital",
            "job_location": "Riyadh, Saudi Arabia",
            "city": "Riyadh",
            "country": "Saudi Arabia",
            "industry": "healthcare",
            "job_url": "https://linkedin.com/jobs/view/8102",
            "seniority_hint": "manager",
        }
    )
    scan.finish("completed")
    resolve_all(sessions)
    with sessions() as session:
        company_id = session.scalar(select(Company.id))
        assert company_id is not None

    add_decision_maker(
        sessions,
        company_id=company_id,
        name="Aisha Rahman",
        designation="Head of Talent Acquisition",
        source="linkedin_people",
        profile_url="https://www.linkedin.com/in/aisha-rahman",
    )
    score_companies(sessions, scan_run_id=scan.scan_run_id)
    with sessions() as session:
        score = session.scalar(select(LeadScore))
        assert score is not None
        factor = score.factor_results_json["decision_maker_accessibility"]
        assert factor["missing_inputs"] == ["verified_decision_maker"]
        assert score.priority != "HIGH"


def test_init_db_keeps_company_contacts_and_still_redacts_jobs(tmp_path) -> None:
    engine, sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="posts", args={})
    with sessions.begin() as session:
        company = Company(canonical_name="Client Hospital")
        session.add(company)
        session.flush()
        session.add(
            CompanyContact(
                company_id=company.id,
                kind="email",
                value="careers@clienthospital.ae",
                value_normalised="careers@clienthospital.ae",
                label="careers",
                source="website_contact",
                is_organizational=True,
                captured_at=datetime.now(UTC),
            )
        )
        session.add(
            DecisionMaker(
                company_id=company.id,
                name="Aisha Rahman",
                designation="Head of Talent Acquisition",
                relevance_rank=5,
                email="aisha.rahman@clienthospital.ae",
                phone="+971 50 123 4567",
                source="website_leadership",
            )
        )
    scan.save_post(
        {
            "source_key": "keep-contacts",
            "post_text": "Email leaked@example.com or +971 50 000 0000",
            "author_name": "Client Hospital",
        }
    )
    scan.finish("completed")

    initialise_database(engine)

    with sessions() as session:
        contact = session.scalar(select(CompanyContact))
        person = session.scalar(select(DecisionMaker))
        assert contact is not None
        assert contact.value == "careers@clienthospital.ae"
        assert person is not None
        assert person.email == "aisha.rahman@clienthospital.ae"
        assert person.phone == "+971 50 123 4567"
        from aambridge.db.models import SourceRecord

        source = session.scalar(select(SourceRecord))
        assert source is not None
        assert "leaked@example.com" not in source.raw_text


def test_decision_maker_contact_columns_exist() -> None:
    columns = {column.name for column in inspect(DecisionMaker).columns}
    assert {"email", "phone", "apollo_person_id", "email_status", "disclosed_at"} <= columns
