"""Exercise per-file modes and accepting cuts after analysis in the desktop UI."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from bsr_ui.app import MainWindow
from bsr_ui.theme import apply_theme


def test_desktop_mode_and_later_acceptance(fixtures, tmp_path, monkeypatch):
    monkeypatch.setenv("BSR_LOG_DIR", str(tmp_path / "logs"))
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    w = MainWindow()

    def settle():
        start = time.monotonic()
        while w._jobs:
            app.processEvents(); time.sleep(0.01)
            assert time.monotonic() - start < 60
        app.processEvents()

    try:
        w.add_paths([fixtures["h264_mp4_longgop"], fixtures["mpeg2_open"]]); settle()
        assert w.mode.currentData() == "smart"
        assert w.mode.count() == 2
        w.queue.setCurrentRow(1); app.processEvents()
        assert w.mode.currentData() == "smart" and w.mode.model().item(1).isEnabled()
        assert not w.mode_reason.text()
        w.queue.setCurrentRow(0); app.processEvents()
        assert w.mode.currentData() == "smart"
        w.analyse_current(); settle()
        s = w.cur.session
        c = next(c for c in s.candidates if abs(c.start_s - 4) < .05)
        w.select_candidate(c.id); settle()
        w.set_state("accepted"); settle()
        assert s.decisions[c.id].choice is None
        assert s.resolved[c.id].status == "reencode"
        assert "Re-encoding:" in w.scope_lbl.text()
        w.mode.setCurrentIndex(0); settle()
        assert s.resolved[c.id].status == "needs-review"
        w.queue.setCurrentRow(1); w.queue.setCurrentRow(0); app.processEvents()
        assert w.mode.currentData() == "copy"
        w.mode.setCurrentIndex(1); settle()
        assert s.resolved[c.id].status == "reencode"
    finally:
        w.close()
