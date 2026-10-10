"""Cut planning: user decisions + proven boundaries -> concrete segments.

Segments are half-open presentation intervals [start_pts, end_pts) on the video
stream's integer timeline. Every source packet is assigned to at most one output
of a contiguous split, so nothing is duplicated; anything not assigned (removed
black, undecodable open-GOP leading pictures) is listed explicitly.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict, replace
from fractions import Fraction
from typing import Optional

import numpy as np

from .boundaries import BoundaryFinder, BoundaryOption
from .detect import Candidate
from .media import MediaInfo, StreamPackets, NOPTS, fmt_time

POSITIONS = ("start", "middle", "end")


@dataclass
class Policy:
    # Open-GOP keyframes inside black: allow dropping up to N undecodable *black*
    # leading frames without asking. 0 = always ask.
    max_auto_drop_black_frames: int = 0
    min_segment_s: float = 2.0
    # Desktop default for supported files. CLI / old saved projects stay strict.
    boundary_reencode: bool = False


@dataclass
class CutDecision:
    id: int
    start_pts: int                 # black interval (or manual point when start == end)
    end_pts: int
    state: str = "proposed"        # proposed | accepted | rejected
    position: str = "middle"       # where in the black interval to split
    remove_black: bool = False     # remove black padding instead of keeping it
    pad_before_s: float = 0.0      # black kept at the end of the earlier part when removing
    pad_after_s: float = 0.0       # black kept at the start of the later part when removing
    choice: Optional[str] = None   # explicit resolution: "boundary:<pts>", "accept-drop:<pts>", "skip", "reencode"
    manual: bool = False
    note: str = ""
    custom_pts: Optional[int] = None  # an explicitly adjusted frame, without losing the detected interval

    def to_json(self):
        return asdict(self)


@dataclass
class ResolvedCut:
    decision_id: int
    status: str                    # "copy" | "copy-drop" | "needs-review" | "skipped" | "reencode" | "rejected"
    label: str                     # plain-language label for the UI
    boundaries: list = field(default_factory=list)   # BoundaryOption(s): 1 for split, 2 for removal
    removed: Optional[tuple] = None                  # (start_pts, end_pts) removed when removing black
    requested_pts: Optional[int] = None
    delta_s: float = 0.0           # planned minus requested
    outside_black: bool = False
    choices: list = field(default_factory=list)      # options presented when review is needed
    messages: list = field(default_factory=list)
    inside: list = field(default_factory=list)       # all inside options (for the timeline)
    nearby: list = field(default_factory=list)
    smart: Optional[dict] = None                     # boundary re-encoding plan (SmartCut)
    smart_edges: list = field(default_factory=list)  # two exact edges for black removal

    def to_json(self):
        d = asdict(self)
        return d


def _ok_clean(o: BoundaryOption) -> bool:
    return bool(o.verified) and not o.dropped_pts


def _ok_drop(o: BoundaryOption, start: int, end: int) -> bool:
    return bool(o.verified) and bool(o.dropped_pts) and all(start <= p < end for p in o.dropped_pts)


def resolve(dec: CutDecision, finder: BoundaryFinder, policy: Policy, tb: Fraction,
            reencode_supported: bool = False, frames=None) -> ResolvedCut:
    if dec.custom_pts is not None:
        adjusted = replace(dec, start_pts=dec.custom_pts, end_pts=dec.custom_pts,
                           manual=True, remove_black=False, custom_pts=None)
        rc = resolve(adjusted, finder, policy, tb, reencode_supported, frames)
        rc.messages.insert(0, f"You adjusted this split to {fmt_time(float(dec.custom_pts * tb))}.")
        return rc
    s, e = dec.start_pts, dec.end_pts
    sec = lambda p: float(p * tb)
    if dec.state == "rejected":
        return ResolvedCut(dec.id, "rejected", "Rejected")
    inside, before, after = finder.options_for(s, e)
    nb_before = finder.nearest_verified(s, -1)
    nb_after = finder.nearest_verified(e, +1)
    nearby = [o for o in (nb_before, nb_after) if o is not None]
    rc = ResolvedCut(dec.id, "needs-review", "Needs review", inside=inside, nearby=nearby)
    if dec.choice == "skip":
        rc.status, rc.label = "skipped", "Skipped"
        return rc

    frac = {"start": 0.0, "middle": 0.5, "end": 1.0}.get(dec.position, 0.5)
    req = int(round(s + (e - s) * frac))
    rc.requested_pts = req

    by_pts = {o.pts: o for o in inside + before + after + nearby}

    # Explicit user choice of a specific boundary
    if dec.choice and dec.choice.startswith(("boundary:", "accept-drop:")):
        p = int(dec.choice.split(":")[1])
        o = by_pts.get(p) or finder.proof(p)
        if o.verified:
            rc.boundaries = [o]
            rc.delta_s = sec(o.pts - req)
            rc.outside_black = not (s <= o.pts <= e)
            if o.dropped_pts:
                rc.status, rc.label = "copy-drop", f"Copied · drops {len(o.dropped_pts)} black frame(s)"
                rc.messages.append(f"{len(o.dropped_pts)} undecodable open-GOP frame(s) "
                                   f"({o.dropped_s*1000:.0f} ms, inside the black interval) are dropped")
            else:
                rc.status, rc.label = "copy", "Copied" + (" · moved by you" if rc.outside_black else "")
            if rc.outside_black:
                d = sec(s - o.pts) if o.pts < s else sec(o.pts - e)
                rc.messages.append(f"Cut is {d:.3f}s {'before the black starts' if o.pts < s else 'after the black ends'}; "
                                   f"that picture moves to the {'next' if o.pts < s else 'previous'} file (nothing is lost)")
            return rc
        rc.messages.append("The boundary you chose could not be proven safe")

    # Black removal is a separate operation; a smart split must never silently
    # turn a requested removal into a split that keeps the padding.
    if dec.remove_black and not dec.manual:
        return _resolve_removal(dec, rc, inside, finder, policy, tb, nb_before, nb_after, reencode_supported, frames)

    clean = [o for o in inside if _ok_clean(o)]
    auto_smart = (policy.boundary_reencode and reencode_supported and not clean and dec.choice is None)
    if dec.choice == "reencode" or auto_smart:
        if reencode_supported and frames is not None:
            from . import smart
            fp, fd = frames
            try:
                T = _requested_frame(dec, req, fp)
                sc = smart.plan_cut(finder, T, fp, fd)
            except Exception as ex:
                rc.messages.append(f"Boundary re-encoding not possible here: {ex}")
            else:
                exact = BoundaryOption(pts=T, seconds=sec(T), packet_index=-1, structure="reencode", verified=True,
                                       verify_note="exact cut with boundary re-encoding")
                rc.boundaries = [exact]
                rc.smart = sc.to_json()
                rc.delta_s = sec(T - req)
                if T == sc.prev_key_pts:
                    rc.status, rc.label = "copy", "Copied"
                    rc.smart = None
                    rc.boundaries = [finder.proof(T)] if T != int(finder.key_pts[0]) else [exact]
                    return rc
                rc.status = "reencode"
                rc.label = f"Boundary re-encoding · {sc.tail_s + sc.head_s:.2f}s of video"
                rc.messages.append(
                    f"Exact cut at {fmt_time(sec(T))}. Re-encodes {sc.tail_frames} frame(s) ({sc.tail_s:.2f}s) at the end of the "
                    f"earlier part and {sc.head_frames} frame(s) "
                    f"({sc.head_s:.2f}s) at the start of the later part (until the keyframe at {fmt_time(sec(sc.next_key_pts))}). "
                    "Everything else, and all audio, is copied. Re-encoded frames are not lossless.")
                return rc
        else:
            rc.messages.append("Boundary re-encoding is not supported for this codec/container")

    drop = [o for o in inside if _ok_drop(o, s, e)]
    if clean:
        o = min(clean, key=lambda o: (abs(o.pts - req), o.pts))
        rc.boundaries = [o]
        rc.status, rc.label = "copy", "Copied"
        rc.delta_s = sec(o.pts - req)
        if dec.manual and o.pts != req:
            rc.messages.append(f"Nearest verified copy boundary is {rc.delta_s:+.3f}s from the requested point")
        return rc
    if drop:
        o = min(drop, key=lambda o: (len(o.dropped_pts), abs(o.pts - req)))
        if len(o.dropped_pts) <= policy.max_auto_drop_black_frames:
            rc.boundaries = [o]
            rc.status, rc.label = "copy-drop", f"Copied · drops {len(o.dropped_pts)} black frame(s)"
            rc.delta_s = sec(o.pts - req)
            rc.messages.append(f"Open GOP: {len(o.dropped_pts)} black leading frame(s) ({o.dropped_s*1000:.0f} ms) "
                               "cannot be decoded on either side of a copy cut and are dropped (allowed by your tolerance setting)")
            return rc
        rc.choices.append({"kind": "accept-drop", "choice": f"accept-drop:{o.pts}", "pts": o.pts,
                           "seconds": o.seconds,
                           "label": f"Copy cut at {fmt_time(o.seconds)} and drop {len(o.dropped_pts)} black frame(s) "
                                    f"({o.dropped_s*1000:.0f} ms) that cannot be decoded after a copy cut",
                           "delta_s": sec(o.pts - req)})
    # nothing inside: offer explicit alternatives, never apply them silently
    if not inside:
        rc.messages.append("No keyframe inside the black interval")
    else:
        bad = [o for o in inside if not o.verified]
        for o in bad:
            rc.messages.append(f"Keyframe at {fmt_time(o.seconds)} rejected: {o.verify_note or o.structure}")
    for o, where in ((nb_before, "before"), (nb_after, "after")):
        if o is None:
            continue
        gap = sec(s - o.pts) if where == "before" else sec(o.pts - e)
        moved = "the end of the earlier part moves into the next file" if where == "before" else \
            "the start of the later part stays in the earlier file"
        rc.choices.append({"kind": "nearby", "choice": f"boundary:{o.pts}", "pts": o.pts, "seconds": o.seconds,
                           "label": f"Cut {gap:.3f}s {where} the black ({fmt_time(o.seconds)}): {gap:.3f}s of picture — "
                                    f"{moved}. Nothing is deleted.",
                           "delta_s": sec(o.pts - req)})
    if reencode_supported and frames is not None:
        from . import smart
        try:
            fp, fd = frames
            sc = smart.plan_cut(finder, _requested_frame(dec, req, fp), fp, fd)
            est = f" (≈{sc.tail_s + sc.head_s:.2f}s of video re-encoded, audio copied)"
        except Exception:
            est = ""
        rc.choices.append({"kind": "reencode", "choice": "reencode",
                           "label": "Cut exactly inside the black with boundary re-encoding" + est})
    rc.choices.append({"kind": "skip", "choice": "skip", "label": "Skip this cut"})
    return rc


def _requested_frame(dec, req, frame_pts):
    """Keep a detected cut inside its half-open black interval, even at 'end'."""
    if not dec.manual:
        inside = frame_pts[(frame_pts >= dec.start_pts) & (frame_pts < dec.end_pts)]
        if len(inside):
            return int(inside[np.argmin(np.abs(inside - req))])
        raise ValueError("No decoded frame inside this black interval")
    i = int(np.searchsorted(frame_pts, req))
    return int(frame_pts[min(i, len(frame_pts) - 1)])


def _resolve_removal(dec, rc, inside, finder, policy, tb, nb_before, nb_after, reencode_supported=False, frames=None):
    s, e = dec.start_pts, dec.end_pts
    sec = lambda p: float(p * tb)
    r1 = s + int(round(dec.pad_before_s / tb))
    r2 = e - int(round(dec.pad_after_s / tb))
    if e in finder.by_pts:
        inside = inside + [finder.proof(e)]
    usable = [o for o in inside if _ok_clean(o) or (_ok_drop(o, s, e) and len(o.dropped_pts) <= policy.max_auto_drop_black_frames)]
    first_frame = int(finder.key_pts[0])
    cand_b1 = [o for o in usable]
    cand_b2 = [o for o in usable]
    pair = None
    best = None
    leading = s <= first_frame
    for b2 in cand_b2:
        if leading:
            cost = abs(b2.pts - r2)
            if best is None or cost < best:
                best, pair = cost, (None, b2)
            continue
        for b1 in cand_b1:
            if b1.pts < b2.pts:
                cost = abs(b1.pts - r1) + abs(b2.pts - r2)
                if best is None or cost < best:
                    best, pair = cost, (b1, b2)
    if pair and reencode_supported and (policy.boundary_reencode or dec.choice == "reencode"):
        b1, b2 = pair
        if (b1.pts if b1 else s) != r1 or b2.pts != r2:
            pair = None  # exact removal preserves the chosen padding
    if pair is None:
        if reencode_supported and frames is not None and (policy.boundary_reencode or dec.choice == "reencode"):
            from . import smart
            fp, fd = frames
            a = int(fp[min(int(np.searchsorted(fp, r1)), len(fp)-1)])
            b = int(fp[min(int(np.searchsorted(fp, r2)), len(fp)-1)]) if r2 < finder.sp.end_pts() else finder.sp.end_pts()
            try:
                if not s <= a < b <= e:
                    raise ValueError("Padding leaves no complete black frames to remove")
                edges = [smart.plan_cut(finder, p, fp, fd).to_json() for p in (a,b)
                         if first_frame < p < finder.sp.end_pts() and not (p in finder.by_pts and _ok_clean(finder.proof(p)))]
            except Exception as ex:
                rc.messages.append(f"Exact black removal not possible: {ex}")
            else:
                rc.removed = (a,b)
                rc.smart_edges = edges
                rc.boundaries = [BoundaryOption(pts=p, seconds=sec(p), packet_index=-1, structure="reencode", verified=True) for p in (a,b)]
                rc.status, rc.label = "reencode", f"Exact removal · {sec(b-a):.3f}s of black"
                rc.messages.append("Removes video and audio inside the reviewed black interval; only surrounding video boundaries are re-encoded.")
                return rc
        rc.messages.append("Removing black needs two verified copy boundaries inside the black interval; "
                           f"found {len(usable)}")
        rc.choices.append({"kind": "split-instead", "choice": "split-instead",
                           "label": "Keep the black and split only"})
        rc.choices.append({"kind": "skip", "choice": "skip", "label": "Skip this cut"})
        return rc
    b1, b2 = pair
    rc.boundaries = [b for b in pair if b is not None]
    rem_start = s if b1 is None else b1.pts
    rc.removed = (rem_start, b2.pts)
    rc.status = "copy-drop" if any(b.dropped_pts for b in rc.boundaries) else "copy"
    rc.label = f"Copied · removes {sec(b2.pts - rem_start):.3f}s of black"
    rc.messages.append(f"Removes video and all audio between {fmt_time(sec(rem_start))} and {fmt_time(sec(b2.pts))}")
    return rc


# ------------------------------------------------------------------ segments

@dataclass
class Segment:
    n: int
    start_pts: int
    end_pts: int
    start_s: float
    end_s: float
    name: str = ""
    drop_video_pts: list = field(default_factory=list)   # leading pictures excluded (open GOP)
    note: str = ""
    reencode_head: Optional[dict] = None   # SmartCut whose later part this segment is
    reencode_tail: Optional[dict] = None   # SmartCut whose earlier part this segment is

    @property
    def duration_s(self):
        return self.end_s - self.start_s

    def to_json(self):
        return asdict(self)


@dataclass
class Plan:
    source: str
    segments: list
    removed: list              # [(start_pts, end_pts)]
    dropped_frames: list       # pts of frames that will not appear anywhere
    issues: list               # blocking problems
    warnings: list
    time_base: str

    def to_json(self):
        return {"source": self.source, "segments": [s.to_json() for s in self.segments],
                "removed": self.removed, "dropped_frames": self.dropped_frames, "issues": self.issues,
                "warnings": self.warnings, "time_base": self.time_base}


def build_segments(info: MediaInfo, vsp: StreamPackets, resolved: list[ResolvedCut],
                   policy: Policy, names: Optional[list] = None) -> Plan:
    tb = vsp.time_base
    sec = lambda p: float(Fraction(int(p)) * tb)
    valid = vsp.pts[vsp.pts != NOPTS]
    t0, t_end = int(valid.min()), int(vsp.end_pts())
    issues, warnings = [], []
    points = []   # (pts, kind) kind: "split" | ("remove", start, end)
    dropped = []
    removed = []
    smart_at = {}
    for rc in resolved:
        if rc.status in ("rejected", "skipped"):
            continue
        if rc.status == "needs-review":
            issues.append(f"Cut {rc.decision_id} needs review: " + "; ".join(rc.messages[:2]))
            continue
        if rc.status == "reencode":
            if rc.removed:
                removed.append(tuple(int(p) for p in rc.removed))
                for edge in rc.smart_edges:
                    smart_at[edge['cut_pts']] = edge
            else:
                T = int(rc.boundaries[0].pts)
                points.append(T)
                smart_at[T] = rc.smart
            continue
        if rc.removed:
            a, b = rc.removed
            removed.append((int(a), int(b)))
        else:
            points.append(int(rc.boundaries[0].pts))
        for o in rc.boundaries:
            dropped.extend(int(p) for p in o.dropped_pts)
    # Build ordered boundary list with removal gaps
    edges = sorted(set(points))
    spans = []
    cur = t0
    events = sorted([(p, "split", None) for p in edges] + [(a, "remove", b) for a, b in removed])
    for p, kind, b in events:
        if kind == "split":
            spans.append((cur, p))
            cur = p
        else:
            spans.append((cur, p))
            cur = b
    spans.append((cur, t_end))
    segs = []
    dset = set(dropped)
    for a, b in spans:
        if b <= a:
            continue
        segs.append(Segment(n=len(segs) + 1, start_pts=a, end_pts=b, start_s=sec(a), end_s=sec(b)))
    # remove segments that are entirely black removal at file edges (zero length handled above)
    for s in segs:
        s.drop_video_pts = sorted(p for p in dset if s.start_pts <= p < s.end_pts)
        s.reencode_head = smart_at.get(s.start_pts)
        tl = smart_at.get(s.end_pts)
        s.reencode_tail = tl if tl and tl.get("tail_frames", 0) > 0 else None
        if s.duration_s < policy.min_segment_s:
            issues.append(f"Part {s.n} would be only {s.duration_s:.3f}s long (minimum {policy.min_segment_s:g}s)")
    stems = default_names(info.path, len(segs))
    for i, s in enumerate(segs):
        s.name = (names[i] if names and i < len(names) and names[i] else stems[i])
    return Plan(source=info.path, segments=segs, removed=removed, dropped_frames=sorted(dset),
                issues=issues, warnings=warnings, time_base=str(tb))


_EPISODE_RE = re.compile(r"\b(?:S\d{1,3}E\d{1,4}(?:E\d{1,4})*|\d{1,3}x\d{1,4})\b", re.I)
_TITLE_SEPARATOR = re.compile(r"\s+[-–—]\s+|\s*[;|_]\s*")


def default_names(path: str, n: int) -> list[str]:
    """Neutral names. Titles are never guessed automatically: an intro, credits or a
    missed boundary would silently mislabel files."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return [f"{stem} - Part {i+1:02d}" for i in range(n)]


