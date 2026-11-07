"""Adapter for batch jobs that walk through a list of items."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from spotsentry.adapters.base import Tier


class ItemListAdapter:
    """State is only the next item index and small counters, so there is a single tier.

    Each item's result is written to the store as it is produced, so nothing else needs saving.
    """

    def __init__(self, items: list[Any] | None = None, total: int | None = None) -> None:
        self.items = items
        self.total = len(items) if items is not None else (total or 0)
        self.next_index = 0
        self.counters: dict[str, int] = {}

    def capture(self, tier: Tier) -> dict:
        return {"next_index": self.next_index, "counters": dict(self.counters)}

    def restore(self, state: dict) -> None:
        self.next_index = int(state["next_index"])
        self.counters = dict(state.get("counters", {}))

    def supported_tiers(self) -> list[Tier]:
        return [Tier.FULL]

    def estimate_tier_bytes(self) -> dict[Tier, int]:
        return {Tier.FULL: 1024}

    def iter_items(self, start: int = 0) -> Iterator[tuple[int, Any]]:
        """Yield (index, item) from ``start`` to the end of the list."""
        for index in range(start, self.total):
            yield index, (self.items[index] if self.items is not None else index)
