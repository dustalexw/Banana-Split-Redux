"""Analysis cache.

Keys combine the source fingerprint, the analysis settings, the engine version and
the FFmpeg library versions, so stale results are never reused after any of those
change. The cache never lives next to source media.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile

import av
import numpy as np

from . import ENGINE_VERSION


def cache_dir() -> str:
    d = os.environ.get("BSR_CACHE_DIR")
    if not d:
        if sys.platform == "darwin":
            d = os.path.expanduser("~/Library/Caches/BananaSplitRedux")
        else:
            d = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "BananaSplitRedux")
    os.makedirs(d, exist_ok=True)
    return d


def tool_versions() -> str:
    v = getattr(av, "ffmpeg_version_info", "")
    return f"engine={ENGINE_VERSION};pyav={av.__version__};ffmpeg={v}"


def key(*parts) -> str:
    h = hashlib.sha1()
    h.update(tool_versions().encode())
    for p in parts:
        h.update(b"\0")
        h.update(json.dumps(p, sort_keys=True, default=str).encode() if not isinstance(p, str) else p.encode())
    return h.hexdigest()[:24]


def _atomic_write(path: str, write):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    os.close(fd)
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_npz(k: str, arrays: dict):
    path = os.path.join(cache_dir(), k + ".npz")

    def w(tmp):
        with open(tmp, "wb") as f:
            np.savez_compressed(f, **arrays)
    _atomic_write(path, w)


def load_npz(k: str):
    if os.environ.get("BSR_NO_CACHE"):
        return None
    path = os.path.join(cache_dir(), k + ".npz")
    if not os.path.exists(path):
        return None
    try:
        return np.load(path, allow_pickle=False)
    except Exception:
        return None


def save_json(k: str, obj):
    path = os.path.join(cache_dir(), k + ".json")

    def w(tmp):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
    _atomic_write(path, w)


def load_json(k: str):
    if os.environ.get("BSR_NO_CACHE"):
        return None
    path = os.path.join(cache_dir(), k + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
