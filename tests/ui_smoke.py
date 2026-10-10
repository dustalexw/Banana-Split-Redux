"""Drive the real desktop window headlessly: import -> analyse -> review -> export.

    QT_QPA_PLATFORM=offscreen python tests/ui_smoke.py FILE OUT_DIR [SCREENSHOT_DIR] [--accept-all-copy]

Dialogs are auto-confirmed; everything else goes through the same code paths a
user's clicks use. Screenshots are saved for visual review.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from bsr_ui import app as A


def wait(appl, cond, timeout=600):
    t0 = time.time()
    while not cond():
        appl.processEvents()
        time.sleep(0.02)
        if time.time() - t0 > timeout:
            raise TimeoutError("timed out")
    appl.processEvents()


def main():
    src, out = sys.argv[1], sys.argv[2]
    shots = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else None
    appl = QApplication([])
    results = {}
    A.PlanDialog.exec = lambda self: True
    A.ResultsDialog.exec = lambda self: results.setdefault("shown", True)
    A.QMessageBox.warning = staticmethod(lambda *a, **k: print("WARNING:", a[2] if len(a) > 2 else a))
    w = A.MainWindow()
    w.resize(1500, 1000)
    w.show()
    w.out_edit.setText(out)
    w.add_paths([src])
    wait(appl, lambda: w.items and w.items[0].session is not None)
    w.analyse_current()
    wait(appl, lambda: w.items[0].state == "ready")
    s = w.items[0].session
    print("candidates:", len(s.candidates))
    for k, rc in sorted(s.resolved.items()):
        print(f"  cut {k}: {rc.status} {[round(b.seconds,3) for b in rc.boundaries]}")
    # select the best copy-able candidate and accept it through the UI handlers
    ok = [k for k, rc in s.resolved.items() if rc.status == "copy"
          and not next(c for c in s.candidates if c.id == k).leading]
    if "--accept-all-copy" not in sys.argv:
        ok = ok[:1]
    for k in ok:
        w.select_candidate(k)
        appl.processEvents()
        w.set_state("accepted")
        wait(appl, lambda: not w._jobs)
    if "--accept-drops" in sys.argv:
        drops = [k for k, rc in s.resolved.items() if any(ch["kind"] == "accept-drop" for ch in rc.choices)
                 and not next(c for c in s.candidates if c.id == k).leading]
        if "--accept-all-copy" not in sys.argv:
            drops = drops[:2]
        for k in drops:
            w.select_candidate(k)
            wait(appl, lambda: not w._jobs)
            appl.processEvents()
            btn = [b for b in w.choice_group.buttons() if str(b.property("choice")).startswith("accept-drop")][0]
            btn.setChecked(True)
            w.apply_choice()
            wait(appl, lambda: not w._jobs)
            ok.append(k)
    # also show a needs-review candidate in the detail panel
    nr = [k for k, rc in s.resolved.items() if rc.status == "needs-review"]
    target = nr[0] if nr else (ok[0] if ok else None)
    if target is not None:
        w.select_candidate(target)
    wait(appl, lambda: not w._jobs)
    for _ in range(30):
        appl.processEvents(); time.sleep(0.05)
    if shots:
        os.makedirs(shots, exist_ok=True)
        w.grab().save(os.path.join(shots, "review.png"))
        w.btn_settings.setChecked(True); appl.processEvents()
        w.grab().save(os.path.join(shots, "review_settings.png"))
        w.btn_settings.setChecked(False)
    if ok:
        w.select_candidate(ok[0])
        wait(appl, lambda: not w._jobs)
    w.review_export()
    wait(appl, lambda: w.items[0].state in ("done", "ready") and not w._jobs, timeout=1800)
    rep = w.items[0].last_report
    print("export ok:", rep and rep["ok"], "parts:", rep and len(rep["outputs"]))
    for o in (rep or {}).get("outputs", []):
        print("  ", o["ok"], os.path.basename(o["path"]), o["error"])
    if shots:
        w.grab().save(os.path.join(shots, "after_export.png"))
    w.close()
    sys.exit(0 if rep and rep["ok"] else 1)


if __name__ == "__main__":
    main()
