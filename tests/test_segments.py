"""Two- and three-segment episodes: recommended splits, episode numbering and database lookup."""
import hashlib
import json
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from bsr.detect import (T_GRID, DetectSettings, FrameStats, PRESETS, break_fractions, find_candidates,
                        recommend_splits, segment_windows, settings_windows, window_half_width)
from bsr.episodes import (Assignment, DbEpisode, FileEntry, file_numbering, match_titles, number_files, parse,
                          part_name, runtime_fractions, season_check)
from bsr import metadata


def stats(levels, n=300):
    frac = np.zeros((n, len(T_GRID)), dtype=np.float32)
    for i, (level, ratio) in levels.items():
        frac[i] = np.where(T_GRID >= level, ratio, 0)
    return FrameStats(0, Fraction(1, 10), np.arange(n, dtype=np.int64), np.ones(n, dtype=np.int64), frac,
                      np.full(n, .3, dtype=np.float32), np.full(n, .5, dtype=np.float32), np.zeros(n, dtype=bool))


# ---------------------------------------------------------------- detection

def test_windows_for_two_and_three_segments():
    fs = stats({})
    assert segment_windows(fs, 2) == pytest.approx([(12, 18, 15)])          # the original 40–60%
    lo1, hi1, t1 = segment_windows(fs, 3)[0]
    assert t1 == pytest.approx(10) and hi1 - lo1 == pytest.approx(30 * 2 * window_half_width(3))
    assert [w[2] for w in segment_windows(fs, 3)] == pytest.approx([10, 20])
    assert segment_windows(fs, 1) == []
    assert break_fractions(3, [0.3, 0.65]) == [0.3, 0.65]
    assert break_fractions(3, [0.7, 0.3]) == pytest.approx([1 / 3, 2 / 3])  # unordered input ignored
    windows = segment_windows(fs, 4)
    assert all(a[1] <= b[0] for a, b in zip(windows, windows[1:]))          # never overlap


def test_three_segments_recommend_thirds_not_the_halfway_scene_change():
    # Strong hard blacks at 15% (inside a segment) and 50% (a scene change), weaker
    # single-frame breaks near one third and two thirds.
    fs = stats({**{i: (0, 1) for i in range(44, 48)}, **{i: (0, 1) for i in range(149, 153)},
                98: (.115, .96), 205: (.115, .96)})
    ds = DetectSettings(**{**PRESETS["balanced"].to_json(), "segments": 3})
    cands = find_candidates(fs, ds)
    picks = recommend_splits(cands, settings_windows(fs, ds))
    assert [round(c.start_s, 1) for c in picks] == [9.8, 20.5]
    assert all(not c.suppressed for c in picks)
    two = DetectSettings(**{**PRESETS["balanced"].to_json(), "segments": 2})
    assert [round(c.start_s, 1) for c in recommend_splits(find_candidates(fs, two), settings_windows(fs, two))] == [14.9]


def test_runtime_fractions_move_the_windows():
    fs = stats({**{i: (0, 1) for i in range(87, 89)}, **{i: (0, 1) for i in range(100, 102)},
                **{i: (0, 1) for i in range(189, 191)}})
    ds = DetectSettings(min_spacing_s=0, segments=3, midpoint_boost=True)
    equal = recommend_splits(find_candidates(fs, ds), settings_windows(fs, ds))
    assert [c.start_s for c in equal] == [10.0, 18.9]
    ds.break_fractions = [0.29, 0.63]                   # 7 + 8 + 7 minute segments, say
    weighted = recommend_splits(find_candidates(fs, ds), settings_windows(fs, ds))
    assert [c.start_s for c in weighted] == [8.7, 18.9]


def test_missing_break_is_reported_as_none_and_old_settings_default_to_two_segments():
    fs = stats({**{i: (0, 1) for i in range(98, 101)}})
    ds = DetectSettings(min_spacing_s=0, segments=3, midpoint_boost=True)
    picks = recommend_splits(find_candidates(fs, ds), settings_windows(fs, ds))
    assert picks[0].start_s == 9.8 and picks[1] is None
    legacy = {k: v for k, v in PRESETS["balanced"].to_json().items() if k not in ("segments", "break_fractions")}
    assert DetectSettings(**legacy).segments == 2


# ---------------------------------------------------------------- file names

