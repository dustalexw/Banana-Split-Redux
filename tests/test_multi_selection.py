"""Drive real Qt selection gestures and batch decisions on generated video."""
import time

import pytest
from PySide6.QtCore import Qt, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bsr_ui.app import MainWindow, Item
from bsr_ui.theme import apply_theme
from test_engine import session


@pytest.fixture
def review_window(fixtures, tmp_path, monkeypatch):
    monkeypatch.setenv("BSR_LOG_DIR", str(tmp_path / "logs"))
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    s = session(fixtures["h264_mp4_longgop"])
    s.resolve()
    w = MainWindow()
    it = Item(s.path); it.session = s; it.state = "ready"
    w.items = [it]; w.cur = it; w.show_item(); w.show(); app.processEvents()

    def settle():
        began = time.monotonic()
        while w._jobs:
            app.processEvents(); time.sleep(.005)
            assert time.monotonic() - began < 60
        app.processEvents()

    yield w, s, app, settle
    settle(); w.close()


def click_row(w, app, row, modifiers=Qt.NoModifier):
    item = w.table.item(row, 0)
    w.table.scrollToItem(item); app.processEvents()
    QTest.mouseClick(w.table.viewport(), Qt.LeftButton, modifiers, w.table.visualItemRect(item).center())
    app.processEvents()


def row_ids(w):
    return [w.table.item(r, 0).data(Qt.UserRole) for r in range(w.table.rowCount())]


def test_shift_keep_skip_undo_and_busy_guard(review_window, tmp_path):
    w, s, app, settle = review_window
    ids = row_ids(w)
    assert len(ids) >= 4
    click_row(w, app, 1)
    click_row(w, app, 3, Qt.ShiftModifier)
    selected = ids[1:4]
    assert w.selected_split_ids() == selected
    w.show_suppressed.setChecked(True); w.show_suppressed.setChecked(False)
    assert w.selected_split_ids() == selected
    assert w.sel_id == selected[-1]  # active preview follows the clicked endpoint
    assert "3 selected" in w.selection_hint.text()
    w.refresh_table()
    assert w.selected_split_ids() == selected
    # Extend an existing range after a refresh without losing the Shift anchor.
    click_row(w, app, 2, Qt.ShiftModifier)
    assert w.selected_split_ids() == ids[1:3]
    click_row(w, app, 3, Qt.ShiftModifier)
    assert w.selected_split_ids() == selected
    before = {k: d.state for k, d in s.decisions.items()}
    for state in ("analysing", "proving", "exporting"):
        w.cur.state = state; w.update_review_progress()
        assert not w.btn_keep_selected.isEnabled() and not w.btn_skip_selected.isEnabled()
        w.set_selected_state("accepted")
        assert {k: d.state for k, d in s.decisions.items()} == before
    w.cur.state = "ready"; w.update_review_progress()
    w.btn_keep_selected.click()
    assert not w.btn_keep_selected.isEnabled()
    settle()
    assert len(w.cur.history) == 1
    assert w.selected_split_ids() == selected
    assert all(s.decisions[k].state == ("accepted" if k in selected else before[k]) for k in ids)
    w.btn_keep_selected.click(); settle()
    assert len(w.cur.history) == 1  # an unchanged batch does not consume Undo
    w.btn_skip_selected.click(); settle()
    assert len(w.cur.history) == 2 and w.selected_split_ids() == selected
    assert all(s.decisions[k].state == ("rejected" if k in selected else before[k]) for k in ids)
    w.btn_undo.click(); settle()
    assert all(s.decisions[k].state == "accepted" for k in selected)
    assert w.selected_split_ids() == selected
    w.btn_undo.click(); settle()
    assert {k: d.state for k, d in s.decisions.items()} == before
    assert w.selected_split_ids() == selected
    w.grab().save(str(tmp_path / "batch-review.png"))
    # Timeline/next navigation remains a single-split action.
    w.select_candidate(ids[0])
    assert w.selected_split_ids() == [ids[0]]
    w.table.clearSelection(); app.processEvents()
    assert not w.btn_keep_selected.isEnabled() and not w.btn_skip_selected.isEnabled()
    w.refresh_table()
    assert w.selected_split_ids() == [] and not w.btn_keep_selected.isEnabled()
    assert w.sel_id == ids[0]  # empty selection leaves the preview usable


def test_manual_rows_hidden_suggestions_and_file_switch(review_window):
    w, s, app, settle = review_window
    s.add_manual(6.0); w.refresh_table()
    ids = row_ids(w)
    before_manual = {k: s.decisions[k].state for k in ids[-2:]}
    click_row(w, app, len(ids)-2)
    click_row(w, app, len(ids)-1, Qt.ShiftModifier)
    assert w.selected_split_ids() == ids[-2:]
    w.btn_skip_selected.click(); settle()
    assert all(s.decisions[k].state == "rejected" for k in ids[-2:])
    w.btn_undo.click(); settle()
    assert {k: s.decisions[k].state for k in ids[-2:]} == before_manual
    # A kept suppressed suggestion is visible; skipping it hides only that row.
    s.candidates[1].suppressed = True
    s.decisions[ids[1]].state = "accepted"
    w.refresh_table()
    click_row(w, app, 0); click_row(w, app, 2, Qt.ShiftModifier)
    w.btn_skip_selected.click(); settle()
    assert ids[1] not in row_ids(w)
    assert w.selected_split_ids() == [ids[0], ids[2]]
    w.btn_undo.click(); settle()
    assert w.selected_split_ids() == ids[:3] and s.decisions[ids[1]].state == "accepted"
    click_row(w, app, 0); click_row(w, app, 1, Qt.ShiftModifier)
    w.btn_skip_selected.click(); settle()
    assert w.selected_split_ids() == [ids[0]]
    w.refresh_table()
    assert w.selected_split_ids() == [ids[0]]  # hidden active row cannot clear the other selections
    w.btn_undo.click(); settle()
    assert w.selected_split_ids() == ids[:2]
    # Another file can have the same decision IDs without inheriting this range.
    other = session(s.path); other.resolve()
    it = Item(other.path); it.session = other; it.state = "ready"
    w.items.append(it); w.refresh_queue(); w.queue.setCurrentRow(1); app.processEvents()
    assert w.cur is it and len(w.selected_split_ids()) == 1
    before = {k: d.state for k, d in s.decisions.items()}
    w.btn_skip_selected.click(); settle()
    assert {k: d.state for k, d in s.decisions.items()} == before


def test_finish_job_after_batch_keep(review_window, tmp_path, monkeypatch):
    from pathlib import Path
    from PySide6.QtWidgets import QDialog
    from bsr_ui.dialogs import PlanDialog, ResultsDialog
    w, s, app, settle = review_window
    s.policy.boundary_reencode = True
    click_row(w, app, 0); click_row(w, app, 1, Qt.ShiftModifier)
    selected = row_ids(w)[:2]
    w.btn_keep_selected.click(); settle()
    assert w.selected_split_ids() == selected
    plan = s.plan(resolve=False)
    assert not plan.issues and len(plan.segments) == 3
    monkeypatch.setattr(PlanDialog, "exec", lambda dialog: QDialog.Accepted)
    monkeypatch.setattr(ResultsDialog, "exec", lambda dialog: QDialog.Accepted)
    w.out_edit.setText(str(tmp_path / "batch-exports"))
    w.btn_export.click(); settle()
    report = w.cur.last_report
    assert report["ok"], report
    assert len(report["outputs"]) == 3
    assert all(o["ok"] and Path(o["path"]).is_file() for o in report["outputs"])
    assert w.selected_split_ids() == selected
