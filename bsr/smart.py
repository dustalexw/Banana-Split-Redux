"""Boundary re-encoding ("smart rendering") - narrow, explicitly tested matrix.

Not enabled until the join has been demonstrated by tests for a codec/container
combination. See docs/SUPPORT_MATRIX.md.
"""
from __future__ import annotations

from .media import MediaInfo

SUPPORTED = {}   # filled in once a path is verified


def supported(info: MediaInfo) -> tuple[bool, str]:
    v = info.video
    key = (v.codec, "matroska" if "matroska" in info.format_name else info.format_name)
    if key in SUPPORTED:
        ok, why = SUPPORTED[key](info)
        return ok, why
    return False, f"Boundary re-encoding is not available for {v.codec} in {key[1]}"
