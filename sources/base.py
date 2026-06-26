"""Source abstraction (DESIGN.md §4.2).

Trivial for V1 (Reddit only), but the contract is in place for V4 sources.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from pipeline.models import RawItem


@dataclass
class SourceCursor:
    """Opaque per-stream cursor. For Reddit, cursor_ts is a UTC epoch second."""

    cursor_ts: Optional[float] = None


@dataclass
class FetchStats:
    """Completeness signals surfaced to runs.completeness (§11.4)."""

    fetched: int = 0
    ceiling_hits: list[tuple[str, float]] = None  # (stream, cursor_gap_seconds)
    comment_cap_hits: list[tuple[str, int, int]] = None  # (post_id, estimated, fetched)

    def __post_init__(self) -> None:
        if self.ceiling_hits is None:
            self.ceiling_hits = []
        if self.comment_cap_hits is None:
            self.comment_cap_hits = []


class Source(ABC):
    name: str

    @abstractmethod
    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        """Yield items newer than cursor; update cursor as you go.

        MUST populate RawItem.url with a direct deep link to the original.
        """
        raise NotImplementedError
