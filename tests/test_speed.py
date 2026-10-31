"""Faster analysis must not change what is detected or proven."""
import os
from fractions import Fraction

import av
import numpy as np

import fixturegen
from bsr import detect
from bsr.boundaries import BoundaryFinder, frame_hash, _padded_packet
from bsr.media import probe, build_packet_index


def _exact(path, info):
    """Every frame measured at full resolution (the coarse pre-pass switched off)."""
    old = detect.COARSE_STEP
    detect.COARSE_STEP = 10 ** 6
    try:
        return detect.analyze(path, info, use_cache=False)
    finally:
        detect.COARSE_STEP = old


def test_frames_that_could_be_black_are_measured_exactly(tmp_path):
    src = str(tmp_path / "src.mkv")
    fixturegen.make(src, frames=500)
    info = probe(src)
    fast, exact = detect.analyze(src, info, use_cache=False), _exact(src, info)
    assert np.array_equal(fast.pts, exact.pts)
    maybe_black = exact.frac[:, -1] >= detect.EXACT_FROM
    assert maybe_black.any()
    assert np.array_equal(fast.frac[maybe_black], exact.frac[maybe_black])
    assert np.abs(fast.frac - exact.frac).max() < 0.02 and np.abs(fast.mean - exact.mean).max() < 0.02
    for name, preset in detect.PRESETS.items():
        a = [(c.start_pts, c.end_pts, c.kind) for c in detect.find_candidates(fast, preset)]
        b = [(c.start_pts, c.end_pts, c.kind) for c in detect.find_candidates(exact, preset)]
        assert a == b, name


def test_frame_fingerprint_sees_a_single_changed_sample():
    frame = av.VideoFrame.from_ndarray(np.full((240, 320, 3), 90, np.uint8), format="rgb24").reformat(format="yuv420p")
    before = frame_hash(frame)
    y = np.frombuffer(frame.planes[0], np.uint8).reshape(240, frame.planes[0].line_size).copy()
    y[123, 201] ^= 1
    changed = av.VideoFrame.from_ndarray(y[:, :320], format="gray").reformat(format="yuv420p")
    changed.planes[1].update(bytes(frame.planes[1])); changed.planes[2].update(bytes(frame.planes[2]))
    assert frame_hash(changed) != before
    assert frame_hash(frame) == before


def test_padded_packets_keep_their_payload():
    data = bytes(range(200))
    assert bytes(_padded_packet(data)) == data


def test_mpeg4_boundary_proofs_are_repeatable(tmp_path):
    src = str(tmp_path / "s.mp4")
    fixturegen.make(src, codec="mpeg4", container="mp4", gop=250, bframes=0, frames=300, sar=Fraction(1, 1))
    info = probe(src)
    sp = build_packet_index(src)[info.video.index]
    os.environ["BSR_NO_CACHE"] = "1"
    try:
        runs = []
        for _ in range(3):
            f = BoundaryFinder(src, info, sp)
            runs.append([(o.pts, o.verified) for o in f.prove_many([int(p) for p in f.key_pts[1:]])])
    finally:
        del os.environ["BSR_NO_CACHE"]
    assert runs[0] == runs[1] == runs[2]
    assert all(ok for _, ok in runs[0])
