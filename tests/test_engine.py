"""Media-engine tests on generated fixtures with known ground truth."""
import hashlib
import os
import shutil
import threading
from fractions import Fraction

import av
import numpy as np
import pytest

import fixturegen as fg
from bsr.detect import DetectSettings, PRESETS, find_candidates
from bsr.export import ExportOptions, export_plan, default_streams
from bsr.media import Cancelled, fingerprint
from bsr.planner import Policy
from bsr.session import SourceSession


def session(path, preset="single-frame", **kw):
    s = SourceSession(path)
    s.set_preset(preset)
    for k, v in kw.items():
        setattr(s.detect, k, v)
    s.analyze()
    s.find(audio_signal=False)
    return s


def frames_of(s, c):
    """Candidate interval as fixture frame numbers (fixtures are 25 fps)."""
    off = s.stats.seconds(s.stats.pts[0])
    return round((c.start_s - off) * 25), round((c.end_s - off) * 25)


def run_export(s, out, accept, choices=None, policy=None, only=None, remove=False, position="middle"):
    if policy:
        s.policy = policy
    for d in s.decisions.values():
        d.state = "accepted" if d.id in accept else "rejected"
        d.position = position
        d.remove_black = remove
    for k, v in (choices or {}).items():
        s.decisions[k].choice = v
    plan = s.plan()
    assert not plan.issues, plan.issues
    rep = export_plan(s.info, s.idx, plan, ExportOptions(out_dir=str(out)), only=only)
    return plan, rep


def sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def beep_onsets(path, stream_pos=0):
    """Absolute times (s) of 1 kHz marker beeps in an audio track (decoded)."""
    c = av.open(path)
    st = c.streams.audio[stream_pos]
    chunks, t0 = [], None
    for fr in c.decode(st):
        if fr.pts is None:
            continue
        if t0 is None:
            t0 = float(fr.pts * st.time_base)
        a = fr.to_ndarray()
        chunks.append(a[0] if a.ndim == 2 else a)
    c.close()
    x = np.concatenate(chunks).astype(np.float64)
    sr = st.codec_context.sample_rate
    loud = np.abs(x) > 0.45
    idx = np.nonzero(loud)[0]
    out, last = [], -10 ** 9
    for i in idx:
        if i - last > 0.3 * sr:
            out.append(t0 + i / sr)
        last = i
    return out


# ------------------------------------------------------------------ detection

def test_detects_known_intervals(fixtures):
    s = session(fixtures["h264_closed"], "single-frame", min_spacing_s=0, pixel_threshold=0.05)
    got = sorted(frames_of(s, c) for c in s.candidates)
    # hard black, single-frame flash and logo frames exactly; fade black within a few frames
    assert (100, 110) in got
    assert (200, 201) in got
    assert (450, 453) in got
    fade = [g for g in got if g[0] <= 312 and g[1] >= 318]
    assert fade and fade[0][0] >= 306 and fade[0][1] <= 324
    kinds = {frames_of(s, c): c.kind for c in s.candidates}
    assert kinds[(200, 201)] == "flash"
    assert "fade" in kinds[fade[0]]


def test_dark_scene_is_not_black(fixtures):
    for preset in ("conservative", "balanced", "sensitive", "single-frame"):
        s = session(fixtures["h264_closed"], preset, min_spacing_s=0)
        for c in s.candidates:
            a, b = frames_of(s, c)
            assert not (a < 271 and b > 240), (preset, a, b)


def test_conservative_ignores_single_frame_flash(fixtures):
    s = session(fixtures["h264_closed"], "conservative", min_spacing_s=0)
    assert all(c.n_frames >= 3 for c in s.candidates)
    assert (200, 201) not in [frames_of(s, c) for c in s.candidates]


