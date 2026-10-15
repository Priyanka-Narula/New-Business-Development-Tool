from __future__ import annotations

from datetime import UTC, datetime

from rapidfuzz.fuzz import ratio
from sqlalchemy import func, select

from aambridge.db.models import (
    Company,
    CompanyAlias,
    CompanyContact,
    CompanyMergeReview,
    Job,
)
from aambridge.db.session import (
    create_database_engine,
    create_session_factory,
    initialise_database,
)
from aambridge.db.storage import ScanStore
from aambridge.resolve.companies import (
    decide_review,
    import_company_groups,
    import_company_overrides,
    normalise_company_name,
    resolve_all,
    resolve_company,
)


def _database(tmp_path):
    engine = create_database_engine(f"sqlite:///{(tmp_path / 'resolve.db').as_posix()}")
    initialise_database(engine)
    return create_session_factory(engine)


def test_legal_suffix_and_al_prefix_variants_normalise_identically() -> None:
    assert normalise_company_name("Al Futtaim") == "alfuttaim"
    assert normalise_company_name("Al-Futtaim Group") == "alfuttaim"
    assert normalise_company_name("Alfuttaim LLC") == "alfuttaim"
    assert normalise_company_name("شركة المثال ذ.م.م") == "المثال"


def test_exact_linkedin_url_is_primary_company_key(tmp_path) -> None:
    sessions = _database(tmp_path)
    with sessions.begin() as session:
        first = resolve_company(
            session,
            name="Example Holdings",
            linkedin_url="https://linkedin.com/company/example/?trk=one",
            country="United Arab Emirates",
        )
        second = resolve_company(
            session,
            name="Example New Brand",
            linkedin_url="https://linkedin.com/company/example?trk=two",
            country="United Arab Emirates",
        )
        assert first.id == second.id

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Company)) == 1
        assert session.scalar(select(func.count()).select_from(CompanyAlias)) == 2


def test_review_range_creates_queue_and_accept_moves_jobs(tmp_path) -> None:
    sessions = _database(tmp_path)
    left = "Example Healthcare"
    right = "Example Health"
    similarity = ratio(
        normalise_company_name(left),
        normalise_company_name(right),
    )
    assert 80 <= similarity < 92

    with sessions.begin() as session:
        candidate = resolve_company(
            session,
            name=left,
            country="Saudi Arabia",
        )
        source = resolve_company(
            session,
            name=right,
            country="Saudi Arabia",
        )
        job = Job(
            source_key="review-job",
            linkedin_job_id="500",
            title="Manager",
            company_name_raw=right,
            company_linkedin_url=None,
            company_id=source.id,
            location_raw="Riyadh",
            city="Riyadh",
            region="Riyadh",
            country="Saudi Arabia",
            first_seen_at=datetime.now(UTC),
            last_seen_at=datetime.now(UTC),
            is_active=True,
        )
        session.add(job)
        assert candidate.id != source.id

    with sessions() as session:
        review = session.scalar(select(CompanyMergeReview))
        assert review is not None
        review_id = review.id
        candidate_id = review.candidate_company_id

    decide_review(sessions, review_id, accept=True)
    with sessions() as session:
        job = session.scalar(select(Job).where(Job.source_key == "review-job"))
        review = session.get(CompanyMergeReview, review_id)
        assert job.company_id == candidate_id
        assert review.status == "accepted"


def test_resolution_links_agency_posted_job_to_agency_company(tmp_path) -> None:
    sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        {
            "job_title": "Finance Director",
            "company_name": "Example Executive Search",
            "company_linkedin_url": "https://linkedin.com/company/example-search",
            "job_location": "Dubai, United Arab Emirates",
            "country": "United Arab Emirates",
            "job_url": "https://linkedin.com/jobs/view/700",
            "posted_by_agency": True,
        }
    )
    scan.finish("completed")

    stats = resolve_all(sessions)

    assert stats["jobs"] == 1
    with sessions() as session:
        company = session.scalar(select(Company))
        job = session.scalar(select(Job))
        assert company is not None and company.is_agency
        assert job.company_id == company.id
        assert session.scalar(select(CompanyContact)) is None


def test_employer_job_copies_listing_email_and_agency_job_keeps_end_client(
    tmp_path,
) -> None:
    sessions = _database(tmp_path)
    scan = ScanStore.start(sessions, mode="jobs", args={})
    assert scan.save_job(
        {
            "job_title": "HR Manager",
            "company_name": "Client Hospital",
            "job_location": "Dubai, United Arab Emirates",
            "country": "United Arab Emirates",
            "job_url": "https://linkedin.com/jobs/view/801",
            "description": "Apply at careers@clienthospital.ae",
            "posted_by_agency": False,
        }
    )
    assert scan.save_job(
        {
            "job_title": "Finance Director",
            "company_name": "Example Executive Search",
            "job_location": "Dubai, United Arab Emirates",
            "country": "United Arab Emirates",
            "job_url": "https://linkedin.com/jobs/view/802",
            "description": (
                "Hiring on behalf of Desert Solar LLC. "
                "Recruiter email agency@search.com"
            ),
            "posted_by_agency": True,
        }
    )
    scan.finish("completed")
    resolve_all(sessions)

    with sessions() as session:
        names = {
            company.canonical_name: company
            for company in session.scalars(select(Company))
        }
        assert "Client Hospital" in names
        assert "Desert Solar LLC" in names
        assert names["Desert Solar LLC"].is_agency is False
        hospital_emails = list(
            session.scalars(
                select(CompanyContact).where(
                    CompanyContact.company_id == names["Client Hospital"].id,
                    CompanyContact.kind == "email",
                )
            )
        )
        assert [item.value for item in hospital_emails] == ["careers@clienthospital.ae"]
        client_emails = list(
            session.scalars(
                select(CompanyContact).where(
                    CompanyContact.company_id == names["Desert Solar LLC"].id,
                    CompanyContact.kind == "email",
                )
            )
        )
        assert client_emails == []
        agency_job = session.scalar(select(Job).where(Job.source_key == "802"))
        assert agency_job is not None
        assert agency_job.company_name_raw == "Example Executive Search"
        assert agency_job.company_id == names["Desert Solar LLC"].id


def test_group_and_override_csv_imports_apply_to_resolved_companies(
    tmp_path,
) -> None:
    sessions = _database(tmp_path)
    with sessions.begin() as session:
        resolve_company(
            session,
            name="Client Hospital",
            country="Saudi Arabia",
            industry="healthcare",
        )

    groups = tmp_path / "groups.csv"
    groups.write_text(
        "company_name,group_name\nClient Hospital,Health Group\n",
        encoding="utf-8",
    )
    overrides = tmp_path / "overrides.csv"
    overrides.write_text(
        "company_name,size_band,jurisdiction,hq_country,industry_key,website\n"
        "Client Hospital,250-999,mainland,Saudi Arabia,healthcare,https://www.clienthospital.ae\n",
        encoding="utf-8",
    )

    assert import_company_groups(sessions, groups) == 1
    assert import_company_overrides(sessions, overrides) == 1
    with sessions() as session:
        company = session.scalar(select(Company))
        assert company is not None
        assert company.group_id is not None
        assert company.size_band == "250-999"
        assert company.jurisdiction == "mainland"
        assert company.hq_country == "Saudi Arabia"
        assert company.website == "https://www.clienthospital.ae"
