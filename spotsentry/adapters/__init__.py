"""Adapters connect a job's state to SpotSentry."""

from __future__ import annotations

from spotsentry.adapters.base import Checkpointable, Position, Tier
from spotsentry.adapters.item_list import ItemListAdapter

__all__ = ["Checkpointable", "Position", "Tier", "ItemListAdapter", "TorchTrainerAdapter"]


def __getattr__(name: str):
    # Imported on demand so the core library works without torch installed.
    if name == "TorchTrainerAdapter":
        from spotsentry.adapters.torch_trainer import TorchTrainerAdapter

        return TorchTrainerAdapter
    raise AttributeError(name)