@pytest.mark.parametrize("name, show, season, episodes, titles, year", [
    ("SpongeBob SquarePants - S01E01 - Help Wanted - Reef Blower - Tea at the Treedome.mkv", "SpongeBob SquarePants", 1, [1],
     ["Help Wanted", "Reef Blower", "Tea at the Treedome"], None),
    ("Dexters.Laboratory.S02E05.1080p.WEB-DL.AAC2.0.H.264.mkv", "Dexters Laboratory", 2, [5], [], None),
    ("The Powerpuff Girls (1998) - S01E03-E04 - Bubblevicious + The Bare Facts [1080p].mp4", "The Powerpuff Girls", 1, [3, 4],
     ["Bubblevicious", "The Bare Facts"], 1998),
    ("Rugrats - S03E12E13 - Moose Country; Cool Hand Angelica.mkv", "Rugrats", 3, [12, 13], ["Moose Country", "Cool Hand Angelica"], None),
    ("Ed Edd n Eddy 1x01-02.mkv", "Ed Edd n Eddy", 1, [1, 2], [], None),
    ("Show S01E01-S01E03.mkv", "Show", 1, [1, 2, 3], [], None),
    ("Spider-Man (1994) - S01E01 - Night of the Lizard.mkv", "Spider-Man", 1, [1], ["Night of the Lizard"], 1994),
])
def test_parse_names(name, show, season, episodes, titles, year):
    ref = parse(name)
    assert (ref.show, ref.season, ref.episodes, ref.titles, ref.year) == (show, season, episodes, titles, year)


def test_parse_uses_folder_when_name_is_only_a_code(tmp_path):
    ref = parse(str(tmp_path / "Hey Arnold! - Season 2" / "S02E07.mkv"))
    assert ref.show == "Hey Arnold!" and ref.season == 2 and ref.episodes == [7]
    assert parse("holiday video 2010.mkv") is None


def test_segment_hint_from_names():
    from bsr.session import SourceSession
    assert parse("A - S01E01 - One - Two.mkv").segment_hint() == 2
    assert parse("A - S01E01-E03.mkv").segment_hint() == 3
    assert parse("A - S01E01 - Only.mkv").segment_hint() is None


# ---------------------------------------------------------------- numbering

def entries(*specs):
    return [FileEntry(f"/x/{name}", parse(name), parts) for name, parts in specs]


def codes(result, path):
    return [a.code for a in result[f"/x/{path}"]]


def test_broadcast_numbering_continues_across_files_and_skips_missing_files():
    es = entries(("Show - S01E01.mkv", 2), ("Show - S01E02.mkv", 3), ("Show - S01E03.mkv", 2), ("Show - S01E05.mkv", 2))
    got = number_files(es, default_segments=2)
    assert codes(got, "Show - S01E01.mkv") == ["S01E01", "S01E02"]
    assert codes(got, "Show - S01E02.mkv") == ["S01E03", "S01E04", "S01E05"]
    assert codes(got, "Show - S01E03.mkv") == ["S01E06", "S01E07"]
    assert codes(got, "Show - S01E05.mkv") == ["S01E10", "S01E11"]   # E04 (two parts) not in the queue
    assert season_check(got) == ["Season 1: no part for E08, E09 (files missing from the queue?)."]


def test_a_season_starting_later_assumes_default_segments_before_it():
    got = number_files(entries(("Show - S02E04.mkv", 3)), default_segments=3)
    assert codes(got, "Show - S02E04.mkv") == ["S02E10", "S02E11", "S02E12"]


def test_files_named_after_their_first_segment_are_detected():
    es = entries(("Show - S01E01.mkv", 2), ("Show - S01E03.mkv", 2), ("Show - S01E05.mkv", 2))
    assert file_numbering(sorted(es, key=lambda e: e.ref.first)) == "database"
    got = number_files(es)
    assert [codes(got, e.path.split("/")[-1]) for e in es] == [["S01E01", "S01E02"], ["S01E03", "S01E04"], ["S01E05", "S01E06"]]
    forced = number_files(es, numbering="broadcast")
    assert codes(forced, "Show - S01E03.mkv") == ["S01E05", "S01E06"]


