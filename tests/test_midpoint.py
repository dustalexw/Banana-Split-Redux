"""Midpoint-only detection, whole-video verification and review recommendations."""
from dataclasses import asdict
from fractions import Fraction

import numpy as np
import pytest

from bsr.detect import (T_GRID, DetectSettings, PRESETS, FrameStats, black_mask,
                        find_candidates, midpoint_window, midpoint_recommendation,
                        video_bounds, verify_candidates)


def stats(levels=None, start=0, pts=None, duration=None):
    n = 100
    points = np.arange(n, dtype=np.int64) if pts is None else np.array(pts, dtype=np.int64)
    n = len(points)
    frac = np.zeros((n, len(T_GRID)), dtype=np.float32)
    for i, (level, ratio) in (levels or {}).items():
        frac[i] = np.where(T_GRID >= level, ratio, 0)
    return FrameStats(0, Fraction(1, 10), points + start,
                      np.ones(n, dtype=np.int64) if duration is None else np.array(duration, dtype=np.int64),
                      frac, np.full(n, .3, dtype=np.float32), np.full(n, .5, dtype=np.float32),
                      np.zeros(n, dtype=bool))


def test_boost_finds_shorter_lighter_midpoint_black_without_changing_other_regions():
    fs = stats({10: (0, 1), 20: (.115, .96), 40: (.115, .96), 50: (.115, .96),
                52: (0, 1), 60: (.115, .96), 80: (.115, .96),
                **{i: (0, 1) for i in range(12, 16)}})
    ds = DetectSettings(min_frames=3, min_duration_s=.3, min_spacing_s=0, midpoint_boost=True)
    cands = find_candidates(fs, ds)
    assert [c.start_s for c in cands] == [1.2, 4, 5, 5.2]
    assert all(c.n_frames == 1 and c.midpoint_only for c in cands if c.near_midpoint)
    assert not cands[0].near_midpoint and cands[0].n_frames == 4
    # Disable boost to get exactly the former global thresholds and minimums.
    ds.midpoint_boost = False
    assert [c.start_s for c in find_candidates(fs, ds)] == [1.2]


def test_boost_still_rejects_visible_dark_scenes_and_respects_more_sensitive_custom_ratios():
    fs = stats({50: (.115, .88), 51: (.16, 1)})
    ds = DetectSettings(min_spacing_s=0, midpoint_boost=True)
    assert not black_mask(fs, ds).any()
    fs = stats({50: (.115, .81)})
    ds.black_ratio = .80
    assert black_mask(fs, ds)[50]  # custom ratio must not become stricter


def test_midpoint_uses_elapsed_video_span_for_offset_and_vfr_timestamps():
    points = [0, 1, 3, 4, 5, 6, 8, 9]
    fs = stats({4: (.115, .96)}, start=100, pts=points, duration=[1, 2, 1, 1, 1, 2, 1, 1])
    assert video_bounds(fs) == (10, 11)
    assert midpoint_window(fs) == pytest.approx((10.4, 10.6))
    assert [c.start_s for c in find_candidates(fs, DetectSettings(midpoint_boost=True))] == [10.5]


def test_single_frame_midpoint_is_not_hidden_by_a_stronger_neighbor():
    fs = stats({50: (.115, .96), **{i: (0, 1) for i in range(37, 40)}})
    ds = DetectSettings(min_frames=2, min_spacing_s=3, midpoint_boost=True)
    cands = find_candidates(fs, ds)
    recommended = midpoint_recommendation(cands, 5)
    assert recommended.start_s == 5 and not recommended.suppressed
    assert not next(c for c in cands if c.start_s == 3.7).suppressed
    ds.midpoint_boost = False
    assert not find_candidates(fs, ds)[0].suppressed


def test_verification_uses_the_full_video_midpoint_not_the_local_scan_midpoint(monkeypatch):
    from bsr import detect
    fs = stats({20: (.115, .96), 50: (.115, .96)})
    ds = DetectSettings(midpoint_boost=True, min_spacing_s=0)
    cands = find_candidates(fs, ds)
    def analyze_window(*args, window=None, **kwargs):
        lo, hi = window
        mask = (fs.pts * float(fs.time_base) >= lo) & (fs.pts * float(fs.time_base) < hi)
        return FrameStats(fs.stream_index, fs.time_base, fs.pts[mask], fs.duration[mask],
                          fs.frac[mask], fs.mean[mask], fs.p99[mask], fs.corrupt[mask])
    monkeypatch.setattr(detect, 'analyze', analyze_window)
    from bsr.detect import ScanSettings
    got = verify_candidates('unused', None, fs, cands, ds, ScanSettings(decimate=2))
    assert [c.start_s for c in got] == [5]
    # An approximate false positive outside the central region must not become
    # black merely because the verification window is centered on it.
    from bsr.detect import Candidate
    false = Candidate(99, 20, 21, 2, 2.1, 1, False, False, 1, 'flash', 50)
    assert verify_candidates('unused', None, fs, [false], ds, ScanSettings(decimate=2)) == []