def test_logo_tolerance(fixtures):
    strict = session(fixtures["h264_closed"], "single-frame", min_spacing_s=0, black_ratio=0.999)
    assert (450, 453) not in [frames_of(strict, c) for c in strict.candidates]
    tolerant = session(fixtures["h264_closed"], "single-frame", min_spacing_s=0, black_ratio=0.97)
    assert (450, 453) in [frames_of(tolerant, c) for c in tolerant.candidates]


def test_letterbox_region(fixtures):
    # with a lax ratio, letterbox bars make the dark scene look black unless the bars are cropped
    lax = dict(min_spacing_s=0, black_ratio=0.79)
    s = session(fixtures["h264_letterbox"], "single-frame", **lax)
    assert any(frames_of(s, c)[0] < 271 and frames_of(s, c)[1] > 240 for c in s.candidates)
    s2 = SourceSession(fixtures["h264_letterbox"])
    s2.set_preset("single-frame")
    s2.detect.min_spacing_s, s2.detect.black_ratio = 0, 0.79
    s2.scan.region.top = s2.scan.region.bottom = 0.17
    s2.analyze(); s2.find(audio_signal=False)
    assert not any(frames_of(s2, c)[0] < 271 and frames_of(s2, c)[1] > 240 for c in s2.candidates)
    assert (100, 110) in [frames_of(s2, c) for c in s2.candidates]


def test_ten_bit_range(fixtures):
    s = session(fixtures["h264_10bit"], "single-frame", min_spacing_s=0, pixel_threshold=0.05)
    assert s.stats.bit_depth == 10
    assert (100, 110) in [frames_of(s, c) for c in s.candidates]


def test_vfr_and_offset_timestamps(fixtures):
    s = session(fixtures["h264_vfr_offset"], "single-frame", min_spacing_s=0)
    starts = sorted(round(c.start_s, 3) for c in s.candidates)
    assert 14.0 in starts                         # 10 s offset + 4.0 s
    # frame durations follow real timestamps in the VFR stretch
    d = set(int(x) for x in s.stats.duration[200:300])
    assert {20, 60} <= d


def test_thresholds_need_no_redecode(fixtures):
    s = session(fixtures["h264_closed"])
    a = len(find_candidates(s.stats, DetectSettings(pixel_threshold=0.05, black_ratio=0.99, min_spacing_s=0)))
    b = len(find_candidates(s.stats, DetectSettings(pixel_threshold=0.20, black_ratio=0.80, min_spacing_s=0)))
    assert b >= a


# ------------------------------------------------------------------ planning

