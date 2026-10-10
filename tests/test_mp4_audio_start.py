"""Audible audio before the first picture must never become MP4 priming."""
from fractions import Fraction

import av
import numpy as np
import pytest

from fixturegen import make
from bsr.export import default_streams, plan_segment, write_segment
from bsr.media import build_packet_index, probe
from bsr.planner import Segment
from bsr.validate import validate_output


def offset_source(tmp_path, ext, keep_priming, offset, subtitles=False):
    raw = tmp_path / "encoded.mp4"
    make(str(raw), container="mp4", audio=("aac", "aac"), frames=150, gop=25)
    path = tmp_path / ("offset." + ext)
    with av.open(str(raw)) as inp, av.open(str(path), "w", format=ext,
                                        options={"avoid_negative_ts": "disabled"}) as out:
        mapping = {s.index: out.add_stream_from_template(s, opaque=True) for s in inp.streams}
        if subtitles:
            subtitle = out.add_mux_stream("mov_text")
            subtitle.time_base = Fraction(1, 1000000)
            subtitle.metadata["language"] = "eng"
        for packet in inp.demux():
            if packet.pts is None:
                continue
            si, tb = packet.stream_index, packet.time_base
            if packet.stream.type == "audio" and packet.pts < 0 and not keep_priming:
                continue
            if packet.stream.type == "video":
                packet.pts += round(offset / tb)
                packet.dts += round(offset / tb)
            packet.stream = mapping[si]
            out.mux(packet)
        if subtitles:
            text = b"Opening caption"
            packet = av.Packet(len(text).to_bytes(2, "big") + text)
            packet.pts = packet.dts = 1000000
            packet.duration = 2500000
            packet.time_base = subtitle.time_base
            packet.stream = subtitle
            out.mux(packet)
    return path


def first_part(path):
    info = probe(str(path))
    idx = build_packet_index(str(path))
    video = idx[info.video.index]
    start = int(video.pts.min())
    end = min(int(p) for p in video.pts[video.key] if p * video.time_base > 3)
    segment = Segment(1, start, end, float(start * video.time_base), float(end * video.time_base), "First")
    streams = default_streams(info)
    return info, idx, plan_segment(info, idx, segment, streams, "." + path.suffix[1:]), streams


def pcm_before(path, audio_position, end=None):
    with av.open(str(path)) as container:
        stream = container.streams.audio[audio_position]
        frames = []
        for frame in container.decode(stream):
            if end is not None and frame.pts * frame.time_base >= end:
                break
            frames.append(frame.to_ndarray())
        return np.concatenate(frames, axis=1)


@pytest.mark.parametrize("ext", ["mp4", "mov"])
@pytest.mark.parametrize("keep_priming", [False, True])
@pytest.mark.parametrize("offset", [Fraction(83, 1000), Fraction(1, 12)])
def test_audio_start_and_real_priming_preserved(tmp_path, ext, keep_priming, offset):
    source = offset_source(tmp_path, ext, keep_priming, offset)
    info, idx, plan, streams = first_part(source)
    output = tmp_path / ("part." + ext)
    assert plan.timestamp_shift > 0
    stats = write_segment(info, idx, plan, streams, ext, str(output))
    assert stats["encoders_created"] == 0
    report = validate_output(info, idx, plan, streams, str(output))
    assert report["ok"], report["failures"]
    actual = probe(str(output))
    for position, (src, dst) in enumerate(zip(info.of_type("audio"), actual.of_type("audio"))):
        assert dst.initial_padding == src.initial_padding
        # Copying bytes alone is insufficient: MP4 can instruct the decoder to
        # discard them. Check every decoded sample, including the opening tone.
        np.testing.assert_array_equal(pcm_before(output, position),
                                      pcm_before(source, position, Fraction(plan.segment.end_pts) * info.video.time_base))


def test_validator_rejects_unintended_mp4_audio_trim(tmp_path):
    source = offset_source(tmp_path, "mp4", False, Fraction(83, 1000))
    info, idx, plan, streams = first_part(source)
    plan.timestamp_shift = Fraction(0)  # reproduce the old export behavior
    output = tmp_path / "trimmed.mp4"
    write_segment(info, idx, plan, streams, "mp4", str(output))
    report = validate_output(info, idx, plan, streams, str(output))
    assert not report["ok"]
    assert not report["checks"]["audio decodes & sync"]["ok"]
    assert probe(str(output)).of_type("audio")[0].initial_padding > 0


def test_shift_keeps_caption_timing_and_validates_blank_mp4_padding(tmp_path):
    source = offset_source(tmp_path, "mp4", False, Fraction(83, 1000), subtitles=True)
    info, idx, plan, streams = first_part(source)
    output = tmp_path / "captions.mp4"
    write_segment(info, idx, plan, streams, "mp4", str(output))
    report = validate_output(info, idx, plan, streams, str(output))
    assert report["ok"], report["failures"]
    assert "container-generated blank subtitle padding" in report["checks"]["packets byte-identical"]["detail"]
    with av.open(str(output)) as container:
        subtitle = container.streams.subtitles[0]
        cues = [p for p in container.demux(subtitle) if bytes(p).endswith(b"Opening caption")]
        assert len(cues) == 1
        assert float(cues[0].pts * cues[0].time_base) == pytest.approx(1, abs=.001)

    # A changed subtitle payload must still fail; leading blank framing is the
    # only new allowance, not a relaxation of cue preservation.
    subtitle_index = info.of_type("subtitle")[0].index
    row = next(j for j, _, _ in plan.sub_events[subtitle_index] if idx[subtitle_index].size[j] > 2)
    idx[subtitle_index].hash[row] ^= np.uint64(1)
    assert not validate_output(info, idx, plan, streams, str(output))["ok"]
