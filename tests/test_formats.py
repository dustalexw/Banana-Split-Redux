"""Real encoders/containers: import, detect, preview, project restore and independently verified exports."""
from pathlib import Path
from fractions import Fraction
import pytest
from fixturegen import make
from test_engine import session, frames_of, run_export
from bsr.smart import supported
from bsr.frames import FrameReader
from bsr.session import SourceSession
from bsr.media import fingerprint

_APP = None


def desktop_import(path, tmp_path):
    """The user-facing mode must actually be selectable on these files."""
    import os, time
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.environ['BSR_LOG_DIR'] = str(tmp_path / 'logs')
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from bsr_ui.app import MainWindow
    from bsr_ui.theme import apply_theme
    global _APP
    _APP = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat,QSettings.UserScope,str(tmp_path/'settings'))
    apply_theme(_APP)
    w=MainWindow()
    try:
        w.add_paths([str(path)])
        started=time.monotonic()
        while w._jobs:
            _APP.processEvents();time.sleep(.005)
            assert time.monotonic()-started < 30
        _APP.processEvents()
        assert w.cur.session is not None
        assert w.mode.model().item(1).isEnabled(), w.mode_reason.text()
        assert w.mode.currentData() == 'smart'
    finally: w.close()

CASES = [
    ('hevc_mkv', 'hevc', 'matroska', 'mkv', ('aac',), False),
    ('hevc_mp4', 'hevc', 'mp4', 'mp4', ('aac',), False),
    ('hevc10_mkv', 'hevc', 'matroska', 'mkv', ('ac3',), True),
    ('hevc10_mp4', 'hevc', 'mp4', 'mp4', ('aac',), True),
    ('h26410_mkv', 'h264', 'matroska', 'mkv', ('ac3',), True),
    ('h264_mov', 'h264', 'mov', 'mov', ('aac',), False),
    ('h264_ts', 'h264', 'mpegts', 'ts', ('aac',), False),
    ('hevc_ts', 'hevc', 'mpegts', 'ts', ('ac3',), False),
    ('mpeg4_avi', 'mpeg4', 'avi', 'avi', ('mp3',), False),
    ('mpeg4_mp4', 'mpeg4', 'mp4', 'mp4', ('aac',), False),
    ('mpeg2_mkv', 'mpeg2video', 'matroska', 'mkv', ('ac3',), False),
    ('mpeg2_mpg', 'mpeg2video', 'mpeg', 'mpg', ('mp2',), False),
    ('mpeg2_ts', 'mpeg2video', 'mpegts', 'ts', ('mp2',), False),
    ('vp8_webm', 'vp8', 'webm', 'webm', ('libopus',), False),
    ('vp9_webm', 'vp9', 'webm', 'webm', ('libopus',), False),
    ('av1_mkv', 'av1', 'matroska', 'mkv', ('aac',), False),
    ('av1_mp4', 'av1', 'mp4', 'mp4', ('aac',), False),
    ('wmv_asf', 'wmv2', 'asf', 'wmv', ('wmav2',), False),
    ('mjpeg_avi', 'mjpeg', 'avi', 'avi', ('mp3',), False),
    ('prores_mov', 'prores', 'mov', 'mov', ('pcm_s16le',), False),
    ('ffv1_mkv', 'ffv1', 'matroska', 'mkv', ('flac',), False),
    ('h264_avi', 'h264', 'avi', 'avi', ('mp3',), False),
    ('hevc_mov', 'hevc', 'mov', 'mov', ('aac',), False),
    ('hevc10_ts', 'hevc', 'mpegts', 'ts', ('aac',), True),
    ('mpeg4_mkv', 'mpeg4', 'matroska', 'mkv', ('ac3',), False),
    ('h264_m4v', 'h264', 'mp4', 'm4v', ('aac',), False),
    ('h264_flv', 'h264', 'flv', 'flv', ('aac',), False),
]

