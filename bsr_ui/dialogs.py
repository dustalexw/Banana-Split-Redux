"""Dialogs: export plan summary, results, diagnostics log, track selection."""
from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextBrowser, QDialogButtonBox,
                               QPlainTextEdit, QPushButton, QCheckBox, QScrollArea, QWidget,
                               QTableWidget, QTableWidgetItem, QHeaderView, QComboBox)

from bsr.media import fmt_time
from bsr import ENGINE_VERSION
from .theme import icon_path


class NamesDialog(QDialog):
    """Map suggested titles to actual parts before changing output names."""
    def __init__(self, parent, prefix, titles, segments, current):
        super().__init__(parent)
        self.setWindowTitle("Suggest part names")
        self.resize(860, 480)
        lay = QVBoxLayout(self)
        hint = QLabel(f"{len(titles)} titles found · {len(segments)} output parts. " +
                      ("Suggested in file-title order; check each part before applying." if len(titles) == len(segments) else
                       "Counts differ. Choose each title after checking for intros, credits or missed splits."))
        hint.setWordWrap(True); lay.addWidget(hint)
        table = QTableWidget(len(segments), 3)
        table.setHorizontalHeaderLabels(["Part", "From → To", "Suggested file name (editable)"])
        table.verticalHeader().hide(); table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.combos = []
        for r, seg in enumerate(segments):
            for col, text in enumerate((str(seg.n), f"{fmt_time(seg.start_s)} → {fmt_time(seg.end_s)}")):
                item = QTableWidgetItem(text); item.setFlags(item.flags() & ~Qt.ItemIsEditable); table.setItem(r, col, item)
            combo = QComboBox(); combo.setEditable(True)
            combo.addItem(current[r])
            for title in titles + ["Intro", "Credits"]:
                combo.addItem(f"{prefix + ' - ' if prefix else ''}{r+1:02d} {title}")
            if len(titles) == len(segments): combo.setCurrentIndex(r+1)
            table.setCellWidget(r, 2, combo); self.combos.append(combo)
        table.resizeColumnsToContents(); lay.addWidget(table)
        note = QLabel("These names come from the source filename. Preview the parts to confirm which short is which.")
        note.setObjectName("muted"); note.setWordWrap(True); lay.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.addButton("Apply names", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); lay.addWidget(buttons)

    def names(self):
        from bsr.planner import safe_filename
        return [safe_filename(combo.currentText()) for combo in self.combos]


