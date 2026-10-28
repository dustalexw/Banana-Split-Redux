"""Episode numbers for split parts, matching TVDB/TMDB season listings.

Animated series are often distributed as one file per broadcast half-hour that
contains two or three story segments, while TVDB and TMDB list every segment as
its own episode (``S01E01`` and ``S01E02`` for the two halves of the first file).
This module works out which database episode each output part is.

Sources of truth, strongest first:

1. **Segment titles that match the database.** ``Show - S01E01 - Help Wanted -
   Reef Blower.mkv`` is matched title by title against the season listing.
2. **Episode ranges in the file name.** ``S01E01-E02``, ``S01E01E02``, ``1x01-02``.
3. **Season order.** Files are numbered consecutively in episode order. A file
   whose number skips ahead (because earlier files are not in the queue) is
   assumed to follow files with the default number of segments.

Nothing here identifies what is actually inside the video; every suggestion is
shown for review before names change.
"""
from __future__ import annotations

import difflib
import os
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Optional

QUALITY = re.compile(r"(?:\b|[._ ])(?:2160p|1080p|1080i|720p|576p|480p|4k|uhd|web[- .]?dl|webrip|web|bluray|blu-ray|bdrip|"
                     r"brrip|hdtv|dvdrip|dvd|remux|x264|x265|h\.?264|h\.?265|hevc|avc|aac\d?(?:\.\d)?|ac3|dd\d\.\d|"
                     r"ddp\d\.\d|10bit|8bit|amzn|nf|dsnp|hmax|hulu|pmtp|repack|proper)\b.*$", re.I)
# S01E01, S01E01E02, S01E01-E02, S01E01-02, S01E01-S01E02 and 1x01, 1x01-02, 1x01-1x02
EPISODE = re.compile(
    r"(?<![A-Za-z0-9])S(?P<s>\d{1,3})\s*E(?P<e>\d{1,4})(?P<more>(?:\s*(?:-\s*|&\s*|\+\s*)?(?:S\d{1,3})?\s*E?\d{1,4}(?![\dpi]))*)"
    r"|(?<![A-Za-z0-9])(?P<s2>\d{1,2})x(?P<e2>\d{1,4})(?P<more2>(?:\s*-\s*(?:\d{1,2}x)?\d{1,4}(?![\dpi]))*)",
    re.I)
_MORE_NUMBER = re.compile(r"(?:S\d{1,3})?\s*E?(\d{1,4})", re.I)
_MORE_NUMBER_X = re.compile(r"(?:\d{1,2}x)?(\d{1,4})", re.I)
TITLE_SEPARATOR = re.compile(r"\s+[-–—+]\s+|\s*[;|_]\s*|\s+/\s+")
YEAR = re.compile(r"[\s.(\[]*\(?((?:19|20)\d\d)\)?[\])\s.]*$")


@dataclass
class EpisodeRef:
    """What a file name says about its show, season, episode numbers and segment titles."""
    show: str
    season: int
    episodes: list[int]
    titles: list[str] = field(default_factory=list)
    year: Optional[int] = None

    @property
    def first(self) -> int:
        return self.episodes[0]

    def segment_hint(self) -> Optional[int]:
        """How many story segments the name implies, if it says."""
        if len(self.episodes) > 1:
            return len(self.episodes)
        if len(self.titles) > 1:
            return len(self.titles)
        return None

    def to_json(self):
        return asdict(self)


def _clean_show(text: str) -> tuple[str, Optional[int]]:
    t = text.strip(" -–—_.[](){}")
    if "." in t and " " not in t:
        t = t.replace(".", " ")
    t = t.replace("_", " ").strip(" -–—.")
    year = None
    m = YEAR.search(t)
    if m and m.start() > 0:
        year = int(m.group(1))
        t = t[:m.start()].strip(" -–—.([")
    return re.sub(r"\s{2,}", " ", t).strip(), year


def _clean_titles(tail: str) -> list[str]:
    tail = re.sub(r"(?:\s*\[[^\]]*\])+", " ", tail)
    tail = re.sub(r"\s*\((?:\d{3,4}p|web[- ]?dl|bluray|hdtv|dvd)[^)]*\)", " ", tail, flags=re.I)
    if "." in tail and " " not in tail.strip(" .-"):
        tail = tail.replace(".", " ")
    tail = QUALITY.sub("", tail).strip(" -–—_.;|+")
    return [t.strip(" .") for t in TITLE_SEPARATOR.split(tail) if t.strip(" .")]


