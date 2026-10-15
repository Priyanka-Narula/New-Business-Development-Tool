"""Shared browser, text classification, and CSV helpers."""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, fields
from functools import cache
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from aambridge.settings import Settings
from config import (
    EXCLUDED_SENIORITY_TERMS,
    SENIORITY_TERMS,
    all_location_terms,
)

log = logging.getLogger(__name__)
T = TypeVar("T")
JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/?]+-)?(\d+)")
POST_URN_RE = re.compile(r"urn:li:(?:activity|ugcPost|share):(\d+)")


def create_driver() -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-extensions")
    return webdriver.Chrome(options=options)


def manual_login(driver: webdriver.Chrome) -> None:
    driver.get("https://www.linkedin.com/login")
    _finish_login_manually(driver, "Sign in to LinkedIn in the opened Chrome window.")


def _finish_login_manually(
    driver: webdriver.Chrome,
    message: str,
    timeout_seconds: int = 900,
) -> None:
    if message:
        log.info(message)
    log.info("Complete any verification yourself; this scraper does not bypass it.")
    log.info(
        "Sign in in the opened Chrome window. Waiting up to %s seconds for a LinkedIn session.",
        timeout_seconds,
    )
    deadline = time.time() + timeout_seconds
    last_log = 0.0
    while time.time() < deadline:
        if _is_authenticated_page(driver):
            log.info("LinkedIn session is ready.")
            return
        now = time.time()
        if now - last_log >= 15:
            log.info(
                "Waiting for LinkedIn login or verification. Current page: %s",
                driver.current_url,
            )
            last_log = now
        time.sleep(2)
    raise RuntimeError(
        "Timed out waiting for a LinkedIn session. "
        "Sign in in the Chrome window and run the command again."
    )


def _first_element(driver: webdriver.Chrome, locators: tuple[tuple[str, str], ...]):
    for by, value in locators:
        elements = driver.find_elements(by, value)
        for element in elements:
            if element.is_displayed():
                return element
    return None


def _is_authenticated_page(driver: webdriver.Chrome) -> bool:
    url = driver.current_url.lower()
    if not url.startswith("https://www.linkedin.com/"):
        return False
    blocked_paths = ("login", "checkpoint", "challenge", "authwall")
    return not any(path in url for path in blocked_paths)


def automatic_login(driver: webdriver.Chrome) -> None:
    """Log in from settings, falling back to manual login when unavailable."""
    settings = Settings()
    if not settings.has_linkedin_credentials:
        driver.get("https://www.linkedin.com/login")
        _finish_login_manually(
            driver,
            "LINKEDIN_EMAIL and LINKEDIN_PASSWORD are not both configured; "
            "sign in manually in the opened Chrome window.",
        )
        return
    email = settings.linkedin_email
    password = settings.linkedin_password

    driver.get("https://www.linkedin.com/login")
    wait = WebDriverWait(driver, 20)

    # LinkedIn redirects /login to the feed when the browser already has a
    # valid session. In that case there is intentionally no username field.
    if _is_authenticated_page(driver):
        log.info("LinkedIn session is already authenticated.")
        return

    username_locators = (
        (By.ID, "username"),
        (By.NAME, "session_key"),
        (By.CSS_SELECTOR, "input[autocomplete='username']"),
        (By.CSS_SELECTOR, "input[type='email']"),
    )
    password_locators = (
        (By.ID, "password"),
        (By.NAME, "session_password"),
        (By.CSS_SELECTOR, "input[autocomplete='current-password']"),
        (By.CSS_SELECTOR, "input[type='password']"),
    )
    try:
        username_input = wait.until(
            lambda current: _first_element(current, username_locators)
            or (_is_authenticated_page(current) and True)
        )
    except Exception:
        _finish_login_manually(
            driver,
            "The LinkedIn login fields did not load automatically. "
            f"Current page: {driver.current_url}",
        )
        return

    if username_input is True or _is_authenticated_page(driver):
        log.info("LinkedIn session is already authenticated.")
        return

    password_input = _first_element(driver, password_locators)
    if password_input is None:
        _finish_login_manually(
            driver,
            "The password field was not detected. Complete login manually.",
        )
        return

    username_input.clear()
    username_input.send_keys(email)
    password_input.clear()
    password_input.send_keys(password)
    submit = _first_element(
        driver,
        (
            (By.CSS_SELECTOR, "button[type='submit']"),
            (By.CSS_SELECTOR, "input[type='submit']"),
        ),
    )
    if submit is None:
        _finish_login_manually(
            driver,
            "The submit button was not detected. Complete login manually.",
        )
        return
    submit.click()

    try:
        wait.until(
            lambda current: _is_authenticated_page(current)
            or "checkpoint" in current.current_url.lower()
            or "challenge" in current.current_url.lower()
        )
    except Exception:
        _finish_login_manually(
            driver,
            "Automatic submission did not leave the login page. Correct any "
            "message shown by LinkedIn or sign in manually.",
        )
        return

    current = driver.current_url.lower()
    if "checkpoint" in current or "challenge" in current:
        _finish_login_manually(
            driver,
            "LinkedIn requires a verification step before continuing.",
        )


