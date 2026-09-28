"""LinkedIn Jobs source adapter and deterministic card parsing."""

from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from aambridge.sources.base import NormalisedRecord
from aambridge.privacy import privacy_safe, redact_personal_contacts
from config import SearchSpec, TIME_FILTER_CODES
from scraper_common import (
    clean_text,
    detect_excluded_terms,
    detect_seniority_terms,
    human_delay,
    save_dataclasses,
)

log = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/?]+-)?(\d+)")
COUNT_RE = re.compile(r"([\d,.]+)\+?\s+(?:jobs?|results?)", re.IGNORECASE)
INTEGER_RE = re.compile(r"(\d[\d,]*)")


class ScanProgress(Protocol):
    def page_loaded(self, cards_seen: int) -> None: ...

    def card_parsed(self) -> None: ...

    def parse_failure(self, selector: str) -> None: ...

    def query_result_count(
        self,
        *,
        source: str,
        keywords: str,
        location: str,
        time_filter: str,
        total_results: int | None,
    ) -> None: ...


@dataclass
class JobLead:
    source: str
    source_key: str
    industry: str
    query: str
    search_location: str
    linkedin_job_id: str
    job_title: str
    company_name: str
    company_linkedin_url: str
    job_location: str
    city: str
    region: str
    country: str
    location_unmatched: bool
    seniority_hint: str
    job_url: str
    description: str
    posted_date: str
    posted_date_precision: str
    posted_date_raw: str
    is_reposted: bool
    is_promoted: bool
    easy_apply: bool
    applicant_count: int | None
    applicant_count_raw: str
    workplace_type: str
    job_criteria: dict[str, str] = field(default_factory=dict)
    nationality_restriction: bool = False
    nationality_restriction_evidence: str = ""
    posted_by_agency: bool = False
    agency_evidence: str = ""
    end_client_hint: dict[str, str] = field(default_factory=dict)