@pytest.mark.parametrize('name,codec,container,ext,audio,ten_bit', CASES, ids=[c[0] for c in CASES])
def test_format_workflow(tmp_path, name, codec, container, ext, audio, ten_bit):
    src = tmp_path / ('source.' + ext)
    make(str(src), codec=codec, container=container, audio=audio, ten_bit=ten_bit,
         gop=250, bframes=3 if codec in ('h264','hevc') else 0, frames=300,
         sar=Fraction(1,1))
    before = fingerprint(str(src))
    desktop_import(src, tmp_path)
    s = session(str(src), 'balanced', min_spacing_s=0)
    s.policy.min_segment_s = 0
    c = next(c for c in s.candidates if frames_of(s,c) == (100,110))
    reader = FrameReader(s.info)
    try:
        f = reader.frame_at(c.start_pts)
        assert f is not None and reader.image(f, width=160).size[0] == 160
    finally: reader.close()
    ok, why = supported(s.info)
    if codec in ('mjpeg','prores'):
        s.policy.boundary_reencode = False
    else:
        assert ok, why
        s.policy.boundary_reencode = True
    s.save_project(str(tmp_path / 'review.bsrproj'))
    assert SourceSession.load_project(str(tmp_path / 'review.bsrproj')).policy == s.policy
    plan, report = run_export(s, tmp_path / 'export', [c.id])
    assert report['ok'], [(o['error'], o.get('validation')) for o in report['outputs']]
    assert not plan.dropped_frames
    # Strict mode must never invoke an encoder. Use a reviewed safe keyframe.
    s = session(str(src), 'balanced', min_spacing_s=0)
    s.policy.min_segment_s = 0
    c = next(c for c in s.candidates if frames_of(s,c) == (100,110))
    key = next(o for o in s.resolve([c.id])[c.id].nearby if o.verified and not o.dropped_pts and o.seconds > 4.4)
    plan, report = run_export(s, tmp_path / 'strict', [c.id], choices={c.id: f'boundary:{key.pts}'})
    assert report['ok'], [o['error'] for o in report['outputs']]
    assert report['mode'] == 'copy'
    assert all(o['plan']['reencoded'].get('video_frames',0) == 0 for o in report['outputs'])
    # Removing black is independent of splitting; encode two exact edges when needed.
    s = session(str(src), 'balanced', min_spacing_s=0)
    s.policy.min_segment_s = 0
    c = next(c for c in s.candidates if frames_of(s,c) == (100,110))
    s.policy.boundary_reencode = supported(s.info)[0]
    plan, report = run_export(s, tmp_path / 'remove', [c.id], remove=True)
    assert report['ok'], [o['error'] for o in report['outputs']]
    assert plan.removed and not plan.dropped_frames
    assert float((plan.removed[0][1] - plan.removed[0][0])*s.vtb) == pytest.approx(.4)
    assert fingerprint(str(src)) == before

@pytest.mark.parametrize('name', ['h264_longgop','mpeg2_open','h264_mp4_longgop'])
def test_nearby_cuts_same_gop(fixtures, tmp_path, name):
    s = session(fixtures[name], 'balanced', min_spacing_s=0)
    for d in s.decisions.values(): d.state = 'rejected'
    s.policy.boundary_reencode = True
    s.policy.min_segment_s = 0
    s.add_manual(4.2)
    s.add_manual(4.24)
    plan = s.plan()
    assert not plan.issues, plan.issues
    from bsr.export import export_plan, ExportOptions
    report = export_plan(s.info, s.idx, plan, ExportOptions(str(tmp_path)))
    assert report['ok'], [o['error'] for o in report['outputs']]
    assert len(report['outputs']) == 3
    assert report['outputs'][1]['validation']['checks']['frame count & order']['ok']

