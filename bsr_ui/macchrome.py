"""Blend a Qt main window's macOS title bar into the app's own colours.

The native title is hidden and the title bar made transparent, so the window's
content runs to the top edge and the traffic lights sit on the app's own
background. Everything goes through the Objective-C runtime with ctypes, so
there is no PyObjC dependency. On other platforms, or if anything fails, the
window keeps its normal title bar and ``integrate`` returns False.

Clicks in the title bar strip belong to macOS (dragging, double-click to
zoom), so keep interactive controls out of the top ``TITLEBAR_HEIGHT`` pixels
(``TOOLBAR_HEIGHT`` when a toolbar is requested).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import sys

TITLEBAR_HEIGHT = 28  # standard title bar strip
TOOLBAR_HEIGHT = 52   # unified toolbar strip; the traffic lights centre in it
TRAFFIC_LIGHTS_WIDTH = 78  # left edge to just past the zoom button

_FULL_SIZE_CONTENT_VIEW = 1 << 15
_TITLE_HIDDEN = 1
_TOOLBAR_STYLE_UNIFIED = 3
_SEPARATOR_NONE = 1

_objc = None
if sys.platform == "darwin":
    try:
        _objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "/usr/lib/libobjc.A.dylib")
        _objc.objc_getClass.restype = ctypes.c_void_p
        _objc.objc_getClass.argtypes = [ctypes.c_char_p]
        _objc.sel_registerName.restype = ctypes.c_void_p
        _objc.sel_registerName.argtypes = [ctypes.c_char_p]
        _MSG_SEND = ctypes.cast(_objc.objc_msgSend, ctypes.c_void_p).value
    except (OSError, AttributeError):
        _objc = None

_protos: dict = {}


def _send(obj, selector: str, *args, restype=ctypes.c_void_p, argtypes=()):
    """objc_msgSend with an exact prototype (required on arm64)."""
    key = (restype, tuple(argtypes))
    fn = _protos.get(key)
    if fn is None:
        fn = _protos[key] = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(_MSG_SEND)
    return fn(obj, _objc.sel_registerName(selector.encode()), *args)


def _cls(name: str):
    return _objc.objc_getClass(name.encode())


def _nsstring(text: str):
    return _send(_cls("NSString"), "stringWithUTF8String:", text.encode(), argtypes=(ctypes.c_char_p,))


def _responds(obj, selector: str) -> bool:
    return bool(_send(obj, "respondsToSelector:", _objc.sel_registerName(selector.encode()),
                      restype=ctypes.c_bool, argtypes=(ctypes.c_void_p,)))


def available() -> bool:
    return _objc is not None


def integrate(widget, background, toolbar: bool = False) -> bool:
    """Make ``widget``'s window title bar transparent over ``background`` (a QColor).

    Safe to call repeatedly: after theme changes, and after full-screen or
    other state changes, in case Qt rebuilt the window's style mask.
    """
    if _objc is None:
        return False
    try:
        view = int(widget.winId())
        window = _send(view, "window") if view else None
        if not window:
            return False
        bool_arg = (ctypes.c_bool,)
        mask = _send(window, "styleMask", restype=ctypes.c_ulong)
        if not mask & _FULL_SIZE_CONTENT_VIEW:
            _send(window, "setStyleMask:", mask | _FULL_SIZE_CONTENT_VIEW, restype=None, argtypes=(ctypes.c_ulong,))
        _send(window, "setTitlebarAppearsTransparent:", True, restype=None, argtypes=bool_arg)
        _send(window, "setTitleVisibility:", _TITLE_HIDDEN, restype=None, argtypes=(ctypes.c_long,))
        if _responds(window, "setTitlebarSeparatorStyle:"):
            _send(window, "setTitlebarSeparatorStyle:", _SEPARATOR_NONE, restype=None, argtypes=(ctypes.c_long,))

        r, g, b, a = background.redF(), background.greenF(), background.blueF(), background.alphaF()
        dbl = ctypes.c_double
        color = _send(_cls("NSColor"), "colorWithSRGBRed:green:blue:alpha:", r, g, b, a,
                      argtypes=(dbl, dbl, dbl, dbl))
        _send(window, "setBackgroundColor:", color, restype=None, argtypes=(ctypes.c_void_p,))
        # Traffic-light and resize-cursor contrast follow the window's appearance.
        dark = background.lightnessF() < 0.5
        name = _nsstring("NSAppearanceNameDarkAqua" if dark else "NSAppearanceNameAqua")
        appearance = _send(_cls("NSAppearance"), "appearanceNamed:", name, argtypes=(ctypes.c_void_p,))
        _send(window, "setAppearance:", appearance, restype=None, argtypes=(ctypes.c_void_p,))

        if toolbar and not _send(window, "toolbar"):
            # An empty toolbar gives the taller unified strip with centred traffic lights.
            tb = _send(_cls("NSToolbar"), "alloc")
            tb = _send(tb, "initWithIdentifier:", _nsstring("IntegratedTitleBar"), argtypes=(ctypes.c_void_p,))
            if _responds(tb, "setShowsBaselineSeparator:"):
                _send(tb, "setShowsBaselineSeparator:", False, restype=None, argtypes=bool_arg)
            _send(window, "setToolbar:", tb, restype=None, argtypes=(ctypes.c_void_p,))
            _send(tb, "release", restype=None)
            if _responds(window, "setToolbarStyle:"):
                _send(window, "setToolbarStyle:", _TOOLBAR_STYLE_UNIFIED, restype=None, argtypes=(ctypes.c_long,))
        return True
    except Exception:  # never let window chrome stop the app from opening
        return False
