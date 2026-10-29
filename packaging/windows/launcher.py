"""Windows entry point for the packaged app.

A windowed app has no console: errors go to %LOCALAPPDATA%\\BananaSplitRedux\\Logs\\launch.log
and a message box explains where to find them.

``--self-test REPORT`` checks the packaged runtime without showing a window (used by CI):
Qt widgets and multimedia, PyAV encode/decode, and building the main window.
"""
import os
import sys
import traceback


def _log_file():
    from bsr.osutil import log_root
    d = log_root()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "launch.log")


def self_test(report_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines = []
    try:
        import av
        import numpy as np
        from fractions import Fraction
        from PySide6.QtWidgets import QApplication
        from PySide6.QtMultimedia import QMediaPlayer
        import bsr
        from bsr_ui.app import MainWindow, APP_NAME
        from bsr_ui.theme import apply_theme, ui_font
        lines.append(f"engine {bsr.ENGINE_VERSION}, PyAV {av.__version__}, FFmpeg {av.ffmpeg_version_info}")
        path = os.path.join(os.path.dirname(os.path.abspath(report_path)), "bsr-self-test.mp4")
        with av.open(path, "w") as out:
            st = out.add_stream("libx264", rate=10)
            st.width = st.height = 64
            st.pix_fmt = "yuv420p"
            for i in range(80):   # 8 s with one black frame at 4 s
                frame = av.VideoFrame.from_ndarray(np.full((64, 64, 3), 0 if i == 40 else 150, np.uint8), format="rgb24")
                frame.pts, frame.time_base = i, Fraction(1, 10)
                for p in st.encode(frame):
                    out.mux(p)
            for p in st.encode():
                out.mux(p)
        from bsr.session import SourceSession
        from bsr.export import ExportOptions, export_plan
        s = SourceSession(path)
        s.analyze(); s.find(audio_signal=False)
        lines.append(f"decoded {len(s.stats)} frames, {len(s.candidates)} black transition(s)")
        for c in s.candidates:
            s.decisions[c.id].state = "accepted"
        s.policy.boundary_reencode = s.reencode_supported()
        plan = s.plan()
        out_dir = os.path.join(os.path.dirname(os.path.abspath(report_path)), "bsr-self-test-out")
        rep = export_plan(s.info, s.idx, plan, ExportOptions(out_dir=out_dir))
        checks = all(o["validation"].get("ok") for o in rep["outputs"])
        lines.append(f"exported {len(rep['outputs'])} part(s) to {out_dir}: {'validated' if rep['ok'] and checks else 'FAILED'}")
        if not (rep["ok"] and checks):
            raise RuntimeError(f"export failed: {[o.get('error') for o in rep['outputs']]}")
        app = QApplication.instance() or QApplication([APP_NAME])
        apply_theme(app)
        w = MainWindow()
        from bsr_ui.app import Item
        it = Item(path); it.session = s; it.state = "ready"
        w.items = [it]; w.cur = it; w.refresh_queue(); w.show_item()
        w.resize(1440, 940); w.show()
        for _ in range(50):
            app.processEvents()
        shot = os.path.join(os.path.dirname(os.path.abspath(report_path)), "bsr-self-test-window.png")
        w.grab().save(shot)
        lines.append(f"window ok, font {ui_font().family()} {ui_font().pointSizeF():g} pt, multimedia {QMediaPlayer is not None}, screenshot {shot}")
        w.close()
        lines.append("OK")
        code = 0
    except BaseException:
        lines.append(traceback.format_exc())
        code = 1
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return code


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        return self_test(sys.argv[2])
    log = None
    if sys.stderr is None or sys.stdout is None:
        try:
            log = open(_log_file(), "a", encoding="utf-8", buffering=1)
            sys.stderr = sys.stderr or log
            sys.stdout = sys.stdout or log
        except OSError:
            pass
    try:
        from bsr_ui.app import main as run
        return run()
    except Exception:
        text = traceback.format_exc()
        try:
            with open(_log_file(), "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f"Banana Split Redux could not start.\n\nDetails: {_log_file()}",
                                             "Banana Split Redux", 0x10)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
