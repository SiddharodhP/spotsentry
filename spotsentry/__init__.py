"""SpotSentry: run long jobs on spot instances without losing progress."""

from __future__ import annotations

from spotsentry.adapters import ItemListAdapter, Position, Tier
from spotsentry.core import SpotSentry
from spotsentry.errors import SpotInterrupted

__version__ = "0.1.0"
__all__ = [
    "SpotSentry",
    "SpotInterrupted",
    "Tier",
    "Position",
    "ItemListAdapter",
    "TorchTrainerAdapter",
]


def __getattr__(name: str):
    if name == "TorchTrainerAdapter":
        from spotsentry.adapters.torch_trainer import TorchTrainerAdapter

        return TorchTrainerAdapter
    raise AttributeError(name)
