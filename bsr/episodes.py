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

# Release details that never appear in real titles; everything from the first one is dropped.
QUALITY = re.compile(r"(?:\b|[._ ])(?:2160p|1080p|1080i|720p|576p|480p|web[- .]dl|webrip|blu-?ray|bdrip|brrip|hdtv|"
                     r"dvdrip|x264|x265|h\.?26[45]|hevc|aac\d(?:\.\d)?|ddp?\d\.\d|10bit|8bit)\b.*$", re.I)
# Words that are release details only in dotted scene names (Show.S01E01.Title.AMZN.WEB.mkv).
_SCENE_WORD = r"(?:web|dvd|uhd|4k|remux|avc|amzn|nf|dsnp|hmax|hulu|pmtp|atvp|repack|proper|internal)"
SCENE_ONLY = re.compile(rf"(?:^|\s){_SCENE_WORD}(?:\s{_SCENE_WORD})*(?:-\w+)?\s*$", re.I)   # only trailing runs
# S01E01, S01E01E02, S01E01-E02, S01E01-02, S01E01 - S01E02, S01E01 & E02 and 1x01, 1x01-02, 1x01 - 1x02.
# A bare number after a spaced separator ("S01E01 - 3 Wishes", "S01E01 2019") belongs to the title.
EPISODE = re.compile(
    r"(?<![A-Za-z0-9])S(?P<s>\d{1,3})\s*E(?P<e>\d{1,4})"
    r"(?P<more>(?:E\d{1,4}|-E?\d{1,4}|\s*[-&+]\s*(?:S\d{1,3}\s*)?E\d{1,4})*)(?![\dpi])"
    r"|(?<![A-Za-z0-9])(?P<s2>\d{1,2})x(?P<e2>\d{1,4})(?P<more2>(?:-\d{1,4}|\s*[-&+]\s*\d{1,2}x\d{1,4})*)(?![\dpi])",
    re.I)
_MORE_NUMBER = re.compile(r"(?:S\d{1,3}\s*)?E?(\d{1,4})", re.I)
_MORE_NUMBER_X = re.compile(r"(?:\d{1,2}x)?(\d{1,4})", re.I)
TAGS = re.compile(r"^\s*(?:\[[^\]]*\]\s*|\{[^}]*\}\s*)+")   # leading [Group] or {tags}
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
    tail: str = ""                     # cleaned title text after the episode code, before splitting

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
    t = re.sub(r"\s*\[[^\]]*\]\s*", " ", TAGS.sub("", text)).strip(" -–—_.(){}")
    if "." in t and " " not in t:
        t = t.replace(".", " ")
    t = t.replace("_", " ").strip(" -–—.")
    year = None
    m = YEAR.search(t)
    if m and m.start() > 0:
        year = int(m.group(1))
        t = t[:m.start()].strip(" -–—.([")
    return re.sub(r"\s{2,}", " ", t).strip(), year


def _clean_tail(tail: str) -> str:
    tail = re.sub(r"(?:\s*\[[^\]]*\])+", " ", tail)
    tail = re.sub(r"\s*\((?:\d{3,4}p|web[- ]?dl|bluray|hdtv|dvd)[^)]*\)", " ", tail, flags=re.I)
    scene = " " not in tail.strip(" .-_") and ("." in tail or "_" in tail)
    if scene:                    # Show.S01E01.Title.Here or show_s01e01_title_here: one title, not several
        tail = re.sub(r"[._]+", " ", tail)
    tail = QUALITY.sub("", tail)
    if scene:
        tail = SCENE_ONLY.sub("", tail)
    return tail.strip(" -–—_.;|+")


def _clean_titles(tail: str) -> list[str]:
    return [t.strip(" .") for t in TITLE_SEPARATOR.split(_clean_tail(tail)) if t.strip(" .")]


def parse(path: str) -> Optional[EpisodeRef]:
    """Show, season, episode numbers and segment titles from a file name, or None."""
    stem = TAGS.sub("", os.path.splitext(os.path.basename(path))[0])
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
        if not 0 < n - episodes[-1] <= 6:
            break                    # not a plausible continuation of the range
        episodes.extend(range(episodes[-1] + 1, n + 1))
    show, year = _clean_show(stem[:m.start()])
    if not show:
        show = os.path.basename(os.path.dirname(os.path.abspath(path)))
        show = re.sub(r"\s*[-–—]?\s*(?:season|series|staffel|saison)\s*\d+\s*$", "", show, flags=re.I)
        show, year = _clean_show(show)
    tail = stem[m.end():]
    tail = re.sub(r"^\s*\(\d{1,3}\)", "", tail)   # absolute number such as (01)
    clean = _clean_tail(tail)
    return EpisodeRef(show=show, season=season, episodes=episodes, year=year, tail=clean,
                      titles=[t.strip(" .") for t in TITLE_SEPARATOR.split(clean) if t.strip(" .")])


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


def _squash(key: str) -> str:
    """Normalised title without spaces, so "Bubble Stand" and "Bubblestand" agree."""
    return key.replace(" ", "")


def _title_pool(listing: list[DbEpisode]) -> dict[str, DbEpisode]:
    pool: dict[str, DbEpisode] = {}
    for e in listing:
        if e.title:
            pool.setdefault(norm_title(e.title), e)
    return pool


def _match_one(key: str, pool: dict[str, DbEpisode], used: set) -> tuple[Optional[DbEpisode], int]:
    """(episode, strength) for one normalised title: 2 = exact, 1 = close spelling, 0 = none."""
    if not key:
        return None, 0
    hit = pool.get(key)
    if hit is not None and hit.number not in used:
        return hit, 2
    squashed = _squash(key)
    for k, e in pool.items():
        if e.number not in used and _squash(k) == squashed:
            return e, 2
    for c in difflib.get_close_matches(key, list(pool), n=3, cutoff=0.84):
        if pool[c].number not in used:
            return pool[c], 1
    return None, 0