def test_ranges_in_names_win_and_seasons_are_separate():
    es = entries(("Show - S01E01-E02.mkv", 2), ("Show - S01E03E04.mkv", 2), ("Show - S02E01.mkv", 2), ("Other - S01E01.mkv", 2))
    got = number_files(es)
    assert codes(got, "Show - S01E03E04.mkv") == ["S01E03", "S01E04"]
    assert codes(got, "Show - S02E01.mkv") == ["S02E01", "S02E02"]
    assert codes(got, "Other - S01E01.mkv") == ["S01E01", "S01E02"]
    assert got["/x/Show - S01E01-E02.mkv"][0].source == "file name"


LISTING = {1: [DbEpisode(1, 1, "Help Wanted", 3), DbEpisode(1, 2, "Reef Blower", 2),
               DbEpisode(1, 3, "Tea at the Treedome", 6), DbEpisode(1, 4, "Bubblestand", 11),
               DbEpisode(1, 5, "Ripped Pants", 11), DbEpisode(1, 6, "Jellyfishing", 11),
               DbEpisode(1, 7, "Plankton!", 11)]}


def test_database_titles_place_parts_even_when_file_numbers_disagree():
    es = entries(("SpongeBob - S01E01 - Help Wanted - Reef Blower - Tea at the Treedome.mkv", 3),
                 ("SpongeBob - S01E02 - Bubble Stand - Ripped Pants.mkv", 2),
                 ("SpongeBob - S01E03.mkv", 2))
    got = number_files(es, LISTING)
    first = got["/x/SpongeBob - S01E01 - Help Wanted - Reef Blower - Tea at the Treedome.mkv"]
    assert [a.code for a in first] == ["S01E01", "S01E02", "S01E03"] and first[0].source == "title match"
    second = got["/x/SpongeBob - S01E02 - Bubble Stand - Ripped Pants.mkv"]
    assert [(a.code, a.title) for a in second] == [("S01E04", "Bubblestand"), ("S01E05", "Ripped Pants")]
    third = got["/x/SpongeBob - S01E03.mkv"]
    assert [(a.code, a.title, a.source) for a in third] == [("S01E06", "Jellyfishing", "sequence"), ("S01E07", "Plankton!", "sequence")]
    assert season_check(got, LISTING) == ["Season 1: 7 part(s) for 7 database episode(s)."]
    assert runtime_fractions(first) == pytest.approx([3 / 11, 5 / 11], abs=1e-3)


def test_season_check_reports_extra_parts_and_repeats():
    es = entries(("Show - S01E01-E02.mkv", 2), ("Show - S01E02-E03.mkv", 2))
    got = number_files(es, {1: [DbEpisode(1, 1, "A"), DbEpisode(1, 2, "B")]})
    notes = season_check(got, {1: [DbEpisode(1, 1, "A"), DbEpisode(1, 2, "B")]})
    assert "E02 would be used twice" in notes[0]
    assert "E03 not in the database" in notes[1]
    assert got["/x/Show - S01E02-E03.mkv"][1].in_database is False


def test_close_title_spelling_matches_once():
    hits = match_titles(["The Bare Facts", "Bubble Vicious", "Unknown"],
                        [DbEpisode(1, 3, "Bubblevicious"), DbEpisode(1, 4, "The Bare Facts")])
    assert [h.number if h else None for h in hits] == [4, 3, None]


def test_part_names():
    a = Assignment(1, 3, "Tea at the Treedome")
    assert part_name("SpongeBob SquarePants", a) == "SpongeBob SquarePants - S01E03 - Tea at the Treedome"
    assert part_name("Spider-Man", Assignment(2, 11, ""), 1994, True) == "Spider-Man (1994) - S02E11"
    assert part_name("Show", Assignment(1, 1, "What/Why: A Tale")) == "Show - S01E01 - What_Why_ A Tale"


# ---------------------------------------------------------------- online services

class FakeService:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        for key, reply in self.replies.items():
            if key in url:
                if isinstance(reply, Exception):
                    raise reply
                return reply(url) if callable(reply) else reply
        raise AssertionError(f"unexpected request {url}")


TMDB_REPLIES = {
    "/search/tv": {"results": [{"id": 387, "name": "SpongeBob SquarePants", "first_air_date": "1999-05-01"},
                               {"id": 9, "name": "Kamp Koral", "first_air_date": "2021-03-04"}]},
    "/tv/387/season/1": {"episodes": [{"season_number": 1, "episode_number": 1, "name": "Help Wanted", "runtime": 9},
                                      {"season_number": 1, "episode_number": 2, "name": "Reef Blower", "runtime": 2},
                                      {"season_number": 1, "episode_number": 3, "name": "Tea at the Treedome", "runtime": 11}]},
}


