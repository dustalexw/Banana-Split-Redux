"""Banana Split Redux - desktop interface (PySide6 / Qt 6).

Workflow: import -> analyse -> review cuts -> choose mode -> review export plan -> export & validate.
All media work runs in background jobs (bsr_ui.workers); the interface thread only
draws and reacts.
"""
from __future__ import annotations

import os
import sys
import time
import copy
from dataclasses import asdict
from fractions import Fraction

import numpy as np
from PySide6.QtCore import Qt, QTimer, QSize, QSettings, QUrl, QItemSelection, QItemSelectionModel, QEvent
from PySide6.QtGui import QAction, QImage, QPixmap, QKeySequence, QDesktopServices, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QComboBox, QProgressBar, QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox,
    QFormLayout, QDoubleSpinBox, QSpinBox, QCheckBox, QLineEdit, QFileDialog, QMessageBox, QRadioButton,
    QButtonGroup, QTextBrowser, QAbstractItemView, QToolButton, QGridLayout, QSizePolicy, QFrame, QStatusBar,
    QScrollArea, QLayout)

from bsr import ENGINE_VERSION
from bsr.detect import PRESETS, AnalysisRegion, black_mask, midpoint_window, video_bounds, midpoint_recommendation, settings_windows
from bsr.export import (ExportOptions, export_plan, default_streams, preservation_report, output_format,
                        plan_segment, boundary_scope)
from bsr.frames import FrameReader
from bsr.media import fmt_time, Cancelled, NOPTS
from bsr.planner import default_names
from bsr.session import SourceSession
from bsr.cache import tool_versions

from .dialogs import NamesDialog, AboutDialog, PlanDialog, ResultsDialog, LogDialog, TracksDialog
from .timeline import Overview, DetailStrip, STATUS_COLORS
from .workers import Job, Pools
from .theme import apply_theme, icon_path, CHARCOAL, CREAM, FILM
from . import macchrome, winchrome
from .player import VideoPlayer

APP_NAME = "Banana Split Redux"
MEDIA_EXT = {".mkv", ".mp4", ".m4v", ".mov", ".ts", ".m2ts", ".mts", ".avi", ".webm", ".mpg", ".mpeg", ".vob", ".wmv", ".asf", ".flv", ".m2v", ".ogv", ".3gp"}

STATUS_LABEL = {
    "copy": "Copied", "copy-drop": "Copied (drops black frames)", "needs-review": "Needs review",
    "reencode": "Boundary re-encoding", "skipped": "Skipped", "rejected": "Rejected",
}


