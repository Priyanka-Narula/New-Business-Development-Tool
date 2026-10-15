from __future__ import annotations

from selenium.common.exceptions import StaleElementReferenceException

from aambridge.sources.stale import parse_indexed_card


class _Card:
    def __init__(self, value: str, *, stale: bool = False) -> None:
        self.value = value
        self.stale = stale


def _parse(card: _Card) -> str:
    if card.stale:
        raise StaleElementReferenceException("stale")
    return card.value


def test_parse_indexed_card_succeeds_on_first_try() -> None:
    refetches: list[int] = []

    def refetch() -> list[_Card]:
        refetches.append(1)
        return [_Card("later")]

    assert (
        parse_indexed_card(
            cards=[_Card("ok")],
            index=0,
            refetch=refetch,
            parse=_parse,
        )
        == "ok"
    )
    assert refetches == []


def test_parse_indexed_card_retries_same_index_after_stale() -> None:
    attempts = {"n": 0}

    def refetch() -> list[_Card]:
        attempts["n"] += 1
        return [_Card("recovered")]

    assert (
        parse_indexed_card(
            cards=[_Card("gone", stale=True)],
            index=0,
            refetch=refetch,
            parse=_parse,
        )
        == "recovered"
    )
    assert attempts["n"] == 1


def test_parse_indexed_card_counts_failure_only_after_retries() -> None:
    refetches: list[int] = []

    def refetch() -> list[_Card]:
        refetches.append(1)
        return [_Card("still", stale=True)]

    try:
        parse_indexed_card(
            cards=[_Card("first", stale=True)],
            index=0,
            refetch=refetch,
            parse=_parse,
        )
    except StaleElementReferenceException:
        assert len(refetches) == 2
    else:
        raise AssertionError("expected stale after retries")
