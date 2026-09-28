from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By

from aambridge.privacy import has_contact_email, privacy_safe
from aambridge.sources.linkedin_posts import (
    _parse_card,
    _title,
    categorise_post,
)
from aambridge.db.storage import post_source_key
from config import SearchSpec, build_post_search_specs

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureElement:
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


def _fixture_card() -> FixtureElement:
    html = (FIXTURES / "linkedin_post_card.html").read_text(encoding="utf-8")
    return FixtureElement(BeautifulSoup(html, "html.parser").select_one("div"))


def test_saved_html_post_captures_author_date_category_and_contact_boolean() -> None:
    spec = SearchSpec(
        "healthcare",
        "healthcare expansion",
        "Saudi Arabia",
        query_set="expansion",
    )

    lead = _parse_card(
        _fixture_card(),
        spec,
        include_gcc=False,
        observed_at=datetime(2026, 9, 22, tzinfo=UTC),
    )

    assert lead is not None
    assert lead.source_key == "777001"
    assert lead.author_name == "Example Recruitment"
    assert lead.author_url == (
        "https://www.linkedin.com/company/example-recruitment"
    )
    assert lead.author_type == "company"
    assert lead.author_is_agency
    assert lead.post_date == "2026-09-20"
    assert lead.post_date_precision == "exact"
    assert lead.post_category == "expansion"
    assert lead.query_set == "expansion"
    assert lead.has_contact_email
    assert lead.job_title == ""
    assert "growth.lead@example.com" not in lead.post_text
    assert "+966 50 123 4567" not in lead.post_text
    assert "[email redacted]" in lead.post_text
    assert "[phone redacted]" in lead.post_text


def test_title_is_set_only_for_explicit_hiring_language() -> None:
    assert _title("We are hiring: Finance Manager in Dubai.") == (
        "Finance Manager in Dubai"
    )
    assert _title("Our Finance Manager spoke at an industry event.") == ""


def test_post_compact_relative_date_is_recorded_as_approximate() -> None:
    html = """
    <article data-urn="urn:li:activity:101">
      <time>2d</time>
      <div class="update-components-text">
        We are hiring: Operations Manager in Riyadh.
      </div>
    </article>
    """
    lead = _parse_card(
        FixtureElement(BeautifulSoup(html, "html.parser").article),
        SearchSpec("hospitality", "hospitality", "Saudi Arabia"),
        include_gcc=False,
        observed_at=datetime(2026, 9, 22, tzinfo=UTC),
    )

    assert lead is not None
    assert lead.post_date == "2026-09-20"
    assert lead.post_date_precision == "approx"


def test_post_categories_have_deterministic_signal_precedence() -> None:
    assert categorise_post("We were awarded the contract and are hiring.") == (
        "project_award"
    )
    assert categorise_post("We are expanding with a new office in Riyadh.") == (
        "expansion"
    )
    assert categorise_post("Jane joins as our new CEO.") == "leadership_change"
    assert categorise_post("We are hiring a Retail Manager.") == "hiring"
    assert categorise_post("A general market update.") == "other"


def test_post_query_sets_are_independently_selectable() -> None:
    hiring = build_post_search_specs(query_set="hiring")
    expansion = build_post_search_specs(query_set="expansion")
    all_specs = build_post_search_specs(query_set="all")

    assert len(hiring) == 24
    assert len(expansion) == 12
    assert len(all_specs) == 36
    assert {spec.query_set for spec in expansion} == {"expansion"}


def test_fallback_source_key_matches_database_key() -> None:
    text = "We are hiring? A manager in Dubai."
    card = FixtureElement(
        BeautifulSoup(
            f"<article><div class='update-components-text'>{text}</div></article>",
            "html.parser",
        ).article
    )
    lead = _parse_card(
        card,
        SearchSpec("retail", "retail", "United Arab Emirates"),
        include_gcc=False,
    )

    assert lead is not None
    assert lead.source_key == post_source_key("", lead.post_text)


def test_privacy_boundary_removes_explicit_contact_fields_recursively() -> None:
    raw = {
        "recruiter_email": "person@example.com",
        "post_text": "Email person@example.com",
        "nested": {"phone": "+971 50 123 4567", "note": "Call +971 50 123 4567"},
        "has_contact_email": True,
    }

    safe = privacy_safe(raw)

    assert "recruiter_email" not in safe
    assert "phone" not in safe["nested"]
    assert not has_contact_email(safe["post_text"])
    assert "person@example.com" not in str(safe)
    assert "+971 50 123 4567" not in str(safe)
    assert safe["has_contact_email"] is True
