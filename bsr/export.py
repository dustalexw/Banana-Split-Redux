"""Strict stream-copy export.

Packets are selected from the packet index *before* anything is written, so the
exact set of source packets in each output is known up front and can be verified
afterwards. This module never creates an encoder: output streams are created from
the input streams as templates and receive compressed packets unchanged. What does
change - and is reported as such - is the container: timestamps are rebased so each
output starts at zero, decode timestamps are regenerated, the Matroska/MP4 headers
are rewritten, and stale statistics tags are dropped.
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

    def summary(self):
        return {"n": self.segment.n, "file": self.filename, "start_s": self.segment.start_s,
                "end_s": self.segment.end_s, "duration_s": self.segment.duration_s,
                "dropped_frames": len(self.segment.drop_video_pts),
                "tracks": [asdict(t) for t in self.tracks], "chapters": self.chapters,
                "est_bytes": self.est_bytes}


def output_format(info: MediaInfo, requested: Optional[str] = None) -> tuple[str, str]:
    if requested:
        return requested, {"matroska": ".mkv", "mp4": ".mp4", "mov": ".mov"}.get(requested, ".mkv")
    f = info.format_name
    if "matroska" in f or "webm" in f:
        return "matroska", ".mkv"
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


def plan_segment(info: MediaInfo, idx: dict, seg: Segment, streams: list[int], ext: str) -> SegmentPlan:
    v = info.video
    vsp = idx[v.index]
    vtb = vsp.time_base
    start_q = Fraction(seg.start_pts) * vtb
    end_q = Fraction(seg.end_pts) * vtb
    wanted, tracks, sub_events = {}, [], {}
    drop = set(seg.drop_video_pts)
    video_dts = {}
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
                if si == v.index and p in drop:
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
        if si == v.index and sel:
            # regenerate monotonic decode timestamps: dts_k = sorted_pts_k - D
            p_dec = [int(sp.pts[j]) for j in sel]
            s_sorted = sorted(p_dec)
            D = max(0, max(sk - pk for sk, pk in zip(s_sorted, p_dec)))
            for j, sk in zip(sel, s_sorted):
                video_dts[j] = sk - D
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
    return SegmentPlan(segment=seg, filename=safe_filename(seg.name) + ext, tracks=tracks, wanted=wanted,
                       sub_events=sub_events, video_dts=video_dts, chapters=chapters, est_bytes=est,
                       is_first=is_first)


def _copy_disposition(dst, src):
    try:
        dst.disposition = src.disposition
    except Exception:
        pass


def write_segment(info: MediaInfo, idx: dict, sp_plan: SegmentPlan, streams: list[int], fmt: str,
                  out_path: str, cancel=None, progress: Optional[Callable[[float], None]] = None) -> dict:
    """Write one output by copying exactly the planned packets. Returns write stats."""
    v = info.video
    vtb = idx[v.index].time_base
    seg = sp_plan.segment
    start_q = Fraction(seg.start_pts) * vtb
    inp = open_input(info.path)
    stats = {"packets_written": {}, "encoders_created": 0}
    try:
        out = av.open(out_path, "w", format=fmt)
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
            ost = out.add_stream_from_template(ist)
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
        inp.seek(seek_to, stream=vst, backward=True, any_frame=False)
        # Seeking can land after subtitle/audio packets that started earlier; for
        # subtitles we read from the beginning if any event starts before the seek point.
        need_rewind = False
        for si, evs in sp_plan.sub_events.items():
            sp = idx[si]
            for j, _, _ in evs:
                if Fraction(int(sp.pts[j])) * sp.time_base < Fraction(seek_to) * vtb + 1:
                    need_rewind = True
        if need_rewind:
            inp.seek(0, backward=True, any_frame=False)
        sel_streams = [inp.streams[si] for si in streams if inp.streams[si].type != "attachment"]
        end_q = Fraction(seg.end_pts) * vtb
        n = 0
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
                off = int(start_q / tb) if (start_q / tb).denominator == 1 else round(start_q / tb)
                newp = pkt.pts - off
                if si == v.index:
                    newd = sp_plan.video_dts[j] - off
                else:
                    newd = newp
                pkt.pts, pkt.dts = newp, newd
                pkt.stream = ost
                out.mux(pkt)
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
            if pkt.pts is not None and Fraction(pkt.pts) * tb > end_q + 10 and si == v.index:
                break
        if remaining:
            raise RuntimeError(f"{remaining} planned packets were not found while reading the source")
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
    segs = [s for s in plan.segments if only is None or s.n in only]
    plans = [plan_segment(info, idx, s, streams, ext) for s in segs]
    need = sum(p.est_bytes for p in plans)
    free = shutil.disk_usage(opts.out_dir).free
    if need * 1.05 > free:
        raise OSError(f"Not enough disk space: need ~{need/1e9:.2f} GB, {free/1e9:.2f} GB free")
    results = []
    report = {"job": job, "engine": ENGINE_VERSION, "tools": tool_versions(), "mode": "copy",
              "source": info.path, "source_fingerprint": info.fingerprint, "container": fmt,
              "streams": streams, "preservation": preservation_report(info, streams, fmt),
              "plan": plan.to_json(), "outputs": []}
    log(f"Export job {job}: {len(plans)} part(s), container {fmt}, streams {streams}")
    for k, sp_plan in enumerate(plans):
        final = os.path.join(opts.out_dir, sp_plan.filename)
        if os.path.realpath(final) == src_real:
            raise ValueError("Refusing to write over the source file")
        tmp = os.path.join(tmp_dir, f"part{sp_plan.segment.n:03d}{ext}")
        log(f"Part {sp_plan.segment.n}: {fmt_time(sp_plan.segment.start_s)} – {fmt_time(sp_plan.segment.end_s)} -> {sp_plan.filename}")
        res = OutputResult(n=sp_plan.segment.n, path="", ok=False, validation={}, plan=sp_plan.summary(), write={})
        try:
            res.write = write_segment(info, idx, sp_plan, streams, fmt, tmp, cancel=cancel,
                                      progress=(lambda f, k=k: progress and progress("write", (k + f * 0.6) / len(plans))))
            if res.write.get("encoders_created"):
                raise RuntimeError("An encoder was opened in copy mode")
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
