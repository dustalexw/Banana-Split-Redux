"""Episode names dialog: number every part to match TVDB/TMDB and name it Show - S01E03 - Title."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QAbstractItemView)

from bsr import metadata
from bsr.episodes import FileEntry, number_files, part_name, runtime_fractions, season_check, show_key, file_numbering


class KeysDialog(QDialog):
    """TMDB / TVDB credentials, kept in the macOS keychain."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Episode lookup keys")
        lay = QVBoxLayout(self)
        info = QLabel("Only the show name is sent to the service you choose. Keys are stored in your "
                      "macOS keychain. TMDB: free API key or Read Access Token from themoviedb.org → Settings → API. "
                      "TVDB: v4 API key from thetvdb.com, plus your subscriber PIN if your key needs one.")
        info.setWordWrap(True); info.setObjectName("muted"); lay.addWidget(info)
        form = QFormLayout()
        self.tmdb = QLineEdit(); self.tmdb.setEchoMode(QLineEdit.Password)
        self.tvdb = QLineEdit(); self.tvdb.setEchoMode(QLineEdit.Password)
        self.pin = QLineEdit(); self.pin.setEchoMode(QLineEdit.Password)
        for w, account in ((self.tmdb, "tmdb"), (self.tvdb, "tvdb"), (self.pin, "tvdb-pin")):
            w.setPlaceholderText("saved" if metadata.get_secret(account) else "not set")
        form.addRow("TMDB key or token", self.tmdb)
        form.addRow("TVDB API key", self.tvdb)
        form.addRow("TVDB PIN (optional)", self.pin)
        lay.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def save(self):
        for w, account in ((self.tmdb, "tmdb"), (self.tvdb, "tvdb"), (self.pin, "tvdb-pin")):
            if w.text().strip():
                metadata.set_secret(account, w.text())
        self.accept()


