"""Compatibility imports for the Phase 4 LinkedIn Posts source adapter."""

from aambridge.sources.linkedin_posts import (
    LinkedInPostsAdapter,
    PostLead,
    _parse_card,
    _title,
    build_content_url,
    categorise_post,
    scrape_posts,
)

__all__ = [
    "LinkedInPostsAdapter",
    "PostLead",
    "_parse_card",
    "_title",
    "build_content_url",
    "categorise_post",
    "scrape_posts",
]
