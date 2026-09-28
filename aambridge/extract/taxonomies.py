"""Read extraction taxonomies from YAML."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


@cache
def load_yaml(relative_path: str) -> dict[str, Any]:
    value = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected mapping in {relative_path}")
    return value


def industry_keys() -> tuple[str, ...]:
    return tuple(load_yaml("taxonomies/industries.yaml")["industries"])


def function_keys() -> tuple[str, ...]:
    return tuple(load_yaml("taxonomies/functions.yaml")["functions"])


def seniority_keys() -> tuple[str, ...]:
    return tuple(load_yaml("taxonomies/seniority.yaml")["levels"])


def target_seniority_keys() -> frozenset[str]:
    levels = load_yaml("taxonomies/seniority.yaml")["levels"]
    return frozenset(key for key, value in levels.items() if value.get("target"))
