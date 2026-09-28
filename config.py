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
        "renewable energy", "solar energy", "wind energy",
        "sustainability", "clean energy",
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
}

OTHER_GCC_LOCATIONS = {
    "Qatar": ("Qatar", "Doha", "قطر", "الدوحة"),
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
        # Two broad domain searches per country cover the vocabulary without
        # creating hundreds of LinkedIn requests.
        keyword_groups = (
            " OR ".join(f'"{term}"' for term in keywords[:3]),
            " OR ".join(f'"{term}"' for term in keywords[3:]),
        )
        for country in locations:
            for group in keyword_groups:
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
            f'"{term}"' for term in SECTOR_KEYWORDS[sector][:3]
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