def test_tmdb_token_styles_and_cache():
    fetch = FakeService(TMDB_REPLIES)
    tmdb = metadata.TMDB(token="eyJhbGciOi.eyJhdWQiOi.sig", fetch=fetch)
    match = tmdb.search("SpongeBob SquarePants", 1999)[0]
    assert (match.id, match.name, match.year) == ("387", "SpongeBob SquarePants", 1999)
    eps = tmdb.season(match.id, 1)
    assert [(e.number, e.title, e.runtime) for e in eps] == [(1, "Help Wanted", 9), (2, "Reef Blower", 2), (3, "Tea at the Treedome", 11)]
    assert fetch.calls[0][1]["Authorization"].startswith("Bearer ")
    assert "first_air_date_year=1999" in fetch.calls[0][0]
    n = len(fetch.calls)
    assert metadata.TMDB(token="eyJhbGciOi.eyJhdWQiOi.sig", fetch=fetch).season("387", 1) == eps
    assert len(fetch.calls) == n                                    # served from the cache
    v3 = FakeService(TMDB_REPLIES)
    metadata.TMDB(token="0123456789abcdef", fetch=v3, use_cache=False).search("SpongeBob")
    assert "api_key=0123456789abcdef" in v3.calls[0][0] and "Authorization" not in v3.calls[0][1]
    cached = [p.read_text() for p in (Path(metadata._cache_path("x")).parent).glob("*.json")]
    assert not any("0123456789abcdef" in text or "eyJhbGci" in text for text in cached)   # keys never cached


def test_tvdb_login_paging_and_season_filter():
    pages = {0: {"data": {"episodes": [{"seasonNumber": 1, "number": 1, "name": "Pilot", "runtime": 11},
                                      {"seasonNumber": 0, "number": 1, "name": "Special"}]},
                 "links": {"next": "page=1"}},
             1: {"data": {"episodes": [{"seasonNumber": 1, "number": 2, "name": "Second", "runtime": 11}]}, "links": {"next": None}}}
    fetch = FakeService({"/login": {"data": {"token": "T"}},
                         "/search": {"data": [{"tvdb_id": "75886", "name": "Show", "year": "1998"}]},
                         "/episodes/default": lambda url: pages[int(url.split("page=")[1].split("&")[0])]})
    tvdb = metadata.TVDB(apikey="K", pin="1234", fetch=fetch, use_cache=False)
    assert tvdb.search("Show")[0].id == "75886"
    assert json.loads(fetch.calls[0][2]) == {"apikey": "K", "pin": "1234"}
    assert [(e.number, e.title) for e in tvdb.season("75886", 1)] == [(1, "Pilot"), (2, "Second")]
    assert all(c[1].get("Authorization") == "Bearer T" for c in fetch.calls[1:])


def test_lookup_errors_are_plain_language(monkeypatch):
    for var in ("BSR_TMDB_TOKEN", "BSR_TVDB_APIKEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(metadata, "_keychain", lambda account: None)
    monkeypatch.setattr(metadata, "_session_secrets", {})
    with pytest.raises(metadata.LookupError_, match="TMDB API key"):
        metadata.TMDB()
    monkeypatch.setenv("BSR_TMDB_TOKEN", "abc")
    assert metadata.configured("tmdb")
    import urllib.error
    def refuse(request, timeout=None):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(metadata.urllib.request, "urlopen", refuse)
    with pytest.raises(metadata.LookupError_, match="internet"):
        metadata.http_json("https://example.invalid", {})


# ---------------------------------------------------------------- real video

@pytest.fixture(scope="module")
def three_segment_video(tmp_path_factory):
    """30 s cartoon-like file: strong blacks at 15% and 50% inside segments, short
    near-black breaks at one third and two thirds where the segments change."""
    import av
    path = tmp_path_factory.mktemp("cartoon") / "Toon Town - S01E02 - First Gag - Second Gag - Third Gag.mp4"
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=10)
        stream.width = stream.height = 96
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "12", "g": "10", "keyint_min": "10", "sc_threshold": "0", "bf": "0"}
        for i in range(300):
            value = 0 if i in range(44, 48) or i in range(149, 153) else 30 if i in (99, 200) else 60 + (i % 40) * 3
            frame = av.VideoFrame.from_ndarray(np.full((96, 96, 3), value, dtype=np.uint8), format="rgb24")
            frame.pts, frame.time_base = i, Fraction(1, 10)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return str(path)


