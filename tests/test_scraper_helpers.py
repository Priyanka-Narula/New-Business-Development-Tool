from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

import pandas as pd

from config import SearchSpec
from aambridge.sources.linkedin_jobs import _parse_card as parse_job_card
from aambridge.sources.linkedin_posts import _parse_card as parse_post_card
from scraper_common import (
    _first_element,
    _is_authenticated_page,
    _term_pattern_source,
    all_matching_terms,
    canonical_source_key,
    clean_text,
    detect_excluded_terms,
    detect_locations,
    detect_seniority_terms,
    first_matching_term,
    human_delay,
    save_dataclasses,
    strip_query_from_url,
)


def test_clean_text_handles_arabic_and_unicode_whitespace() -> None:
    assert clean_text("  مطلوب\tمدير\nفي  الرياض  ") == "مطلوب مدير في الرياض"


def test_all_matches_are_boundary_safe_and_keep_term_order() -> None:
    terms = ("senior manager", "manager", "vp")
    text = "VP, then senior manager; not svp."

    assert all_matching_terms(text, terms) == [
        "senior manager",
        "manager",
        "vp",
    ]
    assert first_matching_term(text, terms) == "senior manager"
    assert first_matching_term("development", ("vp",)) == ""


def test_detectors_return_all_arabic_and_english_matches() -> None:
    text = "مطلوب مدير ومتدرب في الرياض / Dubai."

    assert detect_locations(text) == ["الرياض", "Dubai"]
    assert detect_seniority_terms(text) == ["مدير"]
    assert detect_excluded_terms(text) == ["متدرب"]


def test_arabic_terms_match_attached_conjunction_and_definite_article() -> None:
    text = "مطلوب: والمدير، وأخصائي في والرياض؛ وليس: والمتدرب."

    assert detect_locations(text) == ["الرياض"]
    assert detect_seniority_terms(text) == ["مدير", "أخصائي"]
    assert detect_excluded_terms(text) == ["متدرب"]
    assert re.search(_term_pattern_source("مدير"), "والمدير،")


def test_query_stripping_only_changes_actual_urls() -> None:
    assert (
        strip_query_from_url("https://example.com/post/1?tracking=abc")
        == "https://example.com/post/1"
    )
    assert strip_query_from_url("Are we hiring? Yes, now.") == (
        "Are we hiring? Yes, now."
    )


def test_canonical_source_keys_use_ids_then_normalised_text_hash() -> None:
    assert canonical_source_key(
        {"job_url": "https://linkedin.com/jobs/view/12345?trk=x"}, "job_url"
    ) == "12345"
    assert canonical_source_key(
        {
            "post_url": (
                "https://linkedin.com/feed/update/urn:li:activity:98765?trk=x"
            )
        },
        "post_text",
    ) == "98765"
    expected = hashlib.sha256("why? مدير".encode("utf-8")).hexdigest()
    assert canonical_source_key({"post_text": " Why?   مدير "}, "post_text") == expected
    assert canonical_source_key(
        {"job_url": "https://example.com/opening?tracking=one"}, "job_url"
    ) == canonical_source_key(
        {"job_url": "https://example.com/opening?tracking=two"}, "job_url"
    )


@dataclass
class CsvPost:
    post_text: str
    post_url: str = ""


@dataclass
class CsvJob:
    job_url: str
    description: str = ""


def test_save_dataclasses_does_not_truncate_post_text_at_question_mark(
    tmp_path,
) -> None:
    output = tmp_path / "posts.csv"
    rows = [
        CsvPost("Are we hiring? Manager A"),
        CsvPost("Are we hiring? Manager B"),
    ]

    assert save_dataclasses(rows, output, "post_text", CsvPost) == 2
    assert len(pd.read_csv(output)) == 2


def test_save_dataclasses_deduplicates_tracking_variants_and_writes_empty_headers(
    tmp_path,
) -> None:
    jobs_output = tmp_path / "jobs.csv"
    jobs = [
        CsvJob("https://www.linkedin.com/jobs/view/123?tracking=one"),
        CsvJob("https://www.linkedin.com/jobs/view/123?tracking=two"),
    ]

    assert save_dataclasses(jobs, jobs_output, "job_url", CsvJob) == 1
    assert len(pd.read_csv(jobs_output)) == 1

    empty_output = tmp_path / "empty.csv"
    assert save_dataclasses([], empty_output, "post_text", CsvPost) == 0
    assert list(pd.read_csv(empty_output).columns) == ["post_text", "post_url"]


def test_human_delay_uses_requested_range(monkeypatch) -> None:
    values: list[tuple[float, float] | float] = []
    monkeypatch.setattr(
        "scraper_common.random.uniform",
        lambda low, high: values.append((low, high)) or 2.25,
    )
    monkeypatch.setattr("scraper_common.time.sleep", values.append)

    human_delay(2.0, 2.5)

    assert values == [(2.0, 2.5), 2.25]


class Element:
    def __init__(
        self,
        text: str = "",
        href: str = "",
        displayed: bool = True,
    ) -> None:
        self.text = text
        self._href = href
        self._displayed = displayed

    def is_displayed(self) -> bool:
        return self._displayed

    def get_attribute(self, name: str) -> str:
        if name == "innerText":
            return self.text
        if name == "href":
            return self._href
        return ""


class JobCard:
    def find_elements(self, _by, selector: str):
        if selector == "a.job-card-list__title--link":
            return [Element("Junior Analyst")]
        if selector == ".artdeco-entity-lockup__subtitle":
            return [Element("Example LLC")]
        if selector == ".artdeco-entity-lockup__caption":
            return [Element("London, United Kingdom")]
        if selector == "a[href*='/jobs/view/']":
            return [Element(href="https://linkedin.com/jobs/view/123")]
        return []

    def get_attribute(self, _name: str) -> str:
        return ""


class PostCard:
    text = (
        "Junior opportunity outside the configured markets. "
        "This deliberately long post has no target seniority or GCC location "
        "but must still be collected for later classification."
    )

    def find_elements(self, _by, _selector: str):
        return []

    def get_attribute(self, _name: str) -> str:
        return ""


def test_login_helpers_select_visible_elements_and_recognise_page_state() -> None:
    hidden = Element(displayed=False)
    visible = Element(displayed=True)

    class Driver:
        current_url = "https://www.linkedin.com/feed/"

        def find_elements(self, _by, value: str):
            return [hidden, visible] if value == "username" else []

    driver = Driver()
    assert _first_element(driver, (("id", "username"),)) is visible
    assert _is_authenticated_page(driver)

    driver.current_url = "https://www.linkedin.com/checkpoint/challenge"
    assert not _is_authenticated_page(driver)


def test_collectors_no_longer_drop_junior_or_off_market_cards() -> None:
    spec = SearchSpec("retail", "retail", "United Arab Emirates")

    job = parse_job_card(None, JobCard(), spec, False, False)
    post = parse_post_card(PostCard(), spec, False)

    assert job is not None
    assert job.seniority_hint == "junior"
    assert post is not None
    assert post.seniority_hint == "junior"

