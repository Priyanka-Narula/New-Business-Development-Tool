"""LinkedIn content/post source adapter."""

from __future__ import annotations

import logging
import re
import urllib.parse
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from aambridge.privacy import has_contact_email, redact_personal_contacts
from aambridge.sources.base import NormalisedRecord
from aambridge.sources.linkedin_jobs import detect_agency, parse_relative_date
from aambridge.sources.stale import parse_indexed_card
from config import SearchSpec
from scraper_common import (
    canonical_source_key,
    clean_text,
    detect_excluded_terms,
    detect_locations,
    detect_seniority_terms,
    human_delay,
    save_dataclasses,
)

log = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
URN_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):(\d+)")
POST_ACTIVITY_RE = re.compile(r"(?:activity|ugcPost|share)[-_:](\d+)")


class ScanProgress(Protocol):
    def page_loaded(self, cards_seen: int) -> None: ...

    def card_parsed(self) -> None: ...

    def parse_failure(self, selector: str) -> None: ...


@dataclass
class PostLead:
    source: str
    source_key: str
    industry: str
    query: str
    query_set: str
    search_location: str
    job_title: str
    has_contact_email: bool
    post_text: str
    post_url: str
    author_name: str
    author_url: str
    author_headline: str
    author_type: str
    author_is_agency: bool
    agency_evidence: str
    post_date: str
    post_date_precision: str
    post_date_raw: str
    post_category: str
    location_hint: str
    seniority_hint: str