def test_real_three_segment_file_splits_into_validated_episodes(three_segment_video, tmp_path):
    from bsr.session import SourceSession
    from bsr.export import export_plan, ExportOptions
    from bsr.cli import episode_names
    before = hashlib.sha256(Path(three_segment_video).read_bytes()).hexdigest()
    s = SourceSession(three_segment_video)
    assert s.segments_auto and s.detect.segments == 3            # three titles in the name
    s.analyze(); s.find(audio_signal=False)
    picks = s.recommended()
    assert [round(c.start_s, 1) for c in picks] == [9.9, 20.0]
    assert sorted(s.accept_recommended()) == sorted(c.id for c in picks)
    assert s.part_count() == 3
    s.names = episode_names(s, 3)
    assert s.names == ["Toon Town - S01E04 - First Gag", "Toon Town - S01E05 - Second Gag", "Toon Town - S01E06 - Third Gag"]
    plan = s.plan()
    assert not plan.issues and len(plan.segments) == 3
    report = export_plan(s.info, s.idx, plan, ExportOptions(out_dir=str(tmp_path / "out")))
    assert report["ok"], report
    assert [Path(o["path"]).stem for o in report["outputs"]] == s.names
    assert all(o["validation"]["checks"]["frame count & order"]["ok"] for o in report["outputs"])
    assert all(o["validation"]["checks"]["packets byte-identical"]["ok"] for o in report["outputs"])
    assert hashlib.sha256(Path(three_segment_video).read_bytes()).hexdigest() == before
    project = tmp_path / "toon.bsrproj"; s.save_project(str(project))
    again = SourceSession.load_project(str(project))
    assert again.detect.segments == 3 and again.segments_auto
    s.set_segments(2)
    assert s.detect.segments == 2 and not s.segments_auto
    s.find(audio_signal=False)                                     # the near-black thirds are no longer boosted …
    assert len(s.accepted()) == 2 and len(s.plan().segments) == 3  # … but splits you kept stay kept
    assert {c.note for c in s.candidates if s.decisions[c.id].state == "accepted"} == {"Kept from an earlier detection"}
    s.set_preset("sensitive")
    assert s.detect.segments == 2                                  # presets keep the segment count


def test_cli_segments_accept_recommended_and_episode_names(three_segment_video, tmp_path, capsys):
    from bsr import cli
    with pytest.raises(SystemExit) as done:
        cli.main(["export", three_segment_video, "--out", str(tmp_path / "cli"), "--segments", "3",
                  "--accept-recommended", "--episode-names"])
    assert done.value.code == 0
    names = sorted(p.stem for p in (tmp_path / "cli").glob("*.mp4"))
    assert names == ["Toon Town - S01E04 - First Gag", "Toon Town - S01E05 - Second Gag", "Toon Town - S01E06 - Third Gag"]
    assert "recommended for 3 segments" in capsys.readouterr().out