class EpisodeDialog(QDialog):
    """Season-wide episode numbers for all parts, reviewed before any name changes."""

    COLUMNS = ["File", "Part", "Episode", "Title (editable)", "Numbered from"]

    def __init__(self, parent, entries: list[FileEntry], *, current: str | None = None, default_segments: int = 2,
                 years: dict | None = None, fetch=None):
        super().__init__(parent)
        self.setWindowTitle("Episode names")
        self.resize(1040, 620)
        self.entries = [e for e in entries if e.ref is not None]
        self.current = current
        self.default_segments = default_segments
        self.listings: dict = {}           # season -> [DbEpisode] for the looked-up show
        self.lookup_show = None
        self.matches = []
        self.titles: dict[tuple, str] = {}   # (path, part index) -> edited title
        self.assignments: dict = {}
        self._fetch = fetch
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._future = None
        self._timer = QTimer(self); self._timer.setInterval(100); self._timer.timeout.connect(self._poll)

        lay = QVBoxLayout(self)
        intro = QLabel("TVDB and TMDB list each segment of a two- or three-segment cartoon as its own episode. "
                       "Parts are numbered across the whole season, so the episode after a two-segment file "
                       "continues where it ended. Check a few parts by playing them before applying.")
        intro.setWordWrap(True); intro.setObjectName("muted"); lay.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Show:"))
        shows = sorted({e.ref.show for e in self.entries})
        self.show_edit = QLineEdit(next((e.ref.show for e in self.entries if e.path == current), shows[0] if shows else ""))
        self.show_edit.setMinimumWidth(220); row.addWidget(self.show_edit, 1)
        row.addWidget(QLabel("Episode list:"))
        self.source = QComboBox()
        self.source.addItem("From file names only", None)
        for key, label in metadata.PROVIDERS.items():
            self.source.addItem(label, key)
        row.addWidget(self.source)
        self.lookup_btn = QPushButton("Look up"); self.lookup_btn.clicked.connect(self.lookup); row.addWidget(self.lookup_btn)
        keys = QPushButton("Keys…"); keys.clicked.connect(self.edit_keys); row.addWidget(keys)
        lay.addLayout(row)

        row = QHBoxLayout()
        self.match = QComboBox(); self.match.setMinimumContentsLength(30); self.match.hide()
        self.match.currentIndexChanged.connect(self._match_changed)
        row.addWidget(self.match)
        row.addWidget(QLabel("One number in a file name means:"))
        self.numbering = QComboBox()
        self.numbering.addItem("Auto", "auto")
        self.numbering.addItem("A whole broadcast (E01, E02, E03 …)", "broadcast")
        self.numbering.addItem("Its first segment's episode (E01, E03, E05 …)", "database")
        self.numbering.currentIndexChanged.connect(self.recompute)
        row.addWidget(self.numbering)
        self.year = QCheckBox("Add year to the show name")
        self.years = years or {}
        self.year.setVisible(any(e.ref.year for e in self.entries)); self.year.toggled.connect(self.recompute)
        row.addWidget(self.year)
        row.addStretch()
        lay.addLayout(row)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().hide(); self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._title_edited)
        lay.addWidget(self.table, 1)

        self.notes = QLabel(""); self.notes.setWordWrap(True); lay.addWidget(self.notes)
        self.status = QLabel(""); self.status.setObjectName("muted"); self.status.setWordWrap(True); lay.addWidget(self.status)
        self.runtimes = QCheckBox("Place recommended splits using database runtimes (for files with Auto segments)")
        self.runtimes.setToolTip("When the database lists each segment's length (for example 7, 7 and 8 minutes), "
                                 "look for the breaks at those positions instead of equal thirds.")
        self.runtimes.setEnabled(False); lay.addWidget(self.runtimes)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_btn = buttons.addButton("Apply names to all", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.recompute()

    # -------------------------------------------------------------- numbering
    def _show_entries(self):
        """Files of the show being looked up (all files when the queue holds one show)."""
        keys = {show_key(e.ref.show) for e in self.entries}
        if len(keys) <= 1:
            return list(self.entries)
        typed = show_key(self.show_edit.text() or "")
        mine = [e for e in self.entries if show_key(e.ref.show) == typed]
        if not mine and self.current:
            cur = next((e for e in self.entries if e.path == self.current), None)
            mine = [e for e in self.entries if cur and show_key(e.ref.show) == show_key(cur.ref.show)]
        return mine

    def recompute(self, *_):
        listings = self.listings if self.lookup_show else {}
        mine = self._show_entries() if listings else self.entries
        others = [e for e in self.entries if e not in mine]
        mode = self.numbering.currentData()
        self.assignments = number_files(mine, listings, self.default_segments, mode)
        self.assignments.update(number_files(others, None, self.default_segments, mode))
        if mode == "auto" and self.entries:
            guess = file_numbering(sorted(self.entries, key=lambda e: e.ref.first))
            self.numbering.setItemText(0, "Auto · " + ("a whole broadcast" if guess == "broadcast" else "first segment's episode"))
        self._fill()

    def _fill(self):
        self.table.blockSignals(True)
        rows = [(e, i, a) for e in sorted(self.entries, key=lambda e: (e.ref.season, e.ref.first, e.path))
                for i, a in enumerate(self.assignments.get(e.path, []))]
        self.table.setRowCount(len(rows))
        for r, (e, i, a) in enumerate(rows):
            title = self.titles.get((e.path, i), a.title)
            values = [os.path.basename(e.path) if i == 0 else "", f"{i + 1} of {e.parts}", a.code, title,
                      {"title match": "segment title", "file name": "file name", "sequence": "season order"}[a.source]
                      + (" · not in database" if a.in_database is False else "")]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setData(Qt.UserRole, (e.path, i))
                if c != 3:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if c == 0:
                    item.setToolTip(e.path)
                if a.in_database is False:
                    item.setForeground(QColor("#f5ce69"))
                if e.path == self.current:
                    item.setBackground(QColor(250, 214, 64, 28))
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.blockSignals(False)
        notes = season_check(self.assignments, self.listings if self.lookup_show else None)
        self.notes.setText("\n".join(notes) if notes else
                           f"{sum(len(v) for v in self.assignments.values())} parts across {len(self.entries)} file(s); no gaps or repeats.")
        has_runtimes = any(runtime_fractions(parts) for parts in self.assignments.values())
        self.runtimes.setEnabled(has_runtimes)
        if not has_runtimes:
            self.runtimes.setChecked(False)

    def _title_edited(self, item):
        if item.column() == 3:
            self.titles[tuple(item.data(Qt.UserRole))] = item.text().strip()

    # -------------------------------------------------------------- lookup
    def edit_keys(self):
        KeysDialog(self).exec()

    def lookup(self):
        name = self.source.currentData()
        if not name:
            self.lookup_show, self.listings = None, {}
            self.match.hide(); self.status.setText("Numbering from file names only."); self.recompute(); return
        show = self.show_edit.text().strip()
        if not show:
            self.status.setText("Type the show's name first."); return
        mine = self._show_entries()
        year = next((e.ref.year for e in mine if e.ref.year), None)
        seasons = sorted({e.ref.season for e in mine} or {e.ref.season for e in self.entries})
        kw = {"fetch": self._fetch} if self._fetch else {}

        def job():
            prov = metadata.provider(name, **kw)
            matches = prov.search(show, year)
            if not matches:
                raise metadata.LookupError_(f"No show called “{show}” on {metadata.PROVIDERS[name]}.")
            pick = matches[0]
            return matches, pick, {s: prov.season(pick.id, s) for s in seasons}
        self._start(job, f"Looking up {show} on {metadata.PROVIDERS[name]}…")

    def _match_changed(self, i):
        if i < 0 or not self.matches or self._future:
            return
        pick = self.matches[i]
        if self.lookup_show and pick.id == self.lookup_show.id:
            return
        seasons = sorted({e.ref.season for e in self._show_entries()})
        kw = {"fetch": self._fetch} if self._fetch else {}

        def job():
            prov = metadata.provider(pick.provider, **kw)
            return self.matches, pick, {s: prov.season(pick.id, s) for s in seasons}
        self._start(job, f"Loading {pick.label}…")

    def _start(self, job, message):
        self.status.setText(message)
        self.lookup_btn.setEnabled(False)
        self._future = self._pool.submit(job)
        self._timer.start()

    def _poll(self):
        if not self._future or not self._future.done():
            return
        self._timer.stop()
        future, self._future = self._future, None
        self.lookup_btn.setEnabled(True)
        try:
            matches, pick, listings = future.result()
        except metadata.LookupError_ as e:
            self.status.setText(str(e)); return
        except Exception as e:  # pragma: no cover - unexpected service reply
            self.status.setText(f"Lookup failed: {e}"); return
        self.finish_lookup(matches, pick, listings)

    def finish_lookup(self, matches, pick, listings):
        self.matches, self.lookup_show, self.listings = matches, pick, listings
        self.match.blockSignals(True); self.match.clear()
        for m in matches[:12]:
            self.match.addItem(m.label)
        self.match.setCurrentIndex(max(0, next((i for i, m in enumerate(matches) if m.id == pick.id), 0)))
        self.match.setVisible(len(matches) > 1); self.match.blockSignals(False)
        counts = ", ".join(f"season {s}: {len(v)} episodes" for s, v in sorted(listings.items()))
        self.status.setText(f"{pick.label} on {metadata.PROVIDERS[pick.provider]} · {counts}. "
                            "Titles come from the database; edit any title before applying.")
        self.recompute()

    def wait(self, timeout_s: float = 10.0):
        """Finish a running lookup (used by tests)."""
        if self._future:
            self._future.result(timeout=timeout_s)
            self._poll()

    # -------------------------------------------------------------- results
    def names(self) -> dict[str, list[str]]:
        out = {}
        mine = {e.path for e in self._show_entries()}
        single = len({show_key(x.ref.show) for x in self.entries}) <= 1
        for e in self.entries:
            parts = self.assignments.get(e.path, [])
            if self.lookup_show and e.path in mine:
                show, year = self.lookup_show.name, self.lookup_show.year or e.ref.year
            else:
                show, year = (self.show_edit.text().strip() or e.ref.show) if single else e.ref.show, e.ref.year
            named = []
            for i, a in enumerate(parts):
                a.title = self.titles.get((e.path, i), a.title)
                named.append(part_name(show or e.ref.show, a, year, self.year.isChecked()))
            out[e.path] = named
        return out

    def fractions(self) -> dict[str, list[float]]:
        if not self.runtimes.isChecked():
            return {}
        return {path: f for path, parts in self.assignments.items() if (f := runtime_fractions(parts))}

    def done(self, r):
        self._timer.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        super().done(r)
