"""The icon's charcoal tile, banana, three scoops and mint cut line."""
import os

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import QWidget

CHARCOAL = "#20222c"
FILM = "#101116"
YELLOW = "#fad640"
PINK = "#f6a0be"
CREAM = "#faf4e4"
MINT = "#78dca0"


def icon_path():
    return os.environ.get("BSR_ICON") or os.path.join(os.path.dirname(__file__), "assets", "icon.png")


def ui_font() -> QFont:
    """The platform's interface font at the size the layout was designed for.

    macOS measures points as logical pixels; Windows uses 96 dpi (1 pt = 1.33 px),
    so 9 pt Segoe UI (12 px) matches the 11 px text of the macOS layout, adjusted
    for Segoe's smaller letters.
    """
    if os.sys.platform == "darwin":
        return QFont(".AppleSystemUIFont", 11)
    if os.sys.platform == "win32":
        f = QFont("Segoe UI")
        f.setPointSizeF(9)
        return f
    return QFont("Sans Serif", 11)


class _WindowChrome(QObject):
    """Give every top-level window (dialogs too) the charcoal Windows title bar."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow() \
                and not obj.property("_bsr_chrome"):
            from PySide6.QtWidgets import QMainWindow
            from . import winchrome
            obj.setProperty("_bsr_chrome", True)
            # The main window's caption runs into the film-black brand bar; dialogs sit on charcoal.
            winchrome.integrate(obj, FILM if isinstance(obj, QMainWindow) else CHARCOAL, CREAM)
        return False


def apply_theme(app):
    app.setStyle("Fusion")
    app.setFont(ui_font())
    if os.sys.platform == "win32" and not getattr(app, "_bsr_chrome_filter", None):
        app._bsr_chrome_filter = _WindowChrome(app)
        app.installEventFilter(app._bsr_chrome_filter)
    pal = QPalette()
    for role, color in (
        (QPalette.Window, CHARCOAL), (QPalette.WindowText, CREAM),
        (QPalette.Base, FILM), (QPalette.AlternateBase, "#252731"),
        (QPalette.Text, CREAM), (QPalette.Button, "#30333f"),
        (QPalette.ButtonText, CREAM), (QPalette.Highlight, "#4c4530"),
        (QPalette.HighlightedText, CREAM), (QPalette.ToolTipBase, "#30333f"),
        (QPalette.ToolTipText, CREAM), (QPalette.Mid, "#a8a8b4"),
        (QPalette.Light, "#454854"), (QPalette.Dark, FILM),
        (QPalette.Link, MINT), (QPalette.BrightText, YELLOW),
        (QPalette.PlaceholderText, "#999ba9")):
        pal.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#777986"))
    app.setPalette(pal)
    app.setWindowIcon(QIcon(icon_path()))
    app.setStyleSheet(STYLESHEET)


STYLESHEET = """
QMainWindow, QDialog { background: #20222c; }
QWidget { color: #faf4e4; }
QFrame#brand { background: #101116; border-bottom: 2px solid #fad640; }
QLabel#brandTitle { color: #fad640; font-size: 23px; font-weight: 700; }
QLabel#brandSubtitle, QLabel#muted { color: #afb0bd; }
QLabel#aboutVersion { color: #f6a0be; font-size: 14px; }
QLabel#aboutFeatures { background: #101116; border-radius: 9px; padding: 14px; }
QLabel#workflow { color: #f6a0be; font-size: 12px; }
QWidget#queuePanel { background: #191b23; border-radius: 12px; }
QLabel#sectionTitle { color: #fad640; font-size: 14px; font-weight: 600; }
QLabel#fileTitle { font-size: 14px; }
QFrame#midpointCard { background: #1b2d27; border: 1px solid #3c604c; border-radius: 9px; }
QLabel#midpointTitle { color: #78dca0; font-weight: 600; }
QLabel#scope { color: #78dca0; padding: 4px 2px; }
QLabel#preview { background: #101116; border: 1px solid #454854; border-radius: 10px; }
QLabel#thumb { background: #101116; border: 1px solid #454854; border-radius: 5px; }
QPushButton, QToolButton {
  background: #30333f; border: 1px solid #484b59; border-radius: 7px;
  padding: 6px 10px; min-height: 17px;
}
QPushButton:hover, QToolButton:hover { background: #3b3e4c; border-color: #fad640; }
QPushButton:pressed, QToolButton:pressed, QToolButton:checked { background: #494234; }
QPushButton:focus, QToolButton:focus { border: 1px solid #fad640; }
QPushButton:disabled, QToolButton:disabled { background: #272933; border-color: #363946; color: #777986; }
QPushButton#primary { background: #fad640; color: #20222c; border-color: #fad640; font-weight: 600; }
QPushButton#primary:hover { background: #ffe574; }
QPushButton#primary:disabled { background: #585038; border-color: #585038; color: #a09a86; }
QPushButton#accept { background: #294236; color: #78dca0; border-color: #49745b; }
QPushButton#reject { background: #44303c; color: #f6a0be; border-color: #745060; }
QPushButton#accept:disabled, QPushButton#reject:disabled { background: #272933; border-color: #363946; color: #777986; }
QGroupBox { border: 1px solid #414450; border-radius: 10px; margin-top: 15px; padding: 12px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; color: #fad640; padding: 0 5px; font-weight: 600; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
  background: #171920; border: 1px solid #484b59; border-radius: 6px; padding: 5px 7px; min-height: 18px;
  selection-background-color: #4c4530;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #fad640; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #252731; selection-background-color: #4c4530; color: #faf4e4; }
QTableWidget, QListWidget, QTextBrowser, QPlainTextEdit {
  background: #171920; alternate-background-color: #20222c; border: 1px solid #3b3e4b;
  border-radius: 8px; gridline-color: #333642; selection-background-color: #4c4530;
}
QListWidget { padding: 5px; }
QListWidget::item { padding: 10px 6px; border-radius: 6px; }
QListWidget::item:selected { background: #4c4530; border-left: 3px solid #fad640; }
QTableWidget::item { padding: 5px; }
QHeaderView::section { background: #30333f; color: #faf4e4; border: none; border-right: 1px solid #414450; padding: 7px; }
QTableCornerButton::section { background: #30333f; border: none; }
QCheckBox, QRadioButton { spacing: 7px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #717483; border-radius: 3px; background: #171920; }
QCheckBox::indicator:checked { background: #78dca0; border: 2px solid #294236; }
QRadioButton::indicator { width: 14px; height: 14px; border: 1px solid #717483; border-radius: 8px; background: #171920; }
QRadioButton::indicator:checked { background: #fad640; border: 3px solid #4c4530; }
QProgressBar { border: none; border-radius: 4px; background: #101116; }
QProgressBar::chunk { background: #78dca0; border-radius: 4px; }
QSplitter::handle { background: #20222c; }
QSplitter::handle:hover { background: #484b59; }
QScrollBar:vertical { background: #171920; width: 10px; margin: 0; }
QScrollBar:horizontal { background: #171920; height: 10px; margin: 0; }
QScrollBar::handle { background: #505360; border-radius: 4px; min-width: 20px; min-height: 20px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QStatusBar { background: #191b23; color: #afb0bd; padding: 3px; }
QMenuBar, QMenu { background: #20222c; color: #faf4e4; }
QMenu::item:selected { background: #4c4530; }
QToolTip { background: #30333f; color: #faf4e4; border: 1px solid #646777; padding: 5px; }
"""
