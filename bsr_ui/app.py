"""Banana Split Redux - desktop interface (PySide6 / Qt 6).

Workflow: import -> analyse -> review cuts -> choose mode -> review export plan -> export & validate.
All media work runs in background jobs (bsr_ui.workers); the interface thread only
draws and reacts.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict
from fractions import Fraction

import numpy as np
from PySide6.QtCore import Qt, QTimer, QSize, QSettings, QUrl
from PySide6.QtGui import QAction, QImage, QPixmap, QKeySequence, QDesktopServices, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QComboBox, QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox,
    QFormLayout, QDoubleSpinBox, QSpinBox, QCheckBox, QLineEdit, QFileDialog, QMessageBox, QRadioButton,
    QButtonGroup, QTextBrowser, QAbstractItemView, QToolButton, QGridLayout, QSizePolicy, QFrame, QStatusBar)

from bsr import ENGINE_VERSION
from bsr.detect import PRESETS, AnalysisRegion, black_mask
from bsr.export import (ExportOptions, export_plan, default_streams, preservation_report, output_format,
                        plan_segment)
from bsr.frames import FrameReader
from bsr.media import fmt_time, Cancelled, NOPTS
from bsr.planner import default_names
from bsr.session import SourceSession
from bsr.cache import tool_versions

from .dialogs import PlanDialog, ResultsDialog, LogDialog, TracksDialog
from .timeline import Overview, DetailStrip, STATUS_COLORS
from .workers import Job, Pools

APP_NAME = "Banana Split Redux"
MEDIA_EXT = {".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts", ".mts", ".avi", ".webm", ".mpg", ".mpeg", ".vob"}

STATUS_LABEL = {
    "copy": "Copied", "copy-drop": "Copied (drops black frames)", "needs-review": "Needs review",
    "reencode": "Boundary re-encoding", "skipped": "Skipped", "rejected": "Rejected",
}


def log_path():
    d = os.path.expanduser("~/Library/Logs/BananaSplitRedux") if sys.platform == "darwin" else \
        os.path.join(os.path.expanduser("~/.local/state"), "BananaSplitRedux")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "bsr.log")


def frame_to_qimage(frame, width=None, region=None, sar=None) -> QImage:
    arr = frame.to_ndarray(format="rgb24")
    h, w, _ = arr.shape
    img = QImage(arr.data, w, h, 3 * w, QImage.Format_RGB888).copy()
    if sar:
        try:
            n, d = (int(x) for x in str(sar).replace(":", "/").split("/"))
            if n and d and n != d:
                img = img.scaled(round(w * n / d), h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        except Exception:
            pass
    if region is not None:
        from PySide6.QtGui import QPainter, QPen
        p = QPainter(img)
        p.setPen(QPen(QColor(255, 210, 0), max(1, img.width() // 300)))
        W, H = img.width(), img.height()
        p.drawRect(int(W * region.left), int(H * region.top),
                   W - int(W * region.right) - int(W * region.left) - 1, H - int(H * region.bottom) - int(H * region.top) - 1)
        p.end()
    if width:
        img = img.scaledToWidth(width, Qt.SmoothTransformation)
    return img


class Item:
    """One file in the batch queue."""

    def __init__(self, path):
        self.path = path
        self.session: SourceSession | None = None
        self.state = "queued"      # queued | analysing | proving | ready | exporting | done | error
        self.progress = 0.0
        self.error = ""
        self.streams = None
        self.out_dir = ""
        self.last_report = None

    def label(self):
        name = os.path.basename(self.path)
        st = {"queued": "Not analysed", "analysing": f"Analysing {self.progress*100:.0f}%",
              "proving": f"Checking cut points {self.progress*100:.0f}%", "ready": "Ready for review",
              "exporting": f"Exporting {self.progress*100:.0f}%", "done": "Exported", "error": f"Error: {self.error}"}[self.state]
        if self.state == "ready" and self.session:
            n = sum(1 for d in self.session.decisions.values() if d.state == "accepted")
            nr = sum(1 for k, rc in self.session.resolved.items()
                     if rc.status == "needs-review" and self.session.decisions[k].state == "accepted")
            st = f"{len(self.session.candidates)} candidates · {n} accepted" + (f" · {nr} need review" if nr else "")
        return f"{name}\n{st}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 940)
        self.setAcceptDrops(True)
        self.pools = Pools()
        self.items: list[Item] = []
        self.cur: Item | None = None
        self.sel_id = None
        self.preview_pts = None
        self.preview_frames = []
        self.play_timer = QTimer(self); self.play_timer.timeout.connect(self._play_tick)
        self.logs = []
        self.settings = QSettings("BananaSplitRedux", "BananaSplitRedux")
        self._jobs = set()
        self._build()
        self.log(f"{APP_NAME} {ENGINE_VERSION} — {tool_versions()}")

    # ------------------------------------------------------------------ layout
    def _build(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        a = QAction("Add Files…", self); a.setShortcut(QKeySequence.Open); a.triggered.connect(self.add_files_dialog); fm.addAction(a)
        a = QAction("Open Project…", self); a.triggered.connect(self.open_project); fm.addAction(a)
        a = QAction("Save Project…", self); a.setShortcut(QKeySequence.Save); a.triggered.connect(self.save_project); fm.addAction(a)
        vm = mb.addMenu("&View")
        a = QAction("Diagnostic Log", self); a.setShortcut("Ctrl+L"); a.triggered.connect(self.show_log); vm.addAction(a)
        a = QAction("Last Export Report", self); a.triggered.connect(self.show_last_report); vm.addAction(a)

        root = QSplitter(Qt.Horizontal)
        self.setCentralWidget(root)
        # queue
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(8, 8, 4, 8)
        ll.addWidget(QLabel("<b>Queue</b> — drop video files here"))
        self.queue = QListWidget(); self.queue.setWordWrap(True); self.queue.setSpacing(2)
        self.queue.currentRowChanged.connect(self.select_item)
        ll.addWidget(self.queue, 1)
        hb = QHBoxLayout()
        b = QPushButton("Add…"); b.clicked.connect(self.add_files_dialog); hb.addWidget(b)
        b = QPushButton("Remove"); b.clicked.connect(self.remove_item); hb.addWidget(b)
        b = QPushButton("Analyse all"); b.clicked.connect(self.analyse_all); hb.addWidget(b)
        ll.addLayout(hb)
        root.addWidget(left)

        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(4, 8, 8, 8)
        root.addWidget(right)
        root.setSizes([280, 1160])

        # header
        hdr = QHBoxLayout()
        self.title = QLabel("<i>Add a file to begin</i>"); self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.title.setWordWrap(True)
        hdr.addWidget(self.title, 1)
        hdr.addWidget(QLabel("Detection:"))
        self.preset = QComboBox()
        for name in ("conservative", "balanced", "sensitive", "single-frame", "custom"):
            self.preset.addItem(name.capitalize(), name)
        self.preset.setCurrentIndex(1)
        self.preset.currentIndexChanged.connect(self.preset_changed)
        hdr.addWidget(self.preset)
        self.btn_settings = QToolButton(); self.btn_settings.setText("Settings"); self.btn_settings.setCheckable(True)
        self.btn_settings.toggled.connect(lambda v: self.settings_box.setVisible(v))
        hdr.addWidget(self.btn_settings)
        self.btn_analyse = QPushButton("Analyse"); self.btn_analyse.clicked.connect(self.analyse_current)
        hdr.addWidget(self.btn_analyse)
        self.btn_cancel = QPushButton("Cancel"); self.btn_cancel.clicked.connect(self.cancel_jobs); self.btn_cancel.setEnabled(False)
        hdr.addWidget(self.btn_cancel)
        rl.addLayout(hdr)
        self.info_lbl = QLabel(""); self.info_lbl.setWordWrap(True); self.info_lbl.setStyleSheet("color: palette(mid);")
        rl.addWidget(self.info_lbl)
        self.prog = QProgressBar(); self.prog.setMaximumHeight(10); self.prog.setTextVisible(False); self.prog.hide()
        rl.addWidget(self.prog)
        rl.addWidget(self._build_settings())

        self.overview = Overview(); self.overview.candidateClicked.connect(self.select_candidate)
        self.overview.timeClicked.connect(self.preview_time)
        rl.addWidget(self.overview)

        mid = QSplitter(Qt.Horizontal)
        rl.addWidget(mid, 3)
        # candidates table
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(["#", "Black from", "Length", "Frames", "Type", "Score", "Decision", "Cut"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._table_sel)
        self.show_suppressed = QCheckBox("Show low-priority candidates"); self.show_suppressed.toggled.connect(self.refresh_table)
        tw = QWidget(); tl = QVBoxLayout(tw); tl.setContentsMargins(0, 0, 0, 0); tl.addWidget(self.table); tl.addWidget(self.show_suppressed)
        mid.addWidget(tw)
        mid.addWidget(self._build_detail())
        mid.setSizes([520, 640])

        rl.addWidget(self._build_parts(), 2)

        self.status = QStatusBar(); self.setStatusBar(self.status)
        self._set_enabled(False)

    def _build_settings(self):
        box = QGroupBox("Detection settings")
        g = QGridLayout(box)
        def dspin(lo, hi, step, dec, suffix=""):
            s = QDoubleSpinBox(); s.setRange(lo, hi); s.setSingleStep(step); s.setDecimals(dec); s.setSuffix(suffix)
            return s
        self.s_thr = dspin(0, 25, 0.5, 1, " %"); self.s_thr.setToolTip("A pixel counts as dark when its brightness is at or below this level (0% = nominal black, 100% = nominal white). Uses the stream's signalled range and bit depth.")
        self.s_ratio = dspin(50, 100, 0.5, 1, " %"); self.s_ratio.setToolTip("Share of pixels in the analysis region that must be dark. Lower it to tolerate small logos or burned-in text.")
        self.s_minf = QSpinBox(); self.s_minf.setRange(1, 1000); self.s_minf.setToolTip("Shortest black interval, in frames (1 = single-frame detection)")
        self.s_mind = dspin(0, 30, 0.04, 2, " s")
        self.s_space = dspin(0, 3600, 1, 1, " s"); self.s_space.setToolTip("Weaker candidates closer than this to a stronger one are marked low-priority")
        self.s_bridge = QSpinBox(); self.s_bridge.setRange(0, 25); self.s_bridge.setToolTip("Join black runs separated by up to this many non-black frames")
        self.r_l, self.r_t, self.r_r, self.r_b = (dspin(0, 45, 0.5, 1, " %") for _ in range(4))
        for w in (self.r_l, self.r_t, self.r_r, self.r_b):
            w.setToolTip("Analysis-only crop (does not change exported video). Use top/bottom for letterboxing.")
        self.s_fast = QCheckBox("Fast scan (approximate, verified at full resolution)")
        self.s_drop = QSpinBox(); self.s_drop.setRange(0, 8)
        self.s_drop.setToolTip("Open-GOP sources: allow dropping up to this many undecodable BLACK frames at a copy cut without asking (0 = always ask)")
        self.s_minpart = dspin(0, 600, 1, 1, " s")
        rows = [("Darkness threshold", self.s_thr), ("Dark pixels required", self.s_ratio),
                ("Minimum frames", self.s_minf), ("Minimum duration", self.s_mind),
                ("Minimum spacing", self.s_space), ("Bridge gaps (frames)", self.s_bridge)]
        for i, (lbl, w) in enumerate(rows):
            g.addWidget(QLabel(lbl), i // 3, (i % 3) * 2); g.addWidget(w, i // 3, (i % 3) * 2 + 1)
        g.addWidget(QLabel("Analysis region crop L/T/R/B"), 2, 0)
        rr = QHBoxLayout(); [rr.addWidget(w) for w in (self.r_l, self.r_t, self.r_r, self.r_b)]
        g.addLayout(rr, 2, 1, 1, 3)
        g.addWidget(self.s_fast, 2, 4, 1, 2)
        g.addWidget(QLabel("Auto-accept open-GOP black drop ≤"), 3, 0); g.addWidget(self.s_drop, 3, 1)
        g.addWidget(QLabel("Minimum part length"), 3, 2); g.addWidget(self.s_minpart, 3, 3)
        b = QPushButton("Apply"); b.clicked.connect(self.apply_settings); g.addWidget(b, 3, 5)
        self.settings_box = box
        box.hide()
        for w in (self.s_thr, self.s_ratio, self.s_minf, self.s_mind, self.s_space, self.s_bridge):
            (w.valueChanged).connect(lambda *_: self._mark_custom())
        return box

    def _build_detail(self):
        w = QWidget(); l = QVBoxLayout(w); l.setContentsMargins(4, 0, 0, 0)
        self.d_title = QLabel("<i>Select a candidate</i>"); self.d_title.setWordWrap(True)
        l.addWidget(self.d_title)
        # thumbnails before / after the planned cut
        thumbs = QHBoxLayout()
        self.thumbs = []
        for i in range(6):
            col = QVBoxLayout()
            lb = QLabel(); lb.setFixedSize(132, 100); lb.setAlignment(Qt.AlignCenter); lb.setStyleSheet("background:#111;")
            cap = QLabel(""); cap.setAlignment(Qt.AlignCenter); cap.setStyleSheet("font-size:10px;")
            col.addWidget(lb); col.addWidget(cap)
            thumbs.addLayout(col)
            if i == 2:
                sep = QFrame(); sep.setFrameShape(QFrame.VLine); sep.setStyleSheet("color:#3fa66b;"); thumbs.addWidget(sep)
            self.thumbs.append((lb, cap))
        l.addLayout(thumbs)
        self.strip = DetailStrip(); self.strip.frameClicked.connect(self.preview_pts_at)
        l.addWidget(self.strip)
        # preview + stepping
        pv = QHBoxLayout()
        self.preview = QLabel(); self.preview.setMinimumSize(360, 270); self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background:#000;")
        pv.addWidget(self.preview, 1)
        ctl = QVBoxLayout()
        self.pv_lbl = QLabel("—"); ctl.addWidget(self.pv_lbl)
        row = QHBoxLayout()
        for txt, d in (("◀◀", -10), ("◀", -1), ("▶", 1), ("▶▶", 10)):
            b = QToolButton(); b.setText(txt); b.clicked.connect(lambda _, d=d: self.step(d)); row.addWidget(b)
        ctl.addLayout(row)
        self.btn_play = QPushButton("Play ±2 s around cut"); self.btn_play.clicked.connect(self.play_around)
        self.btn_play.setToolTip("Silent frame preview from the decoded source (no audio)")
        ctl.addWidget(self.btn_play)
        self.chk_region = QCheckBox("Show analysis region"); self.chk_region.toggled.connect(lambda *_: self._show_preview_frame())
        ctl.addWidget(self.chk_region)
        self.btn_manual = QPushButton("Add cut at this frame"); self.btn_manual.clicked.connect(self.add_manual_cut)
        ctl.addWidget(self.btn_manual)
        ctl.addStretch(1)
        pv.addLayout(ctl)
        l.addLayout(pv, 1)
        # decision controls
        dec = QGroupBox("Decision")
        dl = QGridLayout(dec)
        self.btn_accept = QPushButton("Accept"); self.btn_accept.clicked.connect(lambda: self.set_state("accepted"))
        self.btn_reject = QPushButton("Reject"); self.btn_reject.clicked.connect(lambda: self.set_state("rejected"))
        dl.addWidget(self.btn_accept, 0, 0); dl.addWidget(self.btn_reject, 0, 1)
        dl.addWidget(QLabel("Split at"), 0, 2)
        self.pos = QComboBox(); [self.pos.addItem(t, v) for t, v in (("start of black", "start"), ("middle of black", "middle"), ("end of black", "end"))]
        self.pos.currentIndexChanged.connect(self.decision_changed)
        dl.addWidget(self.pos, 0, 3)
        self.chk_remove = QCheckBox("Remove black"); self.chk_remove.toggled.connect(self.decision_changed)
        self.chk_remove.setToolTip("Splitting keeps all content. Removing black also removes any audio under it.")
        dl.addWidget(self.chk_remove, 1, 0)
        dl.addWidget(QLabel("keep before/after"), 1, 1)
        self.pad_b = QDoubleSpinBox(); self.pad_a = QDoubleSpinBox()
        for s in (self.pad_b, self.pad_a):
            s.setRange(0, 10); s.setDecimals(2); s.setSingleStep(0.04); s.setSuffix(" s"); s.valueChanged.connect(self.decision_changed)
        dl.addWidget(self.pad_b, 1, 2); dl.addWidget(self.pad_a, 1, 3)
        self.choice_box = QWidget(); self.choice_lay = QVBoxLayout(self.choice_box); self.choice_lay.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.choice_box, 2, 0, 1, 4)
        self.msgs = QLabel(""); self.msgs.setWordWrap(True); self.msgs.setStyleSheet("font-size:11px;")
        dl.addWidget(self.msgs, 3, 0, 1, 4)
        l.addWidget(dec)
        self.detail_widgets = [self.btn_accept, self.btn_reject, self.pos, self.chk_remove, self.pad_a, self.pad_b,
                               self.btn_play, self.btn_manual]
        return w

    def _build_parts(self):
        box = QGroupBox("Output parts")
        l = QVBoxLayout(box)
        self.parts = QTableWidget(0, 6)
        self.parts.setHorizontalHeaderLabels(["#", "From", "To", "Length", "File name (editable)", "Notes"])
        self.parts.verticalHeader().hide()
        self.parts.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.parts.itemChanged.connect(self.part_renamed)
        l.addWidget(self.parts)
        row = QHBoxLayout()
        row.addWidget(QLabel("Output folder:"))
        self.out_edit = QLineEdit(self.settings.value("out_dir", "")); row.addWidget(self.out_edit, 1)
        b = QPushButton("Choose…"); b.clicked.connect(self.choose_out); row.addWidget(b)
        row.addWidget(QLabel("Mode:"))
        self.mode = QComboBox()
        self.mode.addItem("No re-encoding (strict)", "copy")
        self.mode.addItem("Exact cuts — boundary re-encoding", "smart")
        self.mode.addItem("Full re-encode", "full")
        self.mode.model().item(1).setEnabled(False)
        self.mode.model().item(2).setEnabled(False)
        self.mode.setItemData(1, "Not available for this file's codec/container (see support matrix)", Qt.ToolTipRole)
        self.mode.setItemData(2, "Not offered in this version", Qt.ToolTipRole)
        row.addWidget(self.mode)
        b = QPushButton("Tracks…"); b.clicked.connect(self.choose_tracks); row.addWidget(b)
        self.btn_export = QPushButton("Review && Export…"); self.btn_export.clicked.connect(self.review_export)
        self.btn_export.setDefault(True)
        row.addWidget(self.btn_export)
        l.addLayout(row)
        return box

    # ------------------------------------------------------------------ helpers
    def log(self, msg):
        line = time.strftime("%H:%M:%S ") + str(msg)
        self.logs.append(line)
        try:
            with open(log_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _set_enabled(self, on):
        for w in (self.btn_analyse, self.preset, self.btn_export, self.overview):
            w.setEnabled(on)

    def _busy(self, on, text=""):
        self.prog.setVisible(on)
        self.btn_cancel.setEnabled(on)
        if text:
            self.status.showMessage(text)

    def _run(self, fn, *args, on_done=None, on_progress=None, pool="media", **kw):
        job = Job(fn, *args, **kw)
        self._jobs.add(job)
        def done(res, job=job):
            self._jobs.discard(job)
            if on_done:
                on_done(res)
        def failed(msg, job=job):
            self._jobs.discard(job)
            self._busy(False)
            self.log("ERROR " + msg)
            if "Cancelled" in msg:
                self.status.showMessage("Cancelled")
            else:
                QMessageBox.warning(self, APP_NAME, msg)
            self.refresh_queue()
        job.signals.done.connect(done)
        job.signals.failed.connect(failed)
        job.signals.log.connect(self.log)
        if on_progress:
            job.signals.progress.connect(on_progress)
        (self.pools.media if pool == "media" else self.pools.preview).start(job)
        return job

    def cancel_jobs(self):
        for j in list(self._jobs):
            j.cancel.set()

    # ------------------------------------------------------------------ queue
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        self.add_paths(paths)

    def add_files_dialog(self):
        last = self.settings.value("last_dir", os.path.expanduser("~"))
        paths, _ = QFileDialog.getOpenFileNames(self, "Add video files", last,
                                                "Video (" + " ".join("*" + e for e in sorted(MEDIA_EXT)) + ");;All files (*)")
        if paths:
            self.settings.setValue("last_dir", os.path.dirname(paths[0]))
            self.add_paths(paths)

    def add_paths(self, paths):
        for p in paths:
            if os.path.isdir(p):
                for n in sorted(os.listdir(p)):
                    if os.path.splitext(n)[1].lower() in MEDIA_EXT:
                        self.add_paths([os.path.join(p, n)])
                continue
            if any(i.path == p for i in self.items):
                continue
            it = Item(p)
            self.items.append(it)
            self.log(f"Added {p}")
            self._run(self._job_probe, it, on_done=lambda _, it=it: self._probed(it))
        self.refresh_queue()
        if self.cur is None and self.items:
            self.queue.setCurrentRow(0)
            self.select_item(0)

    @staticmethod
    def _job_probe(job, it: Item):
        it.session = SourceSession(it.path)
        return it

    def _probed(self, it):
        it.streams = default_streams(it.session.info)
        if not self.out_edit.text():
            self.out_edit.setText(os.path.join(os.path.dirname(it.path), "Banana Split Output"))
        self.refresh_queue()
        if it is self.cur:
            self.show_item()

    def remove_item(self):
        r = self.queue.currentRow()
        if 0 <= r < len(self.items):
            del self.items[r]
            self.cur = None
            self.refresh_queue()
            self.queue.setCurrentRow(min(r, len(self.items) - 1))

    def refresh_queue(self):
        cur = self.queue.currentRow()
        self.queue.blockSignals(True)
        self.queue.clear()
        for it in self.items:
            li = QListWidgetItem(it.label())
            li.setToolTip(it.path)
            self.queue.addItem(li)
        self.queue.setCurrentRow(cur if 0 <= cur < len(self.items) else (0 if self.items else -1))
        self.queue.blockSignals(False)

    def select_item(self, row):
        self.cur = self.items[row] if 0 <= row < len(self.items) else None
        self.sel_id = None
        self.show_item()

    # ------------------------------------------------------------------ analysis
    def preset_changed(self):
        if not self.cur or not self.cur.session:
            return
        name = self.preset.currentData()
        if name != "custom":
            self.cur.session.set_preset(name)
            self._load_settings_widgets()
            if self.cur.session.stats is not None:
                self.redetect()

    def _mark_custom(self):
        if getattr(self, "_loading", False):
            return
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(self.preset.findData("custom"))
        self.preset.blockSignals(False)

    def _load_settings_widgets(self):
        s = self.cur.session
        self._loading = True
        d = s.detect
        self.s_thr.setValue(d.pixel_threshold * 100); self.s_ratio.setValue(d.black_ratio * 100)
        self.s_minf.setValue(d.min_frames); self.s_mind.setValue(d.min_duration_s)
        self.s_space.setValue(d.min_spacing_s); self.s_bridge.setValue(d.bridge_frames)
        r = s.scan.region
        self.r_l.setValue(r.left * 100); self.r_t.setValue(r.top * 100); self.r_r.setValue(r.right * 100); self.r_b.setValue(r.bottom * 100)
        self.s_fast.setChecked(s.scan.decimate > 1)
        self.s_drop.setValue(s.policy.max_auto_drop_black_frames)
        self.s_minpart.setValue(s.policy.min_segment_s)
        i = self.preset.findData(s.preset if s.preset in PRESETS else "custom")
        self.preset.blockSignals(True); self.preset.setCurrentIndex(i); self.preset.blockSignals(False)
        self._loading = False

    def apply_settings(self):
        if not self.cur or not self.cur.session:
            return
        s = self.cur.session
        d = s.detect
        d.pixel_threshold = self.s_thr.value() / 100; d.black_ratio = self.s_ratio.value() / 100
        d.min_frames = self.s_minf.value(); d.min_duration_s = self.s_mind.value()
        d.min_spacing_s = self.s_space.value(); d.bridge_frames = self.s_bridge.value()
        s.policy.max_auto_drop_black_frames = self.s_drop.value()
        s.policy.min_segment_s = self.s_minpart.value()
        region = AnalysisRegion(self.r_l.value() / 100, self.r_t.value() / 100, self.r_r.value() / 100, self.r_b.value() / 100)
        dec = 2 if self.s_fast.isChecked() else 1
        rescan = (asdict(region) != asdict(s.scan.region)) or dec != s.scan.decimate
        s.scan.region, s.scan.decimate = region, dec
        if rescan or s.stats is None:
            self.analyse_current()
        else:
            self.redetect()

    def analyse_all(self):
        for it in self.items:
            if it.session and it.state in ("queued", "error"):
                self._analyse(it)

    def analyse_current(self):
        if self.cur and self.cur.session:
            self._analyse(self.cur)

    def _analyse(self, it: Item):
        it.state, it.progress = "analysing", 0.0
        self.refresh_queue()
        self._busy(True, f"Analysing {os.path.basename(it.path)}…")
        def prog(stage, f, it=it):
            it.progress = f
            if it is self.cur:
                self.prog.setValue(int(f * 100))
            self.queue.item(self.items.index(it)).setText(it.label()) if it in self.items else None
        self._run(self._job_analyse, it, on_done=lambda _, it=it: self._analysed(it), on_progress=prog)

    @staticmethod
    def _job_analyse(job, it: Item):
        s = it.session
        s.index(progress=lambda f: job.signals.progress.emit("index", f * 0.15), cancel=job.cancel)
        s.analyze(progress=lambda f: job.signals.progress.emit("analyse", 0.15 + f * 0.75), cancel=job.cancel)
        s.find()
        job.signals.log.emit(f"Analysed {s.path}: {len(s.stats)} frames, range {s.stats.range_used}, "
                             f"{s.stats.bit_depth}-bit, {len(s.candidates)} candidates; GOP {s.finder.gop_stats()}")
        MainWindow._prove_all(job, it, base=0.9, span=0.1)
        return it

    @staticmethod
    def _prove_all(job, it, base=0.0, span=1.0, ids=None):
        s = it.session
        ids = ids or [k for k, d in s.decisions.items() if d.state != "rejected"]
        for n, k in enumerate(ids):
            if job.cancel.is_set():
                raise Cancelled()
            s.resolve([k])
            job.signals.progress.emit("prove", base + span * (n + 1) / max(1, len(ids)))
        return it

    def _analysed(self, it):
        it.state = "ready"
        self._busy(False, "Analysis complete")
        self.refresh_queue()
        if it is self.cur:
            self.show_item()

    def redetect(self):
        it = self.cur
        it.session.find()
        it.state = "proving"
        self._busy(True, "Checking copy boundaries…")
        self._run(self._prove_all, it, on_done=lambda _, it=it: self._analysed(it),
                  on_progress=lambda st, f: self.prog.setValue(int(f * 100)))

    # ------------------------------------------------------------------ display
    def show_item(self):
        it = self.cur
        self._set_enabled(bool(it and it.session))
        if not it or not it.session:
            self.title.setText("<i>Add a file to begin</i>" if not it else f"<b>{os.path.basename(it.path)}</b> — reading…")
            self.info_lbl.setText("")
            self.table.setRowCount(0); self.parts.setRowCount(0)
            self.overview.set_data(1, 0, [], np.zeros(0), [], [], None)
            return
        s = it.session
        self.title.setText(f"<b>{os.path.basename(it.path)}</b>")
        v = s.info.video
        parts = [f"{s.info.format_name.split(',')[0]} · {s.info.duration_s/60:.1f} min",
                 f"video {v.codec} {v.width}×{v.height} {v.pix_fmt} {v.frame_rate or ''} fps"
                 + (f" {v.field_order}" if v.field_order and v.field_order not in ("progressive", "unknown") else "")
                 + (f" range {v.color_range}" if v.color_range else "")]
        for a in s.info.of_type("audio"):
            parts.append(f"audio {a.codec} {a.channels}ch [{a.language or '?'}]{' “' + a.title + '”' if a.title else ''}")
        for t in s.info.of_type("subtitle"):
            parts.append(f"subs {t.codec} [{t.language or '?'}]")
        if s.info.chapters:
            parts.append(f"{len(s.info.chapters)} chapters")
        if s.finder:
            g = s.finder.gop_stats()
            parts.append(f"GOP ≈{g['median_gop_s']:.2f}s (max {g['max_gop_s']:.1f}s), "
                         f"{g['open_gop_keyframes']} of {g['keyframes']} keyframes open-GOP")
        self.info_lbl.setText(" · ".join(parts))
        self._load_settings_widgets()
        from bsr import smart
        ok, why = smart.supported(s.info)
        self.mode.model().item(1).setEnabled(ok)
        self.mode.setItemData(1, why if not ok else "Re-encodes only the GOP around each exact cut", Qt.ToolTipRole)
        self.refresh_table()
        self.refresh_overview()
        self.refresh_parts()
        self.show_detail()

    def _visible_candidates(self):
        s = self.cur.session
        return [c for c in s.candidates if self.show_suppressed.isChecked() or not c.suppressed
                or s.decisions[c.id].state == "accepted"]

    def refresh_table(self):
        if not self.cur or not self.cur.session:
            return
        s = self.cur.session
        cands = self._visible_candidates()
        manual = [d for d in s.decisions.values() if d.manual]
        self.table.blockSignals(True)
        self.table.setRowCount(len(cands) + len(manual))
        rows = [(c.id, c) for c in cands] + [(d.id, None) for d in manual]
        sel_row = None
        for r, (k, c) in enumerate(rows):
            d = s.decisions[k]
            rc = s.resolved.get(k)
            if c is not None:
                vals = [str(k), fmt_time(c.start_s), f"{c.duration_s:.3f}s", str(c.n_frames),
                        c.kind + (" · lead" if c.leading else "") + (" · trail" if c.trailing else ""),
                        f"{c.score:.0f}" + (f" · {c.audio_db:.0f} dB" if c.audio_db is not None else "")]
            else:
                vals = [str(k), fmt_time(float(d.start_pts * s.vtb)), "manual", "", "manual cut", ""]
            dec = {"accepted": "✓ Accepted", "rejected": "✗ Rejected", "proposed": "• Proposed"}[d.state]
            cut = "Checking…" if rc is None else (STATUS_LABEL.get(rc.status, rc.status) if d.state != "rejected" else "")
            if rc is not None and rc.status in ("copy", "copy-drop") and rc.boundaries:
                cut += " @ " + ", ".join(fmt_time(b.seconds) for b in rc.boundaries)
            vals += [dec, cut]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, k)
                if col == 7 and rc is not None and d.state != "rejected":
                    item.setForeground(STATUS_COLORS.get(rc.status, QColor("white")))
                if c is not None and c.suppressed and d.state != "accepted":
                    item.setForeground(QColor(130, 130, 130))
                self.table.setItem(r, col, item)
            if k == self.sel_id:
                sel_row = r
        self.table.resizeColumnsToContents()
        if sel_row is not None:
            self.table.selectRow(sel_row)
        self.table.blockSignals(False)

    def refresh_overview(self):
        s = self.cur.session
        if s.stats is None or s.finder is None:
            self.overview.set_data(s.info.duration_s, 0, [], np.zeros(0), [], [], None)
            return
        tb = s.vtb
        t0 = float(s.stats.pts[0] * tb)
        items = []
        for c in self._visible_candidates():
            d = s.decisions[c.id]; rc = s.resolved.get(c.id)
            st = d.state if d.state != "accepted" else (rc.status if rc else "proposed")
            items.append({"id": c.id, "start": c.start_s, "end": c.end_s, "status": st})
        cuts = []
        for k, rc in s.resolved.items():
            if s.decisions.get(k) and s.decisions[k].state == "accepted":
                for b in rc.boundaries:
                    cuts.append((b.seconds, rc.status))
                if rc.status == "needs-review":
                    d = s.decisions[k]
                    cuts.append((float((d.start_pts + d.end_pts) / 2 * tb), "needs-review"))
        try:
            plan = s.plan(resolve=False)
            parts = [(sg.start_s, sg.end_s, False) for sg in plan.segments] + \
                    [(float(a * tb), float(b * tb), True) for a, b in plan.removed]
        except Exception:
            parts = []
        dur = float(s.idx[s.info.video.index].end_pts() * tb) - t0
        self.overview.set_data(dur, t0, items, s.finder.key_pts * float(tb), cuts, parts, self.sel_id)

    def refresh_parts(self):
        s = self.cur.session
        self.parts.blockSignals(True)
        if s.stats is None:
            self.parts.setRowCount(0); self.parts.blockSignals(False); return
        try:
            plan = s.plan(resolve=False)
        except Exception as e:
            self.parts.setRowCount(0); self.parts.blockSignals(False)
            self.log(f"plan error: {e}")
            return
        self._plan = plan
        self.parts.setRowCount(len(plan.segments))
        ext = output_format(s.info)[1]
        for r, sg in enumerate(plan.segments):
            notes = []
            if sg.drop_video_pts:
                notes.append(f"{len(sg.drop_video_pts)} black frame(s) not written")
            if sg.duration_s < s.policy.min_segment_s:
                notes.append("too short")
            vals = [str(sg.n), fmt_time(sg.start_s), fmt_time(sg.end_s), f"{sg.duration_s:.2f}s", sg.name, "; ".join(notes)]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c != 4:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                else:
                    item.setToolTip(v + ext)
                self.parts.setItem(r, c, item)
        self.parts.resizeColumnsToContents()
        self.parts.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.parts.blockSignals(False)
        issues = plan.issues
        self.btn_export.setToolTip("\n".join(issues) if issues else "")
        self.status.showMessage(f"{len(plan.segments)} part(s)" + (f" — {len(issues)} issue(s) to resolve before export" if issues else ""))

    def part_renamed(self, item):
        if item.column() != 4 or not self.cur:
            return
        s = self.cur.session
        n = len(self._plan.segments)
        names = list(s.names) + [""] * (n - len(s.names))
        names = names[:n]
        for r in range(n):
            names[r] = self.parts.item(r, 4).text()
        s.names = names

    # ------------------------------------------------------------------ candidate detail
    def _table_sel(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            k = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
            if k != self.sel_id:
                self.select_candidate(k)

    def select_candidate(self, k):
        self.sel_id = k
        self.refresh_table()
        self.overview.selected = k; self.overview.update()
        self.show_detail()

    def _decision(self):
        if not self.cur or not self.cur.session or self.sel_id is None:
            return None
        return self.cur.session.decisions.get(self.sel_id)

    def show_detail(self):
        d = self._decision()
        for w in self.detail_widgets:
            w.setEnabled(d is not None)
        self._clear_choices()
        if d is None:
            self.d_title.setText("<i>Select a candidate in the list or on the timeline</i>")
            self.msgs.setText("")
            self.strip.set_data(np.zeros(0, dtype=np.int64), np.zeros(0, dtype=bool), Fraction(1), {}, [], None, [], None, None)
            for lb, cap in self.thumbs:
                lb.clear(); cap.setText("")
            return
        s = self.cur.session
        tb = s.vtb
        rc = s.resolved.get(d.id)
        c = next((c for c in s.candidates if c.id == d.id), None)
        span = f"{fmt_time(float(d.start_pts*tb))} – {fmt_time(float(d.end_pts*tb))}"
        head = f"<b>Cut {d.id}</b> · black {span}" if not d.manual else f"<b>Manual cut {d.id}</b> at {fmt_time(float(d.start_pts*tb))}"
        if c:
            head += f" · {c.n_frames} frame(s) · {c.kind}" + (f" · audio {c.audio_db:.0f} dBFS" if c.audio_db is not None else "")
            if c.suppressed:
                head += f"<br><small>Low priority: {c.note}</small>"
        if rc:
            head += f"<br>Status: <b style='color:{STATUS_COLORS.get(rc.status, QColor('white')).name()}'>{STATUS_LABEL.get(rc.status, rc.status)}</b>"
        self.d_title.setText(head)
        for w, v in ((self.pos, None), (self.chk_remove, None), (self.pad_a, None), (self.pad_b, None)):
            w.blockSignals(True)
        self.pos.setCurrentIndex(max(0, self.pos.findData(d.position)))
        self.chk_remove.setChecked(d.remove_black)
        self.pad_b.setValue(d.pad_before_s); self.pad_a.setValue(d.pad_after_s)
        for w in (self.pos, self.chk_remove, self.pad_a, self.pad_b):
            w.blockSignals(False)
        msgs = list(rc.messages) if rc else ["Checking copy boundaries…"]
        if d.remove_black and c and c.audio_db is not None:
            msgs.append(f"Audio under the removed black measures {c.audio_db:.0f} dBFS "
                        + ("(silent)" if c.audio_db < -50 else "(NOT silent — removing it removes sound)"))
        self.msgs.setText("<br>".join("• " + m for m in msgs))
        if rc and rc.choices and d.state != "rejected":
            self.choice_group = QButtonGroup(self)
            self.choice_lay.addWidget(QLabel("<b>Choose how to handle this cut:</b>"))
            for i, ch in enumerate(rc.choices):
                rb = QRadioButton(ch["label"]); rb.setProperty("choice", ch["choice"])
                self.choice_group.addButton(rb, i)
                self.choice_lay.addWidget(rb)
            b = QPushButton("Apply choice"); b.clicked.connect(self.apply_choice)
            self.choice_lay.addWidget(b)
        elif d.choice:
            b = QPushButton(f"Undo my choice ({d.choice.split(':')[0]})"); b.clicked.connect(self.clear_choice)
            self.choice_lay.addWidget(b)
        # frame strip around the interval
        self._fill_strip()
        cut_pts = rc.boundaries[0].pts if rc and rc.boundaries and not rc.removed else (
            rc.removed[1] if rc and rc.removed else (d.start_pts + d.end_pts) // 2)
        self.preview_pts = cut_pts
        self._load_thumbs(rc, d)
        self._load_preview(cut_pts)

    def _fill_strip(self):
        s = self.cur.session
        d = self._decision()
        rc = s.resolved.get(d.id)
        fs = s.stats
        if fs is None:
            return
        tb = s.vtb
        lo = d.start_pts - int(Fraction(2) / tb); hi = d.end_pts + int(Fraction(2) / tb)
        if rc:
            for o in (rc.nearby + rc.boundaries):
                lo = min(lo, o.pts - int(Fraction(1, 2) / tb)); hi = max(hi, o.pts + int(Fraction(1, 2) / tb))
        sel = (fs.pts >= lo) & (fs.pts <= hi)
        pts = fs.pts[sel]
        if len(pts) > 600:   # keep it readable
            mid = (d.start_pts + d.end_pts) // 2
            sel = (fs.pts >= mid - int(Fraction(6) / tb)) & (fs.pts <= mid + int(Fraction(6) / tb))
            pts = fs.pts[sel]
        blk = black_mask(fs, s.detect)[sel]
        keys = {}
        for p in s.finder.key_pts:
            if pts.size and pts[0] <= p <= pts[-1]:
                if s.finder.is_proven(int(p)):
                    o = s.finder.proof(int(p))
                    keys[int(p)] = "ok" if o.verified and not o.dropped_pts else ("drop" if o.verified else "bad")
                else:
                    keys[int(p)] = "bad" if s.finder.by_pts[int(p)]["leading"] else "drop"
        dropped = [p for o in ((rc.boundaries if rc else [])) for p in o.dropped_pts]
        planned = [(o.pts, rc.status) for o in (rc.boundaries if rc else [])]
        self.strip.set_data(pts, blk, tb, keys, dropped, rc.requested_pts if rc else None, planned,
                            rc.removed if rc else None, (d.start_pts, d.end_pts), self.preview_pts)

    def _clear_choices(self):
        while self.choice_lay.count():
            w = self.choice_lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def set_state(self, st):
        d = self._decision()
        if not d:
            return
        d.state = st
        self._after_decision()

    def decision_changed(self, *_):
        d = self._decision()
        if not d:
            return
        d.position = self.pos.currentData()
        d.remove_black = self.chk_remove.isChecked()
        d.pad_before_s = self.pad_b.value(); d.pad_after_s = self.pad_a.value()
        self._after_decision()

    def apply_choice(self):
        d = self._decision()
        b = self.choice_group.checkedButton() if hasattr(self, "choice_group") else None
        if not d or not b:
            return
        ch = b.property("choice")
        if ch == "split-instead":
            d.remove_black = False
            d.choice = None
        else:
            d.choice = ch
        if d.state == "proposed":
            d.state = "accepted"
        self.log(f"Cut {d.id}: user choice {ch}")
        self._after_decision()

    def clear_choice(self):
        d = self._decision()
        if d:
            d.choice = None
            self._after_decision()

    def _after_decision(self):
        it = self.cur
        k = self.sel_id
        self.status.showMessage("Checking copy boundaries…")
        def done(_):
            self.status.clearMessage()
            self.refresh_table(); self.refresh_overview(); self.refresh_parts(); self.show_detail(); self.refresh_queue()
        self._run(self._prove_all, it, ids=[k], on_done=done)

    def add_manual_cut(self):
        if not self.cur or self.preview_pts is None:
            return
        d = self.cur.session.add_manual(float(self.preview_pts * self.cur.session.vtb))
        self.sel_id = d.id
        self.log(f"Manual cut {d.id} at {fmt_time(float(self.preview_pts * self.cur.session.vtb))}")
        self._after_decision()

    # ------------------------------------------------------------------ previews
    def _load_thumbs(self, rc, d):
        s = self.cur.session
        if rc and rc.boundaries:
            if rc.removed:
                a_pts, b_pts = rc.removed
            else:
                a_pts = b_pts = rc.boundaries[0].pts
        else:
            a_pts, b_pts = d.start_pts, d.end_pts
        dropped = set(p for o in (rc.boundaries if rc else []) for p in o.dropped_pts)
        resolved = bool(rc and rc.boundaries)
        info = s.info
        key = (self.cur.path, a_pts, b_pts)
        self._thumb_key = key

        def job(jobobj):
            r = FrameReader(info)
            try:
                before = [f for f in r.frames_around(a_pts, before=3 + len(dropped), after=0)
                          if int(f.pts) not in dropped and f.pts < a_pts][-3:]
                after = r.frames_around(b_pts, before=0, after=3)
                conv = lambda f: (int(f.pts), frame_to_qimage(f, 132, sar=info.video.sar))
                return [None] * (3 - len(before)) + [conv(f) for f in before] + [conv(f) for f in after]
            finally:
                r.close()

        def done(res):
            if getattr(self, "_thumb_key", None) != key:
                return
            for i, (lb, cap) in enumerate(self.thumbs):
                if i < len(res) and res[i] is not None:
                    p, img = res[i]
                    lb.setPixmap(QPixmap.fromImage(img).scaled(lb.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    if resolved:
                        tag = "last of part · " if i == 2 else ("first of next · " if i == 3 else "")
                    else:
                        tag = "before black · " if i == 2 else ("after black · " if i == 3 else "")
                    cap.setText(tag + fmt_time(float(p * s.vtb))[2:])
                else:
                    lb.clear(); cap.setText("")
        self._run(job, on_done=done, pool="preview")

    def preview_time(self, t):
        if not self.cur or not self.cur.session or self.cur.session.stats is None:
            return
        s = self.cur.session
        p = int(round(t / s.vtb))
        i = int(np.clip(np.searchsorted(s.stats.pts, p), 0, len(s.stats.pts) - 1))
        self._load_preview(int(s.stats.pts[i]))

    def preview_pts_at(self, pts):
        self._load_preview(pts)

    def step(self, n):
        s = self.cur.session if self.cur else None
        if not s or s.stats is None or self.preview_pts is None:
            return
        i = int(np.searchsorted(s.stats.pts, self.preview_pts))
        i = int(np.clip(i + n, 0, len(s.stats.pts) - 1))
        self._load_preview(int(s.stats.pts[i]))

    def _load_preview(self, pts):
        s = self.cur.session
        self.preview_pts = pts
        self.strip.set_current(pts)
        info = s.info
        key = (self.cur.path, pts)
        self._pv_key = key

        def job(jobobj):
            r = FrameReader(info)
            try:
                f = r.frame_at(pts)
                return f
            finally:
                r.close()

        def done(f):
            if getattr(self, "_pv_key", None) != key or f is None:
                return
            self._pv_frame = f
            self._show_preview_frame()
        self._run(job, on_done=done, pool="preview")

    def _show_preview_frame(self):
        f = getattr(self, "_pv_frame", None)
        if f is None or not self.cur:
            return
        s = self.cur.session
        region = s.scan.region if self.chk_region.isChecked() else None
        img = frame_to_qimage(f, None, region=region, sar=s.info.video.sar)
        self.preview.setPixmap(QPixmap.fromImage(img).scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        t = float(f.pts * s.vtb)
        blk = ""
        if s.stats is not None:
            i = int(np.searchsorted(s.stats.pts, f.pts))
            if 0 <= i < len(s.stats) and s.stats.pts[i] == f.pts:
                dark = float(s.stats.frac_at(s.detect.pixel_threshold)[i]) * 100
                blk = f"<br>dark pixels {dark:.1f}% · {'BLACK' if dark >= s.detect.black_ratio*100 else 'picture'}"
        kf = " · keyframe" if s.finder is not None and int(f.pts) in s.finder.by_pts else ""
        self.pv_lbl.setText(f"<b>{fmt_time(t)}</b>{kf}{blk}<br><small>preview only — decoded source frame</small>")

    def play_around(self):
        if self.play_timer.isActive():
            self.play_timer.stop(); self.btn_play.setText("Play ±2 s around cut"); return
        s = self.cur.session
        center = self.preview_pts
        info = s.info
        tb = s.vtb
        self.btn_play.setText("Loading…")

        def job(jobobj):
            r = FrameReader(info)
            try:
                frs = r.frames_around(center, before=int(2 / float(tb) / max(1, float(np.median(s.stats.duration)))),
                                      after=int(2 / float(tb) / max(1, float(np.median(s.stats.duration)))))
                return [(int(f.pts), frame_to_qimage(f, 480, sar=info.video.sar)) for f in frs]
            finally:
                r.close()

        def done(frames):
            self.preview_frames = frames
            self._play_i = 0
            fps = float(info.video.frame_rate or 25)
            self.play_timer.start(int(1000 / fps))
            self.btn_play.setText("Stop")
        self._run(job, on_done=done, pool="preview")

    def _play_tick(self):
        if self._play_i >= len(self.preview_frames):
            self.play_timer.stop(); self.btn_play.setText("Play ±2 s around cut"); return
        p, img = self.preview_frames[self._play_i]
        self.preview.setPixmap(QPixmap.fromImage(img).scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.FastTransformation))
        self.pv_lbl.setText(f"<b>{fmt_time(float(p * self.cur.session.vtb))}</b><br><small>preview only — silent</small>")
        self.strip.set_current(p)
        self._play_i += 1

    # ------------------------------------------------------------------ export
    def choose_out(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_edit.text() or os.path.expanduser("~"))
        if d:
            self.out_edit.setText(d)
            self.settings.setValue("out_dir", d)

    def choose_tracks(self):
        if not self.cur or not self.cur.session:
            return
        dlg = TracksDialog(self, self.cur.session.info, self.cur.streams or default_streams(self.cur.session.info))
        if dlg.exec():
            self.cur.streams = dlg.selected()

    def review_export(self):
        it = self.cur
        if not it or not it.session or it.session.stats is None:
            return
        s = it.session
        out = self.out_edit.text().strip()
        if not out:
            QMessageBox.information(self, APP_NAME, "Choose an output folder first."); return
        if os.path.realpath(out) == os.path.realpath(os.path.dirname(it.path)):
            r = QMessageBox.question(self, APP_NAME, "The output folder is the source folder. Outputs never overwrite "
                                     "sources, but a separate folder is recommended. Continue?")
            if r != QMessageBox.Yes:
                return
        if self.mode.currentData() != "copy":
            QMessageBox.information(self, APP_NAME, "This mode is not available for this file."); return
        plan = s.plan(resolve=False)
        if plan.issues:
            QMessageBox.warning(self, APP_NAME, "Resolve these first:\n\n" + "\n".join(plan.issues)); return
        streams = it.streams or default_streams(s.info)
        fmt, ext = output_format(s.info)
        seg_plans = [plan_segment(s.info, s.idx, sg, streams, ext) for sg in plan.segments]
        notes = preservation_report(s.info, streams, fmt)
        dlg = PlanDialog(self, s, plan, seg_plans, notes, self.mode.currentText(), out)
        if not dlg.exec():
            return
        self.settings.setValue("out_dir", out)
        it.state, it.progress = "exporting", 0.0
        self.refresh_queue()
        self._busy(True, "Exporting…")
        self.log(f"Export {s.path} -> {out}: {len(plan.segments)} parts, streams {streams}")

        def job(jobobj):
            return export_plan(s.info, s.idx, plan, ExportOptions(out_dir=out, streams=streams),
                               progress=lambda st, f: jobobj.signals.progress.emit(st, f), cancel=jobobj.cancel,
                               log=lambda m: jobobj.signals.log.emit(m))

        def prog(st, f):
            it.progress = f
            self.prog.setValue(int(f * 100))

        def done(rep):
            it.last_report = rep
            it.state = "done" if rep.get("ok") else "ready"
            self._busy(False, "Export finished" if rep.get("ok") else "Export finished with problems")
            self.refresh_queue()
            ResultsDialog(self, rep).exec()
        self._run(job, on_done=done, on_progress=prog)

    def show_last_report(self):
        if self.cur and self.cur.last_report:
            ResultsDialog(self, self.cur.last_report).exec()

    def show_log(self):
        LogDialog(self, self.logs, log_path()).exec()

    # ------------------------------------------------------------------ projects
    def save_project(self):
        if not self.cur or not self.cur.session:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save project", os.path.splitext(self.cur.path)[0] + ".bsrproj",
                                           "Banana Split project (*.bsrproj)")
        if p:
            if os.path.realpath(p) == os.path.realpath(self.cur.path):
                return
            self.cur.session.save_project(p)
            self.log(f"Saved project {p}")

    def open_project(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open project", "", "Banana Split project (*.bsrproj)")
        if not p:
            return
        try:
            s = SourceSession.load_project(p)
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, str(e)); return
        it = next((i for i in self.items if i.path == s.path), None)
        if it is None:
            it = Item(s.path); self.items.append(it)
        decisions = dict(s.decisions)
        it.session = s
        it.streams = default_streams(s.info)
        self.refresh_queue()
        self.queue.setCurrentRow(self.items.index(it))

        def job(jobobj, it=it):
            s.index(cancel=jobobj.cancel); s.analyze(cancel=jobobj.cancel); s.find()
            for k, d in decisions.items():
                s.decisions[k] = d
            s.resolved = {}
            return MainWindow._prove_all(jobobj, it)
        it.state = "analysing"
        self._busy(True, "Loading project…")
        self._run(job, on_done=lambda _: self._analysed(it), on_progress=lambda st, f: self.prog.setValue(int(f * 100)))

    def closeEvent(self, e):
        self.cancel_jobs()
        self.pools.wait()
        super().closeEvent(e)


def main(argv=None):
    app = QApplication(argv or sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("BananaSplitRedux")
    w = MainWindow()
    w.show()
    files = [a for a in (argv or sys.argv)[1:] if os.path.exists(a)]
    if files:
        w.add_paths(files)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
