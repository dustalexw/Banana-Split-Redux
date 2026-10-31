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

import zlib
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from typing import Optional

import av
import numpy as np

from . import cache
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

_WEIGHTS: dict[int, np.ndarray] = {}


def _weights(n: int) -> np.ndarray:
    """Fixed odd 64-bit weights for the position-sensitive checksum (same in every run)."""
    w = _WEIGHTS.get(n)
    if w is None:
        w = np.random.default_rng(0x42534652).integers(1, 2**63, n, dtype=np.uint64, endpoint=False) | np.uint64(1)
        _WEIGHTS[n] = w
    return w


def frame_hash(frame) -> bytes:
    """Fingerprint of a decoded picture's exact sample values, for bit-identity checks.

    Each plane contributes CRC-32 and Adler-32 (hardware-accelerated in zlib) and a
    64-bit position-weighted sum with fixed odd weights. A difference in any single
    sample always changes the CRC and the weighted sum; unrelated pictures match by
    chance with odds far below one in 2^96. This is about 12x faster than hashing the
    planes with BLAKE2b, which made checking copy boundaries the slowest part of
    analysis. Both decodes being compared are of the same file, so there is no
    adversary to defend against.
    """
    fmt = frame.format
    bps = 2 if max(c.bits for c in fmt.components) > 8 else 1
    out = bytearray()
    for p in frame.planes:
        arr = np.frombuffer(p, np.uint8).reshape(p.height, p.line_size)
        if p.line_size != p.width * bps:
            arr = np.ascontiguousarray(arr[:, : p.width * bps])
        flat = arr.reshape(-1)
        n8 = flat.size - flat.size % 8
        words = flat[:n8].view(np.uint64)
        weighted = int(np.dot(words, _weights(words.size))) if words.size else 0
        out += zlib.crc32(flat).to_bytes(4, "little") + zlib.adler32(flat).to_bytes(4, "little")
        out += weighted.to_bytes(8, "little") + bytes(flat[n8:])
    return bytes(out)


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
    ctx.thread_type = "AUTO"   # bit-exact either way; frame threading only adds output delay (flushed)
    return ctx


def _padded_packet(data: bytes):
    """A packet whose payload is followed by FFmpeg's zeroed input padding.

    av.Packet(bytes) wraps the bytes without the padding decoders are allowed to
    read past the end of a packet, so they could see whatever memory followed and
    decode the same packets differently from run to run (seen with MPEG-4 Part 2).
    That made proofs fail at random on boundaries that are in fact safe.
    """
    pk = av.Packet(len(data))
    memoryview(pk)[:] = data
    return pk


def _decode_seq(stream, packets, tb) -> dict[int, bytes]:
    """Decode packets (list of (pts, dts, bytes, key)) with a fresh decoder; return pts->hash."""
    ctx = make_decoder(stream)
    out = {}

    def take(frames):
        for f in frames:
            if f.pts is not None:
                out[int(f.pts)] = frame_hash(f)
    for pts, dts, data, key, dur in packets:
        pk = _padded_packet(data)
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


def _proof_ranges(sp: StreamPackets, struct: dict, keys: list[int]):
    """(reference start, start-side end, reference end) decode-order indices for one keyframe."""
    i = struct["index"]
    k = keys.index(i)
    # reference starts two keyframes earlier so frames near the boundary are stable
    r_idx = keys[max(0, k - 2)]
    k2 = keys[k + 2] if k + 2 < len(keys) else len(sp)    # stop reading here
    k_ref_end = keys[k + 3] if k + 3 < len(keys) else len(sp)
    return r_idx, k2, k_ref_end


class ProofWorkspace:
    """Packets and decodes shared by the proofs of neighbouring keyframes.

    Proving one keyframe decodes a reference run of five GOPs and two fresh runs
    of two GOPs each. Keyframes next to each other (the ones before, inside and
    after a black interval) need almost the same reference and some of the very
    same fresh runs, so each is decoded once and reused. Every comparison is
    unchanged: a longer reference that starts earlier is at least as settled.
    """

    def __init__(self, path: str, info: MediaInfo, sp: StreamPackets):
        self.path, self.info, self.sp = path, info, sp
        self.c = None
        self.st = None
        self._packets: dict[int, tuple] = {}
        self._refs: list[tuple[int, int, dict]] = []
        self._fresh: dict[tuple, dict] = {}

    def open(self):
        if self.c is None:
            self.c = open_input(self.path)
            self.st = self.c.streams[self.info.video.index]
        return self.st

    def close(self):
        if self.c is not None:
            try:
                self.c.close()
            finally:
                self.c = None
        self._packets.clear(); self._refs.clear(); self._fresh.clear()

    def packets(self, lo: int, hi: int) -> list[tuple]:
        need = [j for j in range(lo, hi) if self.sp.pts[j] != NOPTS and j not in self._packets]
        if need:
            st = self.open()
            got = _read_packets(self.c, st, self.sp, min(need), max(need) + 1)
            idx = [j for j in range(min(need), max(need) + 1) if self.sp.pts[j] != NOPTS]
            self._packets.update(zip(idx, got))
        return [self._packets[j] for j in range(lo, hi) if self.sp.pts[j] != NOPTS]

    def reference(self, lo: int, hi: int) -> dict:
        """Hashes from one continuous decode that starts at or before `lo` and runs to at least `hi`."""
        for a, b, hashes in self._refs:
            if a <= lo and b >= hi:
                return hashes
        hashes = _decode_seq(self.open(), self.packets(lo, hi), self.sp.time_base)
        self._refs.append((lo, hi, hashes))
        return hashes

    def fresh(self, indices: list[int]) -> dict:
        """Hashes from a fresh decoder that is given exactly these packets."""
        key = tuple(indices)
        if key not in self._fresh:
            by = {}
            if indices:
                lo, hi = min(indices), max(indices) + 1
                live = [j for j in range(lo, hi) if self.sp.pts[j] != NOPTS]
                by = dict(zip(live, self.packets(lo, hi)))
            self._fresh[key] = _decode_seq(self.open(), [by[j] for j in indices if j in by], self.sp.time_base)
        return self._fresh[key]


