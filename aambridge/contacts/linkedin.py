"""Parse LinkedIn company About and People list HTML. No profile contact panels."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from urllib.parse import quote_plus, urlparse

from bs4 import BeautifulSoup

from aambridge.contacts.website import canonical_website, is_official_website

ABOUT_LABELS = {
    "website",
    "الموقع الإلكتروني",
    "الموقع الالكتروني",
    "الموقع",
}
PEOPLE_KEYWORDS = ("HR", "Talent", "Recruitment", "People")
PROFILE_HREF = re.compile(r"linkedin\.com/in/", re.IGNORECASE)


def company_about_url(linkedin_url: str) -> str:
    base = (linkedin_url or "").split("?")[0].rstrip("/")
    if not base:
        return ""
    if base.casefold().endswith("/about"):
        return base
    return f"{base}/about"


def company_people_url(linkedin_url: str, keyword: str) -> str:
    base = (linkedin_url or "").split("?")[0].rstrip("/")
    if not base:
        return ""
    root = re.sub(r"/(about|people|life|jobs)/?$", "", base, flags=re.IGNORECASE)
    return f"{root}/people/?keywords={quote_plus(keyword)}"


def parse_linkedin_about_website(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")

    def official(href: str) -> str:
        if href.startswith("/") or "linkedin.com" in urlparse(href).netloc.casefold():
            return ""
        if not is_official_website(href):
            return ""
        return canonical_website(href)

    for node in soup.find_all(["dt", "h2", "h3", "h4", "span", "div"]):
        label = " ".join(node.get_text(" ", strip=True).split()).casefold()
        if label not in ABOUT_LABELS:
            continue
        container = node.parent if node.parent else node
        for anchor in container.find_all("a", href=True):
            found = official(anchor["href"])
            if found:
                return found
        sibling = node.find_next(["dd", "a", "span"])
        if sibling is not None:
            if sibling.name == "a" and sibling.get("href"):
                found = official(sibling["href"])
                if found:
                    return found
            for anchor in sibling.find_all("a", href=True):
                found = official(anchor["href"])
                if found:
                    return found

    for anchor in soup.find_all("a", href=True):
        nearby = " ".join(anchor.parent.get_text(" ", strip=True).split())[:80].casefold()
        if "website" in nearby or "الموقع" in nearby:
            found = official(anchor["href"])
            if found:
                return found
    return ""


def parse_linkedin_people(
    html: str,
    keywords: tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    people: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    needles = tuple(item.casefold() for item in (keywords or ()))

    for card in soup.select(
        "li, .org-people-profile-card, .artdeco-entity-lockup, article"
    ):
        link = card.find("a", href=PROFILE_HREF)
        if link is None:
            continue
        href = (link.get("href") or "").split("?")[0]
        name = ""
        for selector in (
            ".org-people-profile-card__profile-title",
            ".artdeco-entity-lockup__title",
            "h3",
            "a",
        ):
            node = card.select_one(selector)
            if node is not None:
                name = " ".join(node.get_text(" ", strip=True).split())
                if name:
                    break
        if not name:
            name = " ".join(link.get_text(" ", strip=True).split())
        subtitle = card.select_one(
            ".artdeco-entity-lockup__subtitle, .org-people-profile-card__profile-info"
        )
        designation = (
            " ".join(subtitle.get_text(" ", strip=True).split()) if subtitle else ""
        )
        blob = f"{name} {designation}".casefold()
        if needles and not any(item in blob for item in needles):
            continue
        if not name or len(name) < 3:
            continue
        key = (name.casefold(), designation.casefold())
        if key in seen:
            continue
        seen.add(key)
        people.append(
            {
                "name": name,
                "designation": designation or "People team",
                "profile_url": href,
            }
        )
    return people


class LinkedInBrowser:
    """Visible Chrome session for About website + People list only."""

    def __init__(self, pause_seconds: float = 2.0) -> None:
        self.pause_seconds = pause_seconds
        self.driver = None

    def __enter__(self) -> LinkedInBrowser:
        from scraper_common import automatic_login, create_driver

        self.driver = create_driver()
        automatic_login(self.driver)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None

    def _open(self, url: str) -> str:
        assert self.driver is not None
        self.driver.get(url)
        time.sleep(self.pause_seconds)
        return self.driver.page_source

    def about_html(self, linkedin_url: str) -> str:
        url = company_about_url(linkedin_url)
        return self._open(url) if url else ""

    def people_html(self, linkedin_url: str, keyword: str) -> str:
        url = company_people_url(linkedin_url, keyword)
        return self._open(url) if url else ""


def discover_website_with_browser(linkedin_url: str, browser: LinkedInBrowser) -> str:
    return parse_linkedin_about_website(browser.about_html(linkedin_url))


def list_people_with_browser(
    linkedin_url: str,
    browser: LinkedInBrowser,
    keywords: tuple[str, ...] = PEOPLE_KEYWORDS,
    pause: Callable[[float], None] = time.sleep,
) -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for keyword in keywords:
        for person in parse_linkedin_people(
            browser.people_html(linkedin_url, keyword),
            keywords=(keyword,),
        ):
            key = (person["name"].casefold(), person["designation"].casefold())
            if key in seen:
                continue
            seen.add(key)
            collected.append(person)
        pause(browser.pause_seconds)
        if len(collected) >= 8:
            break
    return collected