@pytest.mark.parametrize('codec,ten_bit,container', [('hevc',False,'mp4'), ('hevc',True,'mp4'), ('mpeg2video',False,'matroska')])
def test_native_source_variants(tmp_path,codec,ten_bit,container):
    src = tmp_path / ('variant.mp4' if container == 'mp4' else 'variant.mkv')
    make(str(src), codec=codec, container=container, ten_bit=ten_bit, frames=300,
         gop=250 if codec=='hevc' else 12, bframes=3 if codec=='hevc' else 2,
         open_gop=codec=='mpeg2video', interlaced='tt' if codec=='mpeg2video' else None,
         sar=Fraction(16,15), audio=('aac',) if codec=='hevc' else ('ac3','ac3'), tag='hvc1' if codec=='hevc' else None)
    s=session(str(src),'balanced',min_spacing_s=0)
    s.policy.boundary_reencode=True
    assert s.reencode_supported()
    c=next(c for c in s.candidates if frames_of(s,c)==(100,110))
    plan,report=run_export(s,tmp_path/'parts',[c.id],choices={c.id:'reencode'})
    assert report['ok'], [o['error'] for o in report['outputs']]
    assert not plan.dropped_frames


def test_hd_hevc(tmp_path, monkeypatch):
    import fixturegen
    monkeypatch.setattr(fixturegen, 'W', 1280)
    monkeypatch.setattr(fixturegen, 'H', 720)
    # Defaults of frame_planes are resolved at definition, so supply current sizes.
    original = fixturegen.frame_planes
    monkeypatch.setattr(fixturegen, 'frame_planes', lambda n, **kw: original(n, w=1280, h=720, **kw))
    src=tmp_path/'HD HEVC.mp4'
    make(str(src),codec='hevc',ten_bit=True,container='mp4',audio=('aac',),frames=200,gop=125,bframes=3)
    s=session(str(src),'balanced',min_spacing_s=0);s.policy.boundary_reencode=True
    c=next(c for c in s.candidates if frames_of(s,c)==(100,110))
    plan,report=run_export(s,tmp_path/'parts',[c.id])
    assert report['ok'], [o['error'] for o in report['outputs']]


def test_avi_mpeg4_bframes(tmp_path):
    src=tmp_path/'Xvid.avi'
    make(str(src),codec='mpeg4',container='avi',audio=('mp3',),frames=300,gop=250,bframes=2)
    # AVI MPEG-4's B-frame timestamps are recovered by the same packet/frame map.
    s=session(str(src),'balanced',min_spacing_s=0);s.policy.boundary_reencode=True
    c=next(c for c in s.candidates if frames_of(s,c)==(100,110))
    plan,report=run_export(s,tmp_path/'parts',[c.id])
    assert report['ok'], [o['error'] for o in report['outputs']]


@pytest.mark.parametrize('name', ['h264_longgop','h264_10bit'])
def test_high_quality_boundary_setting(fixtures,tmp_path,name):
    from bsr.export import export_plan,ExportOptions
    s=session(fixtures[name],'balanced',min_spacing_s=0)
    for d in s.decisions.values():d.state='rejected'
    s.policy.boundary_reencode=True
    d=s.add_manual(4.2);d.choice='reencode'
    plan=s.plan();assert not plan.issues,plan.issues
    report=export_plan(s.info,s.idx,plan,ExportOptions(str(tmp_path),crf=1))
    assert report['ok'], [o['error'] for o in report['outputs']]

@pytest.mark.parametrize('container', ['avi','matroska','mp4'])
@pytest.mark.parametrize('cut_frame', [112,113,114,115])
def test_mpeg4_reordered_join_keeps_every_frame(tmp_path, container, cut_frame):
    """Both sides of a 1–3 frame splice must survive a VOL switch with delayed pictures."""
    from bsr.export import export_plan, ExportOptions
    src=tmp_path/('reordered.'+{'avi':'avi','matroska':'mkv','mp4':'mp4'}[container])
    make(str(src),codec='mpeg4',container=container,audio=('mp3',),frames=200,gop=12,bframes=2)
    s=session(str(src),'balanced',min_spacing_s=0)
    assert s.info.video.has_b_frames
    for d in s.decisions.values(): d.state='rejected'
    s.policy.boundary_reencode=True
    s.add_manual(cut_frame/25)
    plan=s.plan()
    report=export_plan(s.info,s.idx,plan,ExportOptions(str(tmp_path/'parts')))
    assert report['ok'],[o.get('error') for o in report['outputs']]
    assert len(report['outputs'])==2
    assert not plan.dropped_frames
    assert sum(o['plan']['reencoded'].get('video_frames',0) for o in report['outputs'])<=12