def human_delay(low: float = 1.5, high: float = 3.0) -> None:
    time.sleep(random.uniform(low, high))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


@cache
def _compiled_term_patterns(
    terms: tuple[str, ...],
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple(
        (
            term,
            re.compile(_term_pattern_source(term), re.IGNORECASE),
        )
        for term in terms
    )


def _term_pattern_source(term: str) -> str:
    """Build a boundary-safe pattern, including common Arabic prefixes."""

    escaped = re.escape(term)
    if not re.search(r"[\u0600-\u06ff]", term):
        return rf"(?<!\w){escaped}(?!\w)"

    # Arabic conjunction waw is attached to the following word. Terms that
    # do not already contain the definite article may also appear as ال + term.
    definite_article = "" if term.startswith("ال") else r"(?:ال)?"
    return rf"(?<!\w)(?:و)?{definite_article}{escaped}(?!\w)"


def all_matching_terms(text: str, terms: Iterable[str]) -> list[str]:
    """Return every boundary-safe term match in configured term order."""

    patterns = _compiled_term_patterns(tuple(terms))
    return [term for term, pattern in patterns if pattern.search(text or "")]


def first_matching_term(text: str, terms: Iterable[str]) -> str:
    """Return the first configured matching term, or an empty string."""

    matches = all_matching_terms(text, terms)
    return matches[0] if matches else ""


def detect_locations(text: str) -> list[str]:
    """Return all named GCC location aliases without deciding relevance."""

    return all_matching_terms(text, all_location_terms(include_gcc=True))


def detect_seniority_terms(text: str) -> list[str]:
    """Return all target-seniority terms without deciding relevance."""

    return all_matching_terms(text, SENIORITY_TERMS)


def detect_excluded_terms(text: str) -> list[str]:
    """Return all junior/entry-level terms without dropping the record."""

    return all_matching_terms(text, EXCLUDED_SENIORITY_TERMS)


def strip_query_from_url(value: str) -> str:
    """Strip a query only when ``value`` is an absolute HTTP(S) URL."""

    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return value
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment)
    )


def canonical_source_key(
    row: Mapping[str, Any],
    fallback_field: str,
) -> str:
    """Build a canonical job ID, post URN, or normalised-text SHA-256 key."""

    job_url = str(row.get("job_url") or "")
    job_match = JOB_ID_RE.search(job_url)
    if job_match:
        return job_match.group(1)

    post_url = str(row.get("post_url") or "")
    post_match = POST_URN_RE.search(post_url)
    if post_match:
        return post_match.group(1)

    text = str(
        row.get("post_text")
        or row.get("description")
        or row.get(fallback_field)
        or ""
    )
    text = strip_query_from_url(text)
    normalised = clean_text(text).casefold()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def save_dataclasses(
    rows: list[T],
    output_file: Path,
    dedup_field: str,
    row_type: type[T] | None = None,
) -> int:
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError(
            "pandas failed to import (often Windows Application Control "
            "blocking a pyarrow DLL). The database scrape path does not "
            "need pandas."
        ) from exc
    if not rows:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        columns = [field.name for field in fields(row_type)] if row_type else []
        pd.DataFrame(columns=columns).to_csv(
            output_file, index=False, encoding="utf-8-sig"
        )
        log.warning("No matching rows; wrote an empty CSV to %s.", output_file)
        return 0
    output_file.parent.mkdir(parents=True, exist_ok=True)
    values = [asdict(row) for row in rows]
    frame = pd.DataFrame(values)
    source_keys = pd.Series(
        [canonical_source_key(row, dedup_field) for row in values],
        index=frame.index,
    )
    frame = frame.loc[~source_keys.duplicated()].copy()
    frame.to_csv(output_file, index=False, encoding="utf-8-sig")
    log.info("Saved %s unique rows to %s", len(frame), output_file)
    return len(frame)
