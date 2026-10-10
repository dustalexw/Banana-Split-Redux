"""Default desktop fallback, strict mode and independent export verification."""
import pytest
from test_engine import session, frames_of, run_export
from bsr.session import SourceSession


@pytest.mark.parametrize("name", ["h264_longgop", "h264_interlaced_longgop", "h264_mp4_longgop"])
def test_automatic_boundary_reencode_export(fixtures, tmp_path, name):
    s = session(fixtures[name], "balanced", min_spacing_s=0)
    s.policy.boundary_reencode = True
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    plan, rep = run_export(s, tmp_path, [c.id])
    assert rep["ok"], [o["error"] for o in rep["outputs"]]
    assert s.decisions[c.id].choice is None
    assert plan.segments[0].end_s == pytest.approx(4.2)
    assert rep["mode"] == "boundary-reencode"
    for out in rep["outputs"]:
        assert out["validation"]["checks"]["frame count & order"]["ok"]
        assert out["validation"]["checks"]["packets byte-identical"]["ok"]
        assert out["plan"]["reencoded"].get("audio_samples", 0) == 0
        assert out["validation"]["checks"]["audio decodes & sync"]["ok"]
        if name == "h264_mp4_longgop":
            assert out["path"].endswith(".mp4")


def test_automatic_mode_keeps_safe_cuts_and_explicit_choices(fixtures):
    s = session(fixtures["h264_closed"], "balanced", min_spacing_s=0)
    s.policy.boundary_reencode = True
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    assert s.resolve([c.id])[c.id].status == "copy"
    s = session(fixtures["h264_longgop"], "balanced", min_spacing_s=0)
    s.policy.boundary_reencode = True
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    nearby = next(o for o in s.resolve([c.id])[c.id].nearby if o.verified and not o.dropped_pts)
    s.decisions[c.id].choice = "skip"
    assert s.resolve([c.id])[c.id].status == "skipped"
    s.decisions[c.id].choice = f"boundary:{nearby.pts}"
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "copy" and rc.outside_black


def test_auto_cut_end_stays_inside_single_black_frame(fixtures):
    s = session(fixtures["h264_longgop"], "single-frame", min_spacing_s=0)
    s.policy.boundary_reencode = True
    c = next(c for c in s.candidates if frames_of(s, c) == (200, 201))
    d = s.decisions[c.id]; d.position = "end"
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "reencode"
    assert rc.boundaries[0].pts == c.start_pts
    assert c.start_pts <= rc.boundaries[0].pts < c.end_pts


def test_auto_policy_strict_switch_and_black_removal(fixtures, tmp_path):
    s = session(fixtures["h264_longgop"], "balanced", min_spacing_s=0)
    s.policy.boundary_reencode = True
    c = next(c for c in s.candidates if frames_of(s, c) == (100, 110))
    assert s.resolve([c.id])[c.id].status == "reencode"
    project = tmp_path / "auto.bsrproj"
    s.save_project(str(project))
    assert SourceSession.load_project(str(project)).policy.boundary_reencode
    s.policy.boundary_reencode = False
    assert s.resolve([c.id])[c.id].status == "needs-review"
    s.policy.boundary_reencode = True
    s.decisions[c.id].remove_black = True
    s.decisions[c.id].choice = "reencode"
    rc = s.resolve([c.id])[c.id]
    assert rc.status == "reencode" and len(rc.smart_edges) == 2
    s = session(fixtures["mpeg2_open"], "balanced", min_spacing_s=0)
    s.policy.boundary_reencode = True
    assert any(rc.status == "reencode" for rc in s.resolve().values())
