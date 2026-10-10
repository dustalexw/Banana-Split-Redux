import os
import time
import shutil
from pathlib import Path

import pytest
from bsr.planner import title_suggestions, suggested_names
from bsr_ui.review import parse_time


@pytest.mark.parametrize('filename,prefix,titles', [
    ('Show - S01E01 - First - Second.mkv', 'Show - S01E01', ['First','Second']),
    ('Show 1x01 (01) First; Second [DVD].mkv','Show 1x01 (01)',['First','Second']),
    ('Show (2002) - S01E04 - French Fry Falls_Amazin River[540p-WEB-DL][ENGCC].mp4','Show (2002) - S01E04',['French Fry Falls','Amazin River']),
    ('Show S01E01 - Spider-Man - A well-known friend.mp4','Show S01E01',['Spider-Man','A well-known friend']),
    ('Show – S01E01 – First — Second.avi','Show – S01E01',['First','Second']),
    ('Show - First - Second.mp4','Show',['First','Second']),
    ('First - Second.mp4','',['First','Second']),
    ('Show S01E01 A Single Title [1080p].mp4','Show S01E01',['A Single Title']),
    ('Show S01E01 [720p].mp4','Show S01E01',[]),
    ('Untitled video.mp4','Untitled video',[]),
    ('Show 1x01 Title With (Parentheses); Other Title.mkv','Show 1x01',['Title With (Parentheses)','Other Title']),
])
def test_title_suggestions(filename, prefix, titles):
    assert title_suggestions(filename) == (prefix,titles)
    names = suggested_names(prefix,titles)
    assert len(set(names)) == len(titles)
    assert all('/' not in name for name in names)


@pytest.mark.parametrize('text,value',[('215.72',215.72),('03:35.720',215.72),('01:03:35.720',3815.72),('90:00',5400)])
def test_cut_time(text,value):
    assert parse_time(text) == pytest.approx(value)


@pytest.mark.parametrize('text',['-1','nan','1:60','1:99:00','0:00:60','1:2:3:4','garbage'])
def test_bad_cut_time(text):
    with pytest.raises(ValueError):parse_time(text)


def test_quick_review_names_adjustment_and_export(fixtures, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    from bsr_ui.app import MainWindow
    from bsr_ui.dialogs import PlanDialog, ResultsDialog, NamesDialog
    from bsr_ui.theme import apply_theme
    from bsr.session import SourceSession
    os.environ['QT_QPA_PLATFORM']='offscreen'
    monkeypatch.setenv('BSR_LOG_DIR',str(tmp_path/'logs'))
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat,QSettings.UserScope,str(tmp_path/'settings'))
    app=QApplication.instance() or QApplication([]);apply_theme(app)
    source=tmp_path/'Show - S01E01 - First - Second.mp4'
    shutil.copy2(fixtures['h264_mp4_longgop'],source)
    w=MainWindow();w.show()
    reports=[]
    def settle():
        began=time.monotonic()
        while w._jobs:
            app.processEvents();time.sleep(.005)
            assert time.monotonic()-began<60
        app.processEvents()
    try:
        w.add_paths([str(source)],analyse=True);settle()
        s=w.cur.session
        assert s.stats is not None and w.cur.state=='ready'
        w.preset.setCurrentIndex(w.preset.findData('single-frame'));settle()
        first=w.sel_id
        assert s.decisions[first].state=='proposed'
        w.btn_accept.click();settle()
        assert s.decisions[first].state=='accepted' and w.sel_id!=first
        skipped=w.sel_id
        w.btn_reject.click();settle()
        assert s.decisions[skipped].state=='rejected'
        w.btn_undo.click();settle()
        assert s.decisions[skipped].state=='proposed'
        # Keep only the first split, then adjust it to an exact reviewed frame.
        for k,d in list(s.decisions.items()):
            if k!=first and d.state=='proposed':
                w.select_candidate(k);w.set_state('rejected');settle()
        w.select_candidate(first);w.cut_time.setText('00:00:04.280');w.btn_set_time.click();settle()
        assert s.decisions[first].custom_pts is not None
        assert s.resolved[first].boundaries[0].seconds==pytest.approx(4.28)
        assert s.plan(resolve=False).segments[0].end_s==pytest.approx(4.28)
        w.preview_time(10);w.play_around()
        assert w.player.range_ms==(2280,6280)  # preview the selected split, even after watching elsewhere
        w.player.pause()
        project=tmp_path/'review.bsrproj';s.save_project(str(project))
        assert SourceSession.load_project(str(project)).decisions[first].custom_pts==s.decisions[first].custom_pts
        def apply_names(dialog):
            assert len(dialog.combos)==2
            assert dialog.combos[0].currentText()=='Show - S01E01 - 01 First'
            return QDialog.Accepted
        monkeypatch.setattr(NamesDialog,'exec',apply_names)
        w.btn_names.click()
        assert s.names==['Show - S01E01 - 01 First','Show - S01E01 - 02 Second']
        # Exercise the actual export through the interface with its review dialog accepted.
        monkeypatch.setattr(PlanDialog,'exec',lambda dialog:QDialog.Accepted)
        monkeypatch.setattr(ResultsDialog,'exec',lambda dialog:QDialog.Accepted)
        w.out_edit.setText(str(tmp_path/'exports'));w.btn_export.click();settle()
        report=w.cur.last_report
        assert report['ok'],report
        assert len(report['outputs'])==2
        assert all(Path(o['path']).exists() and 'Show - S01E01 -' in o['path'] for o in report['outputs'])
        assert '0 left' in w.review_progress.text()
    finally:w.close()


