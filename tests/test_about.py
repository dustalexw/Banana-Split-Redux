"""About action and startup identity regression checks."""
import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel

from bsr import ENGINE_VERSION
from bsr_ui.app import MainWindow
from bsr_ui.dialogs import AboutDialog
from bsr_ui.theme import apply_theme


def test_about_menu_opens_and_closes(tmp_path, monkeypatch):
    monkeypatch.setenv("BSR_LOG_DIR", str(tmp_path / "logs"))
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    window = MainWindow()
    assert window.about_action.menuRole() == QAction.AboutRole
    seen = []

    def close_about():
        dialog = app.activeModalWidget()
        if isinstance(dialog, AboutDialog):
            labels = "\n".join(label.text() for label in dialog.findChildren(QLabel))
            seen.append((labels, dialog.windowTitle()))
            buttons = dialog.findChild(QDialogButtonBox)
            buttons.button(QDialogButtonBox.Close).click()
        else:
            app.closeAllWindows()

    try:
        for _ in range(2):
            QTimer.singleShot(0, close_about)
            window.about_action.trigger()
        assert len(seen) == 2
        assert all(f"Version {ENGINE_VERSION}" in text and "Cut exactly" in text
                   and title == "About Banana Split Redux" for text, title in seen)
        assert app.activeModalWidget() is None
    finally:
        window.close()


def test_startup_identity_before_native_menu_and_file_args(tmp_path):
    video = tmp_path / "episode with spaces.mp4"
    video.touch()
    # A fresh process is required: Cocoa caches the menu during the first QApplication.
    script = r'''
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from bsr import ENGINE_VERSION
from bsr_ui import app as ui
class EarlyApplication(QApplication):
    def __init__(self, args):
        assert self.applicationName() == ui.APP_NAME
        assert self.applicationDisplayName() == ui.APP_NAME
        assert self.applicationVersion() == ENGINE_VERSION
        assert args[0] == ui.APP_NAME
        super().__init__(args)
ui.QApplication = EarlyApplication
seen = []
held = []
class Window:
    def show(self):
        held.append(QApplication.instance())
        QTimer.singleShot(0, QApplication.instance().quit)
    def add_paths(self, paths):
        seen.extend(paths)
ui.MainWindow = Window
assert ui.main(["/old/path/main.py", sys.argv[1]]) == 0
assert seen == [sys.argv[1]]
assert held[0].arguments() == [ui.APP_NAME, sys.argv[1]]
assert held[0].applicationDisplayName() == ui.APP_NAME
print("PASS: early identity, branded argv, file arguments")
'''
    result = subprocess.run([sys.executable, "-c", script, str(video)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS:" in result.stdout
