# Banana Split Redux

Split animated-series video files at reviewed black transitions, **without re-encoding**
wherever a clean cut is possible, and with every output independently checked.

Import → detect black intervals → review proposed cuts → choose preservation mode →
review the export plan → export and validate.

## Run it on your Mac

Requirements: macOS on Apple silicon or Intel, **Python 3.10 or newer**, and an internet
connection the first time (to download dependencies). FFmpeg is **not** needed separately:
the FFmpeg libraries ship inside the PyAV package.

1. If you don't have Python 3.10+: `brew install python@3.12` (Homebrew), or install from
   <https://www.python.org/downloads/macos/>. (Apple's built-in `/usr/bin/python3` is 3.9 and is
   too old.)
2. In Finder, open this folder and double-click **`Banana Split Redux.command`**.
   - The first run creates a private environment in `.venv` and installs PyAV, NumPy,
     Pillow and PySide6 (≈ 250 MB, a few minutes). Later runs start immediately.
   - If macOS says the file can't be opened, right-click it → **Open**. If Terminal asks for
     access to your Documents folder, allow it (the app reads your episodes from there).
3. Or from Terminal:
   ```bash
   cd ~/Documents/"Banana Split Redux/BananaSplitRedux"
   ./"Banana Split Redux.command"                 # app
   ./bsr-cli analyze "../<episode>.mkv" -v          # command-line engine (after first run)
   ```

Optional: if Homebrew's `ffmpeg` is installed, validation also cross-checks every output
with the `ffmpeg` command-line decoder.

## Using the app

1. **Add** episodes (drag them onto the window, or *Add…*), then **Analyse**. Each frame is
   decoded once for analysis (never for export); results are cached in
   `~/Library/Caches/BananaSplitRedux`, keyed by file fingerprint, settings and library versions.
2. Pick a **Detection** preset — *Conservative*, *Balanced* (default), *Sensitive*,
   *Single-frame* — or open **Settings** for darkness threshold, % dark pixels (lower it to
   tolerate logos such as a channel bug), minimum frames/duration, spacing, and an
   analysis-only crop for letterboxing (*Show analysis region* draws it on the preview).
3. Review candidates in the list or on the timeline. For each one you see the frames around
   the black interval and the planned cut, a frame strip (black frames, keyframes, verified copy
   boundaries), frame stepping and a silent ±2 s preview. **Accept** or **Reject**; choose where
   in the black to split (start / middle / end); optionally **Remove black** with padding to keep.
4. Status labels:
   - **Copied** — a copy-safe cut exists inside the black and was proven by decoding.
   - **Copied (drops black frames)** — open-GOP source: the cut drops a few undecodable
     *black* frames. Needs your acceptance, or set a tolerance in Settings.
   - **Needs review** — no copy-safe point inside the black. You choose: a nearby copy cut
     (the time difference and what moves between files are shown; nothing is deleted),
     **boundary re-encoding** (if supported), or skip. Nothing is moved or re-encoded silently.
   - **Boundary re-encoding** — exact cut; only frames up to the nearest clean keyframe are re-encoded.
5. **Output parts**: edit file names (or *Names from file title…*), choose the output folder and
   tracks, then **Review & Export…**. The plan dialog lists every part, every track's treatment,
   removed content and the encoding scope (estimated re-encoded frames/seconds; audio is never
   re-encoded). After export, the results show every validation check and the *measured*
   re-encoding per part. A JSON report is written next to the outputs.
6. *File → Save Project* stores your decisions; *View → Diagnostic Log* shows everything
   (also in `~/Library/Logs/BananaSplitRedux/bsr.log`).

Modes: **No re-encoding (strict)** is the default. **Exact cuts — boundary re-encoding**
is enabled only for tested codec/container combinations (see
[docs/SUPPORT_MATRIX.md](docs/SUPPORT_MATRIX.md)) and switches unresolved cuts to exact cuts;
you can still change any cut individually. Full re-encoding is not offered in this version.

## Safety guarantees

- Sources are opened read-only; an export refuses to write over a source file.
- Outputs are written to a hidden `.bsr-partial-*` folder, validated, then moved into place.
  A part that fails validation stays there as `FAILED-…` and is reported as failed.
- Copy mode creates no encoder (tested). The exported packets are proven byte-identical
  to the source packets; only the container and timestamps change (each part starts at 0).
- Validation of every output: track set and properties (codec, size, field order, colour,
  language, title, disposition), packet payloads, timestamps, full decode, frame count and
  order, first/last frame identity, seeking, audio decode and A/V offset, subtitles,
  chapters, attachments; joins and PSNR for boundary re-encoding.

## Architecture

| Module | Role |
|---|---|
| `bsr/media.py` | Probing and packet index (timestamps, flags, sizes, payload hashes) — no decoding |
| `bsr/detect.py` | Black analysis: per-frame dark-pixel fractions on a threshold grid (range/bit-depth aware), candidates, presets, optional audio level |
| `bsr/boundaries.py` | Keyframe structure (open GOP, leading/trailing pictures, IDR parsing) and decode **proofs** of copy-safe cut points |
| `bsr/planner.py` | Your decisions → resolved cuts → half-open segments on the integer video timeline |
| `bsr/export.py` | Packet-exact copy export via PyAV (no encoder), subtitle clipping, chapters, attachments, atomic finalisation |
| `bsr/smart.py` | Boundary re-encoding (H.264/MKV) |
| `bsr/validate.py` | Independent output validation |
| `bsr/session.py`, `bsr/cli.py` | Per-file state, projects, command line |
| `bsr_ui/` | PySide6 desktop interface (all media work in background threads) |

Stack: Python + **PyAV** (FFmpeg libraries, bundled in the wheel, so builds are reproducible
and don't depend on whichever FFmpeg is installed) + NumPy + **PySide6/Qt 6** (native macOS
widgets, drag and drop, threads). The engine is portable (tested on Linux x86-64 and arm64).

## Tests

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest tests            # 37 tests, ~30 s; generates fixtures on first run
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/ui_smoke.py FILE OUTDIR [SHOTS] [--accept-drops|--smart]
```

Fixtures (`tests/fixturegen.py`, generated with PyAV's own encoders) have numbered frames,
known black intervals (hard cut, single-frame flash, fade, logo-on-black), a dark scene that
must not be detected, letterboxing, 10-bit, VFR with a 10 s start offset, closed/open/long
GOPs, B-frames, MPEG-2 open GOP, MBAFF interlaced H.264 with 12:11 SAR, AC-3 + AAC tracks,
ASS subtitles crossing cuts, a font attachment, chapters, and audio sync beeps every second.

See [docs/VERIFICATION.md](docs/VERIFICATION.md) for what has been verified on your real
episodes and what has not.