def test_desktop_segments_recommendations_and_episode_dialog(three_segment_video, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from bsr.session import SourceSession
    from bsr_ui.app import MainWindow, Item
    from bsr_ui.theme import apply_theme
    from bsr_ui.episodes_dialog import EpisodeDialog
    monkeypatch.setenv("BSR_LOG_DIR", str(tmp_path / "logs"))
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    s = SourceSession(three_segment_video)
    s.analyze(); s.find(audio_signal=False); s.resolve()
    w = MainWindow(); it = Item(s.path); it.session = s; it.state = "ready"
    w.items = [it]; w.cur = it; w.show_item(); w.show(); app.processEvents()

    def settle():
        began = time.monotonic()
        while w._jobs:
            app.processEvents(); time.sleep(.005)
            assert time.monotonic() - began < 60
        app.processEvents()
    try:
        assert w.segments.currentIndex() == 0 and w.segments.currentText() == "Auto · 3"
        assert "3 segments" in w.midpoint_label.text() and "2 of 2 breaks found: 33%" in w.midpoint_hint.text()
        starred = [r for r in range(w.table.rowCount()) if w.table.item(r, 1).text().startswith("★ ")]
        assert len(starred) == 2
        assert len(w.overview.break_regions) == 2 and len(w.overview.recommended_ids) == 2
        assert "season order suggests S01E04–S01E06" in w.name_hint.text()
        w.btn_keep_rec.click(); settle()
        assert sum(d.state == "accepted" for d in s.decisions.values()) == 2
        assert not w.btn_keep_rec.isEnabled()
        assert len(s.plan(resolve=False).segments) == 3
        # Two segments from the selector: one split near halfway is recommended instead.
        w.segments.setCurrentIndex(w.segments.findData(2)); settle()
        assert s.detect.segments == 2 and not s.segments_auto
        assert w.btn_midpoint.text() == "Review midpoint"
        assert w.settings.value("default_segments") in (2, "2")
        w.segments.setCurrentIndex(0); settle()
        assert s.detect.segments == 3 and s.segments_auto
        # Episode dialog with a database lookup (recorded replies).
        fetch = FakeService({"/search/tv": {"results": [{"id": 5, "name": "Toon Town", "first_air_date": "2001-01-01"}]},
                             "/tv/5/season/1": {"episodes": [{"episode_number": n, "name": t, "runtime": r} for n, t, r in
                                                             ((1, "Pilot", 11), (2, "Two", 11), (3, "Three", 11), (4, "First Gag", 7),
                                                              (5, "Second Gag", 7), (6, "Third Gag", 8))]}})
        monkeypatch.setenv("BSR_TMDB_TOKEN", "abc")
        dlg = EpisodeDialog(w, w._episode_entries(), current=s.path, fetch=fetch)
        assert [dlg.table.item(r, 2).text() for r in range(3)] == ["S01E04", "S01E05", "S01E06"]
        dlg.source.setCurrentIndex(dlg.source.findData("tmdb")); dlg.lookup(); dlg.wait()
        assert "6 episodes" in dlg.status.text()
        assert dlg.table.item(0, 4).text() == "segment title"
        assert "3 part(s) for 6 database episode(s)" in dlg.notes.text() and "missing" not in dlg.notes.text()
        dlg.table.item(2, 3).setText("Third Gag (Extended)")
        dlg.runtimes.setChecked(True)
        names = dlg.names()[s.path]
        assert names == ["Toon Town - S01E04 - First Gag", "Toon Town - S01E05 - Second Gag", "Toon Town - S01E06 - Third Gag (Extended)"]
        assert dlg.fractions()[s.path] == pytest.approx([7 / 22, 14 / 22], abs=1e-3)
        dlg.close()
        monkeypatch.setattr(EpisodeDialog, "exec", lambda self: 1)
        monkeypatch.setattr(EpisodeDialog, "names", lambda self: {s.path: names})
        monkeypatch.setattr(EpisodeDialog, "fractions", lambda self: {s.path: [0.32, 0.64]})
        w.btn_names.click(); settle()
        assert s.names == names and s.detect.break_fractions == [0.32, 0.64]
        assert w.parts.rowCount() == 3, ([(d.state, d.start_pts) for d in s.decisions.values()], s.plan(resolve=False).issues)
        assert [w.parts.item(r, 4).text() for r in range(3)] == names
    finally:
        settle(); w.close()


# ---------------------------------------------------------------- review regressions

@pytest.mark.parametrize("name, episodes, titles", [
    ("Show S01E01 - 3 Wishes.mkv", [1], ["3 Wishes"]),
    ("Show - S01E01 - 10 Things.mkv", [1], ["10 Things"]),
    ("Show S01E01 2019.mkv", [1], ["2019"]),
    ("Show 1x01 - 2 Hearts.mkv", [1], ["2 Hearts"]),
    ("Show.S01E01.The.Web.Of.Lies.mkv", [1], ["The Web Of Lies"]),
    ("Show - S01E01 - Proper Manners - Nf Game.mkv", [1], ["Proper Manners", "Nf Game"]),
    ("show_s01e01_help_wanted.mkv", [1], ["help wanted"]),
    ("Show.S01E03.Title.Here.AMZN.WEB-NTb.mkv", [3], ["Title Here"]),
    ("Show S01E01 & E02.mkv", [1, 2], []),
    ("Show 1x01 - 1x02.mkv", [1, 2], []),
    ("Show S01E01-1080p.mkv", [1], []),
])
def test_numbers_and_words_in_titles_are_not_episodes(name, episodes, titles):
    ref = parse(name)
    assert (ref.episodes, ref.titles) == (episodes, titles)


def test_release_group_tags_are_not_part_of_the_show():
    ref = parse("[SubsPlease] Show Name - S01E01 (1080p) [ABCD1234].mkv")
    assert (ref.show, ref.episodes, ref.titles) == ("Show Name", [1], [])


def test_two_files_with_a_gap_stay_broadcast_numbered():
    got = number_files(entries(("Show - S01E01.mkv", 2), ("Show - S01E03.mkv", 2)))
    assert codes(got, "Show - S01E03.mkv") == ["S01E05", "S01E06"]


def test_uneven_runtime_breaks_never_share_a_split():
    fs = stats({**{i: (0, 1) for i in range(160, 162)}}, n=300)
    windows = segment_windows(fs, 3, [0.5, 0.59])
    assert all(a[1] <= b[0] for a, b in zip(windows, windows[1:]))
    ds = DetectSettings(min_spacing_s=0, segments=3, break_fractions=[0.5, 0.59], midpoint_boost=True)
    picks = recommend_splits(find_candidates(fs, ds), settings_windows(fs, ds))
    assert sum(c is not None for c in picks) == 1


def test_a_transition_straddling_the_window_edge_still_counts():
    fs = stats({**{i: (0, 1) for i in range(116, 122)}})          # 11.6–12.2 s; window starts at 12.0
    ds = DetectSettings(min_spacing_s=0, segments=2, midpoint_boost=True)
    assert recommend_splits(find_candidates(fs, ds), settings_windows(fs, ds))[0].start_s == pytest.approx(11.6)


def test_a_break_with_a_kept_or_manual_split_gets_no_second_one(three_segment_video):
    from bsr.session import SourceSession
    s = SourceSession(three_segment_video)
    s.analyze(); s.find(audio_signal=False)
    first, second = s.recommended()
    s.add_manual(first.start_s + 0.5)                               # your own split near the first break
    ids = s.accept_recommended()
    assert ids == [second.id] and s.decisions[first.id].state == "proposed"
    assert [kept for _, kept in s.break_status()] == [True, True]
    assert s.accept_recommended() == []
    assert len(s.accepted()) == 2                                   # one cut per break, never two


def test_reopened_project_keeps_splits_detection_no_longer_finds(three_segment_video, tmp_path):
    from bsr.session import SourceSession
    s = SourceSession(three_segment_video)
    s.analyze(); s.find(audio_signal=False)
    s.accept_recommended()
    kept = sorted((d.start_pts, d.end_pts) for d in s.accepted())
    s.set_segments(2); s.find(audio_signal=False)                   # thirds are no longer suggested
    project = tmp_path / "p.bsrproj"; s.save_project(str(project))
    again = SourceSession.load_project(str(project))
    again.analyze(); again.find(audio_signal=False)                 # the desktop's open-project path
    assert sorted((d.start_pts, d.end_pts) for d in again.accepted()) == kept
    assert all(again.decisions[c.id].start_pts == c.start_pts for c in again.candidates)
    assert len({c.id for c in again.candidates}) == len(again.candidates)


def test_missing_season_on_the_service_is_reported_not_fatal(monkeypatch):
    from bsr_ui.episodes_dialog import _seasons
    class Prov:
        def season(self, show, n):
            if n == 2:
                raise metadata.LookupError_("Not found on the service.")
            return [DbEpisode(n, 1, "A")]
    listings, missing = _seasons(Prov(), "1", [1, 2])
    assert list(listings) == [1] and missing == [2]
    with pytest.raises(metadata.LookupError_, match="none of these seasons"):
        _seasons(Prov(), "1", [2])


def test_cleared_keys_stay_cleared_this_session(monkeypatch):
    monkeypatch.delenv("BSR_TMDB_TOKEN", raising=False)
    monkeypatch.setattr(metadata, "_session_secrets", {})
    monkeypatch.setattr(metadata, "_keychain", lambda account: "from keychain")
    monkeypatch.setattr(metadata.sys, "platform", "linux")
    assert metadata.get_secret("tmdb") == "from keychain"
    metadata.set_secret("tmdb", "")
    assert metadata.get_secret("tmdb") is None
