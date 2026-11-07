"""Checkpoint storage: unique objects, a manifest pointing at the newest valid one, restore.

Write protocol (crash-safe):

1. Write the checkpoint to a new, unique key. A PUT is all-or-nothing.
2. Only then rewrite ``latest.json``.
3. Delete checkpoints older than ``keep_last``, never one the manifest references.

A crash at any point leaves ``latest.json`` pointing at a complete, valid checkpoint.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from spotsentry import serialize
from spotsentry.adapters.base import Checkpointable, Position, Tier
from spotsentry.errors import ChecksumMismatch, ObjectNotFound
from spotsentry.events import EventLogger, utc_now_iso
from spotsentry.store.base import AbortCheck, Store

_KEY_RE = re.compile(r"(\d{6})_(full|light|tiny)_e(\d+)_s(\d+)\.ckpt$")


@dataclass
class ManifestEntry:
    """One checkpoint as recorded in the manifest."""

    seq: int
    key: str
    tier: str
    epoch: int
    step: int
    size_bytes: int
    sha256: str
    created_at: str
    instance_id: str


@dataclass
class RestoreResult:
    """What restore found. ``entry`` is None when the job starts from scratch."""

    position: Position
    entry: ManifestEntry | None
    fallback: bool = False


def make_envelope(state: dict, tier: Tier, position: Position) -> dict:
    """Wrap adapter state with what restore needs to know."""
    return {
        "format_version": 1,
        "tier": tier.value,
        "position": {"epoch": position.epoch, "step": position.step},
        "state": state,
    }


class CheckpointManager:
    """Owns the job's checkpoint objects and manifest in the store."""

    def __init__(
        self,
        store: Store,
        job_root: str,
        instance_id: str,
        keep_last: int = 3,
        events: EventLogger | None = None,
        fence: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.root = job_root
        self.instance_id = instance_id
        self.keep_last = keep_last
        self.events = events
        self.fence = fence
        self.manifest_key = f"{job_root}latest.json"
        self.status_key = f"{job_root}status.json"
        self._lock = threading.Lock()
        self._manifest: dict[str, Any] | None = None
        self._next_seq = 1

    def _log(self, event: str, **fields: Any) -> None:
        if self.events:
            self.events.log(event, **fields)

    # Manifest ----------------------------------------------------------------------------

    def load_manifest(self) -> dict[str, Any] | None:
        """Read the manifest, or return None if it is missing."""
        try:
            manifest = json.loads(self.store.get_bytes(self.manifest_key).decode("utf-8"))
        except ObjectNotFound:
            return None
        with self._lock:
            self._manifest = manifest
            self._next_seq = max(self._next_seq, int(manifest["seq"]) + 1)
        return manifest

    def next_entry_key(self, tier: Tier, position: Position) -> tuple[int, str]:
        """Allocate a sequence number and a unique checkpoint key."""
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
        name = f"{seq:06d}_{tier.value}_e{position.epoch}_s{position.step}.ckpt"
        return seq, f"{self.root}checkpoints/{name}"

    def write_checkpoint(
        self,
        payload: bytes,
        tier: Tier,
        position: Position,
        abort_check: AbortCheck | None = None,
    ) -> tuple[ManifestEntry, float]:
        """Write the checkpoint object (step 1). Returns the entry; commit it separately."""
        import time

        seq, key = self.next_entry_key(tier, position)
        started = time.monotonic()
        self.store.upload(key, payload, abort_check=abort_check)
        elapsed = time.monotonic() - started
        entry = ManifestEntry(
            seq=seq,
            key=key,
            tier=tier.value,
            epoch=position.epoch,
            step=position.step,
            size_bytes=len(payload),
            sha256=serialize.sha256(payload),
            created_at=utc_now_iso(),
            instance_id=self.instance_id,
        )
        return entry, elapsed

    def commit(self, entry: ManifestEntry) -> bool:
        """Point the manifest at ``entry`` (step 2), then prune (step 3).

        Returns False without changing anything if a newer checkpoint is already committed, or
        if this worker has lost the lease: a late background upload must not move the job back.
        """
        if self.fence is not None and self.fence():
            return False
        with self._lock:
            current = self._manifest
            if current is not None and int(current["seq"]) >= entry.seq:
                return False
            history: list[dict] = []
            if current is not None:
                previous = {k: v for k, v in current.items() if k != "history"}
                history = [previous, *current.get("history", [])][: max(self.keep_last - 1, 0)]
            manifest = {**asdict(entry), "history": history}
            self.store.put_bytes(self.manifest_key, json.dumps(manifest, indent=1).encode("utf-8"))
            self._manifest = manifest
        self.prune()
        return True

    def prune(self) -> None:
        """Delete old checkpoints, keeping the newest ``keep_last`` and all manifest entries."""
        with self._lock:
            manifest = self._manifest
        if manifest is None:
            return
        referenced = {manifest["key"], *(h["key"] for h in manifest.get("history", []))}
        try:
            keys = self.store.list_keys(f"{self.root}checkpoints/")
        except Exception as exc:  # noqa: BLE001
            self._log("error", where="prune", message=str(exc))
            return
        numbered = sorted(
            ((int(m.group(1)), key) for key in keys if (m := _KEY_RE.search(key))), reverse=True
        )
        manifest_seq = int(manifest["seq"])
        kept = 0
        for seq, key in numbered:
            if seq > manifest_seq:
                continue  # newer than the manifest: an upload still on its way
            if key in referenced or kept < self.keep_last:
                kept += 1
                continue
            try:
                self.store.delete(key)
            except Exception as exc:  # noqa: BLE001
                self._log("error", where="prune", message=str(exc))

    # Restore -----------------------------------------------------------------------------

    def _load_entry(self, entry: dict[str, Any], adapter: Checkpointable) -> Position:
        data = self.store.get_bytes(entry["key"])
        if serialize.sha256(data) != entry["sha256"]:
            raise ChecksumMismatch(entry["key"])
        envelope = serialize.loads(data)
        adapter.restore(envelope["state"])
        return Position(int(entry["epoch"]), int(entry["step"]))

    def restore(self, adapter: Checkpointable) -> RestoreResult:
        """Restore the newest valid checkpoint, trying older ones if it is damaged."""
        try:
            manifest = self.load_manifest()
        except (ValueError, KeyError) as exc:
            self._log("restore_fallback", key=self.manifest_key, error=f"bad manifest: {exc}")
            return self._restore_by_listing(adapter)
        if manifest is None:
            return RestoreResult(Position(), None)
        history = manifest.get("history", [])
        candidates = [{k: v for k, v in manifest.items() if k != "history"}, *history]
        for index, candidate in enumerate(candidates):
            try:
                position = self._load_entry(candidate, adapter)
            except Exception as exc:  # noqa: BLE001
                self._log(
                    "restore_fallback",
                    key=candidate.get("key"),
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            entry = ManifestEntry(**candidate)
            fields = {
                **getattr(adapter, "last_restore_info", {}),
                "key": entry.key,
                "seq": entry.seq,
                "tier": entry.tier,
                "epoch": position.epoch,
                "step": position.step,
                "fallback": index > 0,
            }
            self._log("restore_ok", **fields)
            return RestoreResult(position, entry, fallback=index > 0)
        self._log("restore_failed", tried=len(candidates))
        return RestoreResult(Position(), None)

    def _restore_by_listing(self, adapter: Checkpointable) -> RestoreResult:
        """The manifest itself is unreadable: try the checkpoint objects, newest first."""
        keys = self.store.list_keys(f"{self.root}checkpoints/")
        numbered = sorted(
            ((int(m.group(1)), key) for key in keys if (m := _KEY_RE.search(key))), reverse=True
        )
        for seq, key in numbered:
            try:
                data = self.store.get_bytes(key)
                envelope = serialize.loads(data)
                adapter.restore(envelope["state"])
            except Exception as exc:  # noqa: BLE001
                self._log("restore_fallback", key=key, error=f"{type(exc).__name__}: {exc}")
                continue
            position = Position(
                int(envelope["position"]["epoch"]), int(envelope["position"]["step"])
            )
            with self._lock:
                self._next_seq = max(self._next_seq, seq + 1)
            self._log(
                "restore_ok", key=key, seq=seq, epoch=position.epoch, step=position.step,
                fallback=True,
            )
            entry = ManifestEntry(
                seq=seq,
                key=key,
                tier=str(envelope.get("tier", "full")),
                epoch=position.epoch,
                step=position.step,
                size_bytes=len(data),
                sha256=serialize.sha256(data),
                created_at=utc_now_iso(),
                instance_id=self.instance_id,
            )
            return RestoreResult(position, entry, fallback=True)
        self._log("restore_failed", tried=len(numbered))
        return RestoreResult(Position(), None)

    # Status ------------------------------------------------------------------------------

    def read_status(self) -> dict[str, Any] | None:
        """Read status.json, or return None if the job has never run."""
        try:
            return json.loads(self.store.get_bytes(self.status_key).decode("utf-8"))
        except (ObjectNotFound, ValueError):
            return None

    def write_status(self, state: str, **fields: Any) -> None:
        """Write status.json."""
        body = {"state": state, "updated_at": utc_now_iso(), **fields}
        self.store.put_bytes(self.status_key, json.dumps(body, default=str).encode("utf-8"))