def test_new_presets_enable_midpoint_boost_and_older_settings_keep_original_behavior():
    assert all(p.midpoint_boost for p in PRESETS.values())
    previous = DetectSettings(**{k: v for k, v in asdict(PRESETS['balanced']).items() if k != 'midpoint_boost'})
    assert not previous.midpoint_boost
    assert DetectSettings(**PRESETS['balanced'].to_json()).midpoint_boost

@pytest.fixture
def midpoint_video(tmp_path):
    import av
    path = tmp_path / 'Midpoint sample.mp4'
    with av.open(str(path), 'w') as output:
        stream = output.add_stream('libx264', rate=10)
        stream.width = stream.height = 96
        stream.pix_fmt = 'yuv420p'
        stream.options = {'crf': '12', 'g': '10', 'keyint_min': '10', 'sc_threshold': '0', 'bf': '0'}
        for i in range(100):
            # Identical near-black flashes, one outside the midpoint and one at it.
            pixels = np.full((96, 96, 3), 30 if i in (20, 50) else 180, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format='rgb24')
            frame.pts, frame.time_base = i, Fraction(1, 10)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return str(path)


def test_real_video_midpoint_detection_review_and_copy_export(midpoint_video, tmp_path, monkeypatch):
    import hashlib
    import time
    from pathlib import Path
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication
    from bsr.session import SourceSession
    from bsr_ui.app import MainWindow, Item
    from bsr_ui.theme import apply_theme
    from bsr.export import export_plan, ExportOptions
    monkeypatch.setenv('BSR_LOG_DIR', str(tmp_path / 'logs'))
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / 'settings'))
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    before = hashlib.sha256(Path(midpoint_video).read_bytes()).hexdigest()
    s = SourceSession(midpoint_video)
    s.analyze(); s.find(audio_signal=False); s.resolve()
    assert len(s.candidates) == 1
    c = s.candidates[0]
    assert c.start_s == pytest.approx(5) and c.n_frames == 1 and c.midpoint_only
    assert s.decisions[c.id].state == 'proposed'
    w = MainWindow(); it = Item(s.path); it.session = s; it.state = 'ready'
    w.items = [it]; w.cur = it; w.show_item(); w.show(); app.processEvents()
    def settle():
        began = time.monotonic()
        while w._jobs:
            app.processEvents(); time.sleep(.005)
            assert time.monotonic() - began < 60
        app.processEvents()
    try:
        assert w.s_midpoint.isChecked()
        assert w.btn_midpoint.isEnabled()
        assert '0:00:05' in w.midpoint_label.text() and '50%' in w.midpoint_hint.text()
        assert w.table.item(0, 1).text().startswith('★ ')
        assert w.overview.midpoint_region == pytest.approx((4, 6))
        w.sel_id = None
        w.btn_midpoint.click(); settle()
        assert w.sel_id == c.id and s.decisions[c.id].state == 'proposed'
        assert w.preview_pts == s.resolved[c.id].boundaries[0].pts
        assert 'Midpoint boost' in w.msgs.text()
        it.state = 'proving'; w.update_review_progress()
        assert not w.btn_midpoint.isEnabled()
        it.state = 'ready'; w.update_review_progress()
        # Applying the switch works from cached statistics, without decoding again.
        cached = s.stats
        w.s_midpoint.setChecked(False); w.apply_settings(); settle()
        assert s.stats is cached and not s.candidates and not w.btn_midpoint.isEnabled()
        assert w.midpoint_label.text() == 'No midpoint suggestion'
        w.s_midpoint.setChecked(True); w.apply_settings(); settle()
        assert s.stats is cached and len(s.candidates) == 1
        c = s.candidates[0]
        project = tmp_path / 'midpoint.bsrproj'; s.save_project(str(project))
        assert SourceSession.load_project(str(project)).detect.midpoint_boost
        import json
        previous = json.loads(project.read_text()); previous['detect'].pop('midpoint_boost')
        project.write_text(json.dumps(previous))
        assert not SourceSession.load_project(str(project)).detect.midpoint_boost
        w.btn_midpoint.click(); settle()
        w.set_state('accepted'); settle()
        assert s.decisions[c.id].state == 'accepted'
        plan = s.plan()
        assert not plan.issues and len(plan.segments) == 2
        report = export_plan(s.info, s.idx, plan, ExportOptions(out_dir=str(tmp_path / 'exports')))
        assert report['ok'], report
        assert all(o['validation']['checks']['frame count & order']['ok'] for o in report['outputs'])
        assert all(o['validation']['checks']['packets byte-identical']['ok'] for o in report['outputs'])
        assert hashlib.sha256(Path(midpoint_video).read_bytes()).hexdigest() == before
        # Skipping a recommendation does not keep offering the declined split.
        w.set_state('rejected'); settle()
        assert not w.btn_midpoint.isEnabled() and w.overview.recommended_id is None
        w.cur = None; w.show_item(); app.processEvents()
        assert not w.btn_midpoint.isEnabled() and w.overview.midpoint_region is None
    finally:
        settle(); w.close()
