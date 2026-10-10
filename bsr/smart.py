"""Encode only frames needed at reviewed cuts, retaining the source video codec.

Keyframe proofs bound each replacement. H.264 uses separate SPS ids; other
codecs restore the source configuration before copy resumes. Every published
output must pass compressed-payload, frame-order, join-identity and sync checks.
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


def supported(info: MediaInfo) -> tuple[bool, str]:
    v = info.video
    encoder = encoder_name(v.codec)
    if not encoder:
        return False, f"Exact boundary encoding is unavailable for {v.codec}; verified keyframe cuts remain available"
    if v.is_hdr:
        return False, "HDR boundary encoding requires preserving dynamic/mastering metadata; use verified copy cuts"
    if encoder not in av.codecs_available:
        return False, f"The bundled FFmpeg has no {encoder} encoder"
    tested = {"h264": {"yuv420p", "yuv420p10le"}, "hevc": {"yuv420p", "yuv420p10le"},
              "mpeg2video": {"yuv420p"}, "mpeg4": {"yuv420p"}, "vp8": {"yuv420p"},
              "vp9": {"yuv420p"}, "av1": {"yuv420p"}, "wmv2": {"yuv420p"}, "ffv1": {"yuv420p"},
              "mjpeg": {"yuvj420p"}, "prores": {"yuv422p10le"}}
    if v.pix_fmt not in tested.get(v.codec, set()):
        return False, f"Exact {v.codec} cuts have not been verified for {v.pix_fmt}; use verified copy cuts"
    if v.codec == "h264" and v.bit_depth > 8 and v.field_order not in (None, "unknown", "progressive"):
        return False, "Interlaced 10-bit H.264 exact cuts have not been verified; use copy cuts"
    formats = {p.name for p in av.Codec(encoder, 'w').video_formats}
    if v.pix_fmt not in formats:
        return False, f"The {v.codec} encoder cannot preserve {v.pix_fmt}; use verified copy cuts"
    if v.codec == "h264" and (v.has_b_frames or 0) > 2:
        return False, f"Source frame-reordering depth {v.has_b_frames} is outside the tested matrix"
    if v.codec not in ("h264", "mpeg2video") and v.field_order not in (None, "unknown", "progressive"):
        return False, f"Interlaced {v.codec} boundary encoding is not verified; use copy cuts"
    return True, f"{v.codec} exact cuts; unchanged pixel format, audio copied"


def encoder_name(codec):
    return {"h264": "libx264", "hevc": "libx265", "mpeg2video": "mpeg2video", "mpeg4": "mpeg4",
            "vp8": "libvpx", "vp9": "libvpx-vp9", "av1": "libsvtav1", "wmv2": "wmv2", "ffv1": "ffv1",
            "mjpeg": "mjpeg", "prores": "prores_ks"}.get(codec)


def codec_headers(codec, extra):
    """Source parameter sets for restoring the original decoder configuration at joins."""
    if codec == "h264" and extra and extra[0] == 1:
        ls, sps, pps = avcc_info(extra)
        return ls, sps + pps
    if codec == "hevc" and extra and extra[0] == 1:
        ls, pos, nals = (extra[21] & 3) + 1, 23, []
        for _ in range(extra[22]):
            typ = extra[pos] & 63; count = int.from_bytes(extra[pos+1:pos+3], "big"); pos += 3
            for _ in range(count):
                n = int.from_bytes(extra[pos:pos+2], "big"); pos += 2
                if typ in (32, 33, 34): nals.append(extra[pos:pos+n])
                pos += n
        return ls, nals
    if codec in ("h264", "hevc"):
        from .boundaries import _iter_nals
        return 0, list(_iter_nals(extra, 0))
    return 0, [extra] if extra else []


def restore_headers(codec, extra, packet):
    if codec in ("vp8", "vp9", "ffv1"):
        return packet
    if codec == "av1":
        return (extra[4:] if extra[:1] == b'\x81' else extra) + packet
    ls, nals = codec_headers(codec, extra)
    if codec in ("h264", "hevc"):
        return b''.join((len(n).to_bytes(ls, 'big') if ls else b'\0\0\0\1') + n for n in nals) + packet
    return b''.join(nals) + packet


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


def annexb_to_length(data: bytes, ls: int, drop_types=(9,), hevc=False) -> bytes:
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
        if not nal or ((nal[0] >> 1) & 63 if hevc else nal[0] & 0x1F) in drop_types:
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
        if o.verified:
            prev = p; break
    nxt = None
    for p in [k for k in keys if k > cut_pts][:16]:
        o = finder.proof(p)
        if o.verified:
            nxt = p; break
    if nxt is None:
        nxt = sp.end_pts()
    if prev is None:
        raise ValueError("No clean keyframe on both sides of the cut within reach")
    sc = SmartCut(cut_pts=int(cut_pts), prev_key_pts=prev, next_key_pts=nxt,
                  prev_idx=finder.by_pts[prev]["index"], next_idx=finder.by_pts[nxt]["index"] if nxt in finder.by_pts else len(sp))
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
    codec: str = ""
    extradata: bytes = b""

    @property
    def hash(self) -> int:
        from .media import compressed_hash
        return compressed_hash(self.codec, self.data, self.extradata)


def _unused_param_ids(extradata: bytes, sample_packets: list[bytes], ls: int) -> int:
    used = set()
    from .boundaries import _iter_nals
    _, headers = codec_headers('h264', extradata)
    nals = headers + [n for pk in sample_packets for n in _iter_nals(pk, ls)]
    for nal in nals:
        try:
            if nal and nal[0] & 31 == 7: used.add(sps_id(nal))
            elif nal and nal[0] & 31 == 8: used.update(pps_ids(nal))
        except (IndexError, ValueError):
            pass
    for k in range(1, 32):
        if k not in used:
            return k
    raise ValueError("no free SPS id")


def encode_range(info: MediaInfo, sp: StreamPackets, start_idx: int, lo_pts: int, hi_pts: int,
                 crf: int = DEFAULT_CRF, cancel=None) -> list[EncodedPacket]:
    """Decode from clean keyframe start_idx and re-encode frames with lo <= pts < hi."""
    v = info.video
    if crf < 1 or crf > 30:
        raise ValueError("Boundary quality must be CRF 1–30; lossless CRF 0 is not verified across copied-video joins")
    c = open_input(info.path)
    try:
        st = c.streams[v.index]
        st.thread_type = "AUTO"
        tb = Fraction(st.time_base)
        extradata = st.codec_context.extradata or b""
        ls, _ = codec_headers(v.codec, extradata)
        start_pts = int(sp.pts[start_idx])
        c.seek(max(0, start_pts - int(Fraction(3) / tb)), stream=st, backward=True, any_frame=False)
        expected = [int(p) for p in sorted(sp.pts[(sp.pts >= lo_pts) & (sp.pts < hi_pts) & (sp.pts != NOPTS)])]
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
            if pkt.pts is not None and pkt.pts >= hi_pts and pkt.is_keyframe and len({f.pts for f in frames}) >= len(expected):
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
        if not frames:
            return []
        enc = av.CodecContext.create(encoder_name(v.codec), "w")
        enc.width, enc.height = v.width, v.height
        enc.pix_fmt = v.pix_fmt
        enc.time_base = tb
        if v.codec == "mpeg2video":
            enc.time_base = 1 / (v.frame_rate or Fraction(25))
        if v.frame_rate:
            enc.framerate = v.frame_rate
        if v.sar and v.sar != "0/1":
            enc.sample_aspect_ratio = Fraction(v.sar.replace(":", "/"))
        for prop, val in (("color_range", v.color_range), ("colorspace", v.color_space),
                          ("color_trc", v.color_trc), ("color_primaries", v.color_primaries)):
            if val is not None:
                try: setattr(enc, prop, {"tv": 1, "pc": 2}.get(val, int(val) if val.isdigit() else 0))
                except (ValueError, AttributeError): pass
        if v.codec == "h264":
            sid = _unused_param_ids(extradata, sample, ls)

            enc.width, enc.height = v.width, v.height

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
            if v.sar and v.sar != "0/1":
                # not via x264-params: ':' is that option string's separator
                n_, d_ = (int(x) for x in v.sar.replace(":", "/").split("/"))
                enc.sample_aspect_ratio = Fraction(n_, d_)
            for k, val in (("colorprim", v.color_primaries), ("transfer", v.color_trc), ("colormatrix", v.color_space)):
                if val and val not in ("unknown", "unspecified", "reserved", "2"):
                    params.append(f"{k}={val}")
            params.append("range=" + ("tv" if (v.color_range or "tv") == "tv" else "pc"))
            prof = (v.profile or "High").lower()
            enc.options = {"preset": "slow", "x264-params": ":".join(params),
                           "profile": "high10" if v.bit_depth == 10 else ("high" if "high" in prof else ("main" if "main" in prof else "high"))}
        elif v.codec == "hevc":
            enc.options = {"preset": "fast", "crf": str(crf), "x265-params":
                           "repeat-headers=1:annexb=1:open-gop=0:keyint=100000:scenecut=0:log-level=error:pools=1"}
        elif v.codec == "vp9":
            enc.options = {"lossless": "1", "deadline": "good", "cpu-used": "4", "b": "0"}
        elif v.codec == "vp8":
            enc.options = {"crf": "4", "qmin": "0", "qmax": "4", "b": "5M", "deadline": "good", "cpu-used": "2"}
        elif v.codec == "av1":
            enc.options = {"preset": "8", "crf": str(crf), "svtav1-params": "lp=2"}
        else:
            enc.gop_size = 100000
            enc.max_b_frames = 0
            if v.codec == "mpeg4" and v.has_b_frames:
                # Keep the VOL's low_delay flag compatible with the copied video.
                # Switching to low-delay at a join discards a pending reference
                # picture; switching back can repeat the final encoded picture.
                enc.max_b_frames = 2
            enc.options = {"qmin": "1", "qmax": "2", "b": "20M"}
            if v.codec == "mpeg2video" and v.field_order in ("tt", "tb", "bb", "bt"):
                enc.options.update({"flags": "+ildct+ilme", "top": "1" if v.field_order in ("tt","tb") else "0"})
        out = []
        dur = {int(p): int(d) for p, d in zip(sp.pts, sp.duration)}

        def take(pkts):
            for p in pkts:
                data = bytes(p)
                if ls: data = annexb_to_length(data, ls, drop_types=(35,) if v.codec == "hevc" else (9,), hevc=v.codec == "hevc")
                pts = min(expected, key=lambda x: abs(Fraction(x)*tb - (Fraction(p.pts)*enc.time_base + (lo_pts*tb if v.codec == "mpeg2video" else 0))))
                out.append(EncodedPacket(pts=pts, data=data, key=bool(p.is_keyframe),
                                         duration=dur.get(pts, 0), codec=v.codec, extradata=extradata))
        for f in frames:
            nf = f.reformat(format=enc.pix_fmt) if f.format.name != enc.pix_fmt else f
            try:
                nf.pict_type = av.video.frame.PictureType.NONE
            except Exception:
                try:
                    nf.pict_type = 0
                except Exception:
                    pass
            nf.pts = round(Fraction(f.pts - (lo_pts if v.codec == "mpeg2video" else 0))*tb / enc.time_base)
            nf.time_base = enc.time_base
            take(enc.encode(nf))
        take(enc.encode(None))
        if len(out) != len(frames):
            raise RuntimeError(f"encoder produced {len(out)} packets for {len(frames)} frames")
        if not out[0].key:
            raise RuntimeError("re-encoded piece does not start with an IDR")
        return out
    finally:
        c.close()


def psnr(a: np.ndarray, b: np.ndarray, bit_depth=8) -> float:
    d = (a.astype(np.float64) - b.astype(np.float64)) ** 2
    m = d.mean()
    return 99.0 if m == 0 else 10 * np.log10(((1 << bit_depth) - 1) ** 2 / m)