@cache
def _load_yaml(path: str) -> dict[str, Any]:
    value = yaml.safe_load((PROJECT_ROOT / path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return value


def _selectors(name: str) -> tuple[str, ...]:
    values = _load_yaml("config/selectors.yaml")["linkedin_posts"].get(name, [])
    return tuple(str(value) for value in values)


def build_content_url(spec: SearchSpec, time_filter: str) -> str:
    params = {
        "keywords": spec.keywords,
        "locationFallback": spec.location,
        "origin": "GLOBAL_SEARCH_HEADER",
        "sortBy": "date_posted",
    }
    if time_filter != "any":
        params["datePosted"] = {
            "day": "past-24h",
            "week": "past-week",
            "month": "past-month",
        }[time_filter]
    return "https://www.linkedin.com/search/results/content/?" + urllib.parse.urlencode(params)


def _containers(driver) -> list[Any]:
    for selector in _selectors("cards"):
        cards = [
            element
            for element in driver.find_elements(By.CSS_SELECTOR, selector)
            if len(clean_text(element.text)) >= 40
        ]
        if cards:
            return cards
    return []


def _first_element(container, selectors: tuple[str, ...]):
    for selector in selectors:
        elements = container.find_elements(By.CSS_SELECTOR, selector)
        if elements:
            return elements[0]
    return None


def _first_text(container, selectors: tuple[str, ...]) -> str:
    element = _first_element(container, selectors)
    if element is None:
        return ""
    return clean_text(element.get_attribute("innerText") or element.text)


def _canonical_link(value: str) -> str:
    return (value or "").split("?")[0].rstrip("/")


def _post_identity(card) -> tuple[str, str]:
    candidates = [card]
    candidates.extend(card.find_elements(By.CSS_SELECTOR, "[data-urn]"))
    for element in candidates:
        urn = element.get_attribute("data-urn") or ""
        match = URN_RE.search(urn)
        if match:
            activity_id = match.group(1)
            return (
                activity_id,
                f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}",
            )
    for selector in _selectors("post_link"):
        for anchor in card.find_elements(By.CSS_SELECTOR, selector):
            href = anchor.get_attribute("href") or ""
            match = URN_RE.search(href) or POST_ACTIVITY_RE.search(href)
            if match:
                return match.group(1), _canonical_link(href)
            if "/posts/" in href:
                return "", _canonical_link(href)
    return "", ""


def _title(text: str) -> str:
    """Extract a role only when explicit hiring language is present."""

    patterns = (
        r"(?:we are hiring|now hiring|hiring|vacancy|job opening)"
        r"(?:\s+for)?\s*[:\-–]?\s*([^\n|.!]{3,100})",
        r"(?:position|role)\s*[:\-–]\s*([^\n|.!]{3,100})",
        r"(?:مطلوب|نبحث عن)\s*[:\-–]?\s*([^\n|.!،]{3,100})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return clean_text(match.group(1))[:120]
    return ""


def categorise_post(text: str) -> str:
    categories = _load_yaml("config/posts.yaml")["categories"]
    for category in (
        "project_award",
        "expansion",
        "leadership_change",
        "hiring",
    ):
        for phrase in categories.get(category, []):
            if re.search(
                rf"(?<!\w){re.escape(str(phrase))}(?!\w)",
                text,
                re.IGNORECASE,
            ):
                return category
    return "other"


def _author(card) -> tuple[str, str, str, str]:
    name = _first_text(card, _selectors("author_name"))
    headline = _first_text(card, _selectors("author_headline"))
    element = _first_element(card, _selectors("author_link"))
    url = _canonical_link(element.get_attribute("href")) if element else ""
    author_type = (
        "company"
        if "/company/" in url
        else "person"
        if "/in/" in url
        else "unknown"
    )
    return name, url, headline, author_type


def _post_date(card, observed_at: datetime) -> tuple[date | None, str, str]:
    element = _first_element(card, _selectors("posted_time"))
    if element is None:
        return None, "unknown", ""
    raw = clean_text(element.get_attribute("innerText") or element.text)
    exact = clean_text(element.get_attribute("datetime") or "")
    if exact:
        try:
            parsed = datetime.fromisoformat(exact.replace("Z", "+00:00"))
            return parsed.date(), "exact", raw or exact
        except ValueError:
            try:
                return date.fromisoformat(exact[:10]), "exact", raw or exact
            except ValueError:
                pass
    compact = re.fullmatch(r"(\d+)\s*(m|h|d|w|mo)", raw.casefold())
    relative = (
        f"{compact.group(1)} "
        f"{'month' if compact.group(2) == 'mo' else {'m': 'minute', 'h': 'hour', 'd': 'day', 'w': 'week'}[compact.group(2)]} ago"
        if compact
        else raw
    )
    parsed_date, precision = parse_relative_date(relative, observed_at)
    return parsed_date, precision, raw


def _parse_card(
    card,
    spec: SearchSpec,
    include_gcc: bool,
    progress: ScanProgress | None = None,
    observed_at: datetime | None = None,
) -> PostLead | None:
    del include_gcc  # Detection records all configured aliases without filtering.
    observed_at = observed_at or datetime.now(UTC)
    visible_text = _first_text(card, _selectors("post_text")) or clean_text(card.text)
    if len(visible_text) < 20:
        if progress is not None:
            progress.parse_failure("post.text")
        return None

    contact_email = has_contact_email(visible_text)
    text = clean_text(redact_personal_contacts(visible_text))
    source_key, post_url = _post_identity(card)
    name, author_url, headline, author_type = _author(card)
    author_is_agency, agency_evidence = detect_agency(f"{name} {headline}")
    post_date, precision, date_raw = _post_date(card, observed_at)
    locations = detect_locations(text)
    seniority_terms = detect_seniority_terms(text)
    excluded_terms = detect_excluded_terms(text)
    seniority = (
        seniority_terms[0]
        if seniority_terms
        else excluded_terms[0] if excluded_terms else ""
    )
    provisional = {
        "post_url": post_url,
        "post_text": text,
    }
    source_key = source_key or canonical_source_key(provisional, "post_text")
    return PostLead(
        source="post",
        source_key=source_key,
        industry=spec.sector,
        query=spec.keywords,
        query_set=spec.query_set,
        search_location=spec.location,
        job_title=_title(visible_text),
        has_contact_email=contact_email,
        post_text=text,
        post_url=post_url,
        author_name=name,
        author_url=author_url,
        author_headline=headline,
        author_type=author_type,
        author_is_agency=author_is_agency,
        agency_evidence=agency_evidence,
        post_date=post_date.isoformat() if post_date else "",
        post_date_precision=precision,
        post_date_raw=date_raw,
        post_category=categorise_post(visible_text),
        location_hint=locations[0] if locations else "",
        seniority_hint=seniority,
    )


class LinkedInPostsAdapter:
    """Yield normalized LinkedIn post records without relevance filtering."""

    def __init__(
        self,
        *,
        driver,
        specs: list[SearchSpec],
        max_per_query: int,
        time_filter: str,
        include_gcc: bool,
        progress: ScanProgress | None = None,
    ) -> None:
        self.driver = driver
        self.specs = specs
        self.max_per_query = max_per_query
        self.time_filter = time_filter
        self.include_gcc = include_gcc
        self.progress = progress

    def records(self) -> Iterator[NormalisedRecord]:
        seen: set[str] = set()
        for number, spec in enumerate(self.specs, start=1):
            log.info(
                "[posts %s/%s] %s | %s | %s",
                number,
                len(self.specs),
                spec.query_set,
                spec.sector,
                spec.location,
            )
            self.driver.get(build_content_url(spec, self.time_filter))
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "main"))
                )
            except Exception:
                log.warning("Content results did not load for this query.")

            accepted = 0
            stalled = 0
            previous_count = 0
            page_recorded = False
            while accepted < self.max_per_query and stalled < 3:
                cards = _containers(self.driver)
                if self.progress is not None and not page_recorded:
                    self.progress.page_loaded(len(cards))
                    page_recorded = True
                if not cards and previous_count == 0 and stalled == 0:
                    if self.progress is not None:
                        self.progress.parse_failure("post.card")
                    log.warning("No post cards detected at %s", self.driver.current_url)
                page_cards = cards
                for index in range(len(page_cards)):
                    if accepted >= self.max_per_query:
                        break
                    observed_at = datetime.now(UTC)
                    try:
                        lead = parse_indexed_card(
                            cards=page_cards,
                            index=index,
                            refetch=lambda: _containers(self.driver),
                            parse=lambda card: _parse_card(
                                card,
                                spec,
                                self.include_gcc,
                                self.progress,
                                observed_at,
                            ),
                        )
                    except StaleElementReferenceException:
                        if self.progress is not None:
                            self.progress.parse_failure("post.card.stale")
                        log.info("Gave up on stale post card at index %s", index)
                        continue
                    if not lead:
                        continue
                    if self.progress is not None:
                        self.progress.card_parsed()
                    if lead.source_key in seen:
                        continue
                    seen.add(lead.source_key)
                    accepted += 1
                    fields = asdict(lead)
                    yield NormalisedRecord(
                        source="linkedin_post",
                        source_key=lead.source_key,
                        source_url=lead.post_url,
                        observed_at=observed_at,
                        raw_text=lead.post_text,
                        raw_fields=fields,
                    )

                self.driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight)"
                )
                human_delay(1.8, 3.3)
                current_count = len(_containers(self.driver))
                stalled = stalled + 1 if current_count <= previous_count else 0
                previous_count = current_count
            human_delay(2.5, 4.5)


def scrape_posts(
    driver,
    specs: list[SearchSpec],
    max_per_query: int,
    time_filter: str,
    include_gcc: bool,
    output_file: Path | None = None,
    on_record: Callable[[NormalisedRecord], bool] | None = None,
    progress: ScanProgress | None = None,
) -> int:
    adapter = LinkedInPostsAdapter(
        driver=driver,
        specs=specs,
        max_per_query=max_per_query,
        time_filter=time_filter,
        include_gcc=include_gcc,
        progress=progress,
    )
    rows: list[PostLead] = []
    saved_total = 0
    for record in adapter.records():
        if on_record is not None:
            if on_record(record):
                saved_total += 1
            continue
        rows.append(PostLead(**record.raw_fields))
        saved_total += 1

    if on_record is not None:
        return saved_total
    if output_file is None:
        return saved_total
    return save_dataclasses(rows, output_file, "post_text", PostLead)
