"""Background jobs. Long operations never run on the interface thread.

Media access is serialised per source (one job at a time touches a session), and
the pool is bounded so analysis of a batch does not oversubscribe the machine.
"""
from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class JobSignals(QObject):
    progress = Signal(str, float)      # stage, 0..1
    done = Signal(object)
    failed = Signal(str)
    log = Signal(str)


class Job(QRunnable):
    def __init__(self, fn, *args, **kw):
        super().__init__()
        self.fn, self.args, self.kw = fn, args, kw
        self.signals = JobSignals()
        self.cancel = threading.Event()
        self.setAutoDelete(True)

    @Slot()
    def run(self):
        try:
            res = self.fn(self, *self.args, **self.kw)
        except Exception as e:  # report, never crash the UI
            tb = traceback.format_exc()
            self.signals.log.emit(tb)
            self.signals.failed.emit(f"{type(e).__name__}: {e}")
            return
        self.signals.done.emit(res)


class Pools:
    def __init__(self):
        self.media = QThreadPool()
        self.media.setMaxThreadCount(1)     # analysis / proofs / export: one at a time
        self.preview = QThreadPool()
        self.preview.setMaxThreadCount(2)   # thumbnails

    def wait(self):
        self.media.waitForDone(5000)
        self.preview.waitForDone(5000)
