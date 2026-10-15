"""Retry a LinkedIn card by list index after Selenium refreshes the DOM."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from selenium.common.exceptions import StaleElementReferenceException

log = logging.getLogger(__name__)

EXTRA_STALE_RETRIES = 2
T = TypeVar("T")


def parse_indexed_card(
    *,
    cards: Sequence[Any],
    index: int,
    refetch: Callable[[], Sequence[Any]],
    parse: Callable[[Any], T],
    extra_retries: int = EXTRA_STALE_RETRIES,
) -> T:
    """Parse `cards[index]`, refetching on stale up to `extra_retries` times."""

    if 0 <= index < len(cards):
        try:
            return parse(cards[index])
        except StaleElementReferenceException:
            log.info("Stale card at index %s; retrying", index)
    else:
        log.info("Card index %s missing before parse; retrying", index)

    for attempt in range(1, extra_retries + 1):
        refreshed = list(refetch())
        if index >= len(refreshed):
            log.info(
                "Stale card retry %s/%s at index %s: card gone",
                attempt,
                extra_retries,
                index,
            )
            continue
        try:
            return parse(refreshed[index])
        except StaleElementReferenceException:
            log.info(
                "Stale card retry %s/%s at index %s failed",
                attempt,
                extra_retries,
                index,
            )
    raise StaleElementReferenceException(
        f"card at index {index} stale after {extra_retries} retries"
    )
