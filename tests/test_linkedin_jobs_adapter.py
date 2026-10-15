from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup
import pytest
from selenium.webdriver.common.by import By

from aambridge.__main__ import main as aambridge_main
from aambridge.contacts.listing import listing_emails, parse_end_client_name
from aambridge.sources.linkedin_jobs import (
    _parse_card,
    _parse_criteria,
    build_jobs_url,
    detect_agency,
    detect_end_client_hint,
    detect_nationality_restriction,
    parse_location,
    parse_relative_date,
)
from config import SearchSpec
import run_scraper

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureElement:
    """Small Selenium-like wrapper around saved fixture HTML."""

    def __init__(self, node) -> None:
        self.node = node

    @property
    def text(self) -> str:
        return self.node.get_text(" ", strip=True)

    def get_attribute(self, name: str) -> str:
        if name == "innerText":
            return self.text
        value = self.node.attrs.get(name, "")
        return " ".join(value) if isinstance(value, list) else str(value)

    def find_elements(self, by: str, selector: str):
        assert by == By.CSS_SELECTOR
        return [FixtureElement(node) for node in self.node.select(selector)]


def test_saved_html_job_card_parses_all_visible_baseline_fields() -> None:
    html = (FIXTURES / "linkedin_job_card.html").read_text(encoding="utf-8")
    card = FixtureElement(BeautifulSoup(html, "html.parser").select_one("li"))
    spec = SearchSpec("construction_infrastructure", "construction", "Saudi Arabia")

    lead = _parse_card(
        None,
        card,
        spec,
        include_gcc=False,
        include_description=False,
        observed_at=datetime(2026, 9, 22, tzinfo=UTC),
    )

    assert lead is not None
    assert lead.linkedin_job_id == "999001"
    assert lead.source_key == "999001"
    assert lead.company_name == "Example Recruitment"
    assert lead.company_linkedin_url == (
        "https://www.linkedin.com/company/example-recruitment"
    )
    assert lead.posted_date == "2026-09-20"
    assert lead.posted_date_precision == "exact"
    assert lead.is_reposted
    assert lead.is_promoted
    assert lead.easy_apply
    assert lead.applicant_count == 123
    assert lead.applicant_count_raw == "123 applicants"
    assert lead.workplace_type == "On-site"
    assert lead.city == "Riyadh"
    assert lead.region == "Riyadh"
    assert lead.country == "Saudi Arabia"
    assert not lead.location_unmatched
    assert lead.posted_by_agency


def test_experience_filter_defaults_to_all_and_accepts_named_levels() -> None:
    spec = SearchSpec("healthcare", "healthcare", "United Arab Emirates")

    default_url = build_jobs_url(spec, "week")
    selected_url = build_jobs_url(
        spec,
        "week",
        experience_levels=("entry", "mid_senior", "director"),
    )

    assert "f_E=" not in default_url
    assert "f_E=2%2C4%2C5" in selected_url


def test_scrape_cli_defaults_to_all_experience_levels() -> None:
    assert run_scraper.parse_args(["--mode", "jobs"]).experience_levels == ("all",)
    assert run_scraper.parse_args(
        ["--mode", "jobs", "--experience-levels", "entry", "director"]
    ).experience_levels == ["entry", "director"]


def test_module_scrape_help_dispatches_to_scrape_options(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        aambridge_main(["scrape", "--help"])

    assert exit_info.value.code == 0
    assert "--experience-levels" in capsys.readouterr().out


def test_relative_dates_support_english_and_arabic_without_claiming_exactness() -> None:
    observed = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)

    assert parse_relative_date("3 days ago", observed) == (
        datetime(2026, 9, 19).date(),
        "approx",
    )
    assert parse_relative_date("منذ 2 أسبوع", observed) == (
        datetime(2026, 9, 8).date(),
        "approx",
    )


def test_location_parser_maps_aliases_and_never_guesses_unknown_values() -> None:
    assert parse_location("Abu Dhabi, United Arab Emirates") == {
        "city": "Abu Dhabi",
        "region": "Abu Dhabi",
        "country": "United Arab Emirates",
        "location_unmatched": False,
    }
    assert parse_location("جدة، المملكة العربية السعودية") == {
        "city": "Jeddah",
        "region": "Makkah",
        "country": "Saudi Arabia",
        "location_unmatched": False,
    }
    assert parse_location("Doha, Qatar") == {
        "city": "Doha",
        "region": "Doha",
        "country": "Qatar",
        "location_unmatched": False,
    }
    assert parse_location("Kuala Lumpur, Malaysia") == {
        "city": "Kuala Lumpur",
        "region": "Kuala Lumpur",
        "country": "Malaysia",
        "location_unmatched": False,
    }
    assert parse_location("Sydney, Australia") == {
        "city": "Sydney",
        "region": "New South Wales",
        "country": "Australia",
        "location_unmatched": False,
    }
    assert parse_location("London, United Kingdom") == {
        "city": "London",
        "region": "London",
        "country": "United Kingdom",
        "location_unmatched": False,
    }
    assert parse_location("Berlin, Germany") == {
        "city": "Berlin",
        "region": "Berlin",
        "country": "Germany",
        "location_unmatched": False,
    }
    assert parse_location("Toronto, Canada") == {
        "city": "Toronto",
        "region": "Ontario",
        "country": "Canada",
        "location_unmatched": False,
    }
    assert parse_location("Austin, United States") == {
        "city": "Austin",
        "region": "Texas",
        "country": "United States",
        "location_unmatched": False,
    }
    assert parse_location("Unlisted Industrial Zone")["location_unmatched"]


def test_deterministic_flags_retain_evidence_and_inference_basis() -> None:
    restricted, evidence = detect_nationality_restriction(
        "This vacancy is للمواطنين فقط in Riyadh."
    )
    agency, agency_evidence = detect_agency("Example Executive Search")
    hint = detect_end_client_hint(
        "We are hiring on behalf of a leading hospital in Riyadh. Apply today."
    )

    assert restricted and evidence == "للمواطنين فقط"
    assert agency and agency_evidence == "executive search"
    assert hint["value"] == "on behalf of a leading hospital in Riyadh."
    assert hint["evidence"] == "on behalf of"
    assert hint["basis"] == "inference"
    assert hint["company_name"] == ""


def test_end_client_name_is_parsed_only_when_a_company_is_named() -> None:
    assert (
        parse_end_client_name(
            "Hiring on behalf of Acme Hospital LLC in Riyadh. Apply today."
        )
        == "Acme Hospital LLC"
    )
    assert parse_end_client_name(
        "We are hiring on behalf of a leading hospital in Riyadh."
    ) == ""
    assert listing_emails(
        "Email careers@hospital.ae or nora.person@gmail.com"
    ) == ["careers@hospital.ae", "nora.person@gmail.com"]


def test_job_criteria_are_kept_as_raw_deterministic_baselines() -> None:
    assert _parse_criteria(
        [
            "Seniority level Mid-Senior level",
            "Employment type Full-time",
            "Job function Project Management",
            "Industries Construction",
        ]
    ) == {
        "seniority_level": "Mid-Senior level",
        "employment_type": "Full-time",
        "job_function": "Project Management",
        "industries": "Construction",
    }
