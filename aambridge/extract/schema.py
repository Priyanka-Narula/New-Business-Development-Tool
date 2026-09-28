"""Pydantic extraction schemas with explicit epistemic basis."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Basis = Literal["fact", "inference", "unknown"]


class ExtractedField(BaseModel):
    value: Any = None
    evidence: str = ""
    basis: Basis = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


def unknown_field() -> ExtractedField:
    return ExtractedField()


class ExtractionResult(BaseModel):
    job_title_clean: ExtractedField = Field(default_factory=unknown_field)
    company_name_clean: ExtractedField = Field(default_factory=unknown_field)
    industry: ExtractedField = Field(default_factory=unknown_field)
    other_industry_text: ExtractedField = Field(default_factory=unknown_field)
    function: ExtractedField = Field(default_factory=unknown_field)
    seniority: ExtractedField = Field(default_factory=unknown_field)
    experience_years_min: ExtractedField = Field(default_factory=unknown_field)
    country: ExtractedField = Field(default_factory=unknown_field)
    city: ExtractedField = Field(default_factory=unknown_field)
    employment_type: ExtractedField = Field(default_factory=unknown_field)
    work_mode: ExtractedField = Field(default_factory=unknown_field)
    nationality_restriction: ExtractedField = Field(default_factory=unknown_field)
    openings_count: ExtractedField = Field(default_factory=unknown_field)
    is_job_vacancy: ExtractedField = Field(default_factory=unknown_field)
    is_agency_posting: ExtractedField = Field(default_factory=unknown_field)
    end_client_hint: ExtractedField = Field(default_factory=unknown_field)
    post_category: ExtractedField = Field(default_factory=unknown_field)
