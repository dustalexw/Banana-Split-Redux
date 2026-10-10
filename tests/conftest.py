import os
import sys

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fixturegen  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _cache_dir(tmp_path_factory):
    os.environ["BSR_CACHE_DIR"] = str(tmp_path_factory.mktemp("cache"))


@pytest.fixture(autouse=True)
def _close_desktop_windows():
    """Dispose closed Qt windows while QApplication and its event loop are alive."""
    yield
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None:
        for window in app.topLevelWidgets():
            window.close()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


@pytest.fixture(scope="session")
def fixtures():
    out = os.environ.get("BSR_FIXTURE_DIR", os.path.join(HERE, "fixtures_out"))
    return fixturegen.build_all(out)