def title_suggestions(path: str) -> tuple[str, list[str]]:
    """Separate show/episode metadata from human titles; preserve hyphens inside words.

    Examples: Show - S01E01 - First - Second, Show 1x01 (01) First; Second,
    and Show (2002) - S01E04 - First_Second[1080p][ENGCC].
    Suggestions require review; they do not identify content inside the video.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    clean = re.sub(r"(?:\s*\[[^\]]*\])+\s*$", "", stem).strip()
    clean = re.sub(r"\s*\((?:\d{3,4}p|WEB[- ]?DL|BluRay|HDTV|DVD)[^)]*\)\s*$", "", clean, flags=re.I)
    episode = _EPISODE_RE.search(clean)
    if episode:
        end = episode.end()
        number = re.match(r"\s*\(\d{1,3}\)", clean[end:])
        if number: end += number.end()
        prefix, tail = clean[:end].strip(), clean[end:].strip(" -–—_;|")
        titles = [t.strip() for t in _TITLE_SEPARATOR.split(tail) if t.strip()]
        return prefix, titles
    chunks = [t.strip() for t in _TITLE_SEPARATOR.split(clean) if t.strip()]
    if len(chunks) >= 3:
        return chunks[0], chunks[1:]
    if len(chunks) == 2:
        return "", chunks
    return stem, []


def suggested_names(prefix: str, titles: list[str]) -> list[str]:
    """Numbered, collision-resistant stems for the naming preview."""
    return [safe_filename(f"{prefix + ' - ' if prefix else ''}{i+1:02d} {title}")
            for i, title in enumerate(titles)]


def safe_filename(name: str) -> str:
    bad = '/\\:\0'
    s = "".join("_" if ch in bad else ch for ch in name).strip().strip(".")
    return s[:200] or "part"
