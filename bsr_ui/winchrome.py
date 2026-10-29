"""Colour a Qt window's Windows title bar to match the app (Windows 10 and 11).

Windows keeps its own title bar (snap layouts, the window buttons and system
menu all keep working). Windows 11 paints the caption in the app's charcoal with
cream text and a charcoal border, so it reads as part of the brand bar; Windows 10
gets the dark title bar. Everything goes through dwmapi with ctypes; on other
platforms, or if anything fails, ``integrate`` returns False and nothing changes.
"""
from __future__ import annotations

import sys

DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19   # Windows 10 1809–1909
DWMWA_USE_IMMERSIVE_DARK_MODE = 20       # Windows 10 2004+ and 11
DWMWA_BORDER_COLOR = 34                  # Windows 11
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


def available() -> bool:
    return sys.platform == "win32"


def colorref(hex_color: str) -> int:
    """'#rrggbb' -> COLORREF (0x00BBGGRR)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def integrate(window, caption: str, text: str, border: str | None = None) -> bool:
    """Dark title bar everywhere; caption/text/border colours where Windows supports them."""
    if not available():
        return False
    try:
        import ctypes
        from ctypes import wintypes
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwm.DwmSetWindowAttribute.restype = ctypes.c_long
        hwnd = wintypes.HWND(int(window.winId()))

        def set_attr(attr, value) -> bool:
            v = ctypes.c_int(value) if attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD) \
                else wintypes.DWORD(value)
            return dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0

        dark = set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE, 1) or set_attr(DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, 1)
        coloured = set_attr(DWMWA_CAPTION_COLOR, colorref(caption))
        if coloured:
            set_attr(DWMWA_TEXT_COLOR, colorref(text))
            set_attr(DWMWA_BORDER_COLOR, colorref(border or caption))
        return bool(dark or coloured)
    except Exception:
        return False
