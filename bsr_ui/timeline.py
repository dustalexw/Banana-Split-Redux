"""Timeline widgets: whole-file overview and a frame-level detail strip."""
from __future__ import annotations

from fractions import Fraction

import numpy as np
from PySide6.QtCore import Qt, QRectF, Signal, QPointF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont
from PySide6.QtWidgets import QWidget, QToolTip

from bsr.media import fmt_time

STATUS_COLORS = {
    "copy": QColor("#78dca0"),
    "copy-drop": QColor("#aadcb0"),
    "reencode": QColor("#f6a0be"),
    "needs-review": QColor("#fad640"),
    "skipped": QColor("#888888"),
    "rejected": QColor("#777777"),
    "proposed": QColor("#b0b0b0"),
}


class Overview(QWidget):
    """Whole file: parts, black intervals, cuts, copy boundaries."""
    candidateClicked = Signal(int)
    timeClicked = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(76)
        self.setMouseTracking(True)
        self.duration = 0.0
        self.t0 = 0.0
        self.items = []         # dicts: id, start, end, status, selected
        self.keyframes = np.zeros(0)
        self.cuts = []          # (seconds, status)
        self.parts = []         # (start, end, removed)
        self.selected = None
        self.midpoint_region = None
        self.recommended_id = None
        self.break_regions = []    # (start, end) windows around each expected segment break
        self.recommended_ids = set()

    def set_data(self, duration, t0, items, keyframes, cuts, parts, selected):
        self.duration, self.t0 = max(duration, 1e-6), t0
        self.items, self.keyframes, self.cuts, self.parts = items, keyframes, cuts, parts
        self.selected = selected
        self.midpoint_region = None; self.recommended_id = None
        self.break_regions, self.recommended_ids = [], set()
        self.update()

    def set_midpoint(self, region, recommended_id=None):
        self.set_breaks([region] if region is not None else [], [recommended_id] if recommended_id is not None else [])

    def set_breaks(self, regions, recommended_ids=()):
        """Highlight every expected segment break and its recommended black transitions."""
        self.break_regions = list(regions or [])
        self.recommended_ids = {k for k in recommended_ids if k is not None}
        self.midpoint_region = self.break_regions[0] if len(self.break_regions) == 1 else None
        self.recommended_id = next(iter(recommended_ids), None) if recommended_ids else None
        self.update()

    def x_of(self, t):
        return 8 + (self.width() - 16) * (t - self.t0) / self.duration

    def t_of(self, x):
        return self.t0 + (x - 8) / max(1, self.width() - 16) * self.duration

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        pal = self.palette()
        h = self.height()
        base = pal.base().color()
        p.fillRect(self.rect(), pal.window())
        bar = QRectF(8, 14, self.width() - 16, h - 30)
        # Perforations repeat the film band in the app icon.
        for x in range(12, self.width() - 12, 24):
            for y in (4, h - 23):
                p.fillRect(QRectF(x, y, 7, 5), QColor("#464854"))
        p.fillRect(bar, base.darker(108))
        # parts
        for i, (a, b, removed) in enumerate(self.parts):
            r = QRectF(self.x_of(a), bar.top(), max(1.0, self.x_of(b) - self.x_of(a)), bar.height())
            p.fillRect(r, QColor(250, 214, 64, 25 if i % 2 == 0 else 45) if not removed else QColor(200, 60, 60, 90))
        for lo, hi in self.break_regions:
            p.fillRect(QRectF(self.x_of(lo), bar.top(), self.x_of(hi) - self.x_of(lo), bar.height()), QColor(120, 220, 160, 28))
            p.setPen(QPen(QColor(120, 220, 160, 100), 1, Qt.DashLine))
            p.drawLine(QPointF(self.x_of((lo + hi) / 2), bar.top()), QPointF(self.x_of((lo + hi) / 2), bar.bottom()))
        # keyframes (copy boundaries) as faint ticks
        if len(self.keyframes):
            p.setPen(QPen(QColor(128, 128, 128, 90), 1))
            step = max(1, len(self.keyframes) // 2000)
            for t in self.keyframes[::step]:
                x = self.x_of(float(t))
                p.drawLine(QPointF(x, bar.bottom() - 6), QPointF(x, bar.bottom()))
        # black intervals
        for it in self.items:
            col = STATUS_COLORS.get(it["status"], STATUS_COLORS["proposed"])
            x0, x1 = self.x_of(it["start"]), self.x_of(it["end"])
            r = QRectF(x0, bar.top() + 4, max(3.0, x1 - x0), bar.height() - 12)
            p.fillRect(r, QColor(10, 10, 10))
            recommended = it["id"] in self.recommended_ids
            p.fillRect(QRectF(r.left(), bar.top(), r.width(), 4), QColor("#78dca0") if recommended else col)
            if recommended:
                p.setPen(QPen(QColor("#78dca0"), 2))
                p.drawRect(r.adjusted(-2, -3, 2, 2))
            if it["id"] == self.selected:
                p.setPen(QPen(pal.highlight().color(), 2))
                p.drawRect(r.adjusted(-2, -6, 2, 4))
        # planned cuts
        for t, st in self.cuts:
            x = self.x_of(t)
            p.setPen(QPen(STATUS_COLORS.get(st, QColor("white")), 2))
            p.drawLine(QPointF(x, 6), QPointF(x, h - 14))
        # time labels
        p.setPen(pal.text().color())
        f = QFont(self.font()); f.setPointSizeF(max(7.0, f.pointSizeF() - 2)); p.setFont(f)
        p.drawText(QRectF(8, h - 14, 200, 14), Qt.AlignLeft, fmt_time(self.t0)[:-4])
        p.drawText(QRectF(self.width() - 208, h - 14, 200, 14), Qt.AlignRight, fmt_time(self.t0 + self.duration)[:-4])
        n = len(self.break_regions)
        p.setPen(QColor("#78dca0"))
        for k, (lo, hi) in enumerate(self.break_regions, 1):
            text = "Halfway" if n == 1 else f"Break {k}"
            p.drawText(QRectF(self.x_of((lo + hi) / 2) - 35, h - 14, 70, 14), Qt.AlignCenter, text)
        p.end()

    def mousePressEvent(self, ev):
        t = self.t_of(ev.position().x())
        best, bd = None, 1e9
        for it in self.items:
            d = 0 if it["start"] <= t <= it["end"] else min(abs(t - it["start"]), abs(t - it["end"]))
            if d < bd:
                best, bd = it["id"], d
        px = bd / self.duration * (self.width() - 16)
        if best is not None and px < 8:
            self.candidateClicked.emit(best)
        else:
            self.timeClicked.emit(t)

    def mouseMoveEvent(self, ev):
        QToolTip.showText(ev.globalPosition().toPoint(), fmt_time(self.t_of(ev.position().x())), self)


class DetailStrip(QWidget):
    """Frame-level view around one cut: black frames, keyframes, requested and planned cut."""
    frameClicked = Signal(int)   # pts

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(70)
        self.pts = np.zeros(0, dtype=np.int64)
        self.black = np.zeros(0, dtype=bool)
        self.tb = Fraction(1, 1000)
        self.keys = {}       # pts -> "ok" | "drop" | "bad"
        self.dropped = set()
        self.requested = None
        self.planned = []
        self.removed = None
        self.current = None
        self.interval = None

    def set_data(self, pts, black, tb, keys, dropped, requested, planned, removed, interval, current=None):
        self.pts, self.black, self.tb = pts, black, tb
        self.keys, self.dropped = keys, set(dropped)
        self.requested, self.planned, self.removed, self.interval = requested, planned, removed, interval
        self.current = current
        self.update()

    def set_current(self, pts):
        self.current = pts
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        pal = self.palette()
        p.fillRect(self.rect(), pal.window())
        n = len(self.pts)
        if n == 0:
            p.drawText(self.rect(), Qt.AlignCenter, "Select a cut to see its frames")
            p.end(); return
        w = (self.width() - 16) / n
        top, hh = 18, self.height() - 34
        for i, (pt, b) in enumerate(zip(self.pts, self.black)):
            x = 8 + i * w
            r = QRectF(x, top, max(1.0, w - (1 if w > 4 else 0)), hh)
            col = QColor(15, 15, 15) if b else QColor("#a18e78")
            if int(pt) in self.dropped:
                col = QColor(140, 40, 40)
            if self.removed and self.removed[0] <= pt < self.removed[1]:
                col = QColor(90, 30, 30)
            p.fillRect(r, col)
            k = self.keys.get(int(pt))
            if k:
                c = {"ok": QColor("#78dca0"), "drop": QColor("#c8b040"), "bad": QColor("#c04040")}[k]
                p.fillRect(QRectF(x, top - 8, max(2.0, w - 1), 6), c)
            if self.current is not None and int(pt) == int(self.current):
                p.setPen(QPen(pal.highlight().color(), 2))
                p.drawRect(r.adjusted(0, -2, 0, 2))
        def xpos(ptv):
            i = int(np.searchsorted(self.pts, ptv))
            return 8 + i * w
        if self.requested is not None:
            p.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.DashLine))
            x = xpos(self.requested); p.drawLine(QPointF(x, 4), QPointF(x, self.height() - 4))
        for ptv, st in self.planned:
            p.setPen(QPen(STATUS_COLORS.get(st, QColor("white")), 3))
            x = xpos(ptv); p.drawLine(QPointF(x, 2), QPointF(x, self.height() - 2))
        p.setPen(pal.text().color())
        f = QFont(self.font()); f.setPointSizeF(max(7.0, f.pointSizeF() - 2)); p.setFont(f)
        p.drawText(QRectF(8, self.height() - 15, 300, 14), Qt.AlignLeft, fmt_time(float(self.pts[0] * self.tb)))
        p.drawText(QRectF(self.width() - 308, self.height() - 15, 300, 14), Qt.AlignRight, fmt_time(float(self.pts[-1] * self.tb)))
        p.drawText(QRectF(8, 0, self.width() - 16, 14), Qt.AlignCenter,
                   "Mint: safe keyframe · Yellow: frame drop · Dashed: requested · Solid: planned")
        p.end()

    def mousePressEvent(self, ev):
        n = len(self.pts)
        if not n:
            return
        i = int((ev.position().x() - 8) / max(1e-6, (self.width() - 16) / n))
        if 0 <= i < n:
            self.frameClicked.emit(int(self.pts[i]))
