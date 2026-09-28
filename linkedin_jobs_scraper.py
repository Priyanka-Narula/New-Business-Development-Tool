"""Compatibility imports for the Phase 3 LinkedIn Jobs source adapter."""

from aambridge.sources.linkedin_jobs import (
    JobLead,
    LinkedInJobsAdapter,
    _parse_card,
    build_jobs_url,
    detect_agency,
    detect_end_client_hint,
    detect_nationality_restriction,
    experience_level_choices,
    experience_level_codes,
    parse_location,
    parse_relative_date,
    scrape_jobs,
)

__all__ = [
    "JobLead",
    "LinkedInJobsAdapter",
    "_parse_card",
    "build_jobs_url",
    "detect_agency",
    "detect_end_client_hint",
    "detect_nationality_restriction",
    "experience_level_choices",
    "experience_level_codes",
    "parse_location",
    "parse_relative_date",
    "scrape_jobs",
]
