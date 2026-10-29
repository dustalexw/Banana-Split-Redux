"""Per-platform locations and small OS helpers (macOS, Windows, Linux).

    macOS    caches ~/Library/Caches/BananaSplitRedux, logs ~/Library/Logs/BananaSplitRedux
    Windows  %LOCALAPPDATA%\\BananaSplitRedux\\Cache and \\Logs
    Linux    $XDG_CACHE_HOME/BananaSplitRedux, ~/.local/state/BananaSplitRedux
"""
from __future__ import annotations

import os
import sys

APP_DIR = "BananaSplitRedux"
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


def _local_appdata() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")


def cache_root() -> str:
    if IS_MAC:
        return os.path.expanduser(f"~/Library/Caches/{APP_DIR}")
    if IS_WINDOWS:
        return os.path.join(_local_appdata(), APP_DIR, "Cache")
    return os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), APP_DIR)


def log_root() -> str:
    if IS_MAC:
        return os.path.expanduser(f"~/Library/Logs/{APP_DIR}")
    if IS_WINDOWS:
        return os.path.join(_local_appdata(), APP_DIR, "Logs")
    return os.path.join(os.path.expanduser("~/.local/state"), APP_DIR)


def hide(path: str) -> None:
    """Hide a working folder in Explorer (dot-names already hide it on macOS and Linux)."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        FILE_ATTRIBUTE_HIDDEN = 0x2
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs != -1 and attrs != 0xFFFFFFFF:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


def secret_store_name() -> str:
    return "macOS keychain" if IS_MAC else "Windows Credential Manager" if IS_WINDOWS else "this session only"


def alt_key_name() -> str:
    """How the Alt/Option modifier is labelled on this platform."""
    return "Option" if IS_MAC else "Alt"


def return_key_name() -> str:
    return "Return" if IS_MAC else "Enter"
