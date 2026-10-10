"""Copy-safe boundary analysis.

A stream-copy cut at presentation time P (the pts of keyframe packet i) is safe
only if:

  * start side - every frame presented at/after P decodes identically when
    decoding starts at packet i with a fresh decoder; and
  * end side   - every frame presented before P decodes identically from
    packets that precede i in decode order, with nothing from i onward.

A keyframe flag is only a hint. Open GOPs (B-frames after the I-frame in decode
order that are presented before it, i.e. "leading pictures"), multi-reference
codecs and missing parameter sets all break one side or the other. So each
boundary is classified structurally from the packet index and then *proven* by
decoding: frames from a continuous reference decode are hashed and compared
bit-for-bit with frames produced by fresh decoders that see only the packets the
two output files would contain.

Open-GOP keyframes can still be used when every leading picture lies inside the
black interval: those frames are undecodable on either side of the cut and are
dropped. That is reported explicitly (frames, duration) and requires the user's
acceptance or a configured tolerance.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from typing import Optional

import av
import numpy as np

from .media import MediaInfo, StreamPackets, NOPTS, open_input

LOOKAHEAD = 64


@dataclass
class BoundaryOption:
    pts: int                      # presentation time of the first frame after the cut (video tb)
    seconds: float
    packet_index: int             # decode-order index of the keyframe packet
    structure: str                # "clean" | "open-gop" | "needs-proof"
    dropped_pts: list = field(default_factory=list)   # undecodable leading pictures (open GOP)
    dropped_s: float = 0.0
    verified: Optional[bool] = None                   # None = not yet tested
    verify_note: str = ""
    random_access: str = ""       # e.g. "IDR", "I (no leading pictures)", "CRA"

    @property
    def copy_ok(self) -> bool:
        return bool(self.verified)

    def to_json(self):
        return asdict(self)


# ------------------------------------------------------------ bitstream helpers

def _nal_length_size(extradata: bytes) -> int:
    if extradata and len(extradata) > 4 and extradata[0] == 1:
        return (extradata[4] & 3) + 1
    return 0  # Annex B


def _hevc_nal_length_size(extradata: bytes) -> int:
    if extradata and len(extradata) > 22 and extradata[0] == 1:
        return (extradata[21] & 3) + 1
    return 0


def _iter_nals(data: bytes, length_size: int):
    if length_size:
        i, n = 0, len(data)
        while i + length_size <= n:
            ln = int.from_bytes(data[i:i + length_size], "big")
            i += length_size
            if ln <= 0 or i + ln > n:
                break
            yield data[i:i + ln]
            i += ln
    else:
        parts = data.split(b"\x00\x00\x01")
        for p in parts[1:]:
            yield p.rstrip(b"\x00")


def h264_nal_types(data: bytes, extradata: bytes) -> list[int]:
    return [nal[0] & 0x1F for nal in _iter_nals(data, _nal_length_size(extradata)) if nal]


def hevc_nal_types(data: bytes, extradata: bytes) -> list[int]:
    return [(nal[0] >> 1) & 0x3F for nal in _iter_nals(data, _hevc_nal_length_size(extradata)) if nal]


def random_access_kind(codec: str, data: bytes, extradata: bytes) -> tuple[str, bool]:
    """(description, structurally-acceptable) for a keyframe packet payload."""
    if codec == "h264":
        t = h264_nal_types(data, extradata)
        if 5 in t:
            return "IDR", True
        if 1 in t:
            return "non-IDR I (recovery point)", False   # must be proven by decode
        return "unknown", False
    if codec == "hevc":
        t = hevc_nal_types(data, extradata)
        if any(x in (19, 20) for x in t):
            return "IDR", True
        if 21 in t:
            return "CRA", False
        if any(16 <= x <= 18 for x in t):
            return "BLA", False
        return "unknown", False
    if codec in ("mpeg2video", "mpeg1video"):
        return "I", True
    return "keyframe", False


# ------------------------------------------------------------ structural pass

def keyframe_structure(sp: StreamPackets) -> list[dict]:
    """Classify every keyframe from timestamps alone (cheap, whole file)."""
    pts = sp.pts
    keys = np.nonzero(sp.key)[0]
    out = []
    n = len(pts)
    for i in keys:
        p = pts[i]
        if p == NOPTS:
            continue
        hi = n
        nxt = keys[keys > i]
        if len(nxt):
            hi = min(n, int(nxt[0]))
        hi = min(hi, i + LOOKAHEAD)
        lead = [int(j) for j in range(i + 1, hi) if pts[j] != NOPTS and pts[j] < p]
        lo = max(0, i - LOOKAHEAD)
        trail = [int(j) for j in range(lo, i) if pts[j] != NOPTS and pts[j] >= p]
        out.append({"index": int(i), "pts": int(p), "leading": lead, "trailing": trail})
    return out


# ------------------------------------------------------------ decode proof

def frame_hash(frame) -> bytes:
    h = hashlib.blake2b(digest_size=12)
    fmt = frame.format
    bps = 2 if max(c.bits for c in fmt.components) > 8 else 1
    for p in frame.planes:
        arr = np.frombuffer(p, np.uint8).reshape(p.height, p.line_size)[:, : p.width * bps]
        h.update(np.ascontiguousarray(arr).tobytes())
    return h.digest()


def make_decoder(stream):
    cc = stream.codec_context
    ctx = av.CodecContext.create(cc.codec.name, "r")
    if cc.extradata:
        ctx.extradata = cc.extradata
    try:
        ctx.width, ctx.height = cc.width, cc.height
    except Exception:
        pass
    try:
        ctx.pkt_timebase = stream.time_base
    except Exception:
        pass
    ctx.thread_type = "NONE"   # deterministic, no frame-thread delay surprises
    return ctx


def _decode_seq(stream, packets, tb) -> dict[int, bytes]:
    """Decode packets (list of (pts, dts, bytes, key)) with a fresh decoder; return pts->hash."""
    ctx = make_decoder(stream)
    out = {}

    def take(frames):
        for f in frames:
            if f.pts is not None:
                out[int(f.pts)] = frame_hash(f)
    for pts, dts, data, key, dur in packets:
        pk = av.Packet(data)
        pk.pts = pts
        pk.dts = dts
        pk.time_base = tb
        if dur:
            pk.duration = dur
        try:
            take(ctx.decode(pk))
        except av.error.InvalidDataError:
            continue
    try:
        take(ctx.decode(None))
    except Exception:
        pass
    return out


def _read_packets(c, stream, sp: StreamPackets, lo_idx: int, hi_idx: int):
    """Read video packets with decode-order index in [lo_idx, hi_idx) (matched by pts)."""
    want = {int(sp.pts[j]): j for j in range(lo_idx, hi_idx) if sp.pts[j] != NOPTS}
    first_pts = int(min(want)) if want else 0
    tb = stream.time_base
    c.seek(max(0, first_pts - int(round(Fraction(2) / tb))), stream=stream, backward=True, any_frame=False)
    got = {}
    max_pts = max(want) if want else 0
    for pkt in c.demux(stream):
        if pkt.size == 0:
            continue
        if pkt.pts is None:
            continue
        j = want.get(int(pkt.pts))
        if j is not None:
            got[j] = (int(pkt.pts), None if pkt.dts is None else int(pkt.dts), bytes(pkt),
                      bool(pkt.is_keyframe), int(pkt.duration or 0))
        if len(got) == len(want):
            break
        if pkt.pts > max_pts + int(round(Fraction(5) / tb)):
            break
    missing = set(want.values()) - set(got)
    if missing:
        raise RuntimeError(f"could not re-read {len(missing)} packets for boundary proof")
    return [got[j] for j in sorted(got)]


def prove_boundary(path: str, info: MediaInfo, sp: StreamPackets, struct: dict,
                   all_struct: list[dict]) -> BoundaryOption:
    """Classify and verify one keyframe as a stream-copy boundary."""
    v = info.video
    tb = sp.time_base
    i = struct["index"]
    keys = [s["index"] for s in all_struct]
    k = keys.index(i)
    # reference starts two keyframes earlier so frames near the boundary are stable
    r_idx = keys[max(0, k - 2)]
    k2 = keys[k + 2] if k + 2 < len(keys) else len(sp)    # stop reading here
    k_ref_end = keys[k + 3] if k + 3 < len(keys) else len(sp)
    lead = struct["leading"]
    opt = BoundaryOption(pts=struct["pts"], seconds=float(struct["pts"] * tb), packet_index=i,
                         structure="clean" if not lead and not struct["trailing"] else "open-gop",
                         dropped_pts=[int(sp.pts[j]) for j in lead])
    if lead:
        opt.dropped_s = float(sum(int(sp.duration[j]) for j in lead) * tb)
    if struct["trailing"]:
        opt.verified = False
        opt.verify_note = "frames before this keyframe are presented after it"
        return opt

    c = open_input(path)
    try:
        st = c.streams[v.index]
        pk = _read_packets(c, st, sp, r_idx, k_ref_end)
        idx_of = list(range(r_idx, k_ref_end))
        by_idx = dict(zip(idx_of, pk))
        ra, ok_struct = random_access_kind(v.codec, by_idx[i][2], st.codec_context.extradata or b"")
        opt.random_access = ra
        if not ok_struct and opt.structure == "clean":
            opt.structure = "needs-proof"
        ref = _decode_seq(st, pk, tb)
        # frames considered stable: presented at/after the keyframe following r
        stable_from = int(sp.pts[keys[min(len(keys) - 1, max(0, k - 1))]]) if k > 0 else int(sp.pts[r_idx])
        lead_set = set(lead)
        # start side
        start_pk = [by_idx[j] for j in range(i, k2) if j not in lead_set]
        later_pts = [int(sp.pts[j]) for j in range(k2, k_ref_end) if sp.pts[j] != NOPTS]
        win_end = min(later_pts) if later_pts else None
        got_s = _decode_seq(st, start_pk, tb)
        exp_s = {p: h for p, h in ref.items() if p >= opt.pts and (win_end is None or p < win_end)}
        got_s_cmp = {p: h for p, h in got_s.items() if win_end is None or p < win_end}
        start_ok = bool(exp_s) and got_s_cmp == exp_s
        # end side
        end_pk = [by_idx[j] for j in range(r_idx, i)]
        bound_end = min([opt.pts] + opt.dropped_pts)
        got_e = _decode_seq(st, end_pk, tb)
        exp_e = {p: h for p, h in ref.items() if stable_from <= p < bound_end}
        got_e_cmp = {p: h for p, h in got_e.items() if p >= stable_from}
        end_ok = bool(exp_e) and got_e_cmp == exp_e
        notes = []
        if not start_ok:
            miss = len(set(exp_s) - set(got_s_cmp)); bad = sum(1 for p in exp_s if p in got_s_cmp and got_s_cmp[p] != exp_s[p])
            extra = len(set(got_s_cmp) - set(exp_s))
            notes.append(f"start side: {miss} missing, {bad} differing, {extra} unexpected frames")
        if not end_ok:
            miss = len(set(exp_e) - set(got_e_cmp)); bad = sum(1 for p in exp_e if p in got_e_cmp and got_e_cmp[p] != exp_e[p])
            extra = len(set(got_e_cmp) - set(exp_e))
            notes.append(f"end side: {miss} missing, {bad} differing, {extra} unexpected frames")
        # leading pictures really are lost on both sides?
        if lead:
            lost = [p for p in opt.dropped_pts if p in got_s or p in got_e]
            if lost:
                notes.append(f"{len(lost)} leading pictures unexpectedly decodable")
        opt.verified = start_ok and end_ok
        opt.verify_note = "; ".join(notes) if notes else (
            f"decoded {len(exp_s)} frames after and {len(exp_e)} before the cut bit-identically")
        if opt.verified and opt.structure == "needs-proof":
            opt.structure = "clean"
        return opt
    except Exception as e:  # never treat an unprovable boundary as safe
        opt.verified = False
        opt.verify_note = f"proof failed: {e}"
        return opt
    finally:
        c.close()


class BoundaryFinder:
    """Caches structural info and proofs for one source."""

    def __init__(self, path: str, info: MediaInfo, sp: StreamPackets):
        self.path, self.info, self.sp = path, info, sp
        self.struct = keyframe_structure(sp)
        self.by_pts = {s["pts"]: s for s in self.struct}
        self.key_pts = np.array(sorted(self.by_pts), dtype=np.int64)
        self._proofs: dict[int, BoundaryOption] = {}

    def gop_stats(self) -> dict:
        kp = self.key_pts
        tb = self.sp.time_base
        gaps = np.diff(kp) * float(tb) if len(kp) > 1 else np.array([0.0])
        n_open = sum(1 for s in self.struct if s["leading"])
        return {"keyframes": int(len(kp)), "median_gop_s": float(np.median(gaps)), "max_gop_s": float(gaps.max()),
                "open_gop_keyframes": n_open, "closed_keyframes": int(len(kp)) - n_open}

    def proof(self, pts: int) -> BoundaryOption:
        if pts not in self._proofs:
            self._proofs[pts] = prove_boundary(self.path, self.info, self.sp, self.by_pts[pts], self.struct)
        return self._proofs[pts]

    def options_for(self, start_pts: int, end_pts: int, nearby: int = 1) -> tuple[list, list, list]:
        """(inside, before, after) proven options for a black interval [start, end].

        A boundary at exactly end_pts (first non-black frame) is inside: splitting
        there keeps the whole black interval in the earlier file.
        """
        kp = self.key_pts
        inside = [int(p) for p in kp if start_pts <= p <= end_pts and p > kp[0]]
        before = [int(p) for p in kp if p < start_pts and p > kp[0]][-nearby:]
        after = [int(p) for p in kp if p > end_pts][:nearby]
        return ([self.proof(p) for p in inside], [self.proof(p) for p in before], [self.proof(p) for p in after])

    def nearest_verified(self, t_pts: int, direction: int, limit: int = 8) -> Optional[BoundaryOption]:
        kp = self.key_pts
        seq = [int(p) for p in kp if (p < t_pts if direction < 0 else p > t_pts) and p > kp[0]]
        if direction < 0:
            seq = seq[::-1]
        for p in seq[:limit]:
            o = self.proof(p)
            if o.verified and not o.dropped_pts:
                return o
        return None
