"""Common source-adapter contracts."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class NormalisedRecord:
    """A source record with provenance retained alongside raw fields."""

    source: str
    source_key: str
    source_url: str
    observed_at: datetime
    raw_text: str
    raw_fields: dict[str, Any]

    def storage_fields(self) -> dict[str, Any]:
        """Return fields accepted by the incremental persistence layer."""

        return {
            **self.raw_fields,
            "source_key": self.source_key,
            "source_url": self.source_url,
            "observed_at": self.observed_at.isoformat(),
            "raw_text": self.raw_text,
        }


class SourceAdapter(Protocol):
    """Protocol implemented by independently runnable collection sources."""

    def records(self) -> Iterator[NormalisedRecord]:
        """Yield normalized records one at a time."""

