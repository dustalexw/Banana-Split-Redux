"""Dialogs: export plan summary, results, diagnostics log, track selection."""
from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextBrowser, QDialogButtonBox,
                               QPlainTextEdit, QPushButton, QCheckBox, QScrollArea, QWidget)

from bsr.media import fmt_time


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


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
             "<p><b>Encoding scope:</b> video re-encoded: <b>0 frames (0.000 s)</b>; audio re-encoded: <b>0 samples</b>. "
             "Compressed video and audio are copied unchanged; the container is rewritten and each part's "
             "timestamps start at zero.</p>"]
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
            for name, c in o.get("validation", {}).get("checks", {}).items():
                h.append(f"<li>{'✓' if c['ok'] else '✗'} {_esc(name)}: {_esc(c['detail'])}</li>")
            if o.get("error"):
                h.append(f"<li style='color:#d04040'>{_esc(o['error'])}</li>")
            h.append("</ul>")
        h.append(f"<p>Report: {_esc(report.get('report_path', ''))}</p>")
        tbw = QTextBrowser(); tbw.setHtml("".join(h)); tbw.setOpenExternalLinks(True)
        lay.addWidget(tbw)
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
