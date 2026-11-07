"""Turn checkpoint state into bytes and back.

Three formats, chosen automatically from what the state contains:

* ``T``: torch.save, when there are tensors. Loaded with ``weights_only=True``.
* ``N``: JSON metadata followed by raw array bytes, when there are NumPy arrays. No pickle.
* ``J``: plain JSON otherwise (item-list jobs).
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import sys
import zlib
from typing import Any

import numpy as np

from spotsentry.errors import SpotSentryError

MAGIC = b"SSCK"
_HEADER = struct.Struct(">4sBcB")  # magic, version, format, compressed
_LEN = struct.Struct(">Q")


def sha256(data: bytes) -> str:
    """Return the SHA-256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _is_tensor(value: Any) -> bool:
    torch = sys.modules.get("torch")
    return torch is not None and isinstance(value, torch.Tensor)


def _contains(value: Any, predicate) -> bool:
    if predicate(value):
        return True
    if isinstance(value, dict):
        return any(_contains(v, predicate) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_contains(v, predicate) for v in value)
    return False


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _dump_numpy(state: Any) -> bytes:
    arrays: list[np.ndarray] = []

    def strip(value: Any) -> Any:
        if isinstance(value, np.ndarray):
            arrays.append(value)
            return {"__nd__": len(arrays) - 1}
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [strip(v) for v in value]
        return value

    meta = {
        "state": strip(state),
        "arrays": [
            {"dtype": a.dtype.str, "shape": list(a.shape), "nbytes": a.nbytes} for a in arrays
        ],
    }
    meta_bytes = json.dumps(meta, default=_json_default).encode("utf-8")
    views = [memoryview(a if a.flags["C_CONTIGUOUS"] else a.copy()).cast("B") for a in arrays]
    return b"".join([_LEN.pack(len(meta_bytes)), meta_bytes, *views])


def _load_numpy(payload: bytes) -> Any:
    meta_len = _LEN.unpack_from(payload, 0)[0]
    offset = _LEN.size
    meta = json.loads(payload[offset : offset + meta_len].decode("utf-8"))
    offset += meta_len
    arrays = []
    for spec in meta["arrays"]:
        dtype = np.dtype(spec["dtype"])
        count = spec["nbytes"] // dtype.itemsize
        array = np.frombuffer(payload, dtype=dtype, count=count, offset=offset)
        arrays.append(array.reshape(spec["shape"]).copy())
        offset += spec["nbytes"]

    def fill(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"__nd__"}:
                return arrays[value["__nd__"]]
            return {k: fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fill(v) for v in value]
        return value

    return fill(meta["state"])


def dumps(state: Any, compress: bool = False) -> bytes:
    """Serialize a state dict."""
    if _contains(state, _is_tensor):
        import torch

        buffer = io.BytesIO()
        torch.save(state, buffer)
        fmt, payload = b"T", buffer.getvalue()
    elif _contains(state, lambda v: isinstance(v, np.ndarray)):
        fmt, payload = b"N", _dump_numpy(state)
    else:
        fmt, payload = b"J", json.dumps(state, default=_json_default).encode("utf-8")
    if compress:
        payload = zlib.compress(payload, level=1)
    return b"".join([_HEADER.pack(MAGIC, 1, fmt, 1 if compress else 0), payload])


def loads(data: bytes) -> Any:
    """Deserialize bytes from :func:`dumps`. Raises SpotSentryError if they are not a checkpoint."""
    if len(data) < _HEADER.size:
        raise SpotSentryError("checkpoint too short")
    magic, version, fmt, compressed = _HEADER.unpack_from(data, 0)
    if magic != MAGIC or version != 1:
        raise SpotSentryError("not a SpotSentry checkpoint")
    payload = data[_HEADER.size :]
    if compressed:
        payload = zlib.decompress(payload)
    if fmt == b"T":
        import torch

        return torch.load(io.BytesIO(payload), map_location="cpu", weights_only=True)
    if fmt == b"N":
        return _load_numpy(payload)
    if fmt == b"J":
        return json.loads(payload.decode("utf-8"))
    raise SpotSentryError(f"unknown checkpoint format {fmt!r}")
