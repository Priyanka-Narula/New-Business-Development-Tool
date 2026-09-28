"""Deterministic extraction pass that runs before any LLM call."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from aambridge.extract.schema import ExtractedField, ExtractionResult
from aambridge.extract.taxonomies import load_yaml
from aambridge.sources.linkedin_jobs import detect_nationality_restriction


def fact(value: Any, evidence: str, confidence: float = 1.0) -> ExtractedField:
    return ExtractedField(
        value=value,
        evidence=evidence,
        basis="fact",
        confidence=confidence,
    )


def inference(
    value: Any,
    evidence: str,
    confidence: float,
) -> ExtractedField:
    return ExtractedField(
        value=value,
        evidence=evidence,
        basis="inference",
        confidence=confidence,
    )


def _keyword_match(text: str, values: Mapping[str, Any]) -> tuple[str, str]:
    candidates: list[tuple[int, str, str]] = []
    for key, details in values.items():
        keywords = details if isinstance(details, list) else details.get("keywords", [])
        for keyword in keywords:
            match = re.search(
                rf"(?<!\w){re.escape(str(keyword))}(?!\w)",
                text,
                re.IGNORECASE,
            )
            if match:
                candidates.append((len(match.group(0)), str(key), match.group(0)))
    if not candidates:
        return "", ""
    _length, key, evidence = max(candidates)
    return key, evidence


def _experience(text: str) -> tuple[int | None, str]:
    patterns = (
        r"(?:minimum|min\.?|at least)\s*(\d{1,2})\s*\+?\s*years?",
        r"(\d{1,2})\s*\+\s*years?",
        r"(\d{1,2})\s*(?:-|to)\s*\d{1,2}\s*years?",
        r"خبرة\s*(?:لا تقل عن|من)?\s*(\d{1,2})\s*سن(?:ة|وات)",
        r"(\d{1,2})\s*سن(?:ة|وات)\s*خبرة",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1)), match.group(0)
    return None, ""


def _openings(text: str) -> tuple[int | None, str]:
    patterns = (
        r"(\d{1,3})\s+(?:openings?|vacancies|positions?)",
        r"(?:hiring|مطلوب)\s+(\d{1,3})\b",
        r"عدد\s*الشواغر\s*[:\-]?\s*(\d{1,3})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1)), match.group(0)
    return None, ""


def deterministic_extract(
    source: str,
    text: str,
    raw_fields: Mapping[str, Any],
) -> ExtractionResult:
    result = ExtractionResult()
    title = str(raw_fields.get("job_title") or "")
    company = str(raw_fields.get("company_name") or raw_fields.get("author_name") or "")
    location = str(raw_fields.get("job_location") or raw_fields.get("location_hint") or "")

    if title:
        result.job_title_clean = fact(title, title)
    if company:
        result.company_name_clean = fact(company, company)

    industries = load_yaml("taxonomies/industries.yaml")["industries"]
    industry, industry_evidence = _keyword_match(text, industries)
    if industry:
        result.industry = fact(industry, industry_evidence)

    functions = load_yaml("taxonomies/functions.yaml")["functions"]
    function, function_evidence = _keyword_match(text, functions)
    if function:
        result.function = fact(function, function_evidence)

    levels = load_yaml("taxonomies/seniority.yaml")["levels"]
    seniority, seniority_evidence = _keyword_match(text, levels)
    if seniority:
        result.seniority = fact(seniority, seniority_evidence)

    years, years_evidence = _experience(text)
    if years is not None:
        result.experience_years_min = fact(years, years_evidence)

    country = str(raw_fields.get("country") or "")
    city = str(raw_fields.get("city") or "")
    if country and location:
        result.country = fact(country, location)
    if city and location:
        result.city = fact(city, location)

    employment_terms = {
        "Full-Time": ("full-time", "full time", "permanent", "دوام كامل"),
        "Part-Time": ("part-time", "part time", "دوام جزئي"),
        "Contract": ("contract", "contractual", "عقد"),
        "Temporary": ("temporary", "مؤقت"),
    }
    for value, terms in employment_terms.items():
        match = next(
            (
                re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE)
                for term in terms
                if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE)
            ),
            None,
        )
        if match:
            result.employment_type = fact(value, match.group(0))
            break

    work_terms = {
        "Onsite": ("on-site", "onsite", "office-based", "حضوري"),
        "Hybrid": ("hybrid", "هجين"),
        "Remote": ("remote", "work from home", "عن بعد"),
    }
    for value, terms in work_terms.items():
        for term in terms:
            match = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE)
            if match:
                result.work_mode = fact(value, match.group(0))
                break
        if result.work_mode.basis == "fact":
            break

    restricted, restriction_evidence = detect_nationality_restriction(text)
    if restricted:
        result.nationality_restriction = fact(True, restriction_evidence)

    count, count_evidence = _openings(text)
    if count is not None:
        result.openings_count = fact(count, count_evidence)

    if source == "linkedin_job" and title:
        result.is_job_vacancy = fact(True, title)
    elif str(raw_fields.get("post_category") or "") == "hiring":
        category_evidence = title or "hiring"
        if category_evidence.casefold() in text.casefold():
            result.is_job_vacancy = fact(True, category_evidence)

    if raw_fields.get("posted_by_agency") or raw_fields.get("author_is_agency"):
        evidence = str(raw_fields.get("agency_evidence") or company)
        if evidence:
            result.is_agency_posting = inference(True, evidence, 0.9)

    hint = raw_fields.get("end_client_hint")
    if isinstance(hint, Mapping) and hint.get("value"):
        result.end_client_hint = inference(
            hint["value"],
            str(hint.get("evidence") or ""),
            0.7,
        )

    category = str(raw_fields.get("post_category") or "")
    if category:
        evidence = next(
            (
                phrase
                for phrase in load_yaml("config/posts.yaml")["categories"].get(
                    category, []
                )
                if str(phrase).casefold() in text.casefold()
            ),
            "",
        )
        if evidence:
            result.post_category = fact(category, str(evidence))
    return result
