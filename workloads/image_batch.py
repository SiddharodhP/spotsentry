"""A non-ML batch job: generate images, resize and filter them, upload each result."""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

from spotsentry.adapters.item_list import ItemListAdapter
from spotsentry.workload import Workload


class ImageBatchJob(Workload):
    """Position is the next item index. Results are written as they are produced."""

    epochs = 1

    def __init__(
        self, items: int = 200, size: int = 512, output_size: int = 128, seed: int = 7,
        extra_seconds: float = 0.0,
    ) -> None:
        self.items = items
        self.size = size
        self.output_size = output_size
        self.seed = seed
        self.extra_seconds = extra_seconds
        self.steps_per_epoch = items
        self._adapter = ItemListAdapter(total=items)
        self._store = None
        self._root = ""

    def bind(self, sentry) -> None:
        if sentry is not None:
            self._store = sentry.store
            self._root = sentry.job_root

    def adapter(self) -> ItemListAdapter:
        return self._adapter

    def iter_steps(self, epoch: int, start_step: int) -> Iterator[tuple[int, Any]]:
        yield from self._adapter.iter_items(start_step)

    def train_step(self, batch: Any) -> dict[str, Any]:
        index = int(batch)
        # Seeded per item, so redoing an item after an interruption gives identical output.
        rng = np.random.default_rng(self.seed + index)
        pixels = rng.integers(0, 256, size=(self.size, self.size, 3), dtype=np.uint8)
        image = Image.fromarray(pixels).resize((self.output_size, self.output_size))
        image = image.filter(ImageFilter.GaussianBlur(radius=2))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        if self._store is not None:
            self._store.put_bytes(f"{self._root}outputs/item_{index:06d}.png", buffer.getvalue())
        if self.extra_seconds:
            time.sleep(self.extra_seconds)
        self._adapter.next_index = index + 1
        counters = self._adapter.counters
        counters["processed"] = counters.get("processed", 0) + 1
        return {"processed": counters["processed"]}

    def result(self) -> dict[str, Any]:
        return {"items": self.items, "next_index": self._adapter.next_index}
