"""Black-frame analysis.

The expensive pass decodes every presented frame once (for analysis only - this
never affects exported media) and stores, per frame, the fraction of pixels at
or below each of a grid of darkness thresholds. Changing the darkness threshold,
black-pixel percentage, minimum duration or spacing afterwards is instant and
needs no re-decode. Changing the analysis region (crop) or scan resolution does.

Darkness is measured on luma code values normalised by the stream's signalled
range and bit depth: limited ("tv") range maps 16..235 (scaled for >8 bit) to
0..1, full ("pc") range maps 0..max. Values below nominal black (common on VHS
captures) count as black. For HDR transfer functions the thresholds apply to the
normalised PQ/HLG code value, not to nits - this is reported in the analysis.

A frame is black only when a large *percentage of pixels* is dark, never because
its average brightness is low, so dark scenes with visible detail are not black.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from typing import Callable, Optional

import av
import numpy as np

from . import cache
from .media import MediaInfo, Cancelled, fingerprint, open_input

# Darkness thresholds (normalised luma) at which the fraction of dark pixels is stored.
T_GRID = np.round(np.arange(0.0, 0.2501, 0.005), 4)


@dataclass
class AnalysisRegion:
    """Analysis-only crop, as fractions of width/height removed from each edge."""
    left: float = 0.04
    top: float = 0.04
    right: float = 0.04
    bottom: float = 0.04

    def pixels(self, w: int, h: int):
        x0 = int(round(w * self.left)); x1 = w - int(round(w * self.right))
        y0 = int(round(h * self.top)); y1 = h - int(round(h * self.bottom))
        if x1 - x0 < 8 or y1 - y0 < 8:
            raise ValueError("Analysis region is too small")
        return x0, y0, x1, y1


@dataclass
class ScanSettings:
    """Settings that require decoding again when changed."""
    region: AnalysisRegion = field(default_factory=AnalysisRegion)
    decimate: int = 1          # 1 = every pixel; 2/4 = approximate fast scan

    def to_json(self):
        return asdict(self)


@dataclass
class DetectSettings:
    """Settings applied to cached frame statistics (instant)."""
    pixel_threshold: float = 0.10   # normalised luma at/below which a pixel is "dark"
    black_ratio: float = 0.98       # fraction of region pixels that must be dark
    min_frames: int = 1             # shortest interval in frames
    min_duration_s: float = 0.0     # shortest interval in seconds (both must hold)
    min_spacing_s: float = 20.0     # proposed cuts closer than this are suppressed
    bridge_frames: int = 0          # join runs separated by <= this many non-black frames

    midpoint_boost: bool = False    # relax detection in the central 40–60% only

    def to_json(self):
        return asdict(self)


PRESETS = {
    "conservative": DetectSettings(pixel_threshold=0.08, black_ratio=0.99, min_frames=3,
                                   min_duration_s=0.10, min_spacing_s=60.0, midpoint_boost=True),
    "balanced": DetectSettings(pixel_threshold=0.10, black_ratio=0.98, min_frames=2,
                               min_duration_s=0.0, min_spacing_s=20.0, midpoint_boost=True),
    "sensitive": DetectSettings(pixel_threshold=0.12, black_ratio=0.95, min_frames=1,
                                min_duration_s=0.0, min_spacing_s=5.0, midpoint_boost=True),
    "single-frame": DetectSettings(pixel_threshold=0.10, black_ratio=0.97, min_frames=1,
                                   min_duration_s=0.0, min_spacing_s=1.0, midpoint_boost=True),
}


@dataclass
class FrameStats:
    stream_index: int
    time_base: Fraction
    pts: np.ndarray         # int64, presentation order
    duration: np.ndarray    # int64
    frac: np.ndarray        # float32 [n, len(T_GRID)] fraction of pixels <= threshold
    mean: np.ndarray        # float32 normalised mean luma
    p99: np.ndarray         # float32 normalised 99th percentile luma
    corrupt: np.ndarray     # bool, decoder flagged frame as corrupt
    range_used: str = "tv"
    bit_depth: int = 8
    hdr: bool = False
    decimate: int = 1
    region_px: tuple = (0, 0, 0, 0)
    elapsed_s: float = 0.0

    def __len__(self):
        return len(self.pts)

    def frac_at(self, thr: float) -> np.ndarray:
        """Fraction of dark pixels at an arbitrary threshold (linear interp on the grid)."""
        thr = float(np.clip(thr, T_GRID[0], T_GRID[-1]))
        j = int(np.searchsorted(T_GRID, thr, side="right") - 1)
        j = min(j, len(T_GRID) - 2)
        a = (thr - T_GRID[j]) / (T_GRID[j + 1] - T_GRID[j])
        return self.frac[:, j] * (1 - a) + self.frac[:, j + 1] * a

    def seconds(self, pts) -> float:
        return float(Fraction(int(pts)) * self.time_base)

    def to_npz(self):
        return {"pts": self.pts, "duration": self.duration, "frac": self.frac, "mean": self.mean,
                "p99": self.p99, "corrupt": self.corrupt,
                "meta": np.array([self.stream_index, self.time_base.numerator, self.time_base.denominator,
                                  self.bit_depth, int(self.hdr), self.decimate, *self.region_px], dtype=np.int64),
                "range": np.array([self.range_used]), "elapsed": np.array([self.elapsed_s])}

    @classmethod
    def from_npz(cls, d):
        m = d["meta"]
        return cls(stream_index=int(m[0]), time_base=Fraction(int(m[1]), int(m[2])), pts=d["pts"],
                   duration=d["duration"], frac=d["frac"], mean=d["mean"], p99=d["p99"], corrupt=d["corrupt"],
                   range_used=str(d["range"][0]), bit_depth=int(m[3]), hdr=bool(m[4]), decimate=int(m[5]),
                   region_px=tuple(int(x) for x in m[6:10]), elapsed_s=float(d["elapsed"][0]))


def _luma_plane(frame) -> tuple[np.ndarray, int, str]:
    """Return (Y as ndarray, bit depth, assumed range) without any range conversion."""
    name = frame.format.name
    if name.startswith(("yuv", "nv", "gray", "p01", "p21", "p41", "y210", "yuyv")) and not name.startswith(("yuyv", "y210")):
        p = frame.planes[0]
        bits = max(c.bits for c in frame.format.components)
        if bits <= 8:
            arr = np.frombuffer(p, np.uint8).reshape(p.height, p.line_size)[:, : p.width]
        else:
            arr = np.frombuffer(p, np.uint16).reshape(p.height, p.line_size // 2)[:, : p.width]
            if name.startswith("p01"):          # MSB-aligned formats
                arr = arr >> (16 - bits)
        rng = "pc" if name.startswith("yuvj") else None
        return arr, bits, rng
    g = frame.reformat(format="gray")
    p = g.planes[0]
    return np.frombuffer(p, np.uint8).reshape(p.height, p.line_size)[:, : p.width], 8, "pc"


def analyze(path: str, info: MediaInfo, scan: ScanSettings = None,
            progress: Optional[Callable[[float], None]] = None, cancel=None,
            window: Optional[tuple[float, float]] = None, use_cache: bool = True) -> FrameStats:
    """Decode the first video stream and collect per-frame darkness statistics.

    window=(start_s, end_s) restricts analysis to a time range (used to verify
    fast-scan candidates at full resolution); results for windows are not cached.
    """
    scan = scan or ScanSettings()
    v = info.video
    k = cache.key("luma-v3", info.fingerprint, v.index, scan.to_json())
    if use_cache and window is None:
        hit = cache.load_npz(k)
        if hit is not None:
            return FrameStats.from_npz(hit)

    t0 = time.time()
    c = open_input(path)
    try:
        st = c.streams[v.index]
        st.thread_type = "AUTO"
        tb = Fraction(st.time_base)
        signalled = v.color_range  # "tv"/"pc"/None
        rng = signalled or "tv"
        bits_seen = v.bit_depth
        pts_l, frac_l, mean_l, p99_l, corrupt_l, dur_l = [], [], [], [], [], []
        lut_edges = None
        region_px = None
        total = info.duration_s or 1.0
        start_pts = None
        if window is not None:
            seek_s = max(0.0, window[0] - 3.0)
            c.seek(int(seek_s / tb), stream=st, backward=True, any_frame=False)
        n = 0
        for frame in c.decode(st):
            if frame.pts is None:
                continue
            t = float(frame.pts * tb)
            if window is not None:
                if t < window[0]:
                    continue
                if t >= window[1]:
                    break
            y, bits, forced_rng = _luma_plane(frame)
            if lut_edges is None:
                bits_seen = bits
                rng = forced_rng or rng
                h, w = y.shape
                region_px = scan.region.pixels(w, h)
                s = 1 << (bits - 8)
                lo, hi = (16 * s, 235 * s) if rng == "tv" else (0, (1 << bits) - 1)
                # Pixel counts with code <= edge[k] are "dark at threshold T_GRID[k]".
                lut_edges = np.floor(lo + T_GRID * (hi - lo)).astype(np.int64)
                norm = (lo, hi)
                nbins = 1 << bits
            x0, y0, x1, y1 = region_px
            roi = y[y0:y1:scan.decimate, x0:x1:scan.decimate]
            hist = np.bincount(roi.ravel(), minlength=nbins)
            cum = np.cumsum(hist)
            npx = cum[-1]
            frac_l.append((cum[lut_edges] / npx).astype(np.float32))
            codes = np.arange(nbins)
            mean_code = float((hist * codes).sum()) / npx
            mean_l.append((mean_code - norm[0]) / (norm[1] - norm[0]))
            p99_code = int(np.searchsorted(cum, 0.99 * npx))
            p99_l.append((p99_code - norm[0]) / (norm[1] - norm[0]))
            pts_l.append(frame.pts)
            dur_l.append(frame.duration or 0)
            corrupt_l.append(bool(getattr(frame, "is_corrupt", False)))
            n += 1
            if n % 250 == 0:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if progress:
                    if window is None:
                        progress(min(1.0, t / total))
                    else:
                        progress(min(1.0, (t - window[0]) / max(1e-6, window[1] - window[0])))
        if not pts_l:
            raise ValueError("No decodable video frames in the analysed range")
        pts = np.array(pts_l, dtype=np.int64)
        order = np.argsort(pts, kind="stable")
        pts = pts[order]
        dur = np.array(dur_l, dtype=np.int64)[order]
        # Actual frame durations come from presentation timestamps (handles VFR).
        if len(pts) > 1:
            d = np.diff(pts)
            dur[:-1] = np.where(d > 0, d, dur[:-1])
            if dur[-1] <= 0:
                dur[-1] = int(np.median(d))
        fs = FrameStats(stream_index=v.index, time_base=tb, pts=pts, duration=dur,
                        frac=np.array(frac_l, dtype=np.float32)[order],
                        mean=np.array(mean_l, dtype=np.float32)[order],
                        p99=np.array(p99_l, dtype=np.float32)[order],
                        corrupt=np.array(corrupt_l, dtype=bool)[order],
                        range_used=rng, bit_depth=bits_seen, hdr=v.is_hdr, decimate=scan.decimate,
                        region_px=tuple(region_px), elapsed_s=time.time() - t0)
        if use_cache and window is None:
            cache.save_npz(k, fs.to_npz())
        if progress:
            progress(1.0)
        return fs
    finally:
        c.close()


# ---------------------------------------------------------------- candidates

@dataclass
class Candidate:
    id: int
    start_pts: int            # first black frame pts (inclusive)
    end_pts: int              # pts just after the last black frame (exclusive)
    start_s: float
    end_s: float
    n_frames: int
    leading: bool             # touches the start of the file
    trailing: bool            # touches the end of the file
    purity: float             # mean dark-pixel fraction over the interval
    kind: str                 # "hard" | "fade-out" | "fade-in" | "fade" | "flash"
    score: float
    suppressed: bool = False
    note: str = ""
    audio_db: Optional[float] = None   # optional ranking signal
    approximate: bool = False          # from a fast scan, not yet verified

    near_midpoint: bool = False
    midpoint_only: bool = False        # needs the central sensitivity/minimum-frame relaxation

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def to_json(self):
        return asdict(self)


def video_bounds(fs: FrameStats) -> tuple[float, float]:
    """Actual presentation span, including VFR and nonzero first-frame timestamps."""
    if not len(fs):
        return 0.0, 0.0
    return fs.seconds(fs.pts[0]), fs.seconds(fs.pts[-1] + fs.duration[-1])


def midpoint_window(fs: FrameStats, bounds=None) -> tuple[float, float]:
    start, end = video_bounds(fs) if bounds is None else bounds
    span = max(0.0, end - start)
    return start + span * 0.4, start + span * 0.6


def _midpoint_frames(fs: FrameStats, bounds=None) -> np.ndarray:
    lo, hi = midpoint_window(fs, bounds)
    times = fs.pts.astype(np.float64) * float(fs.time_base)
    return (times >= lo) & (times < hi)


def midpoint_thresholds(ds: DetectSettings) -> tuple[float, float]:
    # Only modestly relax pixel/area thresholds; a dark scene with visible detail
    # must still fail. Respect settings already more sensitive than the boost floor.
    return min(float(T_GRID[-1]), ds.pixel_threshold + 0.025), min(ds.black_ratio, max(0.90, ds.black_ratio - 0.03))


def black_mask(fs: FrameStats, ds: DetectSettings, *, bounds=None) -> np.ndarray:
    base = fs.frac_at(ds.pixel_threshold) >= ds.black_ratio
    if not ds.midpoint_boost:
        return base
    threshold, ratio = midpoint_thresholds(ds)
    return base | ((fs.frac_at(threshold) >= ratio) & _midpoint_frames(fs, bounds))


def midpoint_recommendation(cands: list[Candidate], midpoint: float) -> Optional[Candidate]:
    eligible = [c for c in cands if c.near_midpoint and not c.leading and not c.trailing]
    if not eligible:
        return None
    # Prioritize closeness to halfway, using transition confidence to break ties.
    return min(eligible, key=lambda c: (abs((c.start_s + c.end_s) / 2 - midpoint), -c.score, c.start_s))


def find_candidates(fs: FrameStats, ds: DetectSettings, duration_s: Optional[float] = None,
                    *, bounds=None) -> list[Candidate]:
    base = fs.frac_at(ds.pixel_threshold) >= ds.black_ratio
    central = _midpoint_frames(fs, bounds)
    blk = black_mask(fs, ds, bounds=bounds)
    n = len(blk)
    if n == 0:
        return []
    # bridge tiny non-black gaps if requested
    if ds.bridge_frames > 0:
        i = 0
        while i < n:
            if not blk[i]:
                j = i
                while j < n and not blk[j]:
                    j += 1
                if 0 < i and j < n and (j - i) <= ds.bridge_frames:
                    blk[i:j] = True
                i = j
            else:
                i += 1
    runs = []
    i = 0
    while i < n:
        if blk[i]:
            j = i
            while j < n and blk[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    frac = fs.frac_at(ds.pixel_threshold)
    if ds.midpoint_boost:
        frac = np.where(central, fs.frac_at(midpoint_thresholds(ds)[0]), frac)
    out: list[Candidate] = []
    for a, b in runs:
        nfr = b - a
        start = int(fs.pts[a])
        end = int(fs.pts[b]) if b < n else int(fs.pts[b - 1] + fs.duration[b - 1])
        s0, s1 = fs.seconds(start), fs.seconds(end)
        near_midpoint = bool(central[a:b].any())
        short = nfr < ds.min_frames or (s1 - s0) + 1e-9 < ds.min_duration_s
        boosted = ds.midpoint_boost and near_midpoint
        if short and not boosted:
            continue
        midpoint_only = boosted and (short or bool((central[a:b] & ~base[a:b]).any()))
        kind = _classify(fs, a, b)
        purity = float(frac[a:b].mean())
        score = _score(nfr, s1 - s0, purity, kind)
        out.append(Candidate(id=len(out), start_pts=start, end_pts=end, start_s=s0, end_s=s1, n_frames=nfr,
                             leading=(a == 0), trailing=(b == n), purity=purity, kind=kind, score=score,
                             approximate=fs.decimate > 1, near_midpoint=near_midpoint,
                             midpoint_only=midpoint_only))
    start, end = video_bounds(fs) if bounds is None else bounds
    preferred = midpoint_recommendation(out, (start + end) / 2) if ds.midpoint_boost else None
    _suppress(out, ds.min_spacing_s)
    # Keep the best midpoint option visible without demoting stronger ordinary
    # transitions elsewhere. It is an extra proposal, never an accepted cut.
    if preferred:
        preferred.suppressed = False
        preferred.note = ""
    return out


def _classify(fs: FrameStats, a: int, b: int) -> str:
    m = fs.mean
    look = 8

    def ramp(seq):
        # monotone-ish darkening over the preceding frames, ending near black
        if len(seq) < 4:
            return False
        d = np.diff(seq)
        return (d <= 0.01).mean() >= 0.75 and (seq[0] - seq[-1]) > 0.08

    pre = m[max(0, a - look):a]
    post = m[b:b + look][::-1]
    fo = ramp(pre)
    fi = ramp(post)
    if b - a == 1 and not fo and not fi:
        return "flash"
    if fo and fi:
        return "fade"
    if fo:
        return "fade-out"
    if fi:
        return "fade-in"
    return "hard"


def _score(nfr, dur, purity, kind) -> float:
    s = 40 * min(1.0, dur / 0.5) + 40 * max(0.0, (purity - 0.9) / 0.1) + 10 * min(1.0, nfr / 5)
    s += {"hard": 10, "fade": 8, "fade-out": 6, "fade-in": 6, "flash": 0}[kind]
    return round(min(100.0, s), 1)


def _suppress(cands: list[Candidate], spacing: float):
    if spacing <= 0:
        return
    kept: list[Candidate] = []
    for c in sorted(cands, key=lambda c: (-c.score, c.start_s)):
        mid = (c.start_s + c.end_s) / 2
        clash = next((k for k in kept if abs((k.start_s + k.end_s) / 2 - mid) < spacing), None)
        if clash is not None:
            c.suppressed = True
            c.note = f"within {spacing:g}s of a stronger candidate at {clash.start_s:.2f}s"
        else:
            kept.append(c)


def measure_audio(path: str, info: MediaInfo, cands: list[Candidate], stream_index: Optional[int] = None):
    """Optional ranking signal: RMS level (dBFS) of one audio track inside each interval."""
    auds = info.of_type("audio")
    if not auds or not cands:
        return
    si = stream_index if stream_index is not None else next(
        (a.index for a in auds if "default" in a.disposition), auds[0].index)
    c = open_input(path)
    try:
        st = c.streams[si]
        for cand in cands:
            lo, hi = cand.start_s, max(cand.end_s, cand.start_s + 0.02)
            c.seek(int(max(0, lo - 1.0) / st.time_base), stream=st, backward=True)
            acc, cnt = 0.0, 0
            for fr in c.decode(st):
                if fr.pts is None:
                    continue
                t0 = float(fr.pts * st.time_base)
                if t0 > hi:
                    break
                arr = fr.to_ndarray().astype(np.float64)
                if arr.ndim == 2 and fr.format.is_planar:
                    arr = arr.mean(axis=0)
                elif arr.ndim == 2:
                    arr = arr.reshape(-1)
                if fr.format.name.startswith("s16"):
                    arr = arr / 32768.0
                elif fr.format.name.startswith("s32"):
                    arr = arr / 2147483648.0
                ts = t0 + np.arange(len(arr)) / fr.sample_rate
                sel = (ts >= lo) & (ts < hi)
                if sel.any():
                    acc += float((arr[sel] ** 2).sum()); cnt += int(sel.sum())
            cand.audio_db = round(10 * math.log10(acc / cnt + 1e-12), 1) if cnt else None
            if cand.audio_db is not None and cand.audio_db < -50:
                cand.score = round(min(100.0, cand.score + 5), 1)
    finally:
        c.close()


def verify_candidates(path: str, info: MediaInfo, fs_fast: FrameStats, cands: list[Candidate],
                      ds: DetectSettings, scan: ScanSettings) -> list[Candidate]:
    """Re-measure fast-scan candidates at full resolution; drop those that do not hold."""
    out = []
    for c in cands:
        full = analyze(path, info, ScanSettings(region=scan.region, decimate=1),
                       window=(max(0.0, c.start_s - 0.5), c.end_s + 0.5), use_cache=False)
        sub = find_candidates(full, DetectSettings(**{**ds.to_json(), "min_spacing_s": 0}),
                              bounds=video_bounds(fs_fast))
        matching = [x for x in sub if x.start_pts < c.end_pts and x.end_pts > c.start_pts]
        best = max(matching, key=lambda x: x.n_frames, default=None)
        if best is None:
            continue
        best.id = c.id
        best.leading, best.trailing = c.leading and best.start_pts <= c.start_pts, c.trailing
        best.approximate = False
        out.append(best)
    return out
