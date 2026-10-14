from __future__ import annotations

from config import (
    PRIMARY_LOCATIONS,
    SECTOR_KEYWORDS,
    build_search_specs,
    normalize_sectors,
)


def test_green_energy_queries_use_cv_job_posting_language() -> None:
    specs = build_search_specs(["green_energy"], include_gcc=False)
    queries = [spec.keywords for spec in specs]
    joined = "\n".join(queries)

    groups = (len(SECTOR_KEYWORDS["green_energy"]) + 2) // 3
    assert len(specs) == groups * len(PRIMARY_LOCATIONS)
    assert {spec.location for spec in specs} == set(PRIMARY_LOCATIONS)
    assert '"renewable energy"' in joined
    assert '"solar energy"' in joined
    assert '"solar farm"' in joined
    assert '"wind energy"' in joined
    assert '"power generation"' in joined
    assert '"power plant"' in joined
    assert '"solar engineer"' in joined
    assert '"grid connection"' in joined
    assert "(solar OR" in joined
    assert " power OR" not in joined
    assert '"sustainability"' not in joined


def test_new_sectors_and_regions_are_in_default_searches() -> None:
    assert {"education", "datacenters", "ai"} <= set(SECTOR_KEYWORDS)
    assert {
        "Qatar",
        "Malaysia",
        "Australia",
        "United Kingdom",
        "Germany",
        "Canada",
        "United States",
    } <= set(PRIMARY_LOCATIONS)

    specs = build_search_specs(
        ["education", "datacenters", "ai"],
        include_gcc=False,
    )
    countries = {spec.location for spec in specs}
    sectors = {spec.sector for spec in specs}
    assert countries == set(PRIMARY_LOCATIONS)
    assert sectors == {"education", "datacenters", "ai"}
    assert any("school" in spec.keywords.lower() for spec in specs)
    assert any("data center" in spec.keywords.lower() for spec in specs)
    assert any("artificial intelligence" in spec.keywords.lower() for spec in specs)


def test_normalize_sectors_accepts_new_industry_keys() -> None:
    assert normalize_sectors(["education", "ai", "datacenters"]) == [
        "education",
        "ai",
        "datacenters",
    ]