def prove_boundary(path: str, info: MediaInfo, sp: StreamPackets, struct: dict,
                   all_struct: list[dict], ws: Optional[ProofWorkspace] = None) -> BoundaryOption:
    """Classify and verify one keyframe as a stream-copy boundary."""
    v = info.video
    tb = sp.time_base
    i = struct["index"]
    keys = [s["index"] for s in all_struct]
    k = keys.index(i)
    r_idx, k2, k_ref_end = _proof_ranges(sp, struct, keys)
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

    own = ws is None
    ws = ws or ProofWorkspace(path, info, sp)
    try:
        st = ws.open()
        live = [j for j in range(r_idx, k_ref_end) if sp.pts[j] != NOPTS]
        by_idx = dict(zip(live, ws.packets(r_idx, k_ref_end)))
        ra, ok_struct = random_access_kind(v.codec, by_idx[i][2], st.codec_context.extradata or b"")
        opt.random_access = ra
        if not ok_struct and opt.structure == "clean":
            opt.structure = "needs-proof"
        ref = ws.reference(r_idx, k_ref_end)
        # frames considered stable: presented at/after the keyframe following r
        stable_from = int(sp.pts[keys[min(len(keys) - 1, max(0, k - 1))]]) if k > 0 else int(sp.pts[r_idx])
        lead_set = set(lead)
        # start side
        later_pts = [int(sp.pts[j]) for j in range(k2, k_ref_end) if sp.pts[j] != NOPTS]
        win_end = min(later_pts) if later_pts else None
        got_s = ws.fresh([j for j in range(i, k2) if j not in lead_set and j in by_idx])
        exp_s = {p: h for p, h in ref.items() if p >= opt.pts and (win_end is None or p < win_end)}
        got_s_cmp = {p: h for p, h in got_s.items() if win_end is None or p < win_end}
        start_ok = bool(exp_s) and got_s_cmp == exp_s
        # end side
        bound_end = min([opt.pts] + opt.dropped_pts)
        got_e = ws.fresh([j for j in range(r_idx, i) if j in by_idx])
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
        if own:
            ws.close()


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

    def _cached_proof(self, pts: int) -> Optional[BoundaryOption]:
        if pts not in self._proofs:
            hit = cache.load_json(cache.key("proof-v1", self.info.fingerprint, int(pts)))
            if hit is not None:
                self._proofs[pts] = BoundaryOption(**hit)
        return self._proofs.get(pts)

    def prove_many(self, pts_list) -> list[BoundaryOption]:
        """Prove several nearby keyframes, decoding their shared reference only once."""
        todo = [int(p) for p in dict.fromkeys(pts_list) if self._cached_proof(int(p)) is None]
        if len(todo) > 1:
            keys = [s["index"] for s in self.struct]
            spans = [_proof_ranges(self.sp, self.by_pts[p], keys) for p in todo]
            ws = ProofWorkspace(self.path, self.info, self.sp)
            try:
                lo, hi = min(r for r, _, _ in spans), max(e for _, _, e in spans)
                if hi - lo <= 8 * max(1, max(e - r for r, _, e in spans)):
                    ws.open()
                    ws.reference(lo, hi)            # one continuous decode for all of them
                for p in todo:
                    self.proof(p, ws)
            finally:
                ws.close()
        return [self.proof(int(p)) for p in pts_list]

    def proof(self, pts: int, ws: Optional[ProofWorkspace] = None) -> BoundaryOption:
        if pts not in self._proofs:
            k = cache.key("proof-v1", self.info.fingerprint, int(pts))
            hit = cache.load_json(k)
            if hit is not None:
                self._proofs[pts] = BoundaryOption(**hit)
            else:
                o = prove_boundary(self.path, self.info, self.sp, self.by_pts[pts], self.struct, ws)
                if o.verified is not None and not o.verify_note.startswith("proof failed"):
                    cache.save_json(k, o.to_json())
                self._proofs[pts] = o
        return self._proofs[pts]

    def is_proven(self, pts: int) -> bool:
        return pts in self._proofs

    def options_for(self, start_pts: int, end_pts: int, nearby: int = 1) -> tuple[list, list, list]:
        """(inside, before, after) proven options for a black interval [start, end].

        A boundary at exactly end_pts (first non-black frame) is inside: splitting
        there keeps the whole black interval in the earlier file.
        """
        kp = self.key_pts
        inside = [int(p) for p in kp if start_pts <= p <= end_pts and p > kp[0]]
        before = [int(p) for p in kp if p < start_pts and p > kp[0]][-nearby:]
        after = [int(p) for p in kp if p > end_pts][:nearby]
        proven = self.prove_many(inside + before + after)
        return (proven[:len(inside)], proven[len(inside):len(inside) + len(before)], proven[len(inside) + len(before):])

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