def log_path():
    from bsr.osutil import log_root
    d = os.environ.get("BSR_LOG_DIR") or log_root()
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
        self.history = []
        self.auto_analyse = False

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
        macchrome.prepare(self)
        self.pools = Pools()
        self.items: list[Item] = []
        self.cur: Item | None = None
        self.sel_id = None
        self.preview_pts = None
        self.preview_frames = []
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
        hm = mb.addMenu("&Help")
        self.about_action = QAction(f"About {APP_NAME}…", self)
        self.about_action.setMenuRole(QAction.AboutRole)
        self.about_action.triggered.connect(self.show_about)
        hm.addAction(self.about_action)

        root = QSplitter(Qt.Horizontal)
        root.setChildrenCollapsible(False)
        shell = QWidget(); shell_lay = QVBoxLayout(shell); shell_lay.setContentsMargins(0, 0, 0, 0)
        shell_lay.setSpacing(10)
        brand = QFrame(); brand.setObjectName("brand"); brand_lay = QHBoxLayout(brand)
        brand_lay.setContentsMargins(14, 8, 18, 8)
        brand_lay.setSpacing(12)
        logo = QLabel()
        self._brand, self._brand_lay, self._logo = brand, brand_lay, logo
        self._chrome_inset = None
        self._set_logo(44)
        brand_lay.addWidget(logo)
        title = QLabel(APP_NAME); title.setObjectName("brandTitle")
        brand_lay.addWidget(title, 0, Qt.AlignVCenter)
        brand_lay.addStretch()
        shell_lay.addWidget(brand); shell_lay.addWidget(root, 1)
        self.setCentralWidget(shell)
        # queue
        left = QWidget(); left.setObjectName("queuePanel"); ll = QVBoxLayout(left); ll.setContentsMargins(12, 10, 10, 10)
        qtitle = QLabel("YOUR EPISODES"); qtitle.setObjectName("sectionTitle"); ll.addWidget(qtitle)
        hint = QLabel("Drop videos to find split points"); hint.setObjectName("muted"); ll.addWidget(hint)
        self.queue = QListWidget(); self.queue.setWordWrap(True); self.queue.setSpacing(2)
        self.queue.currentRowChanged.connect(self.select_item)
        ll.addWidget(self.queue, 1)
        hb = QHBoxLayout()
        b = QPushButton("Add…"); b.setObjectName("primary"); b.clicked.connect(self.add_files_dialog); hb.addWidget(b)
        b = QPushButton("Remove"); b.clicked.connect(self.remove_item); hb.addWidget(b)
        ll.addLayout(hb)
        b = QPushButton("Analyse all episodes"); b.clicked.connect(self.analyse_all); ll.addWidget(b)
        self.btn_keep_rec_all = QPushButton("Keep recommended splits in all")
        self.btn_keep_rec_all.setToolTip("In every analysed episode, keep the suggested split for each expected segment break. "
                                         "Each cut is still checked, and anything that needs a decision stays marked.")
        self.btn_keep_rec_all.clicked.connect(self.keep_recommended_all); ll.addWidget(self.btn_keep_rec_all)
        self.btn_episodes_all = QPushButton("Episode names for all…")
        self.btn_episodes_all.setToolTip("Number every part to match TVDB/TMDB (S01E01, S01E02 …), using file names and an optional online lookup")
        self.btn_episodes_all.clicked.connect(self.episode_names); ll.addWidget(self.btn_episodes_all)
        root.addWidget(left)

        right = QWidget(); rl = QVBoxLayout(right); rl.setContentsMargins(4, 8, 8, 8)
        root.addWidget(right)
        root.setSizes([280, 1160])

        # header
        hdr = QHBoxLayout()
        self.title = QLabel("<i>Add a file to begin</i>"); self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.title.setWordWrap(True); self.title.setObjectName("fileTitle")
        hdr.addWidget(self.title, 1)
        hdr.addWidget(QLabel("Detection:"))
        self.preset = QComboBox()
        for name in ("conservative", "balanced", "sensitive", "single-frame", "custom"):
            self.preset.addItem(name.capitalize(), name)
        self.preset.setCurrentIndex(1)
        self.preset.currentIndexChanged.connect(self.preset_changed)
        hdr.addWidget(self.preset)
        hdr.addWidget(QLabel("Segments:"))
        self.segments = QComboBox()
        self.segments.setToolTip("Story segments in this file. TVDB and TMDB usually list each segment of a two- or "
                                 "three-segment cartoon as its own episode, so a file with two segments needs one split.\n"
                                 "Auto uses the file name (two episode numbers or two titles = two segments).")
        self.segments.addItem("Auto", None)
        for n, text in ((1, "1 · whole file"), (2, "2 segments"), (3, "3 segments"), (4, "4 segments"),
                        (5, "5 segments"), (6, "6 segments")):
            self.segments.addItem(text, n)
        self.segments.currentIndexChanged.connect(self.segments_changed)
        hdr.addWidget(self.segments)
        self.btn_settings = QToolButton(); self.btn_settings.setText("Settings"); self.btn_settings.setCheckable(True)
        self.btn_settings.toggled.connect(lambda v: self.settings_box.setVisible(v))
        hdr.addWidget(self.btn_settings)
        self.btn_analyse = QPushButton("Find split points"); self.btn_analyse.clicked.connect(self.analyse_current)
        self.btn_analyse.setObjectName("primary")
        hdr.addWidget(self.btn_analyse)
        self.btn_cancel = QPushButton("Cancel"); self.btn_cancel.clicked.connect(self.cancel_jobs); self.btn_cancel.setEnabled(False)
        hdr.addWidget(self.btn_cancel)
        rl.addLayout(hdr)
        self.info_lbl = QLabel(""); self.info_lbl.setWordWrap(True); self.info_lbl.setObjectName("muted")
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
        self.table.setAlternatingRowColors(True); self.table.setShowGrid(False)
        self.table.setHorizontalHeaderLabels(["#", "Black from", "Length", "Frames", "Type", "Score", "Decision", "Cut"])
        # Frame count, transition type and score are available in the detail panel.
        for col in (3, 4, 5):
            self.table.setColumnHidden(col, True)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._table_sel)
        self.table.currentCellChanged.connect(self._table_sel)
        self.show_suppressed = QCheckBox("Show extra suggestions"); self.show_suppressed.toggled.connect(self.refresh_table)
        tw = QWidget(); tl = QVBoxLayout(tw); tl.setContentsMargins(0, 0, 0, 0)
        self.review_progress = QLabel("Add a video, then find its split points.")
        self.review_progress.setObjectName("sectionTitle"); self.review_progress.setWordWrap(True)
        tl.addWidget(self.review_progress)
        self.midpoint_card = QFrame(); self.midpoint_card.setObjectName("midpointCard")
        midpoint_layout = QVBoxLayout(self.midpoint_card); midpoint_layout.setContentsMargins(10, 9, 10, 9)
        self.midpoint_label = QLabel("Midpoint recommendation"); self.midpoint_label.setObjectName("midpointTitle")
        self.midpoint_label.setWordWrap(True); midpoint_layout.addWidget(self.midpoint_label)
        self.midpoint_hint = QLabel("Find black transitions near halfway through your video.")
        self.midpoint_hint.setWordWrap(True); self.midpoint_hint.setObjectName("muted")
        midpoint_layout.addWidget(self.midpoint_hint)
        rec_buttons = QHBoxLayout()
        self.btn_midpoint = QPushButton("Review midpoint"); self.btn_midpoint.setObjectName("accept")
        self.btn_midpoint.clicked.connect(self.review_midpoint); rec_buttons.addWidget(self.btn_midpoint)
        self.btn_keep_rec = QPushButton("Keep recommended")
        self.btn_keep_rec.setToolTip("Keep the suggested split for every expected segment break in this file")
        self.btn_keep_rec.clicked.connect(self.keep_recommended); rec_buttons.addWidget(self.btn_keep_rec)
        midpoint_layout.addLayout(rec_buttons)
        tl.addWidget(self.midpoint_card)
        controls = QHBoxLayout()
        self.btn_review_next = QPushButton("Next to review"); self.btn_review_next.clicked.connect(self.next_unreviewed)
        self.btn_keep_all = QPushButton("Keep all shown"); self.btn_keep_all.clicked.connect(self.keep_all_shown)
        self.btn_undo = QPushButton("Undo"); self.btn_undo.clicked.connect(self.undo_decision)
        for button in (self.btn_review_next, self.btn_keep_all, self.btn_undo): controls.addWidget(button)
        tl.addLayout(controls)
        self.selection_hint = QLabel("Hold Shift to select a range of split points.")
        self.selection_hint.setObjectName("muted"); self.selection_hint.setWordWrap(True)
        tl.addWidget(self.selection_hint)
        selected_controls = QHBoxLayout()
        self.btn_keep_selected = QPushButton("Keep selected")
        self.btn_keep_selected.setObjectName("accept")
        self.btn_keep_selected.clicked.connect(lambda: self.set_selected_state("accepted"))
        self.btn_skip_selected = QPushButton("Skip selected")
        self.btn_skip_selected.setObjectName("reject")
        self.btn_skip_selected.setToolTip("Reject all selected split points")
        self.btn_skip_selected.clicked.connect(lambda: self.set_selected_state("rejected"))
        selected_controls.addWidget(self.btn_keep_selected); selected_controls.addWidget(self.btn_skip_selected)
        tl.addLayout(selected_controls); tl.addWidget(self.table); tl.addWidget(self.show_suppressed)
        mid.addWidget(tw)
        detail_scroll = QScrollArea(); detail_scroll.setWidgetResizable(True)
        self.detail_scroll = detail_scroll
        detail_scroll.setFrameShape(QFrame.NoFrame)
        detail_scroll.setMinimumWidth(640)
        detail_scroll.setWidget(self._build_detail())
        mid.addWidget(detail_scroll)
        mid.setSizes([520, 640])

        rl.addWidget(self._build_parts())

        self.status = QStatusBar(); self.setStatusBar(self.status)
        self._set_enabled(False)
        self.show_detail(); self.update_review_progress()

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
        self.s_midpoint = QCheckBox("More sensitive near expected segment breaks (halfway for 2 segments, thirds for 3)")
        self.s_midpoint.setToolTip("Near each expected break, allow one black frame with no minimum duration, a 2.5-point higher darkness threshold and up to 3 points fewer dark pixels. Detection elsewhere uses the preset above.")
        self.s_midpoint.toggled.connect(lambda *_: self._mark_custom())
        g.addWidget(self.s_midpoint, 4, 0, 1, 6)
        self.settings_box = box
        box.hide()
        for w in (self.s_thr, self.s_ratio, self.s_minf, self.s_mind, self.s_space, self.s_bridge):
            (w.valueChanged).connect(lambda *_: self._mark_custom())
        return box

    def _build_detail(self):
        w = QWidget(); l = QVBoxLayout(w); l.setContentsMargins(4, 0, 0, 0)
        l.setSizeConstraint(QLayout.SetMinimumSize)
        self.d_title = QLabel("Watch your video, then keep the splits you want."); self.d_title.setWordWrap(True)
        l.addWidget(self.d_title)
        self.player = VideoPlayer(self)
        self.player.seekRequested.connect(self.preview_time)
        self.player.positionMoved.connect(self.player_position)
        self.preview = self.player.video
        row = QHBoxLayout()
        self.btn_previous = QPushButton("← Previous"); self.btn_previous.clicked.connect(lambda: self.navigate_cut(-1))
        self.btn_accept = QPushButton("Keep && next"); self.btn_accept.clicked.connect(lambda: self.set_state("accepted", advance=True))
        self.btn_reject = QPushButton("Skip && next"); self.btn_reject.clicked.connect(lambda: self.set_state("rejected", advance=True))
        self.btn_next = QPushButton("Next →"); self.btn_next.clicked.connect(lambda: self.navigate_cut(1))
        self.btn_accept.setObjectName("accept"); self.btn_reject.setObjectName("reject")
        for button in (self.btn_previous, self.btn_accept, self.btn_reject, self.btn_next): row.addWidget(button)
        l.addLayout(row)
        self.player.video.setMaximumHeight(210)
        l.addWidget(self.player)
        row = QHBoxLayout()
        self.btn_play = QPushButton("Preview this split"); self.btn_play.clicked.connect(self.play_around)
        self.btn_play.setToolTip("Play two seconds before and after the split, with sound")
        row.addWidget(self.btn_play)
        self.btn_manual = QPushButton("Add split at playhead"); self.btn_manual.clicked.connect(self.add_manual_cut)
        row.addWidget(self.btn_manual)
        l.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Split time:"))
        self.cut_time = QLineEdit(); self.cut_time.setPlaceholderText("00:03:35.720")
        self.cut_time.setMaximumWidth(130); self.cut_time.returnPressed.connect(self.move_cut_from_time)
        row.addWidget(self.cut_time)
        self.btn_set_time = QPushButton("Set time"); self.btn_set_time.clicked.connect(self.move_cut_from_time); row.addWidget(self.btn_set_time)
        self.btn_move_cut = QPushButton("Use playhead"); self.btn_move_cut.clicked.connect(self.move_cut_to_playhead); row.addWidget(self.btn_move_cut)
        l.addLayout(row)
        self.choice_box = QWidget(); self.choice_lay = QVBoxLayout(self.choice_box); self.choice_lay.setContentsMargins(0, 0, 0, 0)
        l.addWidget(self.choice_box)
        self.msgs = QLabel(""); self.msgs.setWordWrap(True); self.msgs.setStyleSheet("font-size:11px;")
        self.msgs.hide()
        self.advanced_toggle = QPushButton("Frame preview & advanced options ▾")
        self.advanced_toggle.setCheckable(True); l.addWidget(self.advanced_toggle)
        self.advanced = QWidget(); advanced_lay = QVBoxLayout(self.advanced); advanced_lay.setContentsMargins(0, 0, 0, 0)
        self.advanced.hide(); self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        self.advanced_toggle.toggled.connect(lambda *_: self.show_detail())
        l.addWidget(self.advanced)
        advanced_lay.addWidget(self.msgs)
        self.msgs.show()
        thumbs = QHBoxLayout(); self.thumbs = []
        for i in range(6):
            col = QVBoxLayout()
            lb = QLabel(); lb.setFixedSize(94, 72); lb.setAlignment(Qt.AlignCenter); lb.setObjectName("thumb")
            cap = QLabel(""); cap.setAlignment(Qt.AlignCenter); cap.setStyleSheet("font-size:10px;")
            cap.setFixedWidth(94); cap.setWordWrap(True); cap.setMinimumHeight(30)
            col.addWidget(lb); col.addWidget(cap); thumbs.addLayout(col)
            if i == 2:
                sep = QFrame(); sep.setFrameShape(QFrame.VLine); thumbs.addWidget(sep)
            self.thumbs.append((lb, cap))
        advanced_lay.addLayout(thumbs)
        self.strip = DetailStrip(); self.strip.frameClicked.connect(self.preview_pts_at); advanced_lay.addWidget(self.strip)
        row = QHBoxLayout()
        for txt, d in (("−10 frames", -10), ("−1 frame", -1), ("+1 frame", 1), ("+10 frames", 10)):
            b = QToolButton(); b.setText(txt); b.clicked.connect(lambda _, d=d: self.step(d)); row.addWidget(b)
        advanced_lay.addLayout(row)
        self.pv_lbl = QLabel("—"); self.pv_lbl.setWordWrap(True); advanced_lay.addWidget(self.pv_lbl)
        self.chk_region = QCheckBox("Show analysis region"); self.chk_region.toggled.connect(lambda *_: self._show_preview_frame())
        advanced_lay.addWidget(self.chk_region)
        dec = QGroupBox("Fine-tune this split"); dl = QGridLayout(dec)
        dl.addWidget(QLabel("Split at"), 0, 0)
        self.pos = QComboBox()
        for label, value in (("start of black", "start"), ("middle of black", "middle"), ("end of black", "end")):
            self.pos.addItem(label, value)
        self.pos.currentIndexChanged.connect(self.decision_changed); dl.addWidget(self.pos, 0, 1, 1, 3)
        self.chk_remove = QCheckBox("Remove black"); self.chk_remove.toggled.connect(self.decision_changed)
        self.chk_remove.setToolTip("Removing black also removes any audio under it."); dl.addWidget(self.chk_remove, 1, 0)
        dl.addWidget(QLabel("keep before/after"), 1, 1)
        self.pad_b = QDoubleSpinBox(); self.pad_a = QDoubleSpinBox()
        for spin in (self.pad_b, self.pad_a):
            spin.setRange(0, 10); spin.setDecimals(2); spin.setSingleStep(.04); spin.setSuffix(" s")
            spin.valueChanged.connect(self.decision_changed)
        dl.addWidget(self.pad_b, 1, 2); dl.addWidget(self.pad_a, 1, 3); advanced_lay.addWidget(dec)
        self.detail_widgets = [self.btn_accept, self.btn_reject, self.pos, self.chk_remove, self.pad_a,
                               self.pad_b, self.btn_play, self.cut_time, self.btn_set_time, self.btn_move_cut]
        for text, shortcut, callback in (("Keep split", "Alt+Return", lambda: self.set_state("accepted", advance=True)),
                                        ("Skip split", "Alt+Backspace", lambda: self.set_state("rejected", advance=True)),
                                        ("Previous split", "Alt+Left", lambda: self.navigate_cut(-1)),
                                        ("Next split", "Alt+Right", lambda: self.navigate_cut(1))):
            action = QAction(text, self); action.setShortcut(shortcut); action.triggered.connect(callback); self.addAction(action)
        from bsr.osutil import alt_key_name, return_key_name
        self.btn_accept.setToolTip(f"Keep this split and move on · {alt_key_name()}+{return_key_name()}")
        self.btn_reject.setToolTip(f"Skip this split and move on · {alt_key_name()}+Backspace")
        return w

    def _build_parts(self):
        box = QGroupBox("Your output parts")
        l = QVBoxLayout(box)
        self.parts = QTableWidget(0, 6)
        self.parts.setMinimumHeight(80); self.parts.setMaximumHeight(105)
        self.parts.setAlternatingRowColors(True); self.parts.setShowGrid(False)
        self.parts.setHorizontalHeaderLabels(["#", "From", "To", "Length", "File name (editable)", "Notes"])
        self.parts.verticalHeader().hide()
        self.parts.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.parts.itemChanged.connect(self.part_renamed); l.addWidget(self.parts)
        row = QHBoxLayout()
        self.name_hint = QLabel(""); self.name_hint.setObjectName("muted"); self.name_hint.setWordWrap(True)
        row.addWidget(self.name_hint, 1)
        self.btn_names = QPushButton("Suggest names…"); self.btn_names.clicked.connect(self.names_from_title)
        self.btn_names.setToolTip("Pick part names from the titles in the file name")
        row.addWidget(self.btn_names)
        self.btn_episode_names = QPushButton("Episode names…")
        self.btn_episode_names.setToolTip("Name parts as TVDB/TMDB episodes (Show - S01E03 - Title), numbered across the season")
        self.btn_episode_names.clicked.connect(lambda: self.episode_names(current=self.cur.path if self.cur else None))
        row.addWidget(self.btn_episode_names)
        self.output_toggle = QPushButton("Output settings ▾"); self.output_toggle.setCheckable(True)
        row.addWidget(self.output_toggle)
        self.btn_export = QPushButton("Finish job…"); self.btn_export.clicked.connect(self.review_export)
        self.btn_export.setDefault(True); self.btn_export.setObjectName("primary"); row.addWidget(self.btn_export)
        l.addLayout(row)
        self.output_settings = QWidget(); settings = QVBoxLayout(self.output_settings); settings.setContentsMargins(0, 0, 0, 0)
        self.output_settings.hide(); self.output_toggle.toggled.connect(self.output_settings.setVisible)
        l.addWidget(self.output_settings)
        row = QHBoxLayout(); row.addWidget(QLabel("Output folder:"))
        self.out_edit = QLineEdit(self.settings.value("out_dir", "")); row.addWidget(self.out_edit, 1)
        b = QPushButton("Choose…"); b.clicked.connect(self.choose_out); row.addWidget(b)
        b = QPushButton("Tracks…"); b.clicked.connect(self.choose_tracks); row.addWidget(b)
        settings.addLayout(row)
        row = QHBoxLayout(); row.addWidget(QLabel("Mode:"))
        self.mode = QComboBox()
        self.mode.addItem("No re-encoding (strict)", "copy")
        self.mode.addItem("Re-encode near cuts when needed", "smart")
        self.mode.model().item(1).setEnabled(False)
        self.mode.setItemData(1, "Exact cuts are enabled after the file is inspected", Qt.ToolTipRole)
        self.mode.setMinimumContentsLength(32); self.mode.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.mode.currentIndexChanged.connect(self.mode_changed); row.addWidget(self.mode)
        self.crf_lbl = QLabel("Quality (CRF):")
        self.crf = QSpinBox(); self.crf.setRange(1, 30); self.crf.setValue(12)
        self.crf.setToolTip("Quality for boundary frames. Lower values preserve more detail. Other video and all audio are copied.")
        row.addWidget(self.crf_lbl); row.addWidget(self.crf); self.crf_lbl.hide(); self.crf.hide()
        row.addStretch(); settings.addLayout(row)
        self.scope_lbl = QLabel("Keep the splits you want, check the names, then Finish job.")
        self.scope_lbl.setObjectName("scope"); self.scope_lbl.setWordWrap(True); l.addWidget(self.scope_lbl)
        self.mode_reason = QLabel(""); self.mode_reason.setObjectName("muted")
        self.mode_reason.setWordWrap(True); self.mode_reason.hide(); l.addWidget(self.mode_reason)
        row = QHBoxLayout()
        self.export_issue = QLabel(""); self.export_issue.setWordWrap(True)
        self.export_issue.setStyleSheet("color: #f5ce69;")
        row.addWidget(self.export_issue, 1)
        self.btn_export_issue = QPushButton("Review split…")
        self.btn_export_issue.clicked.connect(self.focus_export_issue)
        row.addWidget(self.btn_export_issue)
        l.addLayout(row)
        self.export_issue.hide(); self.btn_export_issue.hide()
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
        analysed = bool(self.cur and self.cur.session and self.cur.session.stats is not None)
        self.btn_export.setEnabled(on and analysed)
        if not analysed:
            self.export_issue.hide(); self.btn_export_issue.hide()
        if self.cur and self.cur.state in ("analysing", "proving", "exporting"):
            self.btn_analyse.setEnabled(False); self.preset.setEnabled(False); self.btn_export.setEnabled(False)

    def _busy(self, on, text=""):
        self.update_selected_actions()
        on = on or any(it.state in ("analysing", "proving", "exporting") for it in self.items)
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
            if pool == "media" and args and isinstance(args[0], Item):
                it = args[0]
                it.state = "ready" if it.session and it.session.stats is not None else "error"
                it.error = msg
                if it is self.cur:
                    self.show_item()
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
        self.add_paths(paths, analyse=True)

    def add_files_dialog(self):
        last = self.settings.value("last_dir", os.path.expanduser("~"))
        paths, _ = QFileDialog.getOpenFileNames(self, "Add video files", last,
                                                "Video (" + " ".join("*" + e for e in sorted(MEDIA_EXT)) + ");;All files (*)")
        if paths:
            self.settings.setValue("last_dir", os.path.dirname(paths[0]))
            self.add_paths(paths, analyse=True)

    def add_paths(self, paths, analyse=False):
        for p in paths:
            if os.path.isdir(p):
                for n in sorted(os.listdir(p)):
                    if os.path.splitext(n)[1].lower() in MEDIA_EXT:
                        self.add_paths([os.path.join(p, n)], analyse=analyse)
                continue
            if any(i.path == p for i in self.items):
                continue
            it = Item(p)
            it.auto_analyse = analyse
            self.items.append(it)
            self.log(f"Added {p}")
            self._run(self._job_probe, it, default_segments=self.default_segments(),
                      on_done=lambda _, it=it: self._probed(it))
        self.refresh_queue()
        if self.cur is None and self.items:
            self.queue.setCurrentRow(0)
            self.select_item(0)

    @staticmethod
    def _job_probe(job, it: Item, default_segments=2):
        it.session = SourceSession(it.path)
        it.session.set_segments(None, default=default_segments)
        it.session.policy.boundary_reencode = it.session.reencode_supported()
        return it

    def _probed(self, it):
        it.streams = default_streams(it.session.info)
        it.out_dir = it.out_dir or self.settings.value("out_dir", "") or os.path.join(os.path.dirname(it.path), "Banana Split Output")
        self.refresh_queue()
        if it is self.cur:
            self.show_item()
        if it.auto_analyse and it in self.items:
            self._analyse(it)

    def remove_item(self):
        r = self.queue.currentRow()
        if 0 <= r < len(self.items):
            it = self.items[r]
            if it.state == "exporting":
                self.status.showMessage("Cancel the running export before removing this video."); return
            for job in list(self._jobs):
                if job.args and job.args[0] is it: job.cancel.set()
            del self.items[r]
            self.cur = None
            self.refresh_queue()
            self.queue.setCurrentRow(min(r, len(self.items) - 1))
            self.select_item(self.queue.currentRow())

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
        if self.cur:
            self.cur.out_dir = self.out_edit.text().strip()
        self.cur = self.items[row] if 0 <= row < len(self.items) else None
        self.sel_id = None
        self.preview_pts = None
        self._pv_key = self._thumb_key = None
        self._pv_frame = None
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
        self.s_midpoint.setChecked(d.midpoint_boost)
        self.segments.blockSignals(True)
        self.segments.setCurrentIndex(0 if s.segments_auto else max(0, self.segments.findData(d.segments)))
        self.segments.setItemText(0, f"Auto · {d.segments}" if s.segments_auto else "Auto")
        self.segments.blockSignals(False)
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
        d.midpoint_boost = self.s_midpoint.isChecked()
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
        if it.state in ("analysing", "proving", "exporting"):
            return
        if it is self.cur:
            self.player.pause()
        it.history.clear()
        it.state, it.progress = "analysing", 0.0
        self.refresh_queue()
        self._busy(True, f"Analysing {os.path.basename(it.path)}…")
        if it is self.cur:
            self.btn_export.setEnabled(False); self.show_detail(); self.update_review_progress()
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
            if self.sel_id is None:
                pending = [k for k, d in it.session.decisions.items() if d.state == "proposed"]
                self.sel_id = pending[0] if pending else None
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
        self.player.set_source(it.session.info if it and it.session else None)
        if it and it.session:
            self.out_edit.setText(it.out_dir or self.out_edit.text())
        if not it or not it.session:
            self.title.setText("<i>Add a file to begin</i>" if not it else f"<b>{os.path.basename(it.path)}</b> — reading…")
            self.info_lbl.setText("")
            self.table.setRowCount(0); self.parts.setRowCount(0)
            self.overview.set_data(1, 0, [], np.zeros(0), [], [], None)
            self.show_detail(); self.update_review_progress()
            self.name_hint.clear(); self.btn_names.setEnabled(False); self.btn_episode_names.setEnabled(False)
            return
        s = it.session
        if s.stats is not None and self.sel_id is None:
            ids = self._review_ids()
            pending = [k for k in ids if s.decisions[k].state == "proposed"]
            self.sel_id = (pending or ids or [None])[0]
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
        self.info_lbl.setToolTip(" · ".join(parts))
        self.info_lbl.setText(f"{fmt_time(s.info.duration_s)[:-4]} · {v.width}×{v.height} · "
                              f"{len(s.info.of_type('audio'))} audio track(s) · {len(s.info.of_type('subtitle'))} subtitle track(s)")
        self._load_settings_widgets()
        from bsr import smart
        ok, why = smart.supported(s.info)
        self.mode.model().item(1).setEnabled(ok)
        self.mode.setToolTip(why if not ok else "Copy safe boundaries; re-encode only near cuts without a safe boundary")
        self.mode.setItemData(1, why if not ok else "Copies safe boundaries; otherwise re-encodes only near the cut", Qt.ToolTipRole)
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(1 if ok and s.policy.boundary_reencode else 0)
        self.mode.blockSignals(False)
        self.mode_reason.setText("" if ok else why)
        self.mode_reason.setVisible(not ok)
        quality = self.mode.currentData() == "smart" and s.info.video.codec in ("h264", "hevc", "av1")
        self.crf_lbl.setVisible(quality); self.crf.setVisible(quality)
        self.refresh_table()
        self.refresh_overview()
        self.refresh_parts()
        self.show_detail()
        self.update_review_progress()

    def _visible_candidates(self):
        s = self.cur.session
        return [c for c in s.candidates if self.show_suppressed.isChecked() or not c.suppressed
                or s.decisions[c.id].state == "accepted" or s.decisions[c.id].custom_pts is not None]

    def refresh_table(self, *, selected_ids=None):
        if not self.cur or not self.cur.session:
            return
        s = self.cur.session
        if selected_ids is None:
            if getattr(self, "_table_session", None) is s and self.table.rowCount():
                selected_ids = self.selected_split_ids()
            else:
                selected_ids = [self.sel_id] if self.sel_id is not None else []
        self._table_session = s
        cands = self._visible_candidates()
        recommended_ids = {c.id for c in self._recommended() if c}
        manual = [d for d in s.decisions.values() if d.manual]
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(cands) + len(manual))
        rows = [(c.id, c) for c in cands] + [(d.id, None) for d in manual]
        sel_row = None
        for r, (k, c) in enumerate(rows):
            d = s.decisions[k]
            rc = s.resolved.get(k)
            if c is not None:
                vals = [str(r+1), fmt_time(c.start_s), f"{c.duration_s:.3f}s", str(c.n_frames),
                        c.kind + (" · lead" if c.leading else "") + (" · trail" if c.trailing else ""),
                        f"{c.score:.0f}" + (f" · {c.audio_db:.0f} dB" if c.audio_db is not None else "")]
            else:
                vals = [str(r+1), fmt_time(float(d.start_pts * s.vtb)), "manual", "", "manual cut", ""]
            dec = {"accepted": "✓ Kept", "rejected": "✗ Skipped", "proposed": "• Review"}[d.state]
            cut = "Checking…" if rc is None else (STATUS_LABEL.get(rc.status, rc.status) if d.state != "rejected" else "")
            if rc is not None and rc.status in ("copy", "copy-drop") and rc.boundaries:
                cut += " @ " + ", ".join(fmt_time(b.seconds) for b in rc.boundaries)
            elif rc is not None and rc.status == "reencode" and rc.smart:
                cut = f"Re-encode {rc.smart['tail_s'] + rc.smart['head_s']:.2f}s"
            vals += [dec, cut]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(("★ " if k in recommended_ids and col == 1 else "") + v)
                item.setData(Qt.UserRole, k)
                if k in recommended_ids:
                    item.setBackground(QColor(120, 220, 160, 20))
                    item.setToolTip("Recommended split at an expected segment break. Review it before keeping the split.")
                if col == 7 and rc is not None and d.state != "rejected":
                    item.setForeground(STATUS_COLORS.get(rc.status, QColor("white")))
                if c is not None and c.suppressed and d.state != "accepted":
                    item.setForeground(QColor(130, 130, 130))
                self.table.setItem(r, col, item)
            if k == self.sel_id:
                sel_row = r
        self.table.resizeColumnsToContents()
        selection = QItemSelection()
        model = self.table.model()
        for r, (k, _) in enumerate(rows):
            if k in selected_ids:
                selection.select(model.index(r, 0), model.index(r, self.table.columnCount()-1))
        sm = self.table.selectionModel()
        sm.select(selection, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        if sel_row is not None:
            sm.setCurrentIndex(model.index(sel_row, 0), QItemSelectionModel.NoUpdate)
        self.table.blockSignals(False)
        self.update_selected_actions()
        self.update_midpoint_recommendation()

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
        self.overview.set_breaks([(lo, hi) for lo, hi, _ in s.break_windows()], [c.id for c in self._recommended() if c])

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
        total_frames, total_seconds = 0, 0.0
        for r, sg in enumerate(plan.segments):
            notes = []
            if sg.drop_video_pts:
                notes.append(f"{len(sg.drop_video_pts)} black frame(s) not written")
            head, tail, duration = boundary_scope(s.idx[s.info.video.index], sg)
            total_frames += len(head) + len(tail)
            total_seconds += duration
            if duration:
                notes.append(f"Re-encode {duration:.2f}s near cut")
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
        smart_cuts = [rc for k, rc in s.resolved.items() if (rc.smart or rc.smart_edges) and s.decisions[k].state == "accepted"]
        self.scope_lbl.setText(
            f"Re-encoding: {total_seconds:.2f}s of video ({total_frames} frames) near {len(smart_cuts)} cut(s). Audio stays copied."
            if smart_cuts else ("Safe cuts are copied. Cuts without a safe keyframe use a short re-encode; review the scope before export."
                               if s.policy.boundary_reencode else "Strict copy: video and audio stay unchanged. Resolve cuts marked Needs review."))
        issues = plan.issues
        self.btn_export.setToolTip("\n".join(issues) if issues else "Review names, tracks and cuts, then export all parts")
        self.btn_export.setEnabled(self.cur.state not in ("analysing", "proving", "exporting"))
        self.export_issue.setText((issues[0] + " · Move, resolve or skip the split to continue.") if issues else "")
        self.export_issue.setVisible(bool(issues)); self.btn_export_issue.setVisible(bool(issues))
        self.update_review_progress()
        from bsr.planner import title_suggestions
        _, titles = title_suggestions(self.cur.path)
        self.btn_names.setEnabled(bool(titles))
        self.btn_episode_names.setEnabled(bool(s.episode))
        codes = self._episode_codes(self.cur)
        if codes:
            count = len(plan.segments)
            hint = (f"{count} part{'' if count == 1 else 's'} · season order suggests {codes[0]}" + (f"–{codes[-1]}" if len(codes) > 1 else "") +
                    (" · Episode names… to apply" if not any(s.names) else ""))
            if len(codes) != len(plan.segments):
                hint += f" · {s.detect.segments} segments expected"
            self.name_hint.setText(hint)
        else:
            self.name_hint.setText(f"{len(titles)} title suggestions · {len(plan.segments)} parts" +
                                   (" · Check intros/credits" if len(titles) != len(plan.segments) else " · Check title order")
                                   if titles else f"{len(plan.segments)} parts · File names are editable")
        self.name_hint.setToolTip(" · ".join(titles))
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
    def selected_split_ids(self):
        if not self.cur or not self.cur.session or getattr(self, "_table_session", None) is not self.cur.session:
            return []
        return [self.table.item(row.row(), 0).data(Qt.UserRole)
                for row in sorted(self.table.selectionModel().selectedRows(), key=lambda row: row.row())
                if self.table.item(row.row(), 0) is not None]

    def update_selected_actions(self):
        ids = self.selected_split_ids()
        enabled = bool(ids and self.cur.state in ("ready", "done"))
        self.btn_keep_selected.setEnabled(enabled); self.btn_skip_selected.setEnabled(enabled)
        self.selection_hint.setText(f"{len(ids)} selected · Hold Shift to select a range."
                                    if ids else "Hold Shift to select a range of split points.")

    def _table_sel(self, *_):
        self.update_selected_actions()
        ids = self.selected_split_ids()
        if ids:
            item = self.table.item(self.table.currentRow(), 0)
            k = item.data(Qt.UserRole) if item is not None else None
            if k not in ids: k = ids[0]
            if k != self.sel_id:
                self.sel_id = k
                self.overview.selected = k; self.overview.update()
                self.show_detail()

    def select_candidate(self, k):
        self.sel_id = k
        self.refresh_table(selected_ids=[k])
        self.overview.selected = k; self.overview.update()
        self.show_detail()

    def _decision(self):
        if not self.cur or not self.cur.session or self.sel_id is None:
            return None
        return self.cur.session.decisions.get(self.sel_id)

    def show_detail(self):
        d = self._decision()
        for w in self.detail_widgets:
            w.setEnabled(d is not None and self.cur.state not in ("analysing", "proving", "exporting"))
        self.btn_manual.setEnabled(bool(self.cur and self.cur.session and self.cur.session.stats is not None and self.cur.state not in ("analysing", "proving", "exporting")))
        self._clear_choices()
        if d is None:
            self.d_title.setText("Watch your video, then choose a suggested split or add one at the playhead.")
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
        ids = self._review_ids()
        number = ids.index(d.id)+1 if d.id in ids else d.id
        state = {"accepted": "Kept", "rejected": "Skipped", "proposed": "Review this split"}[d.state]
        head = f"<b>Split {number} of {len(ids)}</b> · {state}"
        head += f"<br><small>{'Black transition' if not d.manual else 'Manual split'}: {span}"
        if rc: head += f" · {STATUS_LABEL.get(rc.status, rc.status)}"
        head += "</small>"
        self.d_title.setText(head)
        for w, v in ((self.pos, None), (self.chk_remove, None), (self.pad_a, None), (self.pad_b, None)):
            w.blockSignals(True)
        self.pos.setCurrentIndex(max(0, self.pos.findData(d.position)))
        self.chk_remove.setChecked(d.remove_black)
        self.pad_b.setValue(d.pad_before_s); self.pad_a.setValue(d.pad_after_s)
        for w in (self.pos, self.chk_remove, self.pad_a, self.pad_b):
            w.blockSignals(False)
        msgs = list(rc.messages) if rc else ["Checking copy boundaries…"]
        if c and c.midpoint_only:
            msgs.insert(0, ("Midpoint boost" if s.detect.segments == 2 else "Segment-break boost") +
                        " found this shorter or lighter black transition. Watch it before keeping the split.")
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
        if d.custom_pts is not None and not (rc and rc.boundaries):
            cut_pts = d.custom_pts
        self.cut_time.setText(fmt_time(float(cut_pts * tb)))
        self.preview_pts = cut_pts
        if self.advanced_toggle.isChecked(): self._load_thumbs(rc, d)
        self._load_preview(cut_pts, exact=self.advanced_toggle.isChecked())

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

    def set_state(self, st, advance=False):
        d = self._decision()
        if not d or self.cur.state in ("analysing", "proving", "exporting"):
            return
        self.remember_decisions()
        d.state = st
        self._after_decision(advance=advance)

    def decision_changed(self, *_):
        d = self._decision()
        if not d:
            return
        if self.cur.state in ("analysing", "proving", "exporting"):
            return
        self.remember_decisions()
        d.custom_pts = None
        d.choice = None
        d.position = self.pos.currentData()
        d.remove_black = self.chk_remove.isChecked()
        d.pad_before_s = self.pad_b.value(); d.pad_after_s = self.pad_a.value()
        self._after_decision()

    def apply_choice(self):
        d = self._decision()
        b = self.choice_group.checkedButton() if hasattr(self, "choice_group") else None
        if not d or not b:
            return
        if self.cur.state in ("analysing", "proving", "exporting"):
            return
        self.remember_decisions()
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
        if d and self.cur.state not in ("analysing", "proving", "exporting"):
            self.remember_decisions()
            d.choice = None
            self._after_decision()

    def _after_decision(self, advance=False, ids=None):
        it = self.cur; k = self.sel_id
        it.state = "proving"
        self.update_review_progress()
        for widget in self.detail_widgets: widget.setEnabled(False)
        self.btn_manual.setEnabled(False); self.btn_export.setEnabled(False)
        self.status.showMessage("Checking selected split points…" if ids and len(ids) > 1 else "Checking this split…")
        def done(_):
            it.state = "ready"
            self.refresh_queue()
            if it is not self.cur: return
            self.status.clearMessage()
            self.refresh_table(); self.refresh_overview(); self.refresh_parts()
            rc = it.session.resolved.get(k)
            if advance and not (rc and rc.status == "needs-review" and it.session.decisions[k].state == "accepted"):
                self.next_unreviewed()
            else: self.show_detail()
        self._run(self._prove_all, it, ids=ids or [k], on_done=done)

    def remember_decisions(self):
        self.cur.history.append((copy.deepcopy(self.cur.session.decisions), self.sel_id, self.selected_split_ids()))
        self.cur.history = self.cur.history[-50:]

    def undo_decision(self):
        if not self.cur or not self.cur.history or self.cur.state in ("analysing", "proving", "exporting"): return
        self.cur.session.decisions, self.sel_id, selected_ids = self.cur.history.pop()
        self.cur.session.resolved = {k: rc for k, rc in self.cur.session.resolved.items() if k in self.cur.session.decisions}
        if self.sel_id not in self.cur.session.decisions: self.sel_id = None
        self.refresh_table(selected_ids=selected_ids)
        self._after_decision(ids=list(self.cur.session.decisions))

    def set_selected_state(self, state):
        if not self.cur or self.cur.state not in ("ready", "done"): return
        ids = self.selected_split_ids()
        if not ids or state not in ("accepted", "rejected"): return
        s = self.cur.session
        if all(s.decisions[k].state == state for k in ids): return
        self.remember_decisions()
        for k in ids: s.decisions[k].state = state
        self._after_decision(ids=ids)

    def _review_ids(self):
        if not self.cur or not self.cur.session: return []
        s = self.cur.session
        ids = {c.id for c in self._visible_candidates()} | {k for k, d in s.decisions.items() if d.manual}
        return sorted(ids, key=lambda k: s.decisions[k].custom_pts if s.decisions[k].custom_pts is not None else s.decisions[k].start_pts)

    def navigate_cut(self, delta):
        ids = self._review_ids()
        if not ids: return
        i = ids.index(self.sel_id) if self.sel_id in ids else -1
        self.select_candidate(ids[max(0, min(i+delta, len(ids)-1))])

    def next_unreviewed(self):
        if not self.cur or not self.cur.session: return
        s = self.cur.session; ids = self._review_ids()
        pending = [k for k in ids if s.decisions[k].state == "proposed" or
                   (s.decisions[k].state == "accepted" and (k not in s.resolved or s.resolved[k].status == "needs-review"))]
        if pending:
            later = [k for k in pending if self.sel_id not in ids or ids.index(k) > ids.index(self.sel_id)]
            self.select_candidate((later or pending)[0])
        else:
            self.show_detail()
            self.status.showMessage("Review complete — check part names, then Finish job.")

    def keep_all_shown(self):
        if not self.cur or self.cur.state not in ("ready", "done"): return
        s = self.cur.session
        ids = [k for k in self._review_ids() if s.decisions[k].state == "proposed"]
        if not ids: return
        self.remember_decisions()
        for k in ids: s.decisions[k].state = "accepted"
        self._after_decision(ids=ids)

    def _break_status(self):
        """(suggested or kept transition, already kept?) for each expected segment break."""
        if not self.cur or not self.cur.session or self.cur.session.stats is None:
            return []
        return self.cur.session.break_status(self._visible_candidates())

    def _recommended(self):
        """Recommended or kept split (or None) for each expected segment break, among visible suggestions."""
        return [c for c, _ in self._break_status()]

    def _midpoint_candidate(self):
        """The first recommended split still waiting for a decision (else the first recommended)."""
        picks = [c for c in self._recommended() if c]
        if not picks:
            return None
        s = self.cur.session
        return next((c for c in picks if s.decisions[c.id].state == "proposed"), picks[0])

    def update_midpoint_recommendation(self):
        s = self.cur.session if self.cur else None
        candidate = self._midpoint_candidate()
        busy = bool(self.cur and self.cur.state in ("analysing", "proving", "exporting"))
        self.btn_midpoint.setEnabled(candidate is not None and not busy)
        status = self._break_status()
        picks = [c for c, _ in status]
        found = [c for c, kept in status if c or kept]
        pending = [c for c, kept in status if c and not kept and s.decisions[c.id].state == "proposed"]
        self.btn_keep_rec.setEnabled(bool(pending) and not busy)
        self.btn_keep_rec.setVisible(bool(s and s.detect.segments >= 2))
        segments = s.detect.segments if s else 2
        self.btn_midpoint.setText("Review midpoint" if segments == 2 else "Review recommended")
        if not s or s.stats is None:
            self.midpoint_label.setText("Midpoint recommendation" if segments == 2 else f"Recommended splits · {segments} segments")
            self.midpoint_hint.setText(("Looking near halfway…" if segments == 2 else "Looking near each expected segment break…")
                                       if busy else "Find split points to see black transitions where segments usually change.")
            return
        if segments < 2:
            self.midpoint_label.setText("No split recommended")
            self.midpoint_hint.setText("1 segment expected: this file stays whole unless you keep or add a split.")
            return
        start, end = video_bounds(s.stats)
        pct = lambda c: 100 * ((c.start_s + c.end_s) / 2 - start) / max(1e-6, end - start)
        if segments == 2:
            if candidate:
                self.midpoint_label.setText(f"Midpoint suggestion · {fmt_time(candidate.start_s)}")
                self.midpoint_hint.setText(f"{pct(candidate):.0f}% through the video · {candidate.n_frames} black frame(s). Review before keeping.")
            else:
                self.midpoint_label.setText("No midpoint suggestion")
                self.midpoint_hint.setText("No remaining black transition in the middle 40–60%. You can still add a split at the playhead.")
            return
        targets = [100 * (t - start) / max(1e-6, end - start) for _, _, t in s.break_windows()]
        self.midpoint_label.setText(f"Recommended splits · {segments} segments" +
                                    (" · " + ", ".join(fmt_time(c.start_s)[:-4] for c in picks if c) if any(picks) else ""))
        spots = [(f"{pct(c):.0f}% ({c.n_frames} black frame{'' if c.n_frames == 1 else 's'})" if c else f"near {t:.0f}%")
                 + (" kept" if kept else "") if c or kept else
                 f"nothing near {t:.0f}%, add a split at the playhead" for (c, kept), t in zip(status, targets)]
        kept = sum(1 for _, k in status if k)
        self.midpoint_hint.setText(f"{len(found)} of {len(picks)} breaks found: " + "; ".join(spots) + "." +
                                   (f" {kept} kept." if kept else " Review before keeping."))

    def review_midpoint(self):
        if not self.cur or self.cur.state not in ("ready", "done"):
            return
        candidate = self._midpoint_candidate()
        if candidate:
            self.select_candidate(candidate.id)

    def keep_recommended(self):
        if not self.cur or self.cur.state not in ("ready", "done"):
            return
        s = self.cur.session
        ids = [c.id for c, kept in self._break_status() if c and not kept and s.decisions[c.id].state == "proposed"]
        if not ids:
            return
        self.remember_decisions()
        for k in ids:
            s.decisions[k].state = "accepted"
        self.log(f"Kept {len(ids)} recommended split(s) in {s.path}")
        self._after_decision(ids=ids)

    def keep_recommended_all(self):
        """Keep the recommended splits in every analysed episode, then check those cuts."""
        changed = 0
        for it in self.items:
            if it.state not in ("ready", "done") or not it.session or it.session.stats is None:
                continue
            s = it.session
            it.history.append((copy.deepcopy(s.decisions), self.sel_id if it is self.cur else None, []))
            ids = s.accept_recommended()
            if not ids:
                it.history.pop()
                continue
            changed += len(ids)
            it.state = "proving"
            def done(_, it=it):
                it.state = "ready"   # new kept splits mean the file needs exporting again
                self.refresh_queue()
                if it is self.cur:
                    self.refresh_table(); self.refresh_overview(); self.refresh_parts(); self.show_detail()
                    self.update_review_progress()
            self._run(self._prove_all, it, ids=ids, on_done=done)
        self.refresh_queue()
        missing = sum(1 for it in self.items if it.session and it.session.stats is not None
                      and any(c is None for c in it.session.recommended()))
        self.status.showMessage(f"Kept {changed} recommended split(s)" +
                                (f" · {missing} episode(s) have a break with no black transition — add those splits by hand" if missing else "")
                                if changed else "No recommended splits left to keep" + (" — analyse episodes first" if not any(
                                    it.session and it.session.stats is not None for it in self.items) else ""))
        if self.cur and self.cur.state == "proving":
            self.update_review_progress()

    def default_segments(self) -> int:
        try:
            return max(1, min(4, int(self.settings.value("default_segments", 2))))
        except (TypeError, ValueError):
            return 2

    def segments_changed(self, *_):
        if not self.cur or not self.cur.session or getattr(self, "_loading", False):
            return
        if self.cur.state in ("analysing", "proving", "exporting"):
            self._load_settings_widgets(); return
        n = self.segments.currentData()
        if n is not None and n >= 2:
            # Remember 2/3/4 as the season's usual count for new files; a one-off whole file does not change it.
            self.settings.setValue("default_segments", int(n))
        s = self.cur.session
        s.set_segments(n, default=self.default_segments())
        self.log(f"{os.path.basename(s.path)}: expecting {s.detect.segments} segment(s)" + (" (from the file name)" if n is None else ""))
        self._load_settings_widgets()
        if s.stats is not None:
            self.redetect()
        else:
            self.update_review_progress()

    def update_review_progress(self):
        self.update_selected_actions()
        self.update_midpoint_recommendation()
        s = self.cur.session if self.cur else None
        ready = bool(s and s.stats is not None)
        busy = bool(self.cur and self.cur.state in ("analysing", "proving", "exporting"))
        for widget in (self.btn_review_next, self.btn_keep_all, self.btn_previous, self.btn_next): widget.setEnabled(ready and not busy)
        self.btn_keep_rec_all.setEnabled(any(i.session and i.session.stats is not None and i.state in ("ready", "done")
                                             for i in self.items))
        self.btn_episodes_all.setEnabled(any(i.session and i.session.episode for i in self.items))
        self.btn_undo.setEnabled(bool(self.cur and self.cur.history and ready and not busy))
        if not ready:
            self.review_progress.setText("Finding split points…" if busy else "Add a video, then find its split points.")
            self.btn_manual.setEnabled(False)
            return
        ids = self._review_ids()
        kept = sum(s.decisions[k].state == "accepted" for k in ids)
        skipped = sum(s.decisions[k].state == "rejected" for k in ids)
        pending = sum(s.decisions[k].state == "proposed" for k in ids)
        issues = sum(s.decisions[k].state == "accepted" and (k not in s.resolved or s.resolved[k].status == "needs-review") for k in ids)
        self.review_progress.setText(f"{kept} kept · {skipped} skipped · {pending} left" + (f" · {issues} need attention" if issues else ""))

    def player_position(self, seconds):
        if not self.cur or not self.cur.session or not self.player.is_playing(): return
        s = self.cur.session
        if s.stats is None: return
        i = int(np.clip(np.searchsorted(s.stats.pts, seconds/float(s.vtb), side="right")-1, 0, len(s.stats.pts)-1))
        self.preview_pts = int(s.stats.pts[i]); self.strip.set_current(self.preview_pts)

    def add_manual_cut(self):
        if not self.cur or self.cur.session.stats is None or self.cur.state not in ("ready", "done") or self.preview_pts is None: return
        self.player.pause(); self.remember_decisions()
        d = self.cur.session.add_manual(float(self.preview_pts * self.cur.session.vtb))
        self.sel_id = d.id
        self.refresh_table(selected_ids=[d.id])
        self.log(f"Manual split {d.id} at {fmt_time(float(self.preview_pts * self.cur.session.vtb))}")
        self._after_decision()

    def move_cut_to_playhead(self):
        if self.preview_pts is not None: self.move_selected_cut(self.preview_pts)

    def move_cut_from_time(self):
        if not self.cur or not self.cur.session: return
        try:
            from .review import parse_time
            seconds = parse_time(self.cut_time.text())
        except ValueError as exc:
            self.status.showMessage(str(exc)); return
        s = self.cur.session
        if s.stats is None: return
        if not float(s.stats.pts[0]*s.vtb) <= seconds < float(s.idx[s.info.video.index].end_pts()*s.vtb):
            self.status.showMessage("Choose a split time inside this video."); return
        i = int(np.clip(np.searchsorted(s.stats.pts, seconds/float(s.vtb)), 0, len(s.stats.pts)-1))
        self.move_selected_cut(int(s.stats.pts[i]))

    def move_selected_cut(self, pts):
        d = self._decision()
        if not d or self.cur.state not in ("ready", "done"): return
        self.player.pause(); self.remember_decisions()
        d.custom_pts = int(pts); d.choice = None; d.remove_black = False
        self._after_decision()

    # ------------------------------------------------------------------ previews
    def _load_thumbs(self, rc, d):
        """Six frames: picture before black · first black · last of earlier part | first of next part ·
        last black · first picture after black (planned cut, or requested position if unresolved)."""
        s = self.cur.session
        fs = s.stats
        if fs is None:
            return
        pts = fs.pts
        def idx_of(p):
            return int(np.clip(np.searchsorted(pts, p), 0, len(pts) - 1))
        dropped = set(p for o in (rc.boundaries if rc else []) for p in o.dropped_pts)
        resolved = bool(rc and rc.boundaries)
        if resolved:
            a_cut, b_cut = (rc.removed if rc.removed else (rc.boundaries[0].pts, rc.boundaries[0].pts))
        else:
            a_cut = b_cut = rc.requested_pts if rc and rc.requested_pts is not None else (d.start_pts + d.end_pts) // 2
        i_s, i_e = idx_of(d.start_pts), idx_of(d.end_pts)
        i_a = idx_of(a_cut) - 1
        while i_a > 0 and int(pts[i_a]) in dropped:
            i_a -= 1
        slots = [
            (int(pts[max(0, i_s - 1)]), "before black"),
            (int(pts[i_s]), "first black"),
            (int(pts[max(0, i_a)]), "last of part" if resolved else "requested −1"),
            (int(pts[idx_of(b_cut)]), "first of next" if resolved else "requested"),
            (int(pts[max(0, i_e - 1)]), "last black"),
            (int(pts[i_e]), "after black"),
        ]
        if d.manual:
            slots[0] = (int(pts[max(0, i_a - 2)]), ""); slots[1] = (int(pts[max(0, i_a - 1)]), "")
            slots[4] = (int(pts[min(len(pts) - 1, idx_of(b_cut) + 1)]), ""); slots[5] = (int(pts[min(len(pts) - 1, idx_of(b_cut) + 2)]), "")
        info = s.info
        key = (self.cur.path, tuple(p for p, _ in slots))
        self._thumb_key = key

        def job(jobobj):
            r = FrameReader(info)
            try:
                frs = r.frames_at([p for p, _ in slots])
                return {p: frame_to_qimage(f, 94, sar=info.video.sar) for p, f in frs.items()}
            finally:
                r.close()

        def done(res):
            if getattr(self, "_thumb_key", None) != key:
                return
            for (lb, cap), (p, tag) in zip(self.thumbs, slots):
                img = res.get(p)
                if img is not None:
                    lb.setPixmap(QPixmap.fromImage(img).scaled(lb.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                else:
                    lb.clear()
                cap.setText((tag + " · " if tag else "") + fmt_time(float(p * s.vtb))[2:])
        self._run(job, on_done=done, pool="preview")

    def preview_time(self, t):
        if not self.cur or not self.cur.session:
            return
        s = self.cur.session
        if s.stats is None:
            self.player.seek_source(t)
            return
        p = int(round(t / s.vtb))
        i = int(np.clip(np.searchsorted(s.stats.pts, p), 0, len(s.stats.pts) - 1))
        self._load_preview(int(s.stats.pts[i]), exact=self.advanced_toggle.isChecked())

    def preview_pts_at(self, pts):
        self._load_preview(pts)

    def step(self, n):
        s = self.cur.session if self.cur else None
        if not s or s.stats is None or self.preview_pts is None:
            return
        i = int(np.searchsorted(s.stats.pts, self.preview_pts))
        i = int(np.clip(i + n, 0, len(s.stats.pts) - 1))
        self._load_preview(int(s.stats.pts[i]))

    def _load_preview(self, pts, exact=True):
        s = self.cur.session
        self.player.seek_source(float(pts * s.vtb), precise=exact)
        self.preview_pts = pts
        self.strip.set_current(pts)
        info = s.info
        key = (self.cur.path, pts)
        self._pv_key = key
        if not exact:
            self._pv_frame = None
            self.pv_lbl.setText("Playback preview. Open frame preview to inspect the exact source frame.")
            return

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
        self.player.show_still(img)
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
        if not self.cur or self.preview_pts is None: return
        if self.player.is_playing() and self.player.range_ms is not None:
            self.player.pause(); return
        s = self.cur.session
        decision = self._decision()
        resolved = s.resolved.get(decision.id) if decision else None
        pts = (resolved.boundaries[0].pts if resolved and resolved.boundaries else
               decision.custom_pts if decision and decision.custom_pts is not None else
               (decision.start_pts + decision.end_pts)//2 if decision else self.preview_pts)
        center = float(pts * s.vtb)
        self.player.play_range(center-2, center+2)

    # ------------------------------------------------------------------ export
    def mode_changed(self, *_):
        """Store mode per session so accepting later cuts and switching files agree."""
        it = self.cur
        if not it or not it.session:
            return
        smart_mode = self.mode.currentData() == "smart"
        quality = smart_mode and it.session.info.video.codec in ("h264", "hevc", "av1")
        self.crf_lbl.setVisible(quality); self.crf.setVisible(quality)
        s = it.session
        s.policy.boundary_reencode = smart_mode
        for d in s.decisions.values():
            if not smart_mode and d.choice == "reencode":
                d.choice = None
        ids = [k for k, d in s.decisions.items() if d.state != "rejected"]
        if ids:
            it.state = "proving"
            self.show_detail(); self.update_review_progress(); self.btn_export.setEnabled(False)
            self.log(f"Mode {'exact cuts' if smart_mode else 'strict copy'}: updated cuts {ids}")
            self.status.showMessage("Updating cuts…")
            def done(_):
                it.state = "ready"
                if it is not self.cur:
                    self.refresh_queue(); return
                self.refresh_table(); self.refresh_overview(); self.refresh_parts(); self.show_detail(); self.refresh_queue()
            self._run(self._prove_all, it, ids=ids, on_done=done)

    def choose_out(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_edit.text() or os.path.expanduser("~"))
        if d:
            self.out_edit.setText(d)
            self.settings.setValue("out_dir", d)

    def _episode_entries(self):
        from bsr.episodes import FileEntry
        return [FileEntry(i.path, i.session.episode, i.session.part_count())
                for i in self.items if i.session and i.session.episode]

    def _episode_codes(self, it):
        """Offline episode codes this file's parts would get (season order across the queue)."""
        if not it or not it.session or not it.session.episode:
            return []
        from bsr.episodes import number_files
        numbers = number_files(self._episode_entries(), default_segments=self.default_segments())
        return [a.code for a in numbers.get(it.path, [])]

    def episode_names(self, *_, current=None):
        entries = self._episode_entries()
        if not entries:
            QMessageBox.information(self, APP_NAME, "No file names with season and episode numbers (such as S01E01) were found.")
            return False
        from .episodes_dialog import EpisodeDialog
        dlg = EpisodeDialog(self, entries, current=current, default_segments=self.default_segments())
        if not dlg.exec():
            return True
        names, fractions = dlg.names(), dlg.fractions()
        busy = [os.path.basename(i.path) for i in self.items if i.path in names and i.state in ("analysing", "proving", "exporting")]
        names = {p: n for p, n in names.items() if os.path.basename(p) not in busy}
        if busy:
            QMessageBox.information(self, APP_NAME, "These files are busy and kept their names:\n" + "\n".join(busy) +
                                    "\n\nApply episode names again when they finish.")
        for it in self.items:
            if it.session and it.path in names:
                it.session.names = names[it.path]
                f = fractions.get(it.path)
                s = it.session
                if f and s.segments_auto and len(f) == s.detect.segments - 1 and f != s.detect.break_fractions:
                    s.detect.break_fractions = f
                    if s.stats is not None and it.state in ("ready", "done"):
                        self._refind(it)
        self.log(f"Applied episode names to {len(names)} file(s)")
        if self.cur:
            self.refresh_parts()
        self.status.showMessage(f"Episode names applied to {len(names)} file(s). Check them in each file's parts list.")
        return True

    def _refind(self, it):
        """Recompute suggestions from cached analysis, then check copy boundaries (in the background)."""
        it.state = "proving"
        self.refresh_queue()

        def job(jobobj, it):
            it.session.find()
            return MainWindow._prove_all(jobobj, it)

        def done(_, it=it):
            it.state = "ready"
            self.refresh_queue()
            if it is self.cur:
                self.show_item()
        self._run(job, it, on_done=done)

    def names_from_title(self):
        if not self.cur or not self.cur.session or not getattr(self, "_plan", None): return
        from bsr.planner import title_suggestions
        prefix, titles = title_suggestions(self.cur.path)
        if not titles: return
        names = [self.parts.item(r, 4).text() for r in range(self.parts.rowCount())]
        dlg = NamesDialog(self, prefix, titles, self._plan.segments, names)
        if dlg.exec():
            self.cur.session.names = dlg.names()
            self.refresh_parts()

    def choose_tracks(self):
        if not self.cur or not self.cur.session:
            return
        dlg = TracksDialog(self, self.cur.session.info, self.cur.streams or default_streams(self.cur.session.info))
        if dlg.exec():
            self.cur.streams = dlg.selected()

    def focus_export_issue(self):
        if not self.cur or not self.cur.session or self.cur.state in ("analysing", "proving", "exporting"):
            return
        s = self.cur.session
        accepted = s.accepted()
        k = next((d.id for d in accepted if d.id not in s.resolved or
                  s.resolved[d.id].status == "needs-review"), None)
        if k is None:
            plan = s.plan(resolve=False)
            short = next((sg for sg in plan.segments if sg.duration_s < s.policy.min_segment_s), None)
            if short and accepted:
                edge = short.end_pts if short.start_pts == plan.segments[0].start_pts else short.start_pts
                k = min(accepted, key=lambda d: abs((d.custom_pts if d.custom_pts is not None else
                            (d.start_pts + d.end_pts)//2) - edge)).id
        if k is not None:
            self.select_candidate(k)
            self.detail_scroll.ensureWidgetVisible(self.btn_accept)

    def review_export(self):
        it = self.cur
        if not it or not it.session or it.session.stats is None:
            return
        if it.state in ("analysing", "proving", "exporting"):
            return
        self.player.pause()
        s = it.session
        plan = s.plan(resolve=False)
        if plan.issues:
            dlg = QMessageBox(self)
            dlg.setWindowTitle("Review before finishing")
            dlg.setIcon(QMessageBox.Warning)
            dlg.setText("A split needs attention before these parts can be exported.")
            dlg.setInformativeText("\n".join(plan.issues) + "\n\nMove, resolve or skip the split, then finish again.")
            review = dlg.addButton("Review split", QMessageBox.AcceptRole)
            dlg.addButton("Back", QMessageBox.RejectRole)
            dlg.exec()
            if dlg.clickedButton() is review: self.focus_export_issue()
            return
        out = self.out_edit.text().strip()
        if not out:
            QMessageBox.information(self, APP_NAME, "Choose an output folder first."); return
        if os.path.realpath(out) == os.path.realpath(os.path.dirname(it.path)):
            r = QMessageBox.question(self, APP_NAME, "The output folder is the source folder. Outputs never overwrite "
                                     "sources, but a separate folder is recommended. Continue?")
            if r != QMessageBox.Yes:
                return
        if self.mode.currentData() == "full":
            QMessageBox.information(self, APP_NAME, "Full re-encoding is not offered in this version."); return
        streams = it.streams or default_streams(s.info)
        fmt, ext = output_format(s.info)
        seg_plans = [plan_segment(s.info, s.idx, sg, streams, ext) for sg in plan.segments]
        notes = preservation_report(s.info, streams, fmt)
        pending = sum(d.state == "proposed" for d in s.decisions.values())
        if pending:
            notes.append(f"{pending} suggested split(s) are not kept. Only splits you kept are exported.")
        uses_smart = any(sp.head or sp.tail for sp in seg_plans)
        mode_label = ("Exact cuts with boundary re-encoding — other video and audio copied"
                      if uses_smart else "No re-encoding (strict copy)")
        dlg = PlanDialog(self, s, plan, seg_plans, notes, mode_label, out)
        if not dlg.exec():
            return
        self.settings.setValue("out_dir", out)
        it.state, it.progress = "exporting", 0.0
        self.refresh_queue()
        self._busy(True, "Exporting…")
        self.show_detail(); self.update_review_progress(); self.btn_export.setEnabled(False)
        self.log(f"Export {s.path} -> {out}: {len(plan.segments)} parts, streams {streams}")

        def job(jobobj, export_item):
            return export_plan(s.info, s.idx, plan, ExportOptions(out_dir=out, streams=streams, crf=self.crf.value()),
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
            if it is self.cur: self.show_detail(); self.refresh_parts(); self.update_review_progress()
            ResultsDialog(self, rep).exec()
        self._run(job, it, on_done=done, on_progress=prog)

    def show_last_report(self):
        if self.cur and self.cur.last_report:
            ResultsDialog(self, self.cur.last_report).exec()

    def show_log(self):
        LogDialog(self, self.logs, log_path()).exec()

    def show_about(self):
        AboutDialog(self).exec()

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
            s.index(cancel=jobobj.cancel); s.analyze(cancel=jobobj.cancel)
            # Saved decisions are matched to the new detection by their black interval, and
            # kept splits that detection no longer reports are carried over.
            s.decisions = dict(decisions)
            s.find()
            s.resolved = {}
            return MainWindow._prove_all(jobobj, it)
        it.state = "analysing"
        self._busy(True, "Loading project…")
        self._run(job, it, on_done=lambda _: self._analysed(it), on_progress=lambda st, f: self.prog.setValue(int(f * 100)))

    # ------------------------------------------------------------ title bar
    def _set_logo(self, size):
        self._logo.setFixedSize(size, size)
        self._logo.setPixmap(QPixmap(icon_path()).scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _apply_chrome(self):
        """Turn the brand bar into the macOS title bar, traffic lights and all.

        On Windows the system title bar stays (snap layouts, window buttons) and is
        painted charcoal with cream text so it runs straight into the brand bar.
        """
        if winchrome.available():
            if not getattr(self, "_win_chrome", False):
                self._win_chrome = winchrome.integrate(self, FILM, CREAM)
            return
        on = macchrome.integrate(self, QColor(CHARCOAL), toolbar=True)
        inset = macchrome.top_inset(self, macchrome.TOOLBAR_HEIGHT) if on and not self.isFullScreen() else 0
        if inset == self._chrome_inset:
            return
        self._chrome_inset = inset
        if inset:
            # The strip is the brand bar; its yellow rule sits just below the traffic lights.
            self._brand.setFixedHeight(inset + 2)
            self._brand_lay.setContentsMargins(macchrome.TRAFFIC_LIGHTS_WIDTH + 8, 0, 18, 0)
            self._set_logo(32)
        else:
            self._brand.setMinimumHeight(0); self._brand.setMaximumHeight(16777215)
            self._brand_lay.setContentsMargins(14, 8, 18, 8)
            self._set_logo(44)

    def showEvent(self, e):
        super().showEvent(e)
        self._apply_chrome()
        handle = self.windowHandle()
        if handle is not None and hasattr(handle, "safeAreaMarginsChanged") and not getattr(self, "_watching_safe_area", False):
            self._watching_safe_area = True
            handle.safeAreaMarginsChanged.connect(lambda *_: self._apply_chrome())

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._apply_chrome)

    def mousePressEvent(self, e):
        # The brand bar drags the window even where Qt, not macOS, gets the click.
        handle = self.windowHandle()
        if e.button() == Qt.LeftButton and e.position().y() < (self._chrome_inset or 0) and handle:
            handle.startSystemMove()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < (self._chrome_inset or 0):
            self.showNormal() if self.isMaximized() else self.showMaximized()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def closeEvent(self, e):
        self.player.shutdown()
        self.cancel_jobs()
        self.pools.wait()
        super().closeEvent(e)


def main(argv=None):
    args = list(sys.argv if argv is None else argv)
    # Cocoa creates and caches its application menu during QApplication construction.
    # The bundled Python has no CFBundleName, so Qt falls back to its argv[0].
    # Brand both that fallback and the Qt identity before any native menus exist.
    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName(APP_NAME)
    QApplication.setApplicationVersion(ENGINE_VERSION)
    QApplication.setOrganizationName("BananaSplitRedux")
    app = QApplication([APP_NAME, *args[1:]])
    apply_theme(app)
    icon = os.environ.get("BSR_ICON")
    if icon and os.path.exists(icon):
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(icon))
    w = MainWindow()
    w.show()
    files = [a for a in args[1:] if os.path.exists(a)]
    if files:
        w.add_paths(files)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