def parse(path: str) -> Optional[EpisodeRef]:
    """Show, season, episode numbers and segment titles from a file name, or None."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = EPISODE.search(stem)
    if not m:
        return None
    if m.group("s") is not None:
        season, first, more, num = int(m.group("s")), int(m.group("e")), m.group("more") or "", _MORE_NUMBER
    else:
        season, first, more, num = int(m.group("s2")), int(m.group("e2")), m.group("more2") or "", _MORE_NUMBER_X
    episodes = [first]
    for x in num.finditer(more):
        n = int(x.group(1))
        if n > episodes[-1]:
            episodes.extend(range(episodes[-1] + 1, n + 1)) if n - episodes[-1] <= 6 else episodes.append(n)
    show, year = _clean_show(stem[:m.start()])
    if not show:
        show = os.path.basename(os.path.dirname(os.path.abspath(path)))
        show = re.sub(r"\s*[-–—]?\s*(?:season|series|staffel|saison)\s*\d+\s*$", "", show, flags=re.I)
        show, year = _clean_show(show)
    tail = stem[m.end():]
    tail = re.sub(r"^\s*\(\d{1,3}\)", "", tail)   # absolute number such as (01)
    return EpisodeRef(show=show, season=season, episodes=episodes, titles=_clean_titles(tail), year=year)


def norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()
    t = t.replace("&", " and ")
    t = re.sub(r"\(\d\)|part\s*(\d+)|pt\.?\s*(\d+)", lambda m: f" {m.group(1) or m.group(2) or ''} ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"^(?:the|a|an)\s+", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def show_key(name: str) -> str:
    return norm_title(name)


# ---------------------------------------------------------------- database listings

@dataclass
class DbEpisode:
    season: int
    number: int
    title: str
    runtime: Optional[float] = None    # minutes


@dataclass
class Assignment:
    season: int
    episode: int
    title: str = ""
    source: str = "sequence"           # "title match" | "file name" | "sequence"
    runtime: Optional[float] = None
    in_database: Optional[bool] = None

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"


def match_titles(titles: list[str], listing: list[DbEpisode]) -> list[Optional[DbEpisode]]:
    """Best database episode for each segment title (exact, then close spelling)."""
    pool = {norm_title(e.title): e for e in listing if e.title}
    keys = list(pool)
    out: list[Optional[DbEpisode]] = []
    used: set[int] = set()
    for t in titles:
        k = norm_title(t)
        hit = pool.get(k)
        if hit is None and k:
            close = difflib.get_close_matches(k, keys, n=3, cutoff=0.84)
            hit = next((pool[c] for c in close if pool[c].number not in used), None)
        if hit is not None and hit.number in used:
            hit = None
        out.append(hit)
        if hit is not None:
            used.add(hit.number)
    return out


@dataclass
class FileEntry:
    path: str
    ref: Optional[EpisodeRef]
    parts: int                         # output parts this file produces (kept splits + 1, or expected)


NUMBERING = ("auto", "broadcast", "database")


def file_numbering(files: list[FileEntry]) -> str:
    """Guess what single episode numbers in a season's file names mean.

    "broadcast": one number per file (E01, E02, E03 … each holding several segments).
    "database": the number of each file's first segment (E01, E03, E05 …).
    """
    singles = [e for e in files if len(e.ref.episodes) == 1]
    if len(singles) < 2:
        return "broadcast"
    steps = [b.ref.first - a.ref.first for a, b in zip(singles, singles[1:])]
    jumps = sum(1 for (a, step) in zip(singles, steps) if step >= max(2, a.parts))
    return "database" if jumps >= max(1, len(steps) * 0.6) else "broadcast"


def number_files(entries: list[FileEntry], listings: Optional[dict[int, list[DbEpisode]]] = None,
                 default_segments: int = 2, numbering: str = "auto") -> dict[str, list[Assignment]]:
    """Episode numbers for every part of every file, season by season.

    `listings` maps season number -> database episodes for one show. `numbering`
    says what a single number in a file name means (see `file_numbering`).
    """
    listings = listings or {}
    per_file = max(1, int(default_segments or 1))
    out: dict[str, list[Assignment]] = {}
    groups: dict[tuple, list[FileEntry]] = {}
    for e in entries:
        if e.ref is None:
            continue
        groups.setdefault((show_key(e.ref.show), e.ref.season), []).append(e)
    for (_, season), files in groups.items():
        files.sort(key=lambda e: (e.ref.first, e.path))
        mode = file_numbering(files) if numbering == "auto" else numbering
        listing = listings.get(season) or []
        by_number = {x.number: x for x in listing}
        prev_first = prev_last = None
        for e in files:
            n = max(1, e.parts)
            numbers: Optional[list[int]] = None
            source = "sequence"
            if listing and e.ref.titles:
                hits = match_titles(e.ref.titles, listing)
                if len(hits) == n and all(hits):
                    numbers, source = [h.number for h in hits], "title match"
                elif len(hits) == n and hits[0] is not None:
                    numbers, source = [hits[0].number + i for i in range(n)], "title match"
            if numbers is None and len(e.ref.episodes) > 1:
                numbers = list(e.ref.episodes) if len(e.ref.episodes) == n else list(range(e.ref.first, e.ref.first + n))
                source = "file name"
            if numbers is None and mode == "database":
                numbers, source = list(range(e.ref.first, e.ref.first + n)), "file name"
            if numbers is None:
                # Files before this one that are not in the queue are assumed to hold as
                # many segments as this one (seasons are usually uniform), else the default.
                assume = n if n > 1 else per_file
                if prev_last is None:
                    start = (e.ref.first - 1) * assume + 1
                else:
                    skipped = max(0, e.ref.first - prev_first - 1)
                    start = prev_last + 1 + skipped * assume
                numbers = list(range(start, start + n))
            prev_first, prev_last = e.ref.first if len(e.ref.episodes) == 1 else e.ref.episodes[-1], numbers[-1]
            parts = []
            for i, number in enumerate(numbers):
                db = by_number.get(number)
                title = db.title if db else (e.ref.titles[i] if len(e.ref.titles) == n else "")
                parts.append(Assignment(season=season, episode=number, title=title, source=source,
                                        runtime=db.runtime if db else None,
                                        in_database=(db is not None) if listing else None))
            out[e.path] = parts
    return out


def season_check(assignments: dict[str, list[Assignment]], listings: Optional[dict[int, list[DbEpisode]]] = None) -> list[str]:
    """Plain-language notes: duplicates, gaps and how parts compare with the database count."""
    notes = []
    by_season: dict[int, list[int]] = {}
    for parts in assignments.values():
        for a in parts:
            by_season.setdefault(a.season, []).append(a.episode)
    for season in sorted(by_season):
        nums = by_season[season]
        dup = sorted({n for n in nums if nums.count(n) > 1})
        if dup:
            notes.append(f"Season {season}: episode {', '.join(f'E{n:02d}' for n in dup)} would be used twice.")
        listing = (listings or {}).get(season)
        if listing:
            have = set(nums)
            db_numbers = sorted(x.number for x in listing)
            beyond = sorted(n for n in have if n not in set(db_numbers))
            line = f"Season {season}: {len(nums)} part(s) for {len(db_numbers)} database episode(s)"
            if beyond:
                line += f" · {', '.join(f'E{n:02d}' for n in beyond[:6])} not in the database"
            lo, hi = min(have), max(have)
            missing = [n for n in db_numbers if lo <= n <= hi and n not in have]
            if missing:
                line += f" · missing {', '.join(f'E{n:02d}' for n in missing[:6])}" + (" …" if len(missing) > 6 else "")
            notes.append(line + ".")
        else:
            lo, hi = min(nums), max(nums)
            gaps = [n for n in range(lo, hi + 1) if n not in nums]
            if gaps:
                notes.append(f"Season {season}: no part for {', '.join(f'E{n:02d}' for n in gaps[:6])}" +
                             (" …" if len(gaps) > 6 else "") + " (files missing from the queue?).")
    return notes


def part_name(show: str, a: Assignment, year: Optional[int] = None, include_year: bool = False) -> str:
    """Plex/Jellyfin/Kodi-style name: ``Show - S01E03 - Title``."""
    from .planner import safe_filename
    head = f"{show} ({year})" if include_year and year else show
    return safe_filename(f"{head} - {a.code}" + (f" - {a.title}" if a.title else ""))


def runtime_fractions(parts: list[Assignment]) -> Optional[list[float]]:
    """Cumulative break positions from database runtimes, when every part has one."""
    if len(parts) < 2 or any(not a.runtime for a in parts):
        return None
    total = sum(a.runtime for a in parts)
    acc, out = 0.0, []
    for a in parts[:-1]:
        acc += a.runtime
        out.append(round(acc / total, 4))
    return out