class AboutDialog(QDialog):
    """Branded application information, also reached through the macOS app menu."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About Banana Split Redux")
        self.setObjectName("aboutDialog")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 24, 32, 22)
        lay.setSpacing(14)
        logo = QLabel()
        logo.setAlignment(Qt.AlignCenter)
        logo.setPixmap(QPixmap(icon_path()).scaled(112, 112, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lay.addWidget(logo)
        title = QLabel("Banana Split Redux")
        title.setObjectName("brandTitle")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        version = QLabel(f"Version {ENGINE_VERSION}")
        version.setObjectName("aboutVersion")
        version.setAlignment(Qt.AlignCenter)
        lay.addWidget(version)
        tagline = QLabel("Every short deserves its own spotlight.")
        tagline.setObjectName("brandSubtitle")
        tagline.setAlignment(Qt.AlignCenter)
        lay.addWidget(tagline)
        description = QLabel("Turn episodes into individual shorts. Find black transitions,\n"
                             "review your cuts, and export independently playable parts.")
        description.setAlignment(Qt.AlignCenter)
        description.setWordWrap(True)
        lay.addWidget(description)
        features = QLabel("<b>Keep the original quality</b> with verified copy cuts.<br>"
                          "<b>Cut exactly</b> by re-encoding only near a cut when needed.<br>"
                          "<b>Keep your sources safe</b> — original videos stay untouched.")
        features.setObjectName("aboutFeatures")
        features.setWordWrap(True)
        lay.addWidget(features)
        credits = QLabel("Powered by Qt / PySide6 · PyAV · FFmpeg")
        credits.setObjectName("muted")
        credits.setAlignment(Qt.AlignCenter)
        lay.addWidget(credits)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.Close).setDefault(True)
        lay.addWidget(bb)


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _scope(seg_plans):
    vf = sum(len(sp.head_pts) + len(sp.tail_pts) for sp in seg_plans)
    vs = sum(sp.reencode_s for sp in seg_plans)
    txt = (f"<p><b>Encoding scope (estimate):</b> video re-encoded: <b>{vf} frames ({vs:.3f} s)</b>; "
           "audio re-encoded: <b>0 samples</b>. ")
    if vf:
        txt += ("Re-encoded frames are only those between the exact cut and a verified keyframe, using the source video codec; "
                "their quality is checked against the source. Other video and all audio are copied; container framing and decoder headers may be rewritten without changing the copied picture/audio payload. ")
    else:
        txt += "Compressed video and audio are copied unchanged. "
    return txt + "The container and timestamps are rewritten; any common start-time shift preserves audio alignment.</p>"


class PlanDialog(QDialog):
    """Pre-export summary: parts, tracks, cut positions, removed content, encoding scope."""

    def __init__(self, parent, session, plan, seg_plans, notes, mode_label, out_dir):
        super().__init__(parent)
        self.setWindowTitle("Review export plan")
        self.resize(820, 640)
        lay = QVBoxLayout(self)
        tb = session.vtb
        h = [f"<h3>{_esc(os.path.basename(session.path))}</h3>",
             f"<p><b>Mode:</b> {_esc(mode_label)}<br><b>Output folder:</b> {_esc(out_dir)}</p>",
             _scope(seg_plans)]
        if plan.dropped_frames:
            h.append(f"<p><b>Frames not written:</b> {len(plan.dropped_frames)} undecodable open-GOP frame(s), all inside "
                     "black intervals: " + ", ".join(fmt_time(float(p * tb)) for p in plan.dropped_frames[:12]) +
                     ("…" if len(plan.dropped_frames) > 12 else "") + "</p>")
        if plan.removed:
            h.append("<p><b>Removed black (video and all audio):</b> " + "; ".join(
                f"{fmt_time(float(a * tb))} – {fmt_time(float(b * tb))} ({float((b - a) * tb):.3f}s)" for a, b in plan.removed) + "</p>")
        if notes:
            h.append("<p><b>Preservation notes:</b></p><ul>" + "".join(f"<li>{_esc(n)}</li>" for n in notes) + "</ul>")
        h.append("<table cellspacing=0 cellpadding=4 border=1 style='border-collapse:collapse'>"
                 "<tr><th>#</th><th>From</th><th>To</th><th>Length</th><th>File</th><th>Tracks</th></tr>")
        for sp in seg_plans:
            s = sp.segment
            tr = "<br>".join(f"{_esc(t.type)} {_esc(t.codec)} #{t.stream_index}: {_esc(t.treatment)}"
                             + (f" — <i>{_esc('; '.join(t.notes))}</i>" if t.notes else "") for t in sp.tracks)
            if sp.chapters:
                tr += f"<br>chapters: {len(sp.chapters)} (clipped and re-based)"
            if sp.head or sp.tail:
                tr += (f"<br><b>re-encoded:</b> {len(sp.head_pts)} frame(s) at the start, {len(sp.tail_pts)} at the end "
                       f"({sp.reencode_s:.3f} s)")
            h.append(f"<tr><td>{s.n}</td><td>{fmt_time(s.start_s)}</td><td>{fmt_time(s.end_s)}</td>"
                     f"<td>{s.duration_s:.3f}s</td><td>{_esc(sp.filename)}</td><td>{tr}</td></tr>")
        h.append("</table>")
        cuts = []
        for k, rc in sorted(session.resolved.items()):
            if rc.status in ("copy", "copy-drop", "reencode"):
                bs = ", ".join(fmt_time(b.seconds) for b in rc.boundaries) or "—"
                cuts.append(f"<li>Cut {k}: <b>{_esc(rc.label)}</b> at {bs}"
                            + (f" (Δ {rc.delta_s:+.3f}s from requested)" if abs(rc.delta_s) > 1e-9 else "")
                            + "".join(f"<br><small>{_esc(m)}</small>" for m in rc.messages) + "</li>")
        if cuts:
            h.append("<p><b>Cuts:</b></p><ul>" + "".join(cuts) + "</ul>")
        tbw = QTextBrowser(); tbw.setHtml("".join(h))
        lay.addWidget(tbw)
        bb = QDialogButtonBox()
        self.ok = bb.addButton("Export", QDialogButtonBox.AcceptRole)
        bb.addButton(QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class PlaybackDialog(QDialog):
    def __init__(self, parent, path, at_s=None):
        super().__init__(parent)
        from .player import VideoPlayer
        from bsr.media import probe
        self.setWindowTitle(os.path.basename(path))
        self.resize(850, 620)
        lay = QVBoxLayout(self)
        self.player = VideoPlayer(self, expandable=False); self.player.video.setMaximumHeight(500)
        self.player.set_source(probe(path)); lay.addWidget(self.player, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject); lay.addWidget(buttons)
        self.finished.connect(lambda _: self.player.shutdown())
        if at_s is None:
            self.player.toggle()
        else:
            started = [False]
            def start_at(duration):
                if duration > 0 and not started[0]:
                    started[0] = True
                    self.player.seek_source(at_s); self.player.toggle()
            self.player.media.durationChanged.connect(start_at)


class ResultsDialog(QDialog):
    def __init__(self, parent, report):
        super().__init__(parent)
        self.setWindowTitle("Export results")
        self.resize(860, 640)
        lay = QVBoxLayout(self)
        ok = report.get("ok")
        h = [f"<h3>{'✅ All parts exported and validated' if ok else '⚠️ Some parts did not pass'}</h3>"]
        for o in report["outputs"]:
            h.append(f"<p><b>Part {o['n']}</b> — {'<span style=color:#3fa66b>OK</span>' if o['ok'] else '<span style=color:#d04040>FAILED</span>'}"
                     f"<br><small>{_esc(o['path'])}</small></p><ul>")
            rr = o.get("plan", {}).get("reencoded") or {}
            if rr.get("video_frames"):
                h.append(f"<li><b>Measured re-encoding:</b> video {rr['video_frames']} frame(s) / {rr['video_s']:.3f} s "
                         f"(start {rr.get('head_frames', 0)}, end {rr.get('tail_frames', 0)}), audio 0 samples, "
                         f"{rr.get('encoder', '')}" + (f" CRF {rr['crf']}" if rr.get('crf') is not None else "") + "</li>")
            else:
                h.append("<li><b>Measured re-encoding:</b> none — every packet copied</li>")
            for name, c in o.get("validation", {}).get("checks", {}).items():
                h.append(f"<li>{'✓' if c['ok'] else '✗'} {_esc(name)}: {_esc(c['detail'])}</li>")
            if o.get("error"):
                h.append(f"<li style='color:#d04040'>{_esc(o['error'])}</li>")
            h.append("</ul>")
        h.append(f"<p>Report: {_esc(report.get('report_path', ''))}</p>")
        tbw = QTextBrowser(); tbw.setHtml("".join(h)); tbw.setOpenExternalLinks(True)
        lay.addWidget(tbw)
        row = QHBoxLayout()
        self.play_parts = QComboBox()
        for output in report["outputs"]:
            path = output.get("path", "")
            if path and os.path.isfile(path): self.play_parts.addItem(os.path.basename(path), path)
        row.addWidget(self.play_parts, 1)
        self.play_button = QPushButton("Play part")
        self.play_button.setEnabled(self.play_parts.count() > 0)
        self.play_button.clicked.connect(lambda: PlaybackDialog(self, self.play_parts.currentData()).exec())
        row.addWidget(self.play_button)
        folder = os.path.dirname(report.get("report_path", ""))
        if not folder and self.play_parts.count(): folder = os.path.dirname(self.play_parts.itemData(0))
        self.folder_button = QPushButton("Open output folder")
        self.folder_button.setEnabled(bool(folder))
        self.folder_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
        row.addWidget(self.folder_button); lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close); bb.rejected.connect(self.reject); bb.accepted.connect(self.accept)
        lay.addWidget(bb)


class LogDialog(QDialog):
    def __init__(self, parent, lines, path):
        super().__init__(parent)
        self.setWindowTitle("Diagnostic log")
        self.resize(900, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"Also written to: {path}"))
        t = QPlainTextEdit(); t.setReadOnly(True); t.setPlainText("\n".join(lines))
        t.moveCursor(t.textCursor().MoveOperation.End)
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Close); bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class TracksDialog(QDialog):
    def __init__(self, parent, info, selected):
        super().__init__(parent)
        self.setWindowTitle("Tracks to include")
        lay = QVBoxLayout(self)
        self.boxes = []
        for s in info.streams:
            label = s.label()
            if s.type == "video":
                label += f" — {s.width}×{s.height} {s.pix_fmt or ''} {s.field_order or ''}"
            elif s.type == "audio":
                label += f" — {s.sample_rate} Hz {s.channels} ch"
            cb = QCheckBox(label)
            cb.setChecked(s.index in selected)
            if s.type == "video" and s.index == info.video.index:
                cb.setEnabled(False); cb.setChecked(True)
            if s.type == "subtitle" and not s.is_text_sub:
                cb.setToolTip("Bitmap subtitles are not supported for cutting yet")
            self.boxes.append((s.index, cb))
            lay.addWidget(cb)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def selected(self):
        return [i for i, cb in self.boxes if cb.isChecked()]