def test_safe_boundary_inside_black(fixtures):
    s = session(fixtures["h264_closed"], "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    s.decisions[c.id].state = "accepted"
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "copy"
    assert rc.boundaries[0].verified and rc.boundaries[0].pts == c.start_pts
    assert rc.boundaries[0].random_access == "IDR"


def test_no_boundary_needs_review_and_never_auto_moves(fixtures):
    s = session(fixtures["h264_longgop"], "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    s.decisions[c.id].state = "accepted"
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "needs-review"
    kinds = [ch["kind"] for ch in rc.choices]
    assert "nearby" in kinds and "skip" in kinds
    plan = s.plan()
    assert plan.issues and len(plan.segments) == 1      # nothing silently split
    # explicit choice resolves it, with the time difference reported
    near = next(ch for ch in rc.choices if ch["kind"] == "nearby")
    s.decisions[c.id].choice = near["choice"]
    rc2 = s.resolve([c.id])[c.id]
    assert rc2.status == "copy" and rc2.outside_black and rc2.messages


def test_open_gop_requires_acceptance_or_policy(fixtures):
    s = session(fixtures["mpeg2_open"], "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    s.decisions[c.id].state = "accepted"
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "needs-review"
    acc = [ch for ch in rc.choices if ch["kind"] == "accept-drop"]
    assert acc
    s.policy = Policy(max_auto_drop_black_frames=2)
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "copy-drop"
    b = rc.boundaries[0]
    assert len(b.dropped_pts) == 2 and all(c.start_pts <= p < c.end_pts for p in b.dropped_pts)


def test_open_gop_with_visible_leading_frames_is_never_offered(fixtures):
    # h264 open-GOP keyframe at 4.0 s has a leading picture at 3.96 s (not black)
    s = session(fixtures["h264_open"], "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    st = s.finder.by_pts.get(c.start_pts)
    if not st or not st["leading"]:
        pytest.skip("this x264 build closed the GOP at 4.0 s; no visible leading pictures to test")
    s.decisions[c.id].state = "accepted"
    s.policy = Policy(max_auto_drop_black_frames=5)
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "needs-review"
    assert not [ch for ch in rc.choices if ch["kind"] == "accept-drop"]


def test_tiny_segment_blocked(fixtures):
    s = session(fixtures["h264_closed"], "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    s.decisions[c.id].state = "accepted"
    s.add_manual(5.0)
    s.policy.min_segment_s = 2.0
    plan = s.plan()
    assert any("only" in i for i in plan.issues)


# ------------------------------------------------------------------ export + validation

EXPORT_CASES = [
    ("h264_closed", {}),
    ("mpeg2_closed", {}),
    ("mpeg2_open", {"policy": Policy(max_auto_drop_black_frames=2)}),
    ("h264_vfr_offset", {}),
    ("h264_full", {}),
    ("h264_10bit", {}),
    ("h264_mp4", {}),
]


@pytest.mark.parametrize("name,kw", EXPORT_CASES)
def test_export_validates(fixtures, tmp_path, name, kw):
    src = fixtures[name]
    before = sha1(src)
    s = session(src, "balanced", min_spacing_s=0)
    pol = kw.get("policy")
    if pol:
        s.policy = pol
    s.resolve()
    ok_ids = [k for k, rc in s.resolved.items()
              if rc.status in ("copy", "copy-drop") and not s.candidates[k].leading and not s.candidates[k].trailing]
    assert ok_ids, f"no copy-resolvable cut in {name}"
    plan, rep = run_export(s, tmp_path, ok_ids, policy=pol)
    assert rep["ok"], [o["error"] for o in rep["outputs"]]
    assert len(rep["outputs"]) == len(plan.segments) >= 2
    for o in rep["outputs"]:
        v = o["validation"]["checks"]
        for k in ("packets byte-identical", "timestamps", "video decodes", "frame count & order",
                  "first frame matches plan", "last frame matches plan", "seek"):
            assert v[k]["ok"], (k, v[k])
        assert os.path.exists(o["path"])
    # every source video packet appears exactly once across outputs, except reported drops
    total = sum(len(rp_v) for rp_v in [o["plan"]["tracks"] for o in rep["outputs"]])
    vsp = s.idx[s.info.video.index]
    written_video = sum(t["packets"] for o in rep["outputs"] for t in o["plan"]["tracks"] if t["type"] == "video")
    assert written_video + len(plan.dropped_frames) == len(vsp)
    for si in [x.index for x in s.info.of_type("audio")]:
        written = sum(t["packets"] for o in rep["outputs"] for t in o["plan"]["tracks"] if t["stream_index"] == si)
        assert written == len(s.idx[si])           # audio: nothing lost, nothing duplicated
    assert sha1(src) == before                      # source untouched


@pytest.mark.parametrize("name", ["h264_closed", "mpeg2_open", "h264_full", "h264_mp4"])
def test_audio_markers_stay_in_sync(fixtures, tmp_path, name):
    src = fixtures[name]
    s = session(src, "balanced", min_spacing_s=0)
    pol = Policy(max_auto_drop_black_frames=2)
    s.policy = pol
    s.resolve()
    ok_ids = [k for k, rc in s.resolved.items() if rc.status in ("copy", "copy-drop")
              and not s.candidates[k].leading and not s.candidates[k].trailing]
    plan, rep = run_export(s, tmp_path, ok_ids, policy=pol)
    assert rep["ok"]
    n_audio = len(s.info.of_type("audio"))
    for k in range(n_audio):
        src_on = beep_onsets(src, k)
        for o, seg in zip(rep["outputs"], plan.segments):
            out_on = beep_onsets(o["path"], k)
            exp = [t - seg.start_s for t in src_on if seg.start_s + 0.06 <= t < seg.end_s - 0.06]
            got = [t for t in out_on if 0.06 <= t < seg.duration_s - 0.06]
            assert len(exp) == len(got), (name, k, seg.n, exp, got)
            for e, g in zip(exp, got):
                assert abs(e - g) < 0.0015, (name, k, seg.n, e, g)   # sub-millisecond-class sync


def test_subtitles_chapters_attachments(fixtures, tmp_path):
    s = session(fixtures["h264_full"], "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    plan, rep = run_export(s, tmp_path, [c.id])
    assert rep["ok"]
    p2 = rep["outputs"][1]["path"]
    o = av.open(p2)
    assert [st.type for st in o.streams].count("attachment") == 1
    sub = [st for st in o.streams if st.type == "subtitle"][0]
    assert sub.metadata.get("language") == "eng"
    evs = [(float(p.pts * sub.time_base), float(p.duration * sub.time_base)) for p in o.demux(sub) if p.size]
    o.close()
    # cue 3.5-5.0 s crosses the cut at 4.0 s: clipped to 0.0-1.0 s in part 2
    assert (0.0, 1.0) in [(round(a, 3), round(d, 3)) for a, d in evs]
    o = av.open(rep["outputs"][0]["path"])
    sub1 = [st for st in o.streams if st.type == "subtitle"][0]
    evs1 = [(float(p.pts * sub1.time_base), float(p.duration * sub1.time_base)) for p in o.demux(sub1) if p.size]
    o.close()
    assert (3.5, 0.5) in [(round(a, 3), round(d, 3)) for a, d in evs1]
    ch = av.open(p2).chapters()
    assert [round(float(x["start"] * Fraction(x["time_base"])), 3) for x in ch] == [0.0, 2.0, 10.0]


def test_remove_black_padding(fixtures, tmp_path):
    s = session(fixtures["h264_shortgop"], "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    s.decisions[c.id].pad_before_s = 0.0
    s.decisions[c.id].pad_after_s = 0.08
    plan, rep = run_export(s, tmp_path, [c.id], remove=True)
    assert rep["ok"], [o["error"] for o in rep["outputs"]]
    assert plan.removed and len(plan.segments) == 2
    a, b = plan.removed[0]
    tb = s.vtb
    assert float(a * tb) == pytest.approx(4.0) and float(b * tb) == pytest.approx(4.32)   # keeps 2 black frames
    assert plan.segments[0].end_s == pytest.approx(4.0) and plan.segments[1].start_s == pytest.approx(4.32)


def test_no_encoder_in_copy_mode(fixtures, tmp_path):
    """Copy export must not create any codec context, encoder stream or encode call.

    Every call into PyAV's compiled code during the write phase is recorded with a
    profiler hook (works across PyAV versions, whose types cannot be monkeypatched).
    Validation (which decodes) runs separately afterwards.
    """
    import sys
    from bsr.export import plan_segment, write_segment, output_format
    from bsr.validate import validate_output
    s = session(fixtures["h264_full"], "balanced", min_spacing_s=0)
    s.resolve()
    ok = [k for k, rc in s.resolved.items() if rc.status == "copy"]
    for d in s.decisions.values():
        d.state = "accepted" if d.id in ok else "rejected"
    plan = s.plan()
    streams = default_streams(s.info)
    fmt, ext = output_format(s.info)
    calls = []

    def prof(frame, event, arg):
        if event == "c_call":
            calls.append(getattr(arg, "__qualname__", getattr(arg, "__name__", "?")))
    for sg in plan.segments:
        spp = plan_segment(s.info, s.idx, sg, streams, ext)
        out = str(tmp_path / f"p{sg.n}{ext}")
        sys.setprofile(prof)
        try:
            write_segment(s.info, s.idx, spp, streams, fmt, out)
        finally:
            sys.setprofile(None)
        assert validate_output(s.info, s.idx, spp, streams, out)["ok"]
    bad = [c for c in calls if c.split(".")[-1] in ("add_stream", "encode", "encode_lazy", "create", "open")
           and c.split(".")[0] not in ("str", "bytes") and c != "open"]
    assert not bad, sorted(set(bad))
    assert any(c.endswith("add_stream_from_template") for c in calls)


def test_unicode_and_spaces_in_paths(fixtures, tmp_path):
    d = tmp_path / "Ünïcødé dir; with spaces [x]"
    d.mkdir()
    src = d / "Épisode 01; “quoted” name.mkv"
    shutil.copy(fixtures["h264_closed"], src)
    s = session(str(src), "balanced", min_spacing_s=0)
    s.resolve()
    ok = [k for k, rc in s.resolved.items() if rc.status == "copy"]
    plan, rep = run_export(s, d / "out ü", ok)
    assert rep["ok"]


def test_refuses_to_overwrite_source(fixtures, tmp_path):
    src = tmp_path / "x.mkv"
    shutil.copy(fixtures["h264_closed"], src)
    s = session(str(src), "balanced", min_spacing_s=0)
    s.resolve()
    for d in s.decisions.values():
        d.state = "rejected"
    plan = s.plan()
    plan.segments[0].name = "x"
    with pytest.raises(Exception):
        export_plan(s.info, s.idx, plan, ExportOptions(out_dir=str(tmp_path)))
    assert sha1(src) == sha1(fixtures["h264_closed"])


def test_cancellation_leaves_no_final_file(fixtures, tmp_path):
    s = session(fixtures["h264_closed"], "balanced", min_spacing_s=0)
    s.resolve()
    ok = [k for k, rc in s.resolved.items() if rc.status == "copy"]
    for d in s.decisions.values():
        d.state = "accepted" if d.id in ok else "rejected"
    plan = s.plan()
    ev = threading.Event(); ev.set()
    rep = export_plan(s.info, s.idx, plan, ExportOptions(out_dir=str(tmp_path / "o")), cancel=ev)
    assert not rep["ok"]
    finals = [f for f in os.listdir(tmp_path / "o") if f.endswith(".mkv")]
    assert finals == []


def test_cache_invalidated_when_source_changes(fixtures, tmp_path):
    p = tmp_path / "c.mkv"
    shutil.copy(fixtures["h264_closed"], p)
    f1 = fingerprint(str(p))
    with open(p, "r+b") as f:
        f.seek(-10, os.SEEK_END); f.write(b"0123456789")
    assert fingerprint(str(p)) != f1


def test_project_roundtrip(fixtures, tmp_path):
    s = session(fixtures["h264_closed"], "balanced", min_spacing_s=0)
    s.decisions[0].state = "accepted"
    s.decisions[0].position = "end"
    s.add_manual(9.0)
    pj = tmp_path / "p.bsrproj"
    s.save_project(str(pj))
    s2 = SourceSession.load_project(str(pj))
    assert s2.decisions[0].position == "end"
    assert any(d.manual for d in s2.decisions.values())


# ------------------------------------------------------------------ boundary re-encoding

@pytest.mark.parametrize("name", ["h264_longgop", "h264_interlaced_longgop"])
def test_boundary_reencode_exact_cut(fixtures, tmp_path, name):
    src = fixtures[name]
    before = sha1(src)
    s = session(src, "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    d = s.decisions[c.id]
    d.state = "accepted"
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "needs-review"                       # never re-encodes on its own
    assert any(ch["kind"] == "reencode" for ch in rc.choices)
    d.choice = "reencode"
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "reencode" and c.start_pts <= rc.boundaries[0].pts < c.end_pts
    plan, rep = run_export(s, tmp_path, [c.id], choices={c.id: "reencode"})
    assert rep["ok"], [o["error"] for o in rep["outputs"]]
    assert rep["mode"] == "boundary-reencode"
    p1, p2 = rep["outputs"]
    # exact cut inside the black, at the requested (middle) frame
    assert plan.segments[0].end_s == pytest.approx(4.2) and plan.segments[1].start_s == pytest.approx(4.2)
    for o, side in ((p1, "tail"), (p2, "head")):
        chk = o["validation"]["checks"]
        if f"{side} re-encoded frames" in chk:
            assert chk[f"{side} re-encoded frames"]["ok"]
            assert chk[f"{side} join: copied frames identical"]["ok"]
        assert chk["frame count & order"]["ok"] and chk["packets byte-identical"]["ok"]
    # re-encoding is confined to the GOP after the cut (+ at most a couple of frames before it)
    re1 = p1["plan"]["reencoded"]; re2 = p2["plan"]["reencoded"]
    assert re1.get("video_frames", 0) <= 3
    assert re2["video_frames"] == 250 - 105                  # 4.2 s -> next IDR at 10.0 s
    assert re1.get("audio_samples", 0) == 0 and re2["audio_samples"] == 0
    # audio fully copied: nothing lost or duplicated
    for si in [x.index for x in s.info.of_type("audio")]:
        written = sum(t["packets"] for o in rep["outputs"] for t in o["plan"]["tracks"] if t["stream_index"] == si)
        assert written == len(s.idx[si])
    assert sha1(src) == before


def test_boundary_reencode_not_offered_outside_matrix(fixtures):
    s = session(fixtures["mpeg2_open"], "balanced", min_spacing_s=0)
    assert not s.reencode_supported()
    for rc in s.resolve().values():
        assert not any(ch["kind"] == "reencode" for ch in rc.choices)


def test_validator_rejects_a_bad_join(fixtures, tmp_path, monkeypatch):
    """Corrupt the re-encoded head piece: the export must fail validation and not be finalised."""
    from bsr import smart
    real = smart.encode_range

    def broken(*a, **k):
        pk = real(*a, **k)
        for e in pk[1:4]:                         # damage a few non-key frames
            e.data = e.data[:8] + bytes(len(e.data) - 8)
        return pk
    monkeypatch.setattr(smart, "encode_range", broken)
    s = session(fixtures["h264_longgop"], "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    for d in s.decisions.values():
        d.state = "accepted" if d.id == c.id else "rejected"
    s.decisions[c.id].choice = "reencode"
    plan = s.plan()
    rep = export_plan(s.info, s.idx, plan, ExportOptions(out_dir=str(tmp_path)))
    assert not rep["ok"]
    bad = rep["outputs"][1]
    assert not bad["ok"] and "FAILED" in os.path.basename(bad["path"]), bad["error"]
    failed = [k for k, v in bad["validation"]["checks"].items() if not v["ok"]]
    assert failed, "validator should name the failing checks"
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".mkv")][1:]   # only part 1 finalised


def test_boundary_reencode_tail_with_interleaved_audio(fixtures, tmp_path):
    """End-of-part re-encode (cut at a B-frame position) must keep video decode order
    even when audio packets with later timestamps are read first."""
    s = session(fixtures["h264_interlaced_longgop"], "balanced", min_spacing_s=0)
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    for pos in ("start", "middle", "end"):
        for d in s.decisions.values():
            d.state = "accepted" if d.id == c.id else "rejected"
            d.choice = "reencode" if d.id == c.id else None
            d.position = pos
        s.resolved = {}
        plan = s.plan()
        rep = export_plan(s.info, s.idx, plan, ExportOptions(out_dir=str(tmp_path / pos)))
        assert rep["ok"], (pos, [o["error"] for o in rep["outputs"]])
