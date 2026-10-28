"""One source file's working state: analysis, candidates, decisions, plan.

Used by both the command line and the desktop interface. Saved projects store the
decisions and settings (not the cached analysis, which is re-used through the
fingerprinted cache).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from fractions import Fraction
from typing import Optional

from . import ENGINE_VERSION
from .boundaries import BoundaryFinder
from .detect import (AnalysisRegion, DetectSettings, ScanSettings, PRESETS, FrameStats, analyze,
                     find_candidates, measure_audio, verify_candidates, Candidate, recommend_splits,
                     settings_windows, video_bounds)
from . import episodes
from .media import MediaInfo, probe, build_packet_index, fmt_time
from .planner import CutDecision, Policy, ResolvedCut, resolve, build_segments, Plan
from . import smart


class SourceSession:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.info: MediaInfo = probe(self.path)
        self.idx = None
        self.stats: Optional[FrameStats] = None
        self.scan = ScanSettings()
        self.detect = DetectSettings(**asdict(PRESETS["balanced"]))
        self.preset = "balanced"
        self.policy = Policy()
        self.candidates: list[Candidate] = []
        self.decisions: dict[int, CutDecision] = {}
        self.resolved: dict[int, ResolvedCut] = {}
        self.finder: Optional[BoundaryFinder] = None
        self.names: list = []
        self._next_manual = 10000
        self.episode = episodes.parse(self.path)
        # The expected number of story segments follows the file name (two episode
        # numbers or two segment titles = two segments) until you choose a number.
        self.segments_auto = True
        self.detect.segments = self.guess_segments()

    def guess_segments(self, default: int = 2) -> int:
        hint = self.episode.segment_hint() if self.episode else None
        return max(1, min(6, hint or default))

    def set_segments(self, n: Optional[int], fractions=None, default: int = 2):
        """Expected story segments; None returns to the file-name guess (or `default`)."""
        self.segments_auto = n is None
        self.detect.segments = self.guess_segments(default) if n is None else max(1, min(6, int(n)))
        self.detect.break_fractions = list(fractions) if fractions and len(fractions) == self.detect.segments - 1 else None

    # ------------------------------------------------------------- analysis
    @property
    def vtb(self) -> Fraction:
        return self.info.video.time_base

    def index(self, progress=None, cancel=None):
        if self.idx is None:
            self.idx = build_packet_index(self.path, progress=progress, cancel=cancel)
            self.finder = BoundaryFinder(self.path, self.info, self.idx[self.info.video.index])
        return self.idx

    def analyze(self, progress=None, cancel=None):
        self.index(cancel=cancel)
        self.stats = analyze(self.path, self.info, self.scan, progress=progress, cancel=cancel)
        return self.stats

    def set_preset(self, name: str):
        self.preset = name
        if name in PRESETS:
            keep = (self.detect.segments, self.detect.break_fractions)
            self.detect = DetectSettings(**asdict(PRESETS[name]))
            self.detect.segments, self.detect.break_fractions = keep

    def find(self, audio_signal: bool = True):
        if self.stats is None:
            raise RuntimeError("analyse first")
        cands = find_candidates(self.stats, self.detect, self.info.duration_s)
        if self.stats.decimate > 1:
            cands = verify_candidates(self.path, self.info, self.stats, cands, self.detect, self.scan)
        if audio_signal:
            try:
                measure_audio(self.path, self.info, cands)
            except Exception:
                pass
        # A split you kept stays available even when new settings no longer suggest it
        # (for example after changing the number of segments or the preset).
        found = {(c.start_pts, c.end_pts) for c in cands}
        kept_before = {(d.start_pts, d.end_pts) for d in self.decisions.values() if not d.manual and d.state == "accepted"}
        carried = [Candidate(**{**c.to_json(), "suppressed": False, "note": "Kept from an earlier detection"})
                   for c in self.candidates if (c.start_pts, c.end_pts) in kept_before - found]
        if carried:
            cands = sorted(cands + carried, key=lambda c: c.start_pts)
            for i, c in enumerate(cands):
                c.id = i
        self.candidates = cands
        old = {(d.start_pts, d.end_pts): d for d in self.decisions.values() if not d.manual}
        manual = {k: d for k, d in self.decisions.items() if d.manual}
        self.decisions = dict(manual)
        for c in cands:
            prev = old.get((c.start_pts, c.end_pts))
            if prev:
                prev.id = c.id
                self.decisions[c.id] = prev
            else:
                state = "proposed"
                if c.suppressed or c.leading or c.trailing:
                    state = "rejected"
                self.decisions[c.id] = CutDecision(id=c.id, start_pts=c.start_pts, end_pts=c.end_pts, state=state,
                                                   remove_black=(c.leading or c.trailing))
        self.resolved = {}
        return cands

    # ------------------------------------------------------------- recommendations
    def break_windows(self):
        return settings_windows(self.stats, self.detect) if self.stats is not None else []

    def recommended(self, include_suppressed: bool = True) -> list[Optional[Candidate]]:
        """One suggested split per expected segment break (None where no black transition was found).

        Skipped suggestions drop out so the next-best transition near that break is offered.
        """
        if self.stats is None or self.detect.segments < 2:
            return []
        eligible = [c for c in self.candidates if (include_suppressed or not c.suppressed)
                    and self.decisions.get(c.id) and self.decisions[c.id].state != "rejected"]
        return recommend_splits(eligible, self.break_windows())

    def accept_recommended(self) -> list[int]:
        """Keep every recommended split that is still only proposed. Returns the decision ids."""
        ids = []
        for c in self.recommended():
            if c is not None and self.decisions[c.id].state == "proposed":
                self.decisions[c.id].state = "accepted"
                ids.append(c.id)
        return ids

    def part_count(self) -> int:
        """Parts this file will produce: kept splits + 1, or the expected segments before review."""
        kept = len(self.accepted())
        if kept or self.stats is not None and not any(d.state == "proposed" for d in self.decisions.values()):
            return kept + 1
        return max(1, self.detect.segments)

    # ------------------------------------------------------------- decisions
    def add_manual(self, seconds: float) -> CutDecision:
        p = int(round(seconds / self.vtb))
        d = CutDecision(id=self._next_manual, start_pts=p, end_pts=p, state="accepted", manual=True,
                        position="start")
        self._next_manual += 1
        self.decisions[d.id] = d
        return d

    def reencode_supported(self) -> bool:
        return smart.supported(self.info)[0]

    def resolve(self, ids=None):
        self.index()
        for k, d in self.decisions.items():
            if ids is not None and k not in ids:
                continue
            if d.state == "rejected":
                self.resolved[k] = ResolvedCut(k, "rejected", "Rejected")
                continue
            frames = (self.stats.pts, self.stats.duration) if self.stats is not None else None
            self.resolved[k] = resolve(d, self.finder, self.policy, self.vtb, self.reencode_supported(), frames=frames)
        return self.resolved

    def accepted(self):
        return [d for d in self.decisions.values() if d.state == "accepted"]

    def plan(self, resolve: bool = True) -> Plan:
        """Build output parts. With resolve=False only already-resolved cuts are used
        (cheap, no decoding; unresolved accepted cuts are reported as issues)."""
        acc = sorted(self.accepted(), key=lambda d: d.start_pts)
        if resolve:
            self.resolve([d.id for d in acc])
        rcs = []
        for d in acc:
            rc = self.resolved.get(d.id)
            if rc is None:
                rc = ResolvedCut(d.id, "needs-review", "Checking…", messages=["copy boundaries not checked yet"])
            rcs.append(rc)
        return build_segments(self.info, self.idx[self.info.video.index], rcs, self.policy, self.names)

    # ------------------------------------------------------------- persistence
    def to_project(self) -> dict:
        return {"app": "Banana Split Redux", "engine": ENGINE_VERSION, "source": self.path,
                "fingerprint": self.info.fingerprint, "preset": self.preset, "scan": self.scan.to_json(),
                "detect": self.detect.to_json(), "policy": asdict(self.policy),
                "decisions": [d.to_json() for d in self.decisions.values()], "names": self.names,
                "segments_auto": self.segments_auto}

    def save_project(self, path: str):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_project(), f, indent=1)
        os.replace(tmp, path)

    @classmethod
    def load_project(cls, path: str) -> "SourceSession":
        with open(path, encoding="utf-8") as f:
            pj = json.load(f)
        s = cls(pj["source"])
        if pj.get("fingerprint") and pj["fingerprint"] != s.info.fingerprint:
            raise ValueError("The source file has changed since this project was saved")
        s.preset = pj.get("preset", "custom")
        s.scan = ScanSettings(region=AnalysisRegion(**pj["scan"]["region"]), decimate=pj["scan"]["decimate"])
        s.detect = DetectSettings(**pj["detect"])
        s.policy = Policy(**pj.get("policy", {}))
        s.decisions = {d["id"]: CutDecision(**d) for d in pj.get("decisions", [])}
        s.names = pj.get("names", [])
        s.segments_auto = bool(pj.get("segments_auto", "segments" not in pj["detect"]))
        if s.decisions:
            s._next_manual = max(10000, max(s.decisions) + 1)
        return s
