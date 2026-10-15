"""Search configuration for the Middle East recruitment scraper."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class SearchSpec:
    sector: str
    keywords: str
    location: str
    query_set: str = "hiring"


SECTOR_KEYWORDS = {
    "healthcare": (
        "hospital operations", "healthcare", "medical director",
        "clinical operations", "pharmaceutical",
    ),
    "green_energy": (
        "renewable energy", "solar energy", "solar farm",
        "solar", "solar PV", "photovoltaic",
        "BESS", "battery energy storage", "wind energy",
        "power generation", "power plant", "power systems",
        "electrical power", "transmission", "grid connection",
        "substation", "switchyard", "inverter",
        "PCS", "solar engineer", "BESS engineer",
        "electrical design", "PV",
    ),
    "real_estate": (
        "real estate", "property development", "asset management",
        "facilities management",
    ),
    "construction_infrastructure": (
        "construction", "infrastructure", "civil engineering",
        "MEP", "project controls",
    ),
    "hospitality": (
        "hospitality", "hotel operations", "food beverage",
        "resort", "revenue management",
    ),
    "retail": (
        "retail", "store operations", "category management",
        "merchandising", "e-commerce",
    ),
    "education": (
        "international school", "school principal", "head of school",
        "academic director", "K-12",
    ),
    "datacenters": (
        "data center", "data centre", "colocation",
        "hyperscale", "data hall",
    ),
    "ai": (
        "artificial intelligence", "machine learning", "AI engineer",
        "LLM", "MLOps",
    ),
}

SENIORITY_TERMS = (
    # Keep specific/long phrases first because matching returns the first hit.
    "leadership-level professional", "leadership professional",
    "functional expert", "subject matter expert", "department head",
    "senior manager", "general manager", "vice president",
    "specialist", "manager", "head", "director", "chief", "vp", "lead",
    "مدير", "رئيس", "أخصائي",
)
EXCLUDED_SENIORITY_TERMS = (
    "intern", "internship", "junior", "entry level", "entry-level",
    "graduate", "fresher", "trainee", "apprentice", "متدرب", "مبتدئ",
)

PRIMARY_LOCATIONS = {
    "Saudi Arabia": (
        "Saudi Arabia", "Riyadh", "Jeddah", "NEOM", "Dammam", "Khobar",
        "السعودية", "المملكة العربية السعودية", "الرياض", "جدة", "الدمام", "الخبر",
    ),
    "United Arab Emirates": (
        "United Arab Emirates", "UAE", "Dubai", "Abu Dhabi", "Sharjah",
        "الإمارات", "الإمارات العربية المتحدة", "دبي", "أبو ظبي", "الشارقة",
    ),
    "Qatar": ("Qatar", "Doha", "قطر", "الدوحة"),
    "Malaysia": (
        "Malaysia", "Kuala Lumpur", "Selangor", "Penang", "Johor Bahru",
        "Cyberjaya",
    ),
    "Australia": (
        "Australia", "Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide",
        "Canberra",
    ),
    "United Kingdom": (
        "United Kingdom", "UK", "London", "Manchester", "Birmingham",
        "Edinburgh", "Glasgow",
    ),
    "Germany": (
        "Germany", "Berlin", "Munich", "Frankfurt", "Hamburg", "Cologne",
        "Stuttgart", "Düsseldorf",
    ),
    "Canada": (
        "Canada", "Toronto", "Vancouver", "Montreal", "Calgary", "Ottawa",
        "Edmonton",
    ),
    "United States": (
        "United States", "USA", "US", "United States of America",
        "New York", "San Francisco", "Los Angeles", "Chicago", "Houston",
        "Dallas", "Seattle", "Austin", "Boston", "Washington DC",
    ),
}

OTHER_GCC_LOCATIONS = {
    "Bahrain": ("Bahrain", "Manama", "البحرين", "المنامة"),
    "Oman": ("Oman", "Muscat", "عمان", "مسقط"),
    "Kuwait": ("Kuwait", "Kuwait City", "الكويت", "مدينة الكويت"),
}

TIME_FILTER_CODES = {
    "any": "",
    "day": "r86400",
    "week": "r604800",
    "month": "r2592000",
}


def _format_search_term(term: str) -> str:
    """Quote multi-word phrases only so short tokens still match job titles."""

    return f'"{term}"' if " " in term else term


def _keyword_groups(keywords: tuple[str, ...]) -> tuple[str, ...]:
    """Keep each LinkedIn Boolean group to three terms."""

    groups: list[str] = []
    for start in range(0, len(keywords), 3):
        chunk = keywords[start : start + 3]
        if chunk:
            groups.append(" OR ".join(_format_search_term(term) for term in chunk))
    return tuple(groups)


def normalize_sectors(sectors: list[str] | None) -> list[str]:
    if not sectors:
        return list(SECTOR_KEYWORDS)
    normalized = [item.strip().lower().replace("-", "_") for item in sectors]
    unknown = sorted(set(normalized) - set(SECTOR_KEYWORDS))
    if unknown:
        raise ValueError(
            f"Unknown sectors: {', '.join(unknown)}. "
            f"Choose from: {', '.join(SECTOR_KEYWORDS)}"
        )
    return list(dict.fromkeys(normalized))


def build_search_specs(
    sectors: list[str] | None = None,
    include_gcc: bool = False,
) -> list[SearchSpec]:
    """Build a bounded, curated set of searches instead of a huge cross product."""
    selected = normalize_sectors(sectors)
    locations = dict(PRIMARY_LOCATIONS)
    if include_gcc:
        locations.update(OTHER_GCC_LOCATIONS)

    specs: list[SearchSpec] = []
    for sector in selected:
        keywords = SECTOR_KEYWORDS[sector]
        for country in locations:
            for group in _keyword_groups(keywords):
                if group:
                    specs.append(
                        SearchSpec(
                            sector=sector,
                            # Keep the LinkedIn query broad. Jobs experience
                            # filters and local title checks enforce the target
                            # roles; combining two large Boolean groups causes
                            # LinkedIn to return false "no matches" pages.
                            keywords=f"({group})",
                            location=country,
                        )
                    )
    return specs


@cache
def _post_search_config() -> dict:
    value = yaml.safe_load(
        (PROJECT_ROOT / "config" / "posts.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(value, dict):
        raise ValueError("config/posts.yaml must contain a mapping")
    return value


def build_post_search_specs(
    sectors: list[str] | None = None,
    include_gcc: bool = False,
    query_set: str = "hiring",
) -> list[SearchSpec]:
    """Build independently selectable hiring and expansion post searches."""

    if query_set not in {"hiring", "expansion", "all"}:
        raise ValueError("query_set must be hiring, expansion, or all")
    hiring = build_search_specs(sectors, include_gcc)
    if query_set == "hiring":
        return hiring

    selected = normalize_sectors(sectors)
    locations = dict(PRIMARY_LOCATIONS)
    if include_gcc:
        locations.update(OTHER_GCC_LOCATIONS)
    expansion_terms = tuple(_post_search_config()["expansion_search_terms"])
    expansion_group = " OR ".join(f'"{term}"' for term in expansion_terms)
    expansion: list[SearchSpec] = []
    for sector in selected:
        industry_group = " OR ".join(
            _format_search_term(term) for term in SECTOR_KEYWORDS[sector][:3]
        )
        for country in locations:
            expansion.append(
                SearchSpec(
                    sector=sector,
                    keywords=f"({industry_group}) ({expansion_group})",
                    location=country,
                    query_set="expansion",
                )
            )
    return expansion if query_set == "expansion" else [*hiring, *expansion]


def all_location_terms(include_gcc: bool = True) -> tuple[str, ...]:
    groups = dict(PRIMARY_LOCATIONS)
    if include_gcc:
        groups.update(OTHER_GCC_LOCATIONS)
    return tuple(term for aliases in groups.values() for term in aliases)
