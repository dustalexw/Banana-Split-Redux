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
            for i in range(20):
                frame = av.VideoFrame.from_ndarray(np.full((64, 64, 3), 0 if i == 10 else 150, np.uint8), format="rgb24")
                frame.pts, frame.time_base = i, Fraction(1, 10)
                for p in st.encode(frame):
                    out.mux(p)
            for p in st.encode():
                out.mux(p)
        from bsr.session import SourceSession
        s = SourceSession(path)
        s.analyze(); s.find(audio_signal=False)
        lines.append(f"decoded {len(s.stats)} frames, {len(s.candidates)} black transition(s)")
        app = QApplication.instance() or QApplication([APP_NAME])
        apply_theme(app)
        w = MainWindow()
        lines.append(f"window ok, font {ui_font().family()}, multimedia {QMediaPlayer is not None}")
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