def match_titles(titles: list[str], listing: list[DbEpisode]) -> list[Optional[DbEpisode]]:
    """Best database episode for each segment title (exact, then close spelling)."""
    pool = _title_pool(listing)
    out: list[Optional[DbEpisode]] = []
    used: set[int] = set()
    for t in titles:
        hit, _ = _match_one(norm_title(t), pool, used)
        out.append(hit)
        if hit is not None:
            used.add(hit.number)
    return out


# Words that can join two segment titles in a file name ("Help Wanted and Reef Blower",
# "Help Wanted & Reef Blower"). Unlike dashes they also occur inside real titles
# ("Rock and Roll"), so they only split titles when the database or the expected
# number of segments says so.
JOINER = re.compile(r"\s+(?:and|&|n)\s+|\s*&\s*", re.I)
_MAX_ATOMS = 12


def _atoms(tail: str) -> tuple[list[str], list[str]]:
    """Pieces of the title text and what separates them: "hard" (a dash, ; | _ +) or the joining word."""
    atoms, seps = [], []
    for g, group in enumerate(t.strip(" .") for t in TITLE_SEPARATOR.split(tail or "")):
        if not group:
            continue
        pieces, joins, pos = [], [], 0
        for m in JOINER.finditer(group):
            if m.start() > pos and group[pos:m.start()].strip():
                pieces.append(group[pos:m.start()].strip()); joins.append(m.group(0))
                pos = m.end()
        pieces.append(group[pos:].strip())
        if atoms:
            seps.append("hard")
        atoms.append(pieces[0])
        for j, piece in zip(joins, pieces[1:]):
            seps.append(j); atoms.append(piece)
    return [a for a in atoms], seps


def _join(atoms: list[str], seps: list[str], lo: int, hi: int) -> str:
    out = atoms[lo]
    for k in range(lo, hi - 1):
        out += (" - " if seps[k] == "hard" else seps[k]) + atoms[k + 1]
    return out


def segment_titles(ref: EpisodeRef, parts: Optional[int] = None,
                   listing: Optional[list[DbEpisode]] = None) -> list[str]:
    """Segment titles from a file name, one per output part where possible.

    Titles are separated by dashes (and ; | _ +) and, when needed, by "and" or "&":
    ``Help Wanted and Reef Blower`` is two segments for a two-part file, while
    ``Rock and Roll`` stays one title when the database lists it that way. With a
    database listing every way of grouping the pieces is tried and the grouping
    whose titles match the most database episodes wins; without one, dashes always
    separate titles and "and"/"&" split only as far as the expected part count.
    """
    tail = ref.tail if ref.tail or not ref.titles else " - ".join(ref.titles)
    atoms, seps = _atoms(tail)
    if not atoms:
        return []
    soft = [k for k, sp in enumerate(seps) if sp != "hard"]
    hard_groups = len(atoms) - len(soft)
    want = parts if parts and parts > 1 else None
    if not listing and (want is None or hard_groups >= want or not soft):
        return list(ref.titles) if ref.titles else [_join(atoms, seps, 0, len(atoms))]
    if len(atoms) > _MAX_ATOMS:
        return list(ref.titles)
    pool = _title_pool(listing or [])
    keys: dict[tuple, tuple] = {}

    def strength(lo, hi):
        if (lo, hi) not in keys:
            keys[(lo, hi)] = _match_one(norm_title(_join(atoms, seps, lo, hi)), pool, set()) if pool else (None, 0)
        return keys[(lo, hi)]

    best, best_score = None, None
    n_bounds = len(seps)
    for mask in range(1 << n_bounds):
        cuts = [k for k in range(n_bounds) if mask >> k & 1]
        merged_hard = [k for k in range(n_bounds) if seps[k] == "hard" and not mask >> k & 1]
        if merged_hard and not pool:
            continue
        bounds = [0] + [k + 1 for k in cuts] + [len(atoms)]
        groups = list(zip(bounds, bounds[1:]))
        score, numbers, ok = 0.0, set(), True
        for lo, hi in groups:
            hit, st = strength(lo, hi)
            if hit is not None and hit.number in numbers:
                hit, st = None, 0
            if hit is not None:
                numbers.add(hit.number)
            if st < 2 and any(seps[k] == "hard" for k in range(lo, hi - 1)):
                ok = False                     # dashes are merged back only into an exact database title
                break
            score += 3 * st
        if not ok:
            continue
        if want:
            score -= 4 * abs(len(groups) - want)
        score -= 0.1 * sum(1 for k in cuts if seps[k] != "hard")   # keep "and" inside titles when in doubt
        sizes = [sum(len(atoms[i].split()) for i in range(lo, hi)) for lo, hi in groups]
        score -= 0.001 * (max(sizes) - min(sizes))                  # then prefer even-sized titles
        if best_score is None or score > best_score:
            best, best_score = groups, score
    if best is None:
        return list(ref.titles)
    return [_join(atoms, seps, lo, hi) for lo, hi in best]


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
    if len(singles) < 3:
        return "broadcast"           # one gap between two files is not enough to tell
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
            titles = segment_titles(e.ref, n, listing)
            if listing and titles:
                hits = match_titles(titles, listing)
                if len(hits) == n and all(hits):
                    numbers, source = [h.number for h in hits], "title match"
                elif len(hits) == n and any(hits):
                    j = next(j for j, h in enumerate(hits) if h)    # count from the first matched title
                    numbers, source = [hits[j].number - j + i for i in range(n)], "title match"
                elif hits and hits[0] is not None:
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
                title = db.title if db else (titles[i] if len(titles) == n else "")
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