@cache
def _load_yaml(path: str) -> dict[str, Any]:
    value = yaml.safe_load((PROJECT_ROOT / path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return value


def _job_config() -> dict[str, Any]:
    return _load_yaml("config/jobs.yaml")


def _selectors(name: str) -> tuple[str, ...]:
    values = _load_yaml("config/selectors.yaml")["linkedin_jobs"].get(name, [])
    return tuple(str(value) for value in values)


def experience_level_choices() -> tuple[str, ...]:
    return tuple(str(key) for key in _job_config()["experience_levels"])


def experience_level_codes(levels: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    """Translate configured names to LinkedIn codes; empty means all levels."""

    if not levels or "all" in levels:
        return ()
    mapping = _job_config()["experience_levels"]
    unknown = sorted(set(levels) - set(mapping))
    if unknown:
        raise ValueError(
            f"Unknown experience levels: {', '.join(unknown)}. "
            f"Choose from: all, {', '.join(mapping)}"
        )
    return tuple(str(mapping[level]) for level in levels)


def build_jobs_url(
    spec: SearchSpec,
    time_filter: str,
    start: int = 0,
    experience_levels: tuple[str, ...] | list[str] | None = None,
) -> str:
    params = {
        "keywords": spec.keywords,
        "location": spec.location,
        "sortBy": "DD",
        "start": str(start),
    }
    codes = experience_level_codes(experience_levels)
    if codes:
        params["f_E"] = ",".join(codes)
    time_code = TIME_FILTER_CODES[time_filter]
    if time_code:
        params["f_TPR"] = time_code
    return "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)


def _job_cards(driver) -> list[Any]:
    for selector in _selectors("cards"):
        cards = driver.find_elements(By.CSS_SELECTOR, selector)
        if cards:
            return list(cards)
    return []


def _page_state(driver) -> str:
    try:
        text = clean_text(driver.find_element(By.TAG_NAME, "body").text).lower()
    except Exception:
        return ""
    if "no matching jobs found" in text:
        return "no-results"
    if "things aren\u2019t loading" in text or "things aren't loading" in text:
        return "load-error"
    return ""


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


def _all_text(container, selectors: tuple[str, ...]) -> str:
    values: list[str] = []
    for selector in selectors:
        for element in container.find_elements(By.CSS_SELECTOR, selector):
            text = clean_text(element.get_attribute("innerText") or element.text)
            if text and text not in values:
                values.append(text)
    return " | ".join(values)


def _canonical_link(href: str) -> str:
    return (href or "").split("?")[0].rstrip("/")


def _job_identity(card) -> tuple[str, str]:
    for selector in _selectors("job_link"):
        for anchor in card.find_elements(By.CSS_SELECTOR, selector):
            href = anchor.get_attribute("href") or ""
            match = JOB_ID_RE.search(href)
            if match:
                job_id = match.group(1)
                return job_id, f"https://www.linkedin.com/jobs/view/{job_id}"
    for attribute in ("data-occludable-job-id", "data-job-id"):
        job_id = card.get_attribute(attribute) or ""
        if job_id.isdigit():
            return job_id, f"https://www.linkedin.com/jobs/view/{job_id}"
    return "", ""


def _company_url(card) -> str:
    element = _first_element(card, _selectors("company_link"))
    return _canonical_link(element.get_attribute("href")) if element else ""


def _parse_integer(text: str) -> int | None:
    match = INTEGER_RE.search(text or "")
    return int(match.group(1).replace(",", "")) if match else None


def parse_relative_date(
    text: str,
    observed_at: datetime,
) -> tuple[date | None, str]:
    """Parse LinkedIn relative posting text without pretending it is exact."""

    value = clean_text(text).casefold()
    if not value:
        return None, "unknown"
    if value in {"today", "just now", "الآن", "اليوم"}:
        return observed_at.date(), "approx"
    match = re.search(
        r"(\d+)\s*(minute|hour|day|week|month)s?\s+ago",
        value,
    )
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        days = {
            "minute": 0,
            "hour": 0,
            "day": amount,
            "week": amount * 7,
            "month": amount * 30,
        }[unit]
        return (observed_at - timedelta(days=days)).date(), "approx"
    arabic = re.search(
        r"منذ\s*(\d+)\s*(دقيقة|دقائق|ساعة|ساعات|يوم|أيام|أسبوع|أسابيع|شهر|أشهر)",
        value,
    )
    if arabic:
        amount = int(arabic.group(1))
        unit = arabic.group(2)
        days = (
            amount * 30
            if unit in {"شهر", "أشهر"}
            else amount * 7
            if unit in {"أسبوع", "أسابيع"}
            else amount
            if unit in {"يوم", "أيام"}
            else 0
        )
        return (observed_at - timedelta(days=days)).date(), "approx"
    return None, "unknown"


def _posted_date(card, observed_at: datetime) -> tuple[date | None, str, str]:
    element = _first_element(card, _selectors("posted_time"))
    if element is None:
        return None, "unknown", ""
    raw = clean_text(element.get_attribute("innerText") or element.text)
    datetime_value = clean_text(element.get_attribute("datetime") or "")
    if datetime_value:
        try:
            parsed = datetime.fromisoformat(datetime_value.replace("Z", "+00:00"))
            return parsed.date(), "exact", raw or datetime_value
        except ValueError:
            try:
                return date.fromisoformat(datetime_value[:10]), "exact", raw or datetime_value
            except ValueError:
                pass
    parsed_date, precision = parse_relative_date(raw, observed_at)
    return parsed_date, precision, raw


def _normalise_name(value: str) -> str:
    return re.sub(r"[^\w\u0600-\u06ff]+", " ", value.casefold()).strip()


def detect_agency(company_name: str) -> tuple[bool, str]:
    normalised = _normalise_name(company_name)
    for agency in _load_yaml("taxonomies/known_agencies.yaml").get("agencies", []):
        if _normalise_name(str(agency)) in normalised:
            return True, str(agency)
    for term in _job_config().get("agency_terms", []):
        if _normalise_name(str(term)) in normalised:
            return True, str(term)
    return False, ""


def detect_nationality_restriction(text: str) -> tuple[bool, str]:
    for phrase in sorted(
        (str(value) for value in _job_config()["nationality_restriction_phrases"]),
        key=len,
        reverse=True,
    ):
        match = re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE)
        if match:
            return True, match.group(0)
    return False, ""


def detect_end_client_hint(text: str) -> dict[str, str]:
    for phrase in _job_config().get("end_client_phrases", []):
        match = re.search(re.escape(str(phrase)), text, re.IGNORECASE)
        if match:
            tail = clean_text(text[match.start() : match.start() + 180])
            snippet = re.split(r"(?<=[.!?])\s", tail, maxsplit=1)[0]
            return {
                "value": snippet,
                "evidence": match.group(0),
                "basis": "inference",
            }
    return {"value": "", "evidence": "", "basis": "unknown"}


def _alias_match(text: str, alias: str) -> bool:
    return bool(
        re.search(
            rf"(?<!\w){re.escape(alias)}(?!\w)",
            text,
            re.IGNORECASE,
        )
    )


def parse_location(value: str) -> dict[str, Any]:
    """Map only configured aliases; unmatched values are never guessed."""

    countries: Mapping[str, Any] = _load_yaml("taxonomies/locations.yaml")[
        "countries"
    ]
    for country_name, country_data in countries.items():
        regions: Mapping[str, Any] = country_data.get("regions", {})
        for region_name, region_data in regions.items():
            cities: Mapping[str, list[str]] = region_data.get("cities", {})
            for city_name, aliases in cities.items():
                if any(_alias_match(value, str(alias)) for alias in aliases):
                    return {
                        "city": city_name,
                        "region": region_name,
                        "country": country_name,
                        "location_unmatched": False,
                    }
        for region_name, region_data in regions.items():
            if any(
                _alias_match(value, str(alias))
                for alias in region_data.get("aliases", [])
            ):
                return {
                    "city": "",
                    "region": region_name,
                    "country": country_name,
                    "location_unmatched": False,
                }
        if any(
            _alias_match(value, str(alias))
            for alias in country_data.get("aliases", [])
        ):
            return {
                "city": "",
                "region": "",
                "country": country_name,
                "location_unmatched": False,
            }
    return {"city": "", "region": "", "country": "", "location_unmatched": True}


def _parse_criteria(texts: list[str]) -> dict[str, str]:
    criteria: dict[str, str] = {}
    labels = {
        "seniority level": "seniority_level",
        "employment type": "employment_type",
        "job function": "job_function",
        "industries": "industries",
    }
    for text in texts:
        normalised = clean_text(text)
        lower = normalised.casefold()
        for label, key in labels.items():
            if label in lower:
                value = re.sub(label, "", normalised, count=1, flags=re.IGNORECASE)
                criteria[key] = clean_text(value.lstrip(" :-|"))
                break
    return criteria


def _description_and_criteria(driver, card) -> tuple[str, dict[str, str]]:
    try:
        driver.execute_script("arguments[0].click()", card)
        WebDriverWait(driver, 8).until(
            lambda current: _first_element(current, _selectors("description"))
        )
        description = _first_text(driver, _selectors("description"))
        criterion_texts: list[str] = []
        for selector in _selectors("criteria_items"):
            for element in driver.find_elements(By.CSS_SELECTOR, selector):
                text = clean_text(element.get_attribute("innerText") or element.text)
                if text:
                    criterion_texts.append(text)
        return description, _parse_criteria(criterion_texts)
    except Exception:
        return "", {}


def _parse_result_count(driver) -> int | None:
    for selector in _selectors("result_count"):
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            text = clean_text(element.get_attribute("innerText") or element.text)
            match = COUNT_RE.search(text)
            if match:
                return int(match.group(1).replace(",", "").replace(".", ""))
    return None


def _parse_card(
    driver,
    card,
    spec: SearchSpec,
    include_gcc: bool,
    include_description: bool,
    progress: ScanProgress | None = None,
    observed_at: datetime | None = None,
) -> JobLead | None:
    del include_gcc  # Location is recorded, never filtered at collection time.
    observed_at = observed_at or datetime.now(UTC)
    title = _first_text(card, _selectors("title"))
    company = _first_text(card, _selectors("company_name"))
    company_url = _company_url(card)
    location = _first_text(card, _selectors("location"))
    job_id, url = _job_identity(card)
    if not title:
        if progress is not None:
            progress.parse_failure("job.title")
        return None
    if not url:
        if progress is not None:
            progress.parse_failure("job.url")
        return None

    metadata = _all_text(card, _selectors("metadata"))
    posted_date, posted_precision, posted_raw = _posted_date(card, observed_at)
    description, criteria = (
        _description_and_criteria(driver, card)
        if include_description and driver is not None
        else ("", {})
    )
    description = redact_personal_contacts(description)
    criteria = privacy_safe(criteria)
    combined_text = clean_text(f"{title} {company} {location} {metadata} {description}")
    seniority_terms = detect_seniority_terms(combined_text)
    excluded_terms = detect_excluded_terms(combined_text)
    seniority = (
        seniority_terms[0]
        if seniority_terms
        else excluded_terms[0] if excluded_terms else ""
    )
    parsed_location = parse_location(location)
    is_agency, agency_evidence = detect_agency(company)
    nationality, nationality_evidence = detect_nationality_restriction(
        combined_text
    )
    applicant_raw = ""
    applicant_match = re.search(
        r"(?:over\s+)?\d[\d,]*\+?\s+applicants?",
        metadata,
        re.IGNORECASE,
    )
    if applicant_match:
        applicant_raw = applicant_match.group(0)
    workplace = next(
        (
            label
            for label in ("On-site", "Hybrid", "Remote")
            if re.search(rf"\b{re.escape(label)}\b", metadata, re.IGNORECASE)
        ),
        "",
    )
    return JobLead(
        source="job",
        source_key=job_id,
        industry=spec.sector,
        query=spec.keywords,
        search_location=spec.location,
        linkedin_job_id=job_id,
        job_title=title,
        company_name=company,
        company_linkedin_url=company_url,
        job_location=location,
        **parsed_location,
        seniority_hint=seniority,
        job_url=url,
        description=description,
        posted_date=posted_date.isoformat() if posted_date else "",
        posted_date_precision=posted_precision,
        posted_date_raw=posted_raw,
        is_reposted=bool(re.search(r"\breposted\b", metadata, re.IGNORECASE)),
        is_promoted=bool(re.search(r"\bpromoted\b", metadata, re.IGNORECASE)),
        easy_apply=bool(re.search(r"\beasy apply\b", metadata, re.IGNORECASE)),
        applicant_count=_parse_integer(applicant_raw),
        applicant_count_raw=applicant_raw,
        workplace_type=workplace,
        job_criteria=criteria,
        nationality_restriction=nationality,
        nationality_restriction_evidence=nationality_evidence,
        posted_by_agency=is_agency,
        agency_evidence=agency_evidence,
        end_client_hint=detect_end_client_hint(combined_text),
    )


class LinkedInJobsAdapter:
    """Yield normalized LinkedIn Jobs records without collection-time filtering."""

    def __init__(
        self,
        *,
        driver,
        specs: list[SearchSpec],
        max_per_query: int,
        time_filter: str,
        include_gcc: bool,
        include_description: bool,
        experience_levels: tuple[str, ...] | list[str] | None = None,
        progress: ScanProgress | None = None,
    ) -> None:
        self.driver = driver
        self.specs = specs
        self.max_per_query = max_per_query
        self.time_filter = time_filter
        self.include_gcc = include_gcc
        self.include_description = include_description
        self.experience_levels = experience_levels
        self.progress = progress

    def records(self) -> Iterator[NormalisedRecord]:
        seen: set[str] = set()
        for number, spec in enumerate(self.specs, start=1):
            log.info(
                "[jobs %s/%s] %s | %s",
                number,
                len(self.specs),
                spec.sector,
                spec.location,
            )
            accepted = 0
            page = 0
            while accepted < self.max_per_query and page < 4:
                self.driver.get(
                    build_jobs_url(
                        spec,
                        self.time_filter,
                        start=page * 25,
                        experience_levels=self.experience_levels,
                    )
                )
                try:
                    WebDriverWait(self.driver, 15).until(
                        lambda current: _job_cards(current) or _page_state(current)
                    )
                except Exception:
                    log.warning("Job results timed out at %s", self.driver.current_url)
                    break

                state = _page_state(self.driver)
                if state == "load-error":
                    log.warning("LinkedIn reported a loading error; refreshing once.")
                    self.driver.refresh()
                    try:
                        WebDriverWait(self.driver, 15).until(
                            lambda current: _job_cards(current)
                            or _page_state(current) == "no-results"
                        )
                    except Exception:
                        log.warning("Job results still failed after refresh.")
                        break

                cards = _job_cards(self.driver)
                if self.progress is not None:
                    self.progress.page_loaded(len(cards))
                    if page == 0:
                        self.progress.query_result_count(
                            source="linkedin_job",
                            keywords=spec.keywords,
                            location=spec.location,
                            time_filter=self.time_filter,
                            total_results=_parse_result_count(self.driver),
                        )
                if not cards:
                    if self.progress is not None and state != "no-results":
                        self.progress.parse_failure("job.card")
                    log.info(
                        "LinkedIn returned no matches for %s | %s",
                        spec.sector,
                        spec.location,
                    )
                    break

                new_on_page = 0
                for card in cards:
                    if accepted >= self.max_per_query:
                        break
                    observed_at = datetime.now(UTC)
                    try:
                        lead = _parse_card(
                            self.driver,
                            card,
                            spec,
                            self.include_gcc,
                            self.include_description,
                            self.progress,
                            observed_at,
                        )
                    except StaleElementReferenceException:
                        if self.progress is not None:
                            self.progress.parse_failure("job.card.stale")
                        continue
                    if not lead:
                        continue
                    if self.progress is not None:
                        self.progress.card_parsed()
                    if lead.source_key in seen:
                        continue
                    seen.add(lead.source_key)
                    accepted += 1
                    new_on_page += 1
                    fields = asdict(lead)
                    raw_text = clean_text(
                        " ".join(
                            str(fields.get(key, ""))
                            for key in (
                                "job_title",
                                "company_name",
                                "job_location",
                                "description",
                            )
                        )
                    )
                    source_key = lead.source_key or hashlib.sha256(
                        raw_text.casefold().encode("utf-8")
                    ).hexdigest()
                    yield NormalisedRecord(
                        source="linkedin_job",
                        source_key=source_key,
                        source_url=lead.job_url,
                        observed_at=observed_at,
                        raw_text=raw_text,
                        raw_fields=fields,
                    )
                    if self.include_description:
                        human_delay(0.8, 1.5)
                if new_on_page == 0:
                    break
                page += 1
                human_delay(2.5, 4.5)


def scrape_jobs(
    driver,
    specs: list[SearchSpec],
    max_per_query: int,
    time_filter: str,
    include_gcc: bool,
    include_description: bool,
    output_file: Path | None = None,
    on_record: Callable[[NormalisedRecord], bool] | None = None,
    progress: ScanProgress | None = None,
    experience_levels: tuple[str, ...] | list[str] | None = None,
) -> int:
    adapter = LinkedInJobsAdapter(
        driver=driver,
        specs=specs,
        max_per_query=max_per_query,
        time_filter=time_filter,
        include_gcc=include_gcc,
        include_description=include_description,
        experience_levels=experience_levels,
        progress=progress,
    )
    rows: list[JobLead] = []
    saved_total = 0
    for record in adapter.records():
        if on_record is not None:
            if on_record(record):
                saved_total += 1
            continue
        rows.append(JobLead(**record.raw_fields))
        saved_total += 1

    if on_record is not None:
        return saved_total
    if output_file is None:
        return saved_total
    return save_dataclasses(rows, output_file, "job_url", JobLead)
