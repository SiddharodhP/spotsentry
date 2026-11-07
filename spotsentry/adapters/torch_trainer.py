"""Adapter for PyTorch training loops."""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any

import numpy as np
import torch

from spotsentry.adapters.base import Tier


def _to_cpu(value: Any) -> Any:
    """Deep copy a nested structure, moving tensors to CPU."""
    if isinstance(value, torch.Tensor):
        return value.detach().cpu().clone()
    if isinstance(value, dict):
        return {k: _to_cpu(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_cpu(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_to_cpu(v) for v in value)
    return value


def _tensor_bytes(value: Any) -> int:
    if isinstance(value, torch.Tensor):
        return value.numel() * value.element_size()
    if isinstance(value, dict):
        return sum(_tensor_bytes(v) for v in value.values())
    if isinstance(value, list | tuple):
        return sum(_tensor_bytes(v) for v in value)
    return 0


def _capture_rng() -> dict:
    version, internal, gauss = random.getstate()
    np_state = np.random.get_state(legacy=False)
    return {
        "python": {"version": version, "internal": list(internal), "gauss": gauss},
        "numpy": {
            "bit_generator": np_state["bit_generator"],
            "key": torch.from_numpy(np_state["state"]["key"].astype(np.int64)),
            "pos": int(np_state["state"]["pos"]),
            "has_gauss": int(np_state["has_gauss"]),
            "cached_gaussian": float(np_state["gauss"]),
        },
        "torch": torch.get_rng_state(),
    }


def _restore_rng(state: dict) -> None:
    py = state["python"]
    random.setstate((py["version"], tuple(py["internal"]), py["gauss"]))
    npy = state["numpy"]
    np.random.set_state(
        {
            "bit_generator": npy["bit_generator"],
            "state": {"key": npy["key"].numpy().astype(np.uint32), "pos": npy["pos"]},
            "has_gauss": npy["has_gauss"],
            "gauss": npy["cached_gaussian"],
        }
    )
    torch.set_rng_state(state["torch"])


class TorchTrainerAdapter:
    """Saves a model, optimizer, scheduler and random state.

    * ``full``: everything, so a resumed run matches an uninterrupted one.
    * ``light``: no optimizer state (Adam keeps about twice the model's size there). Restoring
      clears the optimizer and sets ``last_restore_info["optimizer_reset"]``.
    * ``tiny``: model weights cast to float16 and the seed.

    ``extra`` is a small dict kept in every tier, for things like accuracy history.
    """

    def __init__(self, model, optimizer=None, scheduler=None, base_seed: int = 42) -> None:
        self.model = model
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.base_seed = base_seed
        self.extra: dict[str, Any] = {}
        self.last_restore_info: dict[str, Any] = {}

    def supported_tiers(self) -> list[Tier]:
        return [Tier.FULL, Tier.LIGHT, Tier.TINY]

    def capture(self, tier: Tier) -> dict:
        state: dict[str, Any] = {
            "tier": tier.value,
            "base_seed": self.base_seed,
            "extra": _to_cpu(self.extra),
        }
        model_state = self.model.state_dict()
        if tier is Tier.TINY:
            state["model"] = {
                k: (
                    v.detach().cpu().to(torch.float16)
                    if v.is_floating_point()
                    else v.detach().cpu().clone()
                )
                for k, v in model_state.items()
            }
            return state
        state["model"] = {k: v.detach().cpu().clone() for k, v in model_state.items()}
        state["rng"] = _capture_rng()
        if tier is Tier.FULL:
            if self.optimizer is not None:
                state["optimizer"] = _to_cpu(self.optimizer.state_dict())
            if self.scheduler is not None:
                state["scheduler"] = _to_cpu(self.scheduler.state_dict())
        return state

    def restore(self, state: dict) -> None:
        own = self.model.state_dict()
        weights = {}
        for key, value in state["model"].items():
            target = own.get(key)
            weights[key] = (
                value.to(target.dtype)
                if target is not None and value.is_floating_point()
                else value
            )
        self.model.load_state_dict(weights)
        self.base_seed = state.get("base_seed", self.base_seed)
        self.extra = state.get("extra", {})
        if "rng" in state:
            _restore_rng(state["rng"])
        optimizer_reset = False
        if self.optimizer is not None:
            if "optimizer" in state:
                self.optimizer.load_state_dict(state["optimizer"])
            else:
                self.optimizer.state.clear()
                optimizer_reset = True
        if self.scheduler is not None and "scheduler" in state:
            self.scheduler.load_state_dict(state["scheduler"])
        self.last_restore_info = {
            "tier": state.get("tier"),
            "optimizer_reset": optimizer_reset,
            "rng_restored": "rng" in state,
        }

    def estimate_tier_bytes(self) -> dict[Tier, int]:
        """Size each tier from tensor sizes, without serializing anything."""
        model = _tensor_bytes(self.model.state_dict())
        optimizer = _tensor_bytes(self.optimizer.state_dict()) if self.optimizer else 0
        return {Tier.FULL: model + optimizer, Tier.LIGHT: model, Tier.TINY: model // 2}

    def iter_batches(
        self, loader_or_dataset, epoch: int, start_step: int = 0, batch_size: int | None = None
    ) -> Iterator[tuple[int, Any]]:
        """Yield (step, batch) in an order fixed by ``base_seed + epoch``, skipping done batches.

        Uses its own generator and no DataLoader iterator: creating one draws from the global
        random generator, which would make a resumed run differ from an uninterrupted one.
        """
        dataset = getattr(loader_or_dataset, "dataset", loader_or_dataset)
        size = batch_size or getattr(loader_or_dataset, "batch_size", None)
        if not size:
            raise ValueError("batch_size is required when passing a dataset")
        generator = torch.Generator().manual_seed(self.base_seed + epoch)
        order = torch.randperm(len(dataset), generator=generator)
        tensors = getattr(dataset, "tensors", None)
        total = (len(dataset) + size - 1) // size
        for step in range(start_step, total):
            indices = order[step * size : (step + 1) * size]
            if tensors is not None:
                yield step, tuple(t[indices] for t in tensors)
            else:
                yield step, torch.utils.data.default_collate([dataset[int(i)] for i in indices])
