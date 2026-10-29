"""Preservation exports: compressed-packet copying with optional boundary encoding.

The plan determines every packet and replacement frame before writing. Strict
mode creates no encoder. Lossless framing conversions, decoder-header restores
and common timestamp shifts are reported and independently verified.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from typing import Callable, Optional

import av
import numpy as np

from . import ENGINE_VERSION
from .cache import tool_versions
from .media import (MediaInfo, StreamPackets, NOPTS, Cancelled, is_stale_tag, open_input, fmt_time,
                    BITMAP_SUB_CODECS)
from .planner import Plan, Segment, safe_filename


@dataclass
class ExportOptions:
    out_dir: str
    streams: Optional[list] = None        # input stream indices to include (None = all supported)
    container: Optional[str] = None       # None = same family as source
    validate: bool = True
    full_decode_check: bool = True
    crf: Optional[int] = None             # quality for boundary re-encoding (libx264 CRF)


@dataclass
class TrackPlan:
    stream_index: int
    type: str
    codec: str
    packets: int = 0
    bytes: int = 0
    first_pts_s: Optional[float] = None    # relative to output start
    last_end_s: Optional[float] = None
    treatment: str = "Copied"
    notes: list = field(default_factory=list)


@dataclass
class SegmentPlan:
    segment: Segment
    filename: str
    tracks: list
    wanted: dict            # stream_index -> set of packet indices (into the index)
    sub_events: dict        # stream_index -> list of (idx, new_pts, new_duration)
    video_dts: dict         # packet index -> regenerated dts (source tb)
    chapters: list
    est_bytes: int = 0
    is_first: bool = True
    head: Optional[dict] = None            # boundary re-encoding at the start (SmartCut)
    tail: Optional[dict] = None            # boundary re-encoding at the end (SmartCut)
    head_pts: list = field(default_factory=list)   # source pts of re-encoded frames
    tail_pts: list = field(default_factory=list)
    reenc_dts: dict = field(default_factory=dict)   # source pts -> dts for re-encoded packets
    reencoded: dict = field(default_factory=dict)   # filled when written: hashes, counts, durations
    restored_hashes: dict = field(default_factory=dict)  # original decoder headers prepended at smart join
    timestamp_shift: Fraction = Fraction(0)  # shared shift avoids negative presentation timestamps
    boundary_seconds: float = 0.0

    @property
    def reencode_s(self) -> float:
        return self.boundary_seconds

    def summary(self):
        return {"n": self.segment.n, "file": self.filename, "start_s": self.segment.start_s,
                "end_s": self.segment.end_s, "duration_s": self.segment.duration_s,
                "dropped_frames": len(self.segment.drop_video_pts),
                "tracks": [asdict(t) for t in self.tracks], "chapters": self.chapters,
                "est_bytes": self.est_bytes, "timestamp_shift_s": float(self.timestamp_shift),
                "reencode_estimate": {"video_frames": len(self.head_pts) + len(self.tail_pts),
                                      "video_s": round(self.reencode_s, 3), "audio_samples": 0},
                "reencoded": {k: v for k, v in self.reencoded.items() if k not in ("head_hashes", "tail_hashes")}}


def output_format(info: MediaInfo, requested: Optional[str] = None) -> tuple[str, str]:
    if requested:
        return requested, {"matroska": ".mkv", "mp4": ".mp4", "mov": ".mov"}.get(requested, ".mkv")
    f = info.format_name
    if "matroska" in f or "webm" in f:
        return "matroska", ".mkv"
    if f == "asf":
        return "asf", ".wmv"
    if "mp4" in f or "mov" in f:
        ext = os.path.splitext(info.path)[1].lower()
        return ("mov", ".mov") if ext == ".mov" else ("mp4", ".mp4")
    return "matroska", ".mkv"   # preservation-friendly default for anything else


def default_streams(info: MediaInfo) -> list[int]:
    out = []
    for s in info.streams:
        if s.type in ("video", "audio", "attachment"):
            out.append(s.index)
        elif s.type == "subtitle" and s.is_text_sub:
            out.append(s.index)
    return out


def preservation_report(info: MediaInfo, streams: list[int], container: str) -> list[str]:
    """Things that cannot be preserved as-is, reported before export."""
    msgs = []
    if container == "matroska" and "matroska" not in info.format_name:
        msgs.append("Output uses MKV to preserve the selected video and audio codecs; compressed payloads are copied with lossless container-framing conversion")
    if info.format_name == "avi" and info.video.has_b_frames:
        msgs.append("AVI B-frame presentation times are recovered from decoded frame order; compressed picture data is unchanged")
    for s in info.streams:
        if s.index not in streams:
            msgs.append(f"{s.label()} is not selected and will not be in the outputs")
            continue
        if s.type == "subtitle" and s.codec in BITMAP_SUB_CODECS:
            msgs.append(f"{s.label()}: bitmap subtitles are not yet supported for cutting — deselect it or keep the source")
        if s.type == "attachment" and container != "matroska":
            msgs.append(f"{s.label()}: attachments need Matroska output")
        if s.type == "audio" and s.initial_padding and s.codec != "opus" and container == "matroska":
            msgs.append(f"{s.label()}: the source declares an encoder delay of {s.initial_padding} samples "
                        f"({s.initial_padding / max(1, s.sample_rate) * 1000:.1f} ms). Copied streams keep that declaration, so "
                        "players skip that much audio at the very start of each later part (timing is unaffected)")
        if s.type == "audio" and s.codec in ("aac",):
            msgs.append(f"{s.label()}: AAC frames after a copy cut decode without the previous frame's overlap; "
                        "the first ~21 ms may differ slightly from the source (inaudible during black/silence)")
        if s.type == "audio" and s.codec in ("opus",):
            msgs.append(f"{s.label()}: Opus pre-skip/pre-roll is kept from the source header; the first 80 ms after a cut "
                        "may decode with a short transient")
    return msgs


def boundary_scope(sp: StreamPackets, seg: Segment):
    """Clip overlapping GOP replacements to this part, counting each frame once."""
    p = sp.pts
    valid = (p != NOPTS) & (p >= seg.start_pts) & (p < seg.end_pts)
    head = valid & (p < seg.reencode_head['next_key_pts']) if seg.reencode_head else np.zeros(len(p),bool)
    tail = valid & (p >= seg.reencode_tail['tail_from_pts']) & ~head if seg.reencode_tail else np.zeros(len(p),bool)
    return sorted(int(x) for x in p[head]), sorted(int(x) for x in p[tail]), float(int(sp.duration[head | tail].sum())*sp.time_base)


def plan_segment(info: MediaInfo, idx: dict, seg: Segment, streams: list[int], ext: str) -> SegmentPlan:
    v = info.video
    vsp = idx[v.index]
    vtb = vsp.time_base
    start_q = Fraction(seg.start_pts) * vtb
    end_q = Fraction(seg.end_pts) * vtb
    wanted, tracks, sub_events = {}, [], {}
    drop = set(seg.drop_video_pts)
    video_dts = {}
    reenc_dts = {}
    head_pts, tail_pts = [], []
    vvalid = vsp.pts[vsp.pts != NOPTS]
    is_first = seg.start_pts <= int(vvalid.min())
    is_last = seg.end_pts >= vsp.end_pts()
    for si in streams:
        sinfo = info.streams[si]
        if sinfo.type == "attachment":
            continue
        sp = idx.get(si)
        if sp is None:
            continue
        tb = sp.time_base
        pts_q = sp.pts.astype(object)
        tp = TrackPlan(stream_index=si, type=sinfo.type, codec=sinfo.codec)
        if sinfo.type == "subtitle":
            evs = []
            for j in range(len(sp)):
                p = int(sp.pts[j])
                if p == NOPTS:
                    continue
                a = Fraction(p) * tb
                b = a + Fraction(int(sp.duration[j])) * tb
                if a < end_q and b > start_q:
                    na, nb = max(a, start_q), min(b, end_q)
                    evs.append((j, int((na - start_q) / tb), max(1, int((nb - na) / tb))))
                    if na != a or nb != b:
                        tp.notes.append(f"cue at {float(a):.3f}s clipped to the part")
            sub_events[si] = evs
            tp.packets = len(evs)
            tp.treatment = "Timing rewritten (text unchanged)" if any(tp.notes) else "Copied"
            if tp.notes:
                tp.notes = [f"{len(tp.notes)} cue(s) crossing a cut were clipped"]
            tracks.append(tp)
            continue
        sel = []
        for j in range(len(sp)):
            p = int(sp.pts[j])
            if p == NOPTS:
                continue
            q = Fraction(p) * tb
            # The first part also takes packets presented before the first picture
            # (e.g. audio encoder priming), the last part everything after the last.
            if (start_q <= q or is_first) and (q < end_q or is_last):
                if si == v.index:
                    if p in drop:
                        continue
                    if seg.reencode_head and p < seg.reencode_head["next_key_pts"]:
                        continue      # replaced by re-encoded frames
                    if seg.reencode_tail and p >= seg.reencode_tail["tail_from_pts"]:
                        continue
                sel.append(j)
        wanted[si] = set(sel)
        if sel:
            tp.packets = len(sel)
            tp.bytes = int(sp.size[sel].sum())
            first = min(Fraction(int(sp.pts[j])) * tb for j in sel)
            last = max(Fraction(int(sp.pts[j] + sp.duration[j])) * tb for j in sel)
            tp.first_pts_s = float(first - start_q)
            tp.last_end_s = float(last - start_q)
            if sinfo.type == "audio":
                lead = tp.first_pts_s
                tail = float(last - end_q)
                tp.notes.append(f"audio starts {lead*1000:.1f} ms after the picture and ends {tail*1000:+.1f} ms "
                                f"relative to it (packet granularity {float(Fraction(int(np.median(sp.duration[sel]))) * tb)*1000:.1f} ms)")
        if si == v.index:
            allp = sp.pts[sp.pts != NOPTS]
            head_pts, tail_pts, boundary_seconds = boundary_scope(sp, seg)
            # regenerate monotonic decode timestamps over the final decode order:
            # re-encoded head (no reordering) + copied packets + re-encoded tail; dts_k = sorted_pts_k - D
            order = [("h", p_) for p_ in head_pts] + [("c", j) for j in sel] + [("t", p_) for p_ in tail_pts]
            p_dec = [x if k != "c" else int(sp.pts[x]) for k, x in order]
            if p_dec:
                s_sorted = sorted(p_dec)
                D = max(0, max(sk - pk for sk, pk in zip(s_sorted, p_dec)))
                for (k, x), sk in zip(order, s_sorted):
                    if k == "c":
                        video_dts[x] = sk - D
                    else:
                        reenc_dts[x] = sk - D
            if head_pts or tail_pts:
                tp.treatment = "Copied + boundary re-encoding"
                if head_pts:
                    tp.notes.append(f"first {len(head_pts)} frame(s) ({seg.reencode_head['head_s']:.2f}s) re-encoded "
                                    f"up to the keyframe at {float(seg.reencode_head['next_key_pts'] * tb):.3f}s")
                if tail_pts:
                    tp.notes.append(f"last {len(tail_pts)} frame(s) ({seg.reencode_tail['tail_s']:.2f}s) re-encoded "
                                    f"from {float(seg.reencode_tail['tail_from_pts'] * tb):.3f}s")
                tp.packets += len(head_pts) + len(tail_pts)
            if drop:
                tp.notes.append(f"{len(drop)} undecodable open-GOP frame(s) inside black are not written")
        tracks.append(tp)
    chapters = []
    for ch in info.chapters:
        a, b = Fraction(ch["start_s"]).limit_denominator(1000000), Fraction(ch["end_s"]).limit_denominator(1000000)
        if a < end_q and b > start_q:
            chapters.append({"start_s": float(max(a, start_q) - start_q), "end_s": float(min(b, end_q) - start_q),
                             "metadata": ch.get("metadata", {})})
    est = sum(t.bytes for t in tracks) + 64 * 1024
    spp = SegmentPlan(segment=seg, filename=safe_filename(seg.name) + ext, tracks=tracks, wanted=wanted,
                      sub_events=sub_events, video_dts=video_dts, chapters=chapters, est_bytes=est,
                      is_first=is_first, head=seg.reencode_head, tail=seg.reencode_tail,
                      head_pts=head_pts, tail_pts=tail_pts, reenc_dts=reenc_dts, boundary_seconds=boundary_seconds)
    if seg.reencode_head:
        kp = vsp.pts[vsp.key & (vsp.pts >= seg.reencode_head["next_key_pts"])]
        spp._keys = sorted(int(x) for x in kp[:4])
    mp4_family = ext in (".mp4", ".mov")
    first_times = []
    for si, rows in wanted.items():
        if not rows:
            continue
        sp, sinfo = idx[si], info.streams[si]
        first = min(Fraction(int(sp.pts[j])) * sp.time_base for j in rows) - start_q
        if mp4_family and sinfo.type == "audio" and is_first and sinfo.sample_rate:
            # Keep real encoder priming negative, but never let an MP4 edit list
            # mistake audible audio before the first picture for encoder padding.
            first += Fraction(sinfo.initial_padding, sinfo.sample_rate)
        first_times.append(first)
    earliest = min(first_times, default=Fraction(0))
    if earliest < 0:
        # One shared shift preserves the original A/V offset and all audible
        # samples. MP4 needs sample precision so genuine priming remains exact.
        import math
        spp.timestamp_shift = -earliest if mp4_family else Fraction(math.ceil(-earliest * 1000), 1000)
        for si, events in spp.sub_events.items():
            shift = round(spp.timestamp_shift / idx[si].time_base)
            spp.sub_events[si] = [(j, p + shift, d) for j, p, d in events]
        for chapter in spp.chapters:
            chapter["start_s"] += float(spp.timestamp_shift)
            chapter["end_s"] += float(spp.timestamp_shift)
    return spp


def _copy_disposition(dst, src):
    try:
        dst.disposition = src.disposition
    except Exception:
        pass


def write_segment(info: MediaInfo, idx: dict, sp_plan: SegmentPlan, streams: list[int], fmt: str,
                  out_path: str, cancel=None, progress: Optional[Callable[[float], None]] = None,
                  crf: Optional[int] = None) -> dict:
    """Write one output by copying exactly the planned packets (plus, only when the plan
    says so, re-encoded boundary frames). Returns write stats."""
    v = info.video
    vsp = idx[v.index]
    vtb = vsp.time_base
    seg = sp_plan.segment
    start_q = Fraction(seg.start_pts) * vtb - sp_plan.timestamp_shift
    stats = {"packets_written": {}, "encoders_created": 0}
    injected = []      # re-encoded packets (EncodedPacket) to interleave by dts
    if sp_plan.head or sp_plan.tail:
        from . import smart
        kw = {"crf": crf} if crf is not None else {}
        re = {}
        if sp_plan.head_pts:
            h = sp_plan.head
            re["head"] = smart.encode_range(info, vsp, h["prev_idx"], min(sp_plan.head_pts), min(seg.end_pts, h["next_key_pts"]), cancel=cancel, **kw)
            if sorted(e.pts for e in re["head"]) != sorted(sp_plan.head_pts):
                raise RuntimeError("re-encoded head frames do not match the plan")
        if sp_plan.tail_pts:
            t = sp_plan.tail
            re["tail"] = smart.encode_range(info, vsp, t["prev_idx"], min(sp_plan.tail_pts), seg.end_pts, cancel=cancel, **kw)
            if sorted(e.pts for e in re["tail"]) != sorted(sp_plan.tail_pts):
                raise RuntimeError("re-encoded tail frames do not match the plan")
        # The encoder may reorder frames (B-frames): rebuild decode timestamps over the
        # real decode order: re-encoded head + copied packets + re-encoded tail.
        sp_plan.head_pts = [e.pts for e in re.get("head", [])]
        sp_plan.tail_pts = [e.pts for e in re.get("tail", [])]
        rows = sorted(sp_plan.wanted.get(v.index, []))
        order = [("h", p_) for p_ in sp_plan.head_pts] + [("c", j) for j in rows] + [("t", p_) for p_ in sp_plan.tail_pts]
        p_dec = [x if k != "c" else int(vsp.pts[x]) for k, x in order]
        s_sorted = sorted(p_dec)
        D = max(0, max(sk - pk for sk, pk in zip(s_sorted, p_dec)))
        sp_plan.reenc_dts = {}
        for (k, x), sk in zip(order, s_sorted):
            if k == "c":
                sp_plan.video_dts[x] = sk - D
            else:
                sp_plan.reenc_dts[x] = sk - D
        for part, lst in re.items():
            injected += lst
        dur = sum(e.duration for e in injected)
        sp_plan.reencoded = {"video_frames": len(injected), "video_s": float(dur * vtb), "audio_samples": 0,
                             "head_frames": len(re.get("head", [])), "tail_frames": len(re.get("tail", [])),
                             "head_hashes": [e.hash for e in re.get("head", [])],
                             "tail_hashes": [e.hash for e in re.get("tail", [])],
                             "encoder": smart.encoder_name(v.codec), "crf": (crf if crf is not None else smart.DEFAULT_CRF) if v.codec in ("h264", "hevc", "av1") else None}
        stats["reencoded"] = {k: v_ for k, v_ in sp_plan.reencoded.items() if not k.endswith("hashes")}
        injected.sort(key=lambda e: sp_plan.reenc_dts[e.pts])
    inp = open_input(info.path)
    try:
        out = av.open(out_path, "w", format=fmt, options={"avoid_negative_ts": "disabled"})
        try:
            out.metadata.update({k: v_ for k, v_ in inp.metadata.items() if not is_stale_tag(k)})
        except Exception:
            pass
        omap = {}
        for si in streams:
            ist = inp.streams[si]
            if ist.type == "attachment":
                data = getattr(ist, "data", None)
                name = getattr(ist, "name", None) or ist.metadata.get("filename")
                mime = getattr(ist, "mimetype", None) or ist.metadata.get("mimetype")
                if data is not None and name:
                    out.add_attachment(name=name, mimetype=mime or "application/octet-stream", data=bytes(data))
                continue
            if info.streams[si].codec == "mp2" and ist.codec_context.name in ("mp3", "mp3float"):
                ost = out.add_mux_stream("mp2", rate=info.streams[si].sample_rate)
                ost.layout = ist.codec_context.layout
                ost.time_base = ist.time_base
            else:
                ost = out.add_stream_from_template(ist, opaque=True)
            if si == v.index and v.codec == "hevc" and fmt in ("mp4", "mov") and (sp_plan.head_pts or sp_plan.tail_pts):
                ost.codec_context.codec_tag = "hev1"
            for k, val in ist.metadata.items():
                if not is_stale_tag(k):
                    ost.metadata[k] = val
            _copy_disposition(ost, ist)
            omap[si] = ost
        if sp_plan.chapters:
            out.set_chapters([{"id": i + 1, "start": int(round(c["start_s"] * 1000)), "end": int(round(c["end_s"] * 1000)),
                               "time_base": Fraction(1, 1000), "metadata": c["metadata"]}
                              for i, c in enumerate(sp_plan.chapters)])
        # Identify packets by (stream, pts, size). File position is NOT unique:
        # Matroska lacing stores several audio frames in one block.
        want_pos = {}
        for si, rows in sp_plan.wanted.items():
            sp = idx[si]
            for j in rows:
                want_pos[(si, int(sp.pts[j]), int(sp.size[j]))] = j
        sub_want = {}
        for si, evs in sp_plan.sub_events.items():
            sp = idx[si]
            for j, npts, ndur in evs:
                sub_want[(si, int(sp.pts[j]), int(sp.size[j]))] = (j, npts, ndur)
        n_expected = sum(len(r) for r in sp_plan.wanted.values()) + sum(len(e) for e in sp_plan.sub_events.values())
        if len(want_pos) + len(sub_want) != n_expected:
            raise RuntimeError("packet identities are not unique; refusing to export")
        remaining = len(want_pos) + len(sub_want)
        total = max(1, remaining)
        vst = inp.streams[v.index]
        seek_to = max(0, int((Fraction(seg.start_pts) * vtb - 3) / vtb))
        if not sp_plan.is_first and info.format_name != "avi":
            inp.seek(seek_to, stream=vst, backward=True, any_frame=False)
        # Seeking can land after subtitle/audio packets that started earlier; for
        # subtitles we read from the beginning if any event starts before the seek point.
        need_rewind = False
        for si, evs in sp_plan.sub_events.items():
            sp = idx[si]
            for j, _, _ in evs:
                if Fraction(int(sp.pts[j])) * sp.time_base < Fraction(seek_to) * vtb + 1:
                    need_rewind = True
        if need_rewind and not sp_plan.is_first:
            # A seek to zero can land at the first video keyframe and skip
            # earlier audio or subtitle packets. A fresh demuxer reads them all.
            inp.close()
            inp = open_input(info.path)
            vst = inp.streams[v.index]
        sel_streams = [inp.streams[si] for si in streams if inp.streams[si].type != "attachment"]
        end_q = Fraction(seg.end_pts) * vtb
        n = 0
        import math
        inject_tb = Fraction(1, math.lcm(vtb.denominator, start_q.denominator))
        vost = omap[v.index]

        tail_set = set(sp_plan.tail_pts)
        vid_left = [len(sp_plan.wanted.get(v.index, ()))]

        def flush_injected(until_q):
            """Mux re-encoded packets whose decode time is <= until_q (seconds, output timeline).
            Tail packets follow every copied video packet in decode order, so they wait until
            all copied video has been written (audio timing alone must not release them)."""
            while injected and Fraction(sp_plan.reenc_dts[injected[0].pts]) * vtb - start_q <= until_q:
                if injected[0].pts in tail_set and vid_left[0] > 0:
                    break
                e = injected.pop(0)
                pk = av.Packet(e.data)
                pk.time_base = inject_tb
                pk.pts = int((Fraction(e.pts)*vtb - start_q)/inject_tb)
                pk.dts = int((Fraction(sp_plan.reenc_dts[e.pts])*vtb - start_q)/inject_tb)
                pk.duration = int(Fraction(e.duration)*vtb/inject_tb)
                pk.is_keyframe = e.key
                pk.stream = vost
                out.mux(pk)
                stats["packets_written"][v.index] = stats["packets_written"].get(v.index, 0) + 1
        for pkt in inp.demux(*sel_streams):
            if pkt.size == 0 and pkt.pts is None:
                continue
            si = pkt.stream_index
            key = (si, pkt.pts, pkt.size)
            ost = omap.get(si)
            if ost is None:
                continue
            tb = pkt.time_base
            if key in want_pos:
                j = want_pos.pop(key)
                import math
                mux_tb = Fraction(1, math.lcm(tb.denominator, start_q.denominator))
                newp = int((Fraction(pkt.pts)*tb - start_q) / mux_tb)
                if si == v.index:
                    newd = int((Fraction(sp_plan.video_dts[j])*tb - start_q) / mux_tb)
                    restore_first = sp_plan.head_pts and j == min(sp_plan.wanted[v.index]) and v.codec != "h264"
                    # HEVC encoders reuse parameter-set IDs. After decoding an
                    # encoded tail, backward seeking must restore the original
                    # configuration at any copied keyframe, not only at a join.
                    restore_seek = v.codec == "hevc" and (sp_plan.head_pts or sp_plan.tail_pts) and pkt.is_keyframe
                    if restore_first or restore_seek:
                        from .smart import restore_headers
                        from .media import compressed_hash
                        data = restore_headers(v.codec, vst.codec_context.extradata or b"", bytes(pkt))
                        if data != bytes(pkt):
                            sp_plan.restored_hashes[j] = compressed_hash(v.codec, data, vst.codec_context.extradata or b"")
                        restored = av.Packet(data)
                        restored.time_base, restored.duration, restored.is_keyframe = pkt.time_base, pkt.duration, pkt.is_keyframe
                        pkt = restored
                else:
                    newd = newp
                pkt.duration = int(Fraction(pkt.duration)*tb/mux_tb)
                pkt.time_base = mux_tb
                pkt.pts, pkt.dts = newp, newd
                if injected:
                    flush_injected(Fraction(newd) * mux_tb)
                pkt.stream = ost
                out.mux(pkt)
                if si == v.index:
                    vid_left[0] -= 1
                stats["packets_written"][si] = stats["packets_written"].get(si, 0) + 1
                remaining -= 1
            elif key in sub_want:
                j, npts, ndur = sub_want.pop(key)
                pkt.pts = npts
                pkt.dts = npts
                pkt.duration = ndur
                pkt.stream = ost
                out.mux(pkt)
                stats["packets_written"][si] = stats["packets_written"].get(si, 0) + 1
                remaining -= 1
            n += 1
            if n % 500 == 0:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if progress:
                    progress(1 - remaining / total)
            if remaining == 0:
                break
        if remaining:
            raise RuntimeError(f"{remaining} planned packets were not found while reading the source")
        flush_injected(Fraction(10 ** 9))
        for s in out.streams:
            cc = getattr(s, "codec_context", None)
            if cc is not None and getattr(cc, "is_open", False) and getattr(cc, "is_encoder", False):
                stats["encoders_created"] += 1
        out.close()
    finally:
        inp.close()
    return stats


@dataclass
class OutputResult:
    n: int
    path: str
    ok: bool
    validation: dict
    plan: dict
    write: dict
    error: str = ""


def export_plan(info: MediaInfo, idx: dict, plan: Plan, opts: ExportOptions,
                progress: Optional[Callable[[str, float], None]] = None, cancel=None,
                log: Optional[Callable[[str], None]] = None, only: Optional[list] = None) -> dict:
    from . import validate as V
    log = log or (lambda m: None)
    if plan.issues:
        raise ValueError("Plan has unresolved issues:\n" + "\n".join(plan.issues))
    streams = opts.streams if opts.streams is not None else default_streams(info)
    if info.video.index not in streams:
        raise ValueError("The video stream must be selected")
    fmt, ext = output_format(info, opts.container)
    os.makedirs(opts.out_dir, exist_ok=True)
    src_real = os.path.realpath(info.path)
    job = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    tmp_dir = os.path.join(opts.out_dir, f".bsr-partial-{job}")
    os.makedirs(tmp_dir, exist_ok=True)
    from .osutil import hide
    hide(tmp_dir)
    segs = [s for s in plan.segments if only is None or s.n in only]
    plans = [plan_segment(info, idx, s, streams, ext) for s in segs]
    need = sum(p.est_bytes for p in plans)
    free = shutil.disk_usage(opts.out_dir).free
    if need * 1.05 > free:
        raise OSError(f"Not enough disk space: need ~{need/1e9:.2f} GB, {free/1e9:.2f} GB free")
    results = []
    smart_used = any(p.head or p.tail for p in plans)
    report = {"job": job, "engine": ENGINE_VERSION, "tools": tool_versions(),
              "mode": "boundary-reencode" if smart_used else "copy",
              "source": info.path, "source_fingerprint": info.fingerprint, "container": fmt,
              "streams": streams, "preservation": preservation_report(info, streams, fmt),
              "plan": plan.to_json(), "outputs": []}
    if info.video.codec == "hevc" and fmt in ("mp4", "mov") and smart_used:
        report['preservation'].append("HEVC boundary exports use the hev1 sample entry so each encoded boundary can carry its decoder configuration")
    log(f"Export job {job}: {len(plans)} part(s), container {fmt}, streams {streams}")
    for k, sp_plan in enumerate(plans):
        final = os.path.join(opts.out_dir, sp_plan.filename)
        if os.path.realpath(final) == src_real:
            raise ValueError("Refusing to write over the source file")
        tmp = os.path.join(tmp_dir, f"part{sp_plan.segment.n:03d}{ext}")
        log(f"Part {sp_plan.segment.n}: {fmt_time(sp_plan.segment.start_s)} – {fmt_time(sp_plan.segment.end_s)} -> {sp_plan.filename}")
        res = OutputResult(n=sp_plan.segment.n, path="", ok=False, validation={}, plan=sp_plan.summary(), write={})
        try:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            res.write = write_segment(info, idx, sp_plan, streams, fmt, tmp, cancel=cancel, crf=opts.crf,
                                      progress=(lambda f, k=k: progress and progress("write", (k + f * 0.6) / len(plans))))
            if res.write.get("encoders_created"):
                raise RuntimeError("An encoder was attached to an output stream (never expected)")
            if opts.validate:
                res.validation = V.validate_output(info, idx, sp_plan, streams, tmp, full_decode=opts.full_decode_check,
                                                   cancel=cancel)
                ok = res.validation.get("ok", False)
            else:
                ok = True
            if not ok:
                res.error = "validation failed: " + "; ".join(res.validation.get("failures", []))
                log(f"  FAILED: {res.error}")
                bad = os.path.join(tmp_dir, f"FAILED-part{sp_plan.segment.n:03d}{ext}")
                os.replace(tmp, bad)
                res.path = bad
            else:
                dest = _unique(final)
                os.replace(tmp, dest)
                res.path = dest
                res.ok = True
                log(f"  OK -> {os.path.basename(dest)}")
        except Cancelled:
            log("  cancelled")
            res.error = "cancelled"
            results.append(res)
            break
        except Exception as e:
            res.error = f"{type(e).__name__}: {e}"
            log(f"  ERROR {res.error}")
        res.plan = sp_plan.summary()
        results.append(res)
        if progress:
            progress("write", (k + 1) / len(plans))
    report["outputs"] = [asdict(r) for r in results]
    report["ok"] = bool(results) and all(r.ok for r in results) and len(results) == len(plans)
    rpath = os.path.join(opts.out_dir, f"bsr-report-{job}.json")
    with open(rpath, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=_json_default)
    report["report_path"] = rpath
    try:
        if not os.listdir(tmp_dir):
            os.rmdir(tmp_dir)
    except OSError:
        pass
    return report


def _unique(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    if isinstance(o, Fraction):
        return str(o)
    return str(o)
