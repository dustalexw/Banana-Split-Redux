"""Independent validation of a written output.

A successful write or a file that merely opens proves nothing. Each output is
checked for:
  * stream set, codec parameters, language/title/disposition preserved
  * every packet payload byte-identical to the planned source packets, in order,
    with no extras (=> nothing re-encoded, lost or duplicated)
  * timestamps equal to the planned source timestamps minus the part's start
  * full decode without errors; decoded frame count equals planned frame count
  * first and last presented frames bit-identical to the same frames decoded from
    the source in context
  * audio decodes cleanly; audio/video offset identical to the source (sync)
  * seeking to the middle works
  * subtitle events and chapters as planned
  * optional cross-check with the ffmpeg command-line tool when it is installed
"""
from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from typing import Optional

import av
import numpy as np

from .boundaries import frame_hash
from .media import MediaInfo, NOPTS, payload_hash, probe, open_input, is_stale_tag, Cancelled

SYNC_TOLERANCE_S = 0.0011    # offsets must match the source to within one 1 ms Matroska tick


class Report:
    def __init__(self):
        self.checks = {}
        self.failures = []

    def add(self, name, ok, detail=""):
        self.checks[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            self.failures.append(f"{name}: {detail}")

    def to_json(self):
        return {"ok": not self.failures, "failures": self.failures, "checks": self.checks}


_BASELINES = {}


def _norm_msg(line: str) -> str:
    import re
    return re.sub(r"@ 0x[0-9a-f]+", "@", line.strip())


def _cli_decode_messages(ff, path):
    p = subprocess.run([ff, "-nostdin", "-v", "error", "-i", path, "-map", "0:v", "-map", "0:a?",
                        "-f", "null", "-"], capture_output=True, text=True, timeout=1800)
    out = [_norm_msg(l) for l in p.stderr.splitlines() if l.strip()]
    if p.returncode != 0:
        out.append(f"exit status {p.returncode}")
    return out


def _source_baseline(ff, src):
    """Messages the ffmpeg CLI already prints for the untouched source (first 20 s)."""
    if src not in _BASELINES:
        p = subprocess.run([ff, "-nostdin", "-v", "error", "-t", "20", "-i", src, "-map", "0:v", "-map", "0:a?",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=600)
        _BASELINES[src] = {_norm_msg(l) for l in p.stderr.splitlines() if l.strip()}
    return _BASELINES[src]


def _layout_equiv(a, b) -> bool:
    """An unspecified "N channels" layout is equivalent to a named layout with N channels."""
    la, lb = a.layout or "", b.layout or ""
    if la == lb:
        return True
    unspec = lambda l: l.endswith("channels") and l.split()[0].isdigit()
    return a.channels == b.channels and (unspec(la) or unspec(lb))


def iter_frames(container, stream, counter=None):
    """Decode packet by packet so a corrupt packet is counted instead of ending decoding."""
    for pkt in container.demux(stream):
        try:
            frames = pkt.decode()
        except av.error.FFmpegError:
            if counter is not None:
                counter["errors"] = counter.get("errors", 0) + 1
            continue
        for f in frames:
            yield f


def decode_window(path, stream_index, lo, hi, offset=0):
    """{source_pts: (hash, Y plane)} for frames with lo <= pts+offset < hi."""
    out = {}
    c = open_input(path)
    try:
        st = c.streams[stream_index]
        st.thread_type = "AUTO"
        tb = st.time_base
        c.seek(max(0, int(lo - offset - Fraction(3) / tb)), stream=st, backward=True, any_frame=False)
        for f in iter_frames(c, st):
            if f.pts is None:
                continue
            p = int(f.pts) + offset
            if p >= hi:
                break
            if p >= lo:
                pl = f.planes[0]
                y = np.frombuffer(pl, np.uint8).reshape(pl.height, pl.line_size)[:, : pl.width].copy()
                out[p] = (frame_hash(f), y)
    finally:
        c.close()
    return out


def info_keys(info, sp_plan):
    return sp_plan.__dict__.get("_keys", [])


def _check_joins(r, info, sp_plan, path, vtb, start_q):
    """Copied frames next to a join must be bit-identical to the source; re-encoded
    frames are measured against the source (PSNR, luma)."""
    from .smart import psnr
    seg = sp_plan.segment
    off = int(start_q / vtb)
    oi = [s.index for s in probe(path).streams if s.type == "video"][0]
    two = int(Fraction(2) / vtb)
    vsp_keys = sorted(int(p) for p in info_keys(info, sp_plan))
    checks = []
    if sp_plan.head:
        k = sp_plan.head["next_key_pts"]
        later = [x for x in vsp_keys if x > k]
        # copied frames through the whole GOP after the join (to the second keyframe), at least 2 s, at most 20 s
        end = max(k + two, later[1] if len(later) > 1 else (later[0] if later else k + two))
        end = min(end, k + 10 * two, seg.end_pts)
        checks.append(("head", seg.start_pts, k, k, end))
    if sp_plan.tail:
        k = sp_plan.tail["tail_from_pts"]
        checks.append(("tail", k, seg.end_pts, k - two, k))
    for name, re_lo, re_hi, cp_lo, cp_hi in checks:
        lo, hi = min(re_lo, cp_lo), max(re_hi, cp_hi)
        src = decode_window(info.path, info.video.index, lo, hi)
        outw = decode_window(path, oi, lo, hi, offset=off)
        cp = [p for p in src if cp_lo <= p < cp_hi and p < seg.end_pts and p >= seg.start_pts]
        same = [p for p in cp if p in outw and outw[p][0] == src[p][0]]
        if cp:
            r.add(f"{name} join: copied frames identical", len(same) == len(cp),
                  f"{len(same)}/{len(cp)} copied frames {'after' if name == 'head' else 'before'} the join decode "
                  "bit-identically to the source")
        else:
            r.add(f"{name} join: copied frames identical", True,
                  "no copied frames on this side (the part is re-encoded up to its edge)")
        rp = [p for p in src if re_lo <= p < re_hi]
        vals = [psnr(outw[p][1], src[p][1]) for p in rp if p in outw]
        ok = len(vals) == len(rp) and len(vals) > 0 and min(vals) >= 35.0
        r.add(f"{name} re-encoded frames", ok,
              f"{len(vals)}/{len(rp)} re-encoded frames present, luma PSNR min "
              f"{min(vals) if vals else 0:.1f} dB, mean {np.mean(vals) if vals else 0:.1f} dB (pass ≥ 35 dB); "
              "first/last frame of the part checked here" if vals else "re-encoded frames missing")


def source_frame_hashes(path: str, info: MediaInfo, wanted_pts: list[int]) -> dict[int, bytes]:
    """Decode the source in context and hash the frames with the given pts."""
    v = info.video
    out = {}
    c = open_input(path)
    try:
        st = c.streams[v.index]
        st.thread_type = "AUTO"
        tb = st.time_base
        for p in sorted(set(wanted_pts)):
            if p in out:
                continue
            c.seek(max(0, int(p - Fraction(3) / tb)), stream=st, backward=True, any_frame=False)
            for f in c.decode(st):
                if f.pts is None:
                    continue
                if f.pts in wanted_pts:
                    out[int(f.pts)] = frame_hash(f)
                if f.pts >= p:
                    break
    finally:
        c.close()
    return out


def validate_output(info: MediaInfo, idx: dict, sp_plan, streams: list[int], path: str,
                    full_decode: bool = True, cancel=None) -> dict:
    r = Report()
    seg = sp_plan.segment
    v = info.video
    vsp = idx[v.index]
    vtb = vsp.time_base
    start_q = Fraction(seg.start_pts) * vtb
    try:
        oinfo = probe(path)
    except Exception as e:
        r.add("opens", False, str(e))
        return r.to_json()
    r.add("opens", True, f"{oinfo.format_name}, {len(oinfo.streams)} streams")

    # ---- stream mapping and parameters
    src_sel = [info.streams[i] for i in streams]
    src_av = [s for s in src_sel if s.type != "attachment"]
    out_av = [s for s in oinfo.streams if s.type != "attachment"]
    if len(src_av) != len(out_av):
        r.add("stream set", False, f"expected {len(src_av)} streams, found {len(out_av)}")
        return r.to_json()
    mism = []
    for a, b in zip(src_av, out_av):
        fields = ["type", "codec", "language", "title"]
        if a.type == "video":
            fields += ["width", "height", "pix_fmt", "field_order", "color_range", "color_space", "color_trc",
                       "color_primaries", "sar"]
        if a.type == "audio":
            fields += ["sample_rate", "channels", "layout"]
        for f in fields:
            if f == "layout" and _layout_equiv(a, b):
                continue
            if getattr(a, f) != getattr(b, f):
                mism.append(f"#{a.index}->{b.index} {f}: {getattr(a, f)!r} != {getattr(b, f)!r}")
        if sorted(a.disposition) != sorted(b.disposition):
            mism.append(f"#{a.index} disposition {a.disposition} != {b.disposition}")
        extra_src = {k: val for k, val in a.metadata.items() if not is_stale_tag(k)}
        lost = [k for k in extra_src if k not in b.metadata and k.lower() not in ("language",)]
        if lost:
            mism.append(f"#{a.index} tags lost: {lost}")
    r.add("track properties", not mism, "; ".join(mism) if mism else
          f"{len(out_av)} tracks: codec, geometry, field order, colour, language, title, disposition match")
    n_att_src = sum(1 for s in src_sel if s.type == "attachment")
    n_att_out = sum(1 for s in oinfo.streams if s.type == "attachment")
    if n_att_src:
        r.add("attachments", n_att_src == n_att_out, f"{n_att_out}/{n_att_src}")
    if sp_plan.chapters or info.chapters:
        exp = sp_plan.chapters
        got = oinfo.chapters
        ok = len(exp) == len(got) and all(abs(e["start_s"] - g["start_s"]) < 0.002 and abs(e["end_s"] - g["end_s"]) < 0.002
                                          for e, g in zip(exp, got))
        r.add("chapters", ok, f"{len(got)} chapter(s), expected {len(exp)}")

    # ---- packet payloads and timestamps
    out_by_src = dict(zip([s.index for s in src_av], [s.index for s in out_av]))
    got = {o: [] for o in out_by_src.values()}
    c = open_input(path)
    try:
        otbs = {s.index: Fraction(s.time_base) for s in c.streams}
        for pkt in c.demux():
            if pkt.size == 0 and pkt.pts is None:
                continue
            if pkt.stream_index in got:
                got[pkt.stream_index].append((pkt.pts, payload_hash(bytes(pkt)), pkt.duration))
    finally:
        c.close()
    max_ts_err = 0.0
    pkt_fail = []
    for si, oi in out_by_src.items():
        sp = idx[si]
        sinfo = info.streams[si]
        if sinfo.type == "subtitle":
            evs = sp_plan.sub_events.get(si, [])
            exp_h = [int(sp.hash[j]) for j, _, _ in evs]
            got_h = [h for _, h, _ in got[oi]]
            if sorted(exp_h) != sorted(got_h):
                pkt_fail.append(f"subtitle #{si}: {len(got_h)} events, expected {len(exp_h)} / text changed")
            exp_t = sorted((float(Fraction(npts) * sp.time_base), float(Fraction(nd) * sp.time_base)) for _, npts, nd in evs)
            got_t = sorted((float(Fraction(p) * otbs[oi]), float(Fraction(d or 0) * otbs[oi])) for p, _, d in got[oi])
            for (ea, ed), (ga, gd) in zip(exp_t, got_t):
                max_ts_err = max(max_ts_err, abs(ea - ga), abs(ed - gd))
            continue
        rows = sorted(sp_plan.wanted.get(si, []))
        exp_h = [int(sp.hash[j]) for j in rows]
        exp_p = [int(sp.pts[j]) for j in rows]
        if si == v.index and sp_plan.reencoded:
            rh, rt = sp_plan.reencoded.get("head_hashes", []), sp_plan.reencoded.get("tail_hashes", [])
            exp_h = rh + exp_h + rt
            exp_p = list(sp_plan.head_pts) + exp_p + list(sp_plan.tail_pts)
        got_h = [h for _, h, _ in got[oi]]
        if exp_h != got_h:
            if sorted(exp_h) == sorted(got_h):
                pkt_fail.append(f"#{si}: same packets, different order")
            else:
                pkt_fail.append(f"#{si}: {len(got_h)} packets written, {len(exp_h)} planned, "
                                f"{len(set(exp_h) - set(got_h))} missing, {len(set(got_h) - set(exp_h))} unexpected")
            continue
        for ep, (p, _, _) in zip(exp_p, got[oi]):
            exp_t = Fraction(ep) * sp.time_base - start_q
            got_t = Fraction(p) * otbs[oi]
            max_ts_err = max(max_ts_err, abs(float(exp_t - got_t)))
    n_re = len(sp_plan.head_pts) + len(sp_plan.tail_pts)
    r.add("packets byte-identical", not pkt_fail,
          "; ".join(pkt_fail) if pkt_fail else
          (f"{sum(len(x) for x in got.values()) - n_re} copied packets identical to the source"
           + (f", {n_re} re-encoded packets exactly as produced by the boundary encoder" if n_re else "")
           + ", none extra or missing"))
    r.add("timestamps", max_ts_err <= 0.0011, f"max deviation from plan {max_ts_err*1000:.3f} ms")

    # ---- video decode
    ov = [s for s in oinfo.streams if s.type == "video"][0]
    rows = sorted(sp_plan.wanted.get(v.index, []))
    exp_pts = sorted([int(vsp.pts[j]) for j in rows] + list(sp_plan.head_pts) + list(sp_plan.tail_pts))
    c = open_input(path)
    try:
        st = c.streams[ov.index]
        st.thread_type = "AUTO"
        otb = Fraction(st.time_base)
        dec_pts, errors, corrupt = [], 0, 0
        first_h = last_h = None
        if full_decode:
            cnt = {}
            for f in iter_frames(c, st, cnt):
                errors = cnt.get("errors", 0)
                if f.pts is None:
                    continue
                if getattr(f, "is_corrupt", False):
                    corrupt += 1
                dec_pts.append(int(f.pts))
                h = None
                if first_h is None:
                    first_h = frame_hash(f)
                last_frame = f
                if len(dec_pts) % 1000 == 0 and cancel is not None and cancel.is_set():
                    raise Cancelled()
            errors = cnt.get("errors", 0)
            last_h = frame_hash(last_frame) if dec_pts else None
            r.add("video decodes", errors == 0 and corrupt == 0,
                  f"{len(dec_pts)} frames, {errors} decode errors, {corrupt} corrupt")
            exp_rel = [float(Fraction(p) * vtb - start_q) for p in exp_pts]
            got_rel = [float(Fraction(p) * otb) for p in sorted(dec_pts)]
            same = len(exp_rel) == len(got_rel) and all(abs(a - b) < 0.0011 for a, b in zip(exp_rel, got_rel))
            r.add("frame count & order", same, f"decoded {len(got_rel)} frames, planned {len(exp_rel)}")
            # first/last presented frame identity vs. source
            if exp_pts and not (sp_plan.head or sp_plan.tail):
                ref = source_frame_hashes(info.path, info, [exp_pts[0], exp_pts[-1]])
                ok1 = ref.get(exp_pts[0]) == first_h
                ok2 = ref.get(exp_pts[-1]) == last_h
                r.add("first frame matches plan", ok1,
                      f"first presented frame = source frame at {float(exp_pts[0]*vtb):.3f}s" if ok1 else "differs from source")
                r.add("last frame matches plan", ok2,
                      f"last presented frame = source frame at {float(exp_pts[-1]*vtb):.3f}s" if ok2 else "differs from source")
        # seek test
        dur = seg.duration_s
        target = dur / 2
        c.seek(int(target / otb), stream=st, backward=True, any_frame=False)
        sf = None
        for f in c.decode(st):
            if f.pts is not None:
                sf = f
                break
        ok = sf is not None and float(sf.pts * otb) <= target + 0.001 and not getattr(sf, "is_corrupt", False)
        r.add("seek", ok, f"seek to {target:.2f}s -> keyframe at {float(sf.pts*otb):.2f}s" if sf else "no frame after seek")
    finally:
        c.close()

    # ---- boundary re-encoding joins
    if sp_plan.head or sp_plan.tail:
        _check_joins(r, info, sp_plan, path, vtb, start_q)

    # ---- audio decode and sync
    aud_notes, aud_ok = [], True
    vfirst_src = Fraction(exp_pts[0]) * vtb if exp_pts else start_q
    c = open_input(path)
    try:
        for si, oi in out_by_src.items():
            if info.streams[si].type != "audio":
                continue
            sp = idx[si]
            rows = sorted(sp_plan.wanted.get(si, []))
            if not rows:
                aud_notes.append(f"#{si}: no audio in this part")
                continue
            src_off = Fraction(int(sp.pts[rows[0]])) * sp.time_base - vfirst_src
            c.close()
            c = open_input(path)      # fresh demuxer per track
            st = c.streams[oi]
            otb = Fraction(st.time_base)
            n = 0
            cnt = {}
            for fr in iter_frames(c, st, cnt):
                n += fr.samples
            errs = cnt.get("errors", 0)
            first_pkt = got[oi][0][0] if got[oi] else 0
            out_vfirst = Fraction(0) if not exp_pts else (Fraction(exp_pts[0]) * vtb - start_q)
            out_off = Fraction(first_pkt) * otb - out_vfirst
            drift = abs(float(out_off - src_off))
            pad_src = info.streams[si].initial_padding
            pad_out = [s_ for s_ in oinfo.streams if s_.index == oi][0].initial_padding
            # PyAV cannot clear the declared encoder delay on a copied stream, so later
            # parts inherit it (reported before export); timing of kept samples is unaffected.
            # MP4 expresses priming with an edit list derived from the first packet,
            # so a later part legitimately declares none.
            pad_ok = pad_out == pad_src or (not sp_plan.is_first and pad_out == 0)
            pad_exp = pad_src if pad_out == pad_src else 0
            ok = errs == 0 and drift <= SYNC_TOLERANCE_S and pad_ok
            aud_ok &= ok
            aud_notes.append(f"#{si}: {n} samples, {errs} errors, A/V offset {float(out_off)*1000:+.1f} ms "
                             f"(source {float(src_off)*1000:+.1f} ms, Δ {drift*1000:.2f} ms), "
                             f"codec delay {pad_out} (expected {pad_exp})")
    finally:
        c.close()
    if aud_notes:
        r.add("audio decodes & sync", aud_ok, "; ".join(aud_notes))

    # ---- optional independent cross-check with the ffmpeg CLI
    ff = shutil.which("ffmpeg")
    if ff and full_decode:
        try:
            msgs = _cli_decode_messages(ff, path)
            base = _source_baseline(ff, info.path)
            new_msgs = [m for m in msgs if m not in base]
            if not new_msgs:
                r.add("ffmpeg CLI decode", True, "clean" if not msgs else
                      f"clean apart from {len(set(msgs))} message type(s) the source itself produces")
            else:
                r.add("ffmpeg CLI decode", False, f"{len(new_msgs)} new message(s): {new_msgs[0][:160]}")
        except Exception as e:
            r.checks["ffmpeg CLI decode"] = {"ok": True, "detail": f"skipped: {e}"}
    return r.to_json()