def test_naming_count_mismatch_keeps_existing_names():
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from bsr_ui.dialogs import NamesDialog
    app=QApplication.instance() or QApplication([])
    segments=[SimpleNamespace(n=i+1,start_s=i*10,end_s=(i+1)*10) for i in range(3)]
    current=['Intro','A custom name','Credits']
    dlg=NamesDialog(None,'Show',['First','Second'],segments,current)
    assert dlg.names()==current
    dlg.combos[1].setCurrentIndex(2)
    assert dlg.names()[1]=='Show - 02 Second'
    dlg.close()


def test_result_player_and_output_folder(fixtures,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QDesktopServices
    from bsr_ui.dialogs import ResultsDialog,PlaybackDialog
    app=QApplication.instance() or QApplication([])
    source=tmp_path/'Part 01.mp4';shutil.copy2(fixtures['h264_mp4_longgop'],source)
    report={'ok':True,'report_path':str(tmp_path/'report.json'),
            'outputs':[{'n':1,'path':str(source),'ok':True,'plan':{},'validation':{}}]}
    dialog=ResultsDialog(None,report)
    visited=[];folders=[]
    def inspect(player_dialog):
        player_dialog.show();start=time.monotonic()
        while player_dialog.player.video.image is None:
            app.processEvents();time.sleep(.005);assert time.monotonic()-start<10
        visited.append(player_dialog.player.path);player_dialog.accept();return 1
    monkeypatch.setattr(PlaybackDialog,'exec',inspect)
    monkeypatch.setattr(QDesktopServices,'openUrl',lambda url:folders.append(url.toLocalFile()) or True)
    try:
        dialog.play_button.click();dialog.folder_button.click()
        assert visited==[str(source)] and folders==[str(tmp_path)]
    finally:dialog.close()

@pytest.mark.parametrize('blocker',['unresolved','short'])
def test_finish_explains_blocker_and_opens_split(fixtures,tmp_path,monkeypatch,blocker):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication,QMessageBox
    from bsr_ui.app import MainWindow,Item
    from test_engine import session
    app=QApplication.instance() or QApplication([])
    QSettings.setPath(QSettings.IniFormat,QSettings.UserScope,str(tmp_path/'settings'))
    s=session(fixtures['h264_mp4_longgop'])
    for d in s.decisions.values(): d.state='rejected'
    s.policy.boundary_reencode = blocker=='short'
    first=s.add_manual(4.28)
    if blocker=='short':
        s.add_manual(4.32)
    s.resolve()
    assert s.plan(resolve=False).issues
    w=MainWindow();it=Item(s.path);it.session=s;it.state='ready';it.out_dir=str(tmp_path/'exports')
    w.items=[it];w.cur=it;w.show_item();w.show()
    messages=[]
    def explain(dlg):
        messages.append(dlg.informativeText())
        review=next(b for b in dlg.buttons() if b.text()=='Review split')
        review.click()
        return 0
    monkeypatch.setattr(QMessageBox,'exec',explain)
    try:
        assert w.btn_export.isEnabled()
        assert w.export_issue.isVisible() and w.btn_export_issue.isVisible()
        w.btn_export.click()
        assert messages and w.sel_id in [d.id for d in s.accepted()]
        assert not (tmp_path/'exports').exists()
        # Skip the highlighted split, resolving the issue, then finish becomes ready.
        s.decisions[w.sel_id].state='rejected';s.resolve();w.refresh_parts()
        assert not s.plan().issues
        assert w.btn_export.isEnabled() and not w.export_issue.isVisible()
    finally:
        w.close()
        start=time.monotonic()
        while w._jobs:
            app.processEvents();time.sleep(.005);assert time.monotonic()-start<30


@pytest.mark.parametrize('reason',['Disk is full','Cancelled'])
def test_failed_export_can_be_retried(fixtures,tmp_path,monkeypatch,reason):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication,QMessageBox
    from bsr_ui.app import MainWindow,Item
    from bsr_ui.dialogs import PlanDialog
    import bsr_ui.app as module
    from test_engine import session
    app=QApplication.instance() or QApplication([])
    QSettings.setPath(QSettings.IniFormat,QSettings.UserScope,str(tmp_path/'settings'))
    s=session(fixtures['h264_mp4_longgop'])
    for d in s.decisions.values():d.state='rejected'
    w=MainWindow();it=Item(s.path);it.session=s;it.state='ready';it.out_dir=str(tmp_path/'output')
    w.items=[it];w.cur=it;w.show_item()
    monkeypatch.setattr(PlanDialog,'exec',lambda *_:1)
    monkeypatch.setattr(QMessageBox,'warning',lambda *_:None)
    def fail(*args,**kwargs):raise RuntimeError(reason)
    monkeypatch.setattr(module,'export_plan',fail)
    try:
        w.btn_export.click()
        began=time.monotonic()
        while w._jobs:
            app.processEvents();time.sleep(.005);assert time.monotonic()-began<30
        app.processEvents()
        assert it.state=='ready' and reason in it.error
        assert w.btn_export.isEnabled() and not w.btn_cancel.isEnabled()
        assert not w.prog.isVisible()
    finally:w.close()
