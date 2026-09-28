"""Ollama extraction, prompt rendering, merging, and evidence validation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from aambridge.extract.schema import ExtractedField, ExtractionResult
from aambridge.extract.taxonomies import (
    function_keys,
    industry_keys,
    seniority_keys,
)

ROOT = Path(__file__).resolve().parents[2]
OLLAMA_URL = "http://localhost:11434/api/chat"
PROMPT_VERSIONS = {
    "linkedin_job": "job_v1",
    "linkedin_post": "post_v1",
}


def normalise_evidence(value: str) -> str:
    return " ".join((value or "").casefold().split())


def truncate_input(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    requirement = re.search(
        r"(requirements?|qualifications?|المتطلبات|المؤهلات)",
        text,
        re.IGNORECASE,
    )
    start_length = int(limit * 0.65)
    start = text[:start_length]
    if requirement:
        remainder = text[requirement.start() : requirement.start() + limit - start_length]
    else:
        remainder = text[-(limit - start_length) :]
    return f"{start}\n[...truncated...]\n{remainder}"


def render_prompt(
    source: str,
    input_text: str,
    deterministic: ExtractionResult,
    max_chars: int,
) -> tuple[str, str]:
    version = PROMPT_VERSIONS[source]
    template = (
        ROOT / "aambridge" / "extract" / "prompts" / f"{version}.md"
    ).read_text(encoding="utf-8")
    prompt = (
        template.replace("{{industries}}", ", ".join(industry_keys()))
        .replace("{{functions}}", ", ".join(function_keys()))
        .replace("{{seniority}}", ", ".join(seniority_keys()))
        .replace(
            "{{deterministic}}",
            deterministic.model_dump_json(exclude_none=True),
        )
        .replace("{{input}}", truncate_input(input_text, max_chars))
    )
    prompt += (
        "\n\nJSON schema:\n"
        + json.dumps(ExtractionResult.model_json_schema(), ensure_ascii=False)
    )
    return prompt, version


def ollama_call(prompt: str, model: str, timeout: int) -> dict[str, Any]:
    from aambridge.settings import Settings

    response = requests.post(
        Settings().ollama_url or OLLAMA_URL,
        json={
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    content = response.json()["message"]["content"]
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            raise ValueError("Ollama response contained no JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("Ollama response was not an object")
    return value


def _unknown() -> ExtractedField:
    return ExtractedField()


def validate_result(
    result: ExtractionResult,
    input_text: str,
) -> tuple[ExtractionResult, int]:
    rejected = 0
    normalised_input = normalise_evidence(input_text)
    taxonomy_values = {
        "industry": set(industry_keys()),
        "function": set(function_keys()),
        "seniority": set(seniority_keys()),
        "post_category": {
            "hiring",
            "expansion",
            "project_award",
            "leadership_change",
            "other",
        },
    }
    fact_only = {
        "company_name_clean",
        "city",
        "experience_years_min",
        "openings_count",
    }
    for name in ExtractionResult.model_fields:
        field = getattr(result, name)
        invalid = False
        if field.basis in {"fact", "inference"}:
            evidence = normalise_evidence(field.evidence)
            invalid = not evidence or evidence not in normalised_input
        if field.basis == "unknown" and field.value not in (None, ""):
            invalid = True
        if name in fact_only and field.value is not None and field.basis != "fact":
            invalid = True
        if name in taxonomy_values and field.value not in (None, ""):
            invalid = field.value not in taxonomy_values[name]
        if name in {"experience_years_min", "openings_count"} and field.value is not None:
            invalid = invalid or not isinstance(field.value, (int, float))
        if invalid:
            setattr(result, name, _unknown())
            rejected += 1
    return result, rejected


def merge_deterministic(
    deterministic: ExtractionResult,
    llm_result: ExtractionResult,
) -> ExtractionResult:
    merged = llm_result.model_copy(deep=True)
    for name in ExtractionResult.model_fields:
        value = getattr(deterministic, name)
        if value.basis != "unknown":
            setattr(merged, name, value)
    return merged


def extract_with_llm(
    *,
    source: str,
    input_text: str,
    deterministic: ExtractionResult,
    model: str,
    timeout: int,
    max_chars: int,
    call: Callable[[str, str, int], dict[str, Any]] = ollama_call,
) -> tuple[ExtractionResult, str, int]:
    prompt, version = render_prompt(source, input_text, deterministic, max_chars)
    raw = call(prompt, model, timeout)
    llm_result = ExtractionResult.model_validate(raw)
    llm_result, rejected = validate_result(llm_result, input_text)
    merged = merge_deterministic(deterministic, llm_result)
    merged, merged_rejected = validate_result(merged, input_text)
    return merged, version, rejected + merged_rejected
