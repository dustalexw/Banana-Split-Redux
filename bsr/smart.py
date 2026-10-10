"""Boundary re-encoding ("smart rendering") for a narrow, tested matrix.

Supported (see docs/SUPPORT_MATRIX.md):
    H.264, 8-bit 4:2:0, in Matroska; progressive or interlaced (re-encoded as MBAFF
    with the source field order). Encoder: libx264 bundled with PyAV.

For an exact cut at frame T between clean keyframes Kp <= T < Kn:
    earlier part: copy packets up to the last clean decode-order point before T
                  (no frame reordering across it), re-encode only the few frames
                  from there to T (often none)
    later part:   re-encode frames [T, Kn), copy packets from Kn on
Only the frames between the two clean IDR boundaries around the cut are
re-encoded. The re-encoded pieces start with their own IDR and carry their own
SPS/PPS in-band under ids that do not occur in the source, so the decoder never
confuses them with the original parameter sets; the original stream resumes at a
clean IDR. Every join is verified by decoding: copied frames after/before the
join must be bit-identical to the source, re-encoded frames are measured (PSNR).
This is NOT lossless for the re-encoded frames.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

import av
import numpy as np

from .media import MediaInfo, StreamPackets, NOPTS, open_input

DEFAULT_CRF = 12


def _matroska(info: MediaInfo) -> bool:
    return "matroska" in info.format_name


def supported(info: MediaInfo) -> tuple[bool, str]:
    v = info.video
    if v.codec != "h264":
        return False, f"Boundary re-encoding is not available for {v.codec} (tested matrix: H.264 in Matroska)"
    if not _matroska(info):
        return False, "Boundary re-encoding is only tested for Matroska output"
    if v.pix_fmt not in ("yuv420p", "yuvj420p"):
        return False, f"Boundary re-encoding is only tested for 8-bit 4:2:0 (this file: {v.pix_fmt})"
    if "libx264" not in av.codecs_available:
        return False, "The bundled FFmpeg has no H.264 encoder"
    if (v.has_b_frames or 0) > 2:
        return False, f"Source frame-reordering depth {v.has_b_frames} is outside the tested matrix"
    return True, "H.264 8-bit 4:2:0 in Matroska — tested"


# --------------------------------------------------------------- bitstream utils

def _ue(bits, pos):
    z = 0
    while bits[pos] == 0:
        z += 1; pos += 1
    pos += 1
    val = (1 << z) - 1
    for i in range(z):
        val += bits[pos] << (z - 1 - i); pos += 1
    return val, pos


def _rbsp_bits(nal: bytes):
    out = bytearray()
    zeros = 0
    for b in nal:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return np.unpackbits(np.frombuffer(bytes(out), np.uint8))


def avcc_info(extradata: bytes):
    """(nal_length_size, [sps nal bytes], [pps nal bytes]) from an avcC record."""
    if not extradata or extradata[0] != 1:
        return 0, [], []
    ls = (extradata[4] & 3) + 1
    n_sps = extradata[5] & 31
    p = 6
    sps, pps = [], []
    for _ in range(n_sps):
        ln = int.from_bytes(extradata[p:p + 2], "big"); p += 2
        sps.append(extradata[p:p + ln]); p += ln
    n_pps = extradata[p]; p += 1
    for _ in range(n_pps):
        ln = int.from_bytes(extradata[p:p + 2], "big"); p += 2
        pps.append(extradata[p:p + ln]); p += ln
    return ls, sps, pps


def sps_id(nal: bytes) -> int:
    bits = _rbsp_bits(nal[1:])
    return _ue(bits, 24)[0]


def pps_ids(nal: bytes) -> tuple[int, int]:
    bits = _rbsp_bits(nal[1:])
    pid, pos = _ue(bits, 0)
    sid, _ = _ue(bits, pos)
    return pid, sid


def annexb_to_length(data: bytes, ls: int, drop_types=(9,)) -> bytes:
    """Annex B (start codes) -> length-prefixed NAL units, dropping access unit delimiters."""
    out = bytearray()
    i, n = 0, len(data)
    starts = []
    while i < n - 2:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            starts.append(i + 3); i += 3
        else:
            i += 1
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        nal = data[s:e]
        while nal and nal[-1] == 0:   # trailing zero of a 4-byte start code
            nal = nal[:-1]
        if not nal or (nal[0] & 0x1F) in drop_types:
            continue
        out += len(nal).to_bytes(ls, "big") + nal
    return bytes(out)


# --------------------------------------------------------------- planning

@dataclass
class SmartCut:
    cut_pts: int          # first frame of the later part
    prev_key_pts: int     # clean keyframe at/before the cut (start of re-encode in the earlier part)
    next_key_pts: int     # clean keyframe after the cut (copy resumes here in the later part)
    prev_idx: int
    next_idx: int
    tail_frames: int = 0  # frames re-encoded at the end of the earlier part
    head_frames: int = 0  # frames re-encoded at the start of the later part
    tail_s: float = 0.0
    head_s: float = 0.0
    tail_from_pts: int = 0   # first re-encoded frame of the earlier part (== cut_pts: nothing re-encoded)

    def to_json(self):
        return dict(self.__dict__)


def plan_cut(finder, cut_pts: int, frame_pts: np.ndarray, frame_dur: np.ndarray) -> SmartCut:
    sp = finder.sp
    tb = sp.time_base
    keys = [int(p) for p in finder.key_pts]
    first = keys[0]
    prev = None
    for p in reversed([k for k in keys if k <= cut_pts]):
        if p == first:
            prev = p; break
        o = finder.proof(p)
        if o.verified and not o.dropped_pts:
            prev = p; break
    nxt = None
    for p in [k for k in keys if k > cut_pts][:16]:
        o = finder.proof(p)
        if o.verified and not o.dropped_pts:
            nxt = p; break
    if prev is None or nxt is None:
        raise ValueError("No clean keyframe on both sides of the cut within reach")
    sc = SmartCut(cut_pts=int(cut_pts), prev_key_pts=prev, next_key_pts=nxt,
                  prev_idx=finder.by_pts[prev]["index"], next_idx=finder.by_pts[nxt]["index"])
    # The earlier part can keep copying up to the last decode-order position L whose
    # packets present exactly the frames before some time T_L <= cut (no reordering
    # across L). Frames before L never reference later packets, so they decode
    # identically; only frames [T_L, cut) need re-encoding.
    pts = sp.pts
    lo, hi = sc.prev_idx, sc.next_idx
    seg_pts = pts[lo:hi]
    best_L, best_T = lo, prev
    run_max = None
    suffix_min = np.minimum.accumulate(seg_pts[::-1])[::-1] if len(seg_pts) else seg_pts
    for k in range(1, len(seg_pts) + 1):
        run_max = seg_pts[k - 1] if run_max is None else max(run_max, seg_pts[k - 1])
        if run_max >= cut_pts:
            break
        nxt_min = int(suffix_min[k]) if k < len(seg_pts) else int(nxt)
        if run_max < nxt_min:
            best_L, best_T = lo + k, min(nxt_min, int(cut_pts))
    sc.tail_from_pts = int(best_T)
    tail = (frame_pts >= sc.tail_from_pts) & (frame_pts < cut_pts)
    head = (frame_pts >= cut_pts) & (frame_pts < nxt)
    sc.tail_frames, sc.head_frames = int(tail.sum()), int(head.sum())
    sc.tail_s = float(int(frame_dur[tail].sum()) * tb)
    sc.head_s = float(int(frame_dur[head].sum()) * tb)
    return sc


# --------------------------------------------------------------- encoding

@dataclass
class EncodedPacket:
    pts: int
    data: bytes
    key: bool
    duration: int

    @property
    def hash(self) -> int:
        return int.from_bytes(hashlib.blake2b(self.data, digest_size=8).digest(), "little")


def _unused_param_ids(extradata: bytes, sample_packets: list[bytes], ls: int) -> int:
    used = set()
    _, sps, pps = avcc_info(extradata)
    for s in sps:
        try:
            used.add(sps_id(s))
        except Exception:
            pass
    for pk in sample_packets:
        i = 0
        while i + ls <= len(pk):
            n = int.from_bytes(pk[i:i + ls], "big"); i += ls
            nal = pk[i:i + n]; i += n
            if nal and (nal[0] & 0x1F) == 7:
                try:
                    used.add(sps_id(nal))
                except Exception:
                    pass
    for k in range(1, 32):
        if k not in used:
            return k
    raise ValueError("no free SPS id")


def encode_range(info: MediaInfo, sp: StreamPackets, start_idx: int, lo_pts: int, hi_pts: int,
                 crf: int = DEFAULT_CRF, cancel=None) -> list[EncodedPacket]:
    """Decode from clean keyframe start_idx and re-encode frames with lo <= pts < hi."""
    v = info.video
    c = open_input(info.path)
    try:
        st = c.streams[v.index]
        st.thread_type = "AUTO"
        tb = Fraction(st.time_base)
        extradata = st.codec_context.extradata or b""
        ls, sps, _ = avcc_info(extradata)
        if not ls:
            raise ValueError("H.264 stream without avcC configuration is not supported")
        start_pts = int(sp.pts[start_idx])
        c.seek(start_pts, stream=st, backward=True, any_frame=False)
        frames = []
        sample = []
        for pkt in c.demux(st):
            if pkt.size == 0:
                continue
            if pkt.pts is not None and pkt.is_keyframe and len(sample) < 2:
                sample.append(bytes(pkt))
            for f in pkt.decode():
                if f.pts is None:
                    continue
                if lo_pts <= f.pts < hi_pts:
                    frames.append(f)
            if pkt.pts is not None and pkt.pts >= hi_pts and pkt.is_keyframe:
                break
            if cancel is not None and cancel.is_set():
                raise RuntimeError("cancelled")
        # flush remaining delayed frames
        try:
            for f in st.codec_context.decode(None):
                if f.pts is not None and lo_pts <= f.pts < hi_pts:
                    frames.append(f)
        except Exception:
            pass
        frames.sort(key=lambda f: f.pts)
        seen, uniq = set(), []
        for f in frames:
            if f.pts not in seen:
                seen.add(f.pts); uniq.append(f)
        frames = uniq
        expected = [int(p) for p in sorted(sp.pts[(sp.pts >= lo_pts) & (sp.pts < hi_pts) & (sp.pts != NOPTS)])]
        if [int(f.pts) for f in frames] != expected:
            raise RuntimeError(f"decoded {len(frames)} frames for re-encoding, expected {len(expected)}")
        sid = _unused_param_ids(extradata, sample, ls)
        enc = av.CodecContext.create("libx264", "w")
        enc.width, enc.height = v.width, v.height
        enc.pix_fmt = "yuv420p"
        enc.time_base = tb
        if v.frame_rate:
            enc.framerate = v.frame_rate
        try:
            enc.color_range = 1 if (v.color_range or "tv") == "tv" else 2
        except Exception:
            pass
        # The re-encoded piece must declare the same frame-reordering depth as the
        # source: decoders keep their reorder buffer across the SPS switch, and a
        # mismatch makes them silently drop pictures after the join (found by
        # validation on real interlaced material).
        reorder = int(v.has_b_frames or 0)
        if reorder == 0:
            bf = ["bframes=0"]
        elif reorder == 1:
            bf = ["bframes=2", "b-pyramid=none"]
        elif reorder == 2:
            bf = ["bframes=3", "b-pyramid=normal"]
        else:
            raise ValueError(f"source reorder depth {reorder} is outside the tested matrix")
        params = [f"sps-id={sid}", "keyint=100000", "min-keyint=100000", "scenecut=0", "open-gop=0",
                  "repeat-headers=1", "aud=0", f"crf={crf}", "ref=4"] + bf
        fo = (v.field_order or "progressive")
        if fo in ("tt", "tb"):
            params += ["interlaced=1", "tff=1"]
        elif fo in ("bb", "bt"):
            params += ["interlaced=1", "bff=1"]
        if v.sar and v.sar not in ("0/1", "1/1"):
            # not via x264-params: ':' is that option string's separator
            n_, d_ = (int(x) for x in v.sar.replace(":", "/").split("/"))
            enc.sample_aspect_ratio = Fraction(n_, d_)
        for k, val in (("colorprim", v.color_primaries), ("transfer", v.color_trc), ("colormatrix", v.color_space)):
            if val and val not in ("unknown", "unspecified", "reserved", "2"):
                params.append(f"{k}={val}")
        params.append("range=" + ("tv" if (v.color_range or "tv") == "tv" else "pc"))
        prof = (v.profile or "High").lower()
        enc.options = {"preset": "slow", "x264-params": ":".join(params),
                       "profile": "high" if "high" in prof else ("main" if "main" in prof else "high")}
        out = []
        dur = {int(p): int(d) for p, d in zip(sp.pts, sp.duration)}

        def take(pkts):
            for p in pkts:
                data = annexb_to_length(bytes(p), ls)
                out.append(EncodedPacket(pts=int(p.pts), data=data, key=bool(p.is_keyframe),
                                         duration=dur.get(int(p.pts), 0)))
        for f in frames:
            nf = f.reformat(format="yuv420p") if f.format.name != "yuv420p" else f
            try:
                nf.pict_type = av.video.frame.PictureType.NONE
            except Exception:
                try:
                    nf.pict_type = 0
                except Exception:
                    pass
            nf.pts = f.pts
            nf.time_base = tb
            take(enc.encode(nf))
        take(enc.encode(None))
        if len(out) != len(frames):
            raise RuntimeError(f"encoder produced {len(out)} packets for {len(frames)} frames")
        if not out[0].key:
            raise RuntimeError("re-encoded piece does not start with an IDR")
        return out
    finally:
        c.close()


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    d = (a.astype(np.float64) - b.astype(np.float64)) ** 2
    m = d.mean()
    return 99.0 if m == 0 else 10 * np.log10(255.0 ** 2 / m)
