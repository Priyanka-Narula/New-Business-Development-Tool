"""Parse official company pages for public contact channels and named roles."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag

from aambridge.decision_makers import rank_designation
from aambridge.privacy import EMAIL_RE, PHONE_RE

log = logging.getLogger(__name__)
USER_AGENT = "AAMBRIDGEContactEnrichment/1.0 (+https://localhost)"
MAX_PAGES = 8
FETCH_TIMEOUT = 15
MAX_HTML_BYTES = 2_000_000
SEARCH_URL = "https://html.duckduckgo.com/html/"
SEARCH_API = "https://api.duckduckgo.com/"

PAGE_LABELS = (
    ("website_contact", ("contact", "contact-us", "contactus", "get-in-touch", "getintouch", "reach-us", "تواصل", "اتصل")),
    ("website_careers", ("career", "careers", "jobs", "join-us", "joinus", "work-with-us", "وظائف", "التوظيف")),
    ("website_leadership", ("leadership", "team", "our-team", "management", "executive", "فريق")),
    ("website_about", ("about", "about-us", "aboutus", "من-نحن")),
)
SKIP_HOST_SUFFIXES = (
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "youtube.com",
    "wa.me",
    "duckduckgo.com",
)
DIRECTORY_HOST_SUFFIXES = (
    "crunchbase.com",
    "zoominfo.com",
    "indeed.com",
    "glassdoor.com",
    "bayt.com",
    "wikipedia.org",
    "dnb.com",
    "bloomberg.com",
    "rocketreach.co",
    "apollo.io",
    "yellowpages.com",
    "naukrigulf.com",
    "owler.com",
)
NAME_STOPWORDS = {
    "the",
    "and",
    "of",
    "for",
    "ltd",
    "limited",
    "llc",
    "inc",
    "plc",
    "gmbh",
    "co",
    "company",
    "group",
    "holding",
    "holdings",
    "international",
    "corporation",
    "corp",
    "pjsc",
}
COUNTRY_TLDS = {
    "united arab emirates": (".ae",),
    "uae": (".ae",),
    "saudi arabia": (".sa", ".com.sa"),
    "qatar": (".qa",),
    "malaysia": (".my", ".com.my"),
    "australia": (".au", ".com.au"),
    "united kingdom": (".uk", ".co.uk"),
    "uk": (".uk", ".co.uk"),
    "germany": (".de",),
}
ORG_EMAIL_LOCAL = re.compile(
    r"^(hr|humanresources|talent|careers?|recruit(?:ment|ing)?|jobs|info|contact|hello|admin|enquir(?:y|ies)|reception)$",
    re.IGNORECASE,
)
ADDRESS_HINT = re.compile(
    r"(?:p\.?\s*o\.?\s*box|po box|pobox|street|road|avenue|office|floor|building|صندوق|ص\.?\s*ب)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExtractedChannel:
    kind: str
    value: str
    label: str
    is_organizational: bool
    source: str


@dataclass(frozen=True)
class ExtractedPerson:
    name: str
    designation: str
    email: str = ""
    phone: str = ""
    profile_url: str = ""


def canonical_website(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def is_official_website(value: str) -> bool:
    parsed = urlparse(canonical_website(value) or value)
    host = (parsed.netloc or "").casefold().split(":")[0]
    if not host or host.endswith("linkedin.com"):
        return False
    return parsed.scheme in {"http", "https"}


def _host(url: str) -> str:
    host = urlparse(canonical_website(url) or url).netloc.casefold().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _host_blocked(url: str) -> bool:
    host = _host(url)
    blocked = SKIP_HOST_SUFFIXES + DIRECTORY_HOST_SUFFIXES
    return any(host == suffix or host.endswith("." + suffix) for suffix in blocked)


def _name_tokens(name: str) -> tuple[str, ...]:
    parts = re.findall(r"[a-z0-9]{3,}", (name or "").casefold())
    return tuple(part for part in parts if part not in NAME_STOPWORDS)


def _registrable_label(host: str) -> str:
    multi = (".co.uk", ".com.au", ".com.sa", ".com.my", ".co.ae", ".org.uk")
    for suffix in multi:
        if host.endswith(suffix):
            remainder = host[: -len(suffix)]
            return remainder.rsplit(".", 1)[-1] if remainder else ""
    if "." not in host:
        return host
    return host.rsplit(".", 1)[0].rsplit(".", 1)[-1]


def _has_country_tld(host: str, country: str) -> bool:
    suffixes = COUNTRY_TLDS.get((country or "").strip().casefold(), ())
    return any(host.endswith(suffix if suffix.startswith(".") else "." + suffix) for suffix in suffixes)


def _unwrap_search_url(href: str) -> str:
    text = (href or "").strip()
    if text.startswith("//"):
        text = "https:" + text
    parsed = urlparse(text)
    encoded = parse_qs(parsed.query).get("uddg")
    if encoded:
        return unquote(encoded[0])
    return text


def _search_query(name: str, country: str) -> str:
    title = re.sub(r"\s+", " ", (name or "").strip())
    if not title:
        return ""
    place = re.sub(r"\s+", " ", (country or "").strip())
    if place:
        return f'"{title}" official website {place}'
    return f'"{title}" official website'


def _score_candidate(url: str, tokens: tuple[str, ...], country: str) -> int:
    host = _host(url)
    label = _registrable_label(host).replace("-", "")
    if not tokens or not label:
        return 0
    overlap = sum(1 for token in tokens if token in label)
    concatenated = "".join(tokens)
    if concatenated and concatenated in label:
        overlap = max(overlap, len(tokens))
    if overlap == 0:
        return 0
    score = overlap * 10
    if _has_country_tld(host, country):
        score += 2
    return score


def parse_search_result_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    found: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        canonical = canonical_website(_unwrap_search_url(anchor["href"]))
        if not canonical or not is_official_website(canonical) or _host_blocked(canonical):
            continue
        key = canonical.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(canonical)
    return found


def _extract_infobox_url(value: str) -> str:
    text = (value or "").strip()
    linked = re.search(r"\((https?://[^)]+)\)", text)
    if linked:
        return canonical_website(linked.group(1))
    labelled = re.search(r"\[([^\]]+)\]", text)
    if labelled:
        return canonical_website(labelled.group(1))
    return canonical_website(text)


def parse_ddg_instant_urls(payload: dict) -> list[str]:
    found: list[str] = []
    for result in payload.get("Results") or []:
        if not isinstance(result, dict):
            continue
        found.append(str(result.get("FirstURL") or ""))
    infobox = payload.get("Infobox") or {}
    content = infobox.get("content") if isinstance(infobox, dict) else None
    for item in content or []:
        if not isinstance(item, dict):
            continue
        label = (item.get("label") or "").casefold()
        if any(word in label for word in ("website", "official")):
            found.append(_extract_infobox_url(str(item.get("value") or "")))
    urls: list[str] = []
    seen: set[str] = set()
    for raw in found:
        canonical = canonical_website(raw)
        if not canonical or not is_official_website(canonical) or _host_blocked(canonical):
            continue
        key = canonical.casefold()
        if key in seen:
            continue
        seen.add(key)
        urls.append(canonical)
    return urls


def default_fetch_search(query: str) -> str:
    response = requests.get(
        SEARCH_URL,
        params={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=FETCH_TIMEOUT,
    )
    response.raise_for_status()
    return response.text[:MAX_HTML_BYTES]


def default_fetch_ddg_json(query: str) -> dict:
    response = requests.get(
        SEARCH_API,
        params={
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
            "t": "aambridge",
        },
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        },
        timeout=FETCH_TIMEOUT,
    )
    if response.status_code >= 400:
        response.raise_for_status()
    text = (response.text or "").strip()
    if not text:
        return {}
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _guessed_homepages(name: str, country: str) -> list[str]:
    tokens = _name_tokens(name)
    if not tokens:
        return []
    slugs = ["".join(tokens), "-".join(tokens)]
    if tokens[0] not in slugs:
        slugs.append(tokens[0])
    tlds = [".com", *COUNTRY_TLDS.get((country or "").strip().casefold(), ())]
    found: list[str] = []
    seen: set[str] = set()
    for slug in slugs:
        compact = re.sub(r"[^a-z0-9-]", "", slug.casefold())[:40]
        if len(compact) < 4:
            continue
        for tld in tlds:
            suffix = tld if tld.startswith(".") else f".{tld}"
            for raw in (f"https://www.{compact}{suffix}", f"https://{compact}{suffix}"):
                canonical = canonical_website(raw)
                if not canonical:
                    continue
                key = canonical.casefold()
                if key in seen:
                    continue
                seen.add(key)
                found.append(canonical)
                if len(found) >= 4:
                    return found
    return found


def _pick_scored_url(
    urls: list[str],
    tokens: tuple[str, ...],
    country: str,
) -> str:
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    for url in urls:
        key = url.casefold()
        if key in seen:
            continue
        seen.add(key)
        score = _score_candidate(url, tokens, country)
        if score <= 0:
            continue
        scored.append((score, url))
    if not scored:
        return ""
    scored.sort(key=lambda item: (-item[0], len(item[1])))
    return scored[0][1]


def _probe_homepage(url: str) -> str:
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=8,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            return ""
        return response.text[:MAX_HTML_BYTES]
    except Exception:
        return ""


def _homepage_mentions_company(url: str, tokens: tuple[str, ...]) -> bool:
    html = _probe_homepage(url)
    if not html:
        return False
    blob = html.casefold()
    return any(token in blob for token in tokens)


def discover_official_website(
    name: str,
    country: str = "",
    fetch_search: Callable[[str], str] | None = None,
) -> str:
    """Pick an official site from public search. Empty when confidence is low."""

    title = re.sub(r"\s+", " ", (name or "").strip())
    if not title:
        log.info("Website search skipped: empty company name")
        return ""
    query = _search_query(name, country)
    tokens = _name_tokens(name)
    urls: list[str] = []
    if fetch_search is not None:
        try:
            urls.extend(parse_search_result_urls(fetch_search(query)))
        except Exception:
            log.exception("Website search failed. query=%s", query)
            return ""
    else:
        try:
            urls.extend(parse_ddg_instant_urls(default_fetch_ddg_json(title)))
        except Exception:
            log.exception("Website search API failed. query=%s", title)
    chosen = _pick_scored_url(urls, tokens, country)
    if chosen:
        log.info("Website search chose %s query=%s", chosen, query)
        return chosen
    if fetch_search is None:
        guesses = [
            url
            for url in _guessed_homepages(name, country)
            if _score_candidate(url, tokens, country) > 0
            and _homepage_mentions_company(url, tokens)
        ]
        chosen = _pick_scored_url(guesses, tokens, country)
        if chosen:
            log.info("Website guess chose %s query=%s", chosen, query)
            return chosen
    log.info("Website search skipped (no confident match). query=%s", query)
    return ""


def normalise_channel_value(kind: str, value: str) -> str:
    text = (value or "").strip()
    if kind == "email":
        return text.casefold()
    if kind == "phone":
        digits = re.sub(r"\D+", "", text)
        if text.strip().startswith("+"):
            return "+" + digits
        return digits
    if kind == "page_url":
        return canonical_website(text).casefold()
    return re.sub(r"\s+", " ", text).casefold()


def classify_email(address: str) -> tuple[str, bool]:
    local = address.split("@", 1)[0].split("+", 1)[0]
    token = re.sub(r"[^a-z0-9]", "", local.casefold())
    if token.startswith("hr") or token in {"humanresources", "hrdept"}:
        return "hr", True
    if any(token.startswith(part) for part in ("career", "job", "recruit", "talent")):
        return "careers", True
    if token in {"info", "hello", "admin", "enquiry", "enquiries", "reception"}:
        return "info", True
    if token == "contact":
        return "contact", True
    if ORG_EMAIL_LOCAL.match(token):
        return "info", True
    return "personal", False


def classify_phone(text: str, nearby: str = "") -> str:
    blob = f"{text} {nearby}".casefold()
    if any(word in blob for word in ("hr", "human resource", "talent", "recruit")):
        return "hr"
    if any(word in blob for word in ("career", "job")):
        return "careers"
    return "switchboard"


def page_source_label(url: str) -> str:
    path = urlparse(url).path.casefold()
    for label, tokens in PAGE_LABELS:
        if any(token in path for token in tokens):
            return label
    return "website_home"


def _visible_text(node: Tag | BeautifulSoup) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True))


def extract_channels(html: str, page_url: str) -> list[ExtractedChannel]:
    soup = BeautifulSoup(html or "", "html.parser")
    source = page_source_label(page_url)
    found: list[ExtractedChannel] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, label: str, organizational: bool) -> None:
        cleaned = value.strip()
        key = (kind, normalise_channel_value(kind, cleaned))
        if not cleaned or not key[1] or key in seen:
            return
        seen.add(key)
        found.append(
            ExtractedChannel(
                kind=kind,
                value=cleaned,
                label=label,
                is_organizational=organizational,
                source=source,
            )
        )

    for anchor in soup.select("a[href]"):
        href = (anchor.get("href") or "").strip()
        if href.startswith("mailto:"):
            address = href.split(":", 1)[1].split("?", 1)[0]
            if EMAIL_RE.fullmatch(address):
                label, organizational = classify_email(address)
                add("email", address, label, organizational)
        elif href.startswith("tel:"):
            number = href.split(":", 1)[1]
            add("phone", number, classify_phone(number, _visible_text(anchor)), True)

    text = _visible_text(soup)
    for match in EMAIL_RE.finditer(text):
        address = match.group(0)
        label, organizational = classify_email(address)
        add("email", address, label, organizational)
    for match in PHONE_RE.finditer(text):
        number = match.group(0)
        add("phone", number, classify_phone(number, text[max(0, match.start() - 40): match.end() + 40]), True)

    for node in soup.select("[class*='address'], [id*='address'], address"):
        address = _visible_text(node)
        if len(address) >= 12:
            add("address", address, "office", True)
    if not any(item.kind == "address" for item in found):
        for line in (html or "").splitlines():
            clean = re.sub(r"<[^>]+>", " ", line)
            clean = re.sub(r"\s+", " ", clean).strip()
            if ADDRESS_HINT.search(clean) and 12 <= len(clean) <= 240:
                add("address", clean, "office", True)
                break

    add("page_url", canonical_website(page_url) or page_url, source, True)
    return found


def extract_people(html: str) -> list[ExtractedPerson]:
    soup = BeautifulSoup(html or "", "html.parser")
    people: list[ExtractedPerson] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, designation: str, email: str = "", phone: str = "") -> None:
        clean_name = re.sub(r"\s+", " ", name).strip()
        clean_title = re.sub(r"\s+", " ", designation).strip()
        if len(clean_name.split()) < 2 or rank_designation(clean_title) >= 99:
            return
        key = (clean_name.casefold(), clean_title.casefold())
        if key in seen:
            return
        seen.add(key)
        people.append(
            ExtractedPerson(
                name=clean_name,
                designation=clean_title,
                email=email,
                phone=phone,
            )
        )

    for heading in soup.select("h2, h3, h4, h5, .name, .member-name, figcaption"):
        name = heading.get_text(" ", strip=True)
        sibling = heading.find_next_sibling()
        designation = sibling.get_text(" ", strip=True) if sibling else ""
        parent_text = _visible_text(heading.parent) if heading.parent else ""
        emails = EMAIL_RE.findall(parent_text)
        phones = PHONE_RE.findall(parent_text)
        add(name, designation, emails[0] if emails else "", phones[0] if phones else "")

    return people


def discover_internal_links(html: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    host = urlparse(page_url).netloc.casefold()
    found: list[str] = []
    seen: set[str] = set()
    interesting = {token for _label, tokens in PAGE_LABELS for token in tokens}
    for anchor in soup.select("a[href]"):
        href = urljoin(page_url, (anchor.get("href") or "").strip())
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc.casefold() != host:
            continue
        if any(parsed.netloc.casefold().endswith(suffix) for suffix in SKIP_HOST_SUFFIXES):
            continue
        path = parsed.path.casefold()
        if not any(token in path for token in interesting):
            continue
        canonical = canonical_website(href)
        if not canonical or canonical.casefold() in seen:
            continue
        seen.add(canonical.casefold())
        found.append(canonical)
    return found


def allowed_by_robots(url: str, user_agent: str = USER_AGENT) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = RobotFileParser()
    try:
        parser.set_url(robots_url)
        parser.read()
        return parser.can_fetch(user_agent, url)
    except Exception:
        return True


def default_fetch_page(url: str) -> str:
    if not allowed_by_robots(url):
        return ""
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=FETCH_TIMEOUT,
    )
    response.raise_for_status()
    return response.text[:MAX_HTML_BYTES]


def crawl_company_site(
    website: str,
    fetch_page: Callable[[str], str] | None = None,
    *,
    max_pages: int = MAX_PAGES,
) -> list[tuple[str, str]]:
    fetch = fetch_page or default_fetch_page
    start = canonical_website(website)
    if not start:
        return []
    queue = [start]
    seen: set[str] = {start.casefold()}
    pages: list[tuple[str, str]] = []
    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        try:
            html = fetch(url)
        except Exception:
            continue
        if not html:
            continue
        pages.append((url, html))
        for link in discover_internal_links(html, url):
            key = link.casefold()
            if key in seen:
                continue
            seen.add(key)
            queue.append(link)
    return pages
