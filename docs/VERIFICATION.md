# Verification record — 29 Oct 2026, engine 0.6.0

## 0.6.0 Windows

The full test suite runs on GitHub's Windows Server runners (Python 3.12, the pinned PyAV/Qt
wheels): **219 passed, 2 skipped** — the same two skips as macOS/Linux (an x264 build that closes
the GOP, and the real-episode test that needs `BSR_REAL_DIR`). Six runs: five finished in about
five minutes. One stalled at about one third of the tests (the format workflows that open the player)
before per-test logging was on; it did not recur in four later runs. The job now dumps stacks and stops
after 45 minutes if it does.

The packaged app (PyInstaller, 105 MB zip, Inno Setup installer) passes its own self-test on Windows:
it encodes a test video with the bundled FFmpeg, detects the black frame, splits it with boundary
re-encoding, independently validates both parts, builds the main window with Segoe UI and Qt Multimedia,
and saves a screenshot rendered by the real Windows display. The screenshot shows the same theme and
layout as the Mac app. Two tests needed Windows-neutral path comparisons; no app code failed.

Not verified: interactive use on a physical Windows PC, video playback with sound (the runners have
no audio device), the Windows 11 caption colour (the screenshot covers the window contents only),
high-DPI scaling, and Windows on ARM (the x64 app should run under emulation).


## 0.5.0 two- and three-segment episodes

New checks (`tests/test_segments.py`) cover break windows for 1–4 segments (two segments keep the
original 40–60%), recommending one third and two thirds over a stronger black at halfway,
runtime-weighted breaks, a break with no transition, older settings, file-name parsing for common
naming styles, season-wide numbering (ranges, gaps, files named by their first segment, separate
seasons), database title matching with close spellings, count checks, part names, and TMDB/TVDB
replies (both TMDB key styles, TVDB login and paging, caching without keys, plain-language errors).

A generated 30-second MP4 with three segments and decoy blacks inside segments is analysed,
splits are recommended at 33% and 67%, kept, named `Toon Town - S01E04/05/06 - …` and exported.
All three parts pass independent frame count/order and copied-packet validation; the source
SHA-256 is unchanged. The same file is exported from the CLI. A desktop test drives the segment
selector, recommendation card, stars, timeline, Keep recommended, the Episode names dialog with a
recorded TMDB season, edited titles and runtime-based breaks. Changing the segment count no longer
drops splits you kept that the new settings would not suggest.

Full suite: see the pull request. On headless Linux the desktop tests need `QT_AUDIO_BACKEND=alsa`
(PipeWire otherwise crashes Qt Multimedia). Online lookups were tested with recorded replies, not
against the live services. Live Cocoa playback is unchanged and not newly validated.


# Earlier versions

## 0.4.6 integrated title bar

On macOS the main window uses Qt's expanded client area with a transparent title bar, so the brand bar sits under the window buttons. Checked on macOS with PySide 6.12: the bar fills the top of the window, the buttons sit beside the logo, dragging the bar moves the window, and full screen restores the regular layout. Other platforms keep the normal title bar. `tests/test_about.py` passes; the offscreen desktop tests crash in Qt Multimedia on headless Linux before and after this change.

## 0.4.5 midpoint recommendations and sensitivity

Seven new checks cover single-frame near-black transitions inside the central
40–60%, unchanged detection elsewhere, dark-scene rejection, custom thresholds,
variable frame timing and nonzero starting timestamps, nearby stronger suggestions,
full-resolution verification using the original video's midpoint, and older projects.

An actual generated MP4 contains matching near-black flashes at 20% and 50%.
Balanced detection recommends only the single frame at 50%. The Qt interface test
checks the card, star, timeline, review-only action, busy guard, cached settings
updates, saved projects, rejection and reset. Accepting the suggestion exports two
parts that pass independent frame order/count and copied-packet validation; the
source SHA-256 remains unchanged.

Relevant engine, desktop, review, multiple-selection, format, auto-cut, MP4 audio
startup and About regressions bring coverage to **143 distinct checks passed,
1 existing encoder-dependent test skipped**. Actual windows were rendered and
visually inspected at 1,280 and 1,440 pixels wide, including expanded settings.
These Qt checks use the offscreen platform; live Cocoa playback is not newly
validated. The original native Python, Qt and media libraries are reused.

## 0.4.4 multiple split selection

Actual Qt Shift-click gestures on macOS (offscreen platform) select a contiguous
range, and Keep selected / Skip selected change only those decisions. Background
checks preserve the selection; one Undo restores the whole batch. Coverage includes
manual rows, hidden rejected suggestions, empty selection, repeat decisions,
busy-state guards, and switching files with overlapping decision IDs.

**33 desktop/review checks pass**, including the original single-split workflow
and a new batch workflow that exports three independently validated segments from
a generated MP4. A rendered window confirms the count, hint and batch buttons.
The media engine and native runtime are unchanged. Live Cocoa gestures and
hardware playback were not exercised by these offscreen checks.

## 0.4.3 simpler app header

The main-window banner now contains only the app icon and name, aligned vertically
in a 60-pixel header. The tagline and numbered Import / Review / Split labels are
removed. Actual Qt windows were rendered and inspected at 1,440 pixels and the
minimum permitted width. This changes the layout only; media functions and the
native runtime are unchanged. Live Cocoa interaction remains unverified.

## 0.4.2 MP4 audio startup preservation

The reported ChalkZone S02E01 MP4 starts video at 83 ms and audio at zero.
The old export rebased video to zero, producing audio packets at -83 ms; the
MP4 muxer declared those 3,984 real audio samples to be encoder padding.
The validator correctly rejected this loss. The fix shifts all selected tracks
by the same amount, preserves the original -83 ms A/V offset, and retains genuine
encoder priming. Subtitle cues and chapters follow the same timeline shift.

Both original reviewed parts pass independent export validation without creating
an encoder: **17,184 + 16,725 video frames**. The first part has **34,406,400 audio
samples**, all checked sample-for-sample against the source, recovering the 3,984
samples previously skipped. Every selected packet, subtitle text, subtitle timing,
track property and planned frame is preserved. Full source SHA-256 is unchanged.
Corrected parts and the verification report are delivered outside version control.

Ten new generated MP4/MOV checks cover two audio tracks, two nonzero picture offsets,
real AAC priming, unintended padding rejection, caption timing and rejection of
changed subtitle text. The existing subtitle/chapter test now checks the common
shift applied to the whole timeline. Native playback is unchanged.
Regression coverage: **101 distinct checks passed, 1 existing test skipped**. The
first broad run had 90 passes and exposed the old subtitle assertion, which
assumed subtitles did not share the audio/video shift. After correcting that
expectation, the subtitle/chapter check and all 10 new startup checks pass.

## 0.4.1 cut recovery and Finish job

The reported ChalkZone S02E07 Xvid AVI split at **645.311333 s** is reproduced and
fixed. MPEG-4 boundary encoders now preserve the source's reorder delay in their
VOL headers. Switching to low-delay previously discarded a pending copied frame;
switching back repeated an encoded frame. With the fix, the independently decoded
parts contain **15,472 + 18,354 = 33,826 frames**, exactly matching the plan.
Only **2 tail + 4 head video frames (0.25025 s)** are encoded; MP3 stays copied.
Copied join frames, encoded-frame PSNR, packet identities, timing, track properties,
seeking and audio sync all pass. Full source SHA-256 matches before/after.
The reviewed project, both playable parts and JSON report are delivered separately.

The generated MPEG-4 B-frame fixture was previously configured in an unreachable
branch, so its old test did not actually contain B-frames. The fixture is corrected,
and 12 new cuts across AVI, MKV and MP4 assert real reordering before testing joins.
The MP4 seek check now prerolls by measured packet reordering and decodes to the
expected target frame, including single-frame parts. The CLI cross-check retains
the demuxer's timestamp precision rather than converting VFR to a frame-rate clock.
Seeking after decoding an HEVC encoded tail exposed stale parameter-set IDs.
Encoded HEVC exports now repeat the original decoder headers at every copied
keyframe, preserving compressed pictures and restoring correct backward seeks.
The stronger seek check decodes through the expected target, rather than testing
only the first keyframe returned by the demuxer. Strict-copy exports are unchanged.

Frame-count, presentation times, copied identity and encoder packet checks remain
strict; failed outputs are never published as successful exports.

Finish job stays clickable for analysed, idle files. Blockers are shown next to
the button and **Review split** opens the unresolved or too-close cut. Exports still
require a valid plan. Tests cover both blockers, recovery by skipping a cut, disk
errors and cancellation. Export jobs and project-loading jobs retain their source
item so failure restores Ready and clears the busy state.

Playback shutdown now detaches the Qt video/audio outputs before widget deletion.
The test harness disposes closed windows while QApplication remains alive, avoiding
native objects being destroyed during Python interpreter teardown. Player checks
and desktop review checks both exit cleanly in isolated runs.

Final checks: **153 passed, 2 skipped**, across two complete groups: desktop/format/player/review **109 passed**, and remaining engine/About/real-file checks **44 passed, 2 skipped**. Both processes exit cleanly. The skips are the existing encoder-dependent open-GOP fixture and optional externally configured real-file suite; the reported real AVI was checked separately.
Live Cocoa interaction remains unavailable; actual Qt widgets are tested offscreen.

## 0.4.0 player, fast review and naming

Full suite: **135 passed, 2 skipped**. Subsequent targeted checks for the final
compact layout and two new playback-dialog tests: **29 passed**. The skips are
an existing encoder-dependent open-GOP fixture and the optional real-file suite,
which requires its own input folder; real playback was checked separately below.

- Actual Qt FFmpeg playback/rendering, decoded audio buffers, pause, seeking, audio
  selection, speed, mute and volume pass on **28 container/codec combinations**.
  Cut-loop playback and source switching also pass. The matrix includes all 27
  existing format inputs plus two-audio-track H.264 MP4.
- MPEG/TS seeking initially failed in Qt's direct demuxing. A separately cached,
  stream-copy MKV preview fixes it; source files and export paths are untouched.
- AV1 software video stays aligned to Qt's playback/audio clock; Qt's native AV1
  path required unavailable hardware in this environment. Ten-bit CPU frames use
  mapped planes when Qt's toImage cannot convert them. All matrix variants pass.
- The actual desktop import → detect → keep/skip/undo → adjust → name → export
  workflow passes. A custom 4.280-second split persists in the project and exports
  two real, independently validated files with the chosen names.
- Naming tests cover episode metadata, hyphens, compound words, semicolons,
  underscores, dashes, tag removal and title/part count mismatches. Time input
  accepts seconds/MM:SS/HH:MM:SS.mmm and rejects malformed values.
- The expanded player resumes at the playhead. Results can play an exported part
  and open its output folder; the dialog controls are exercised with real playback.
- Real read-only **1x01 MPEG-2 MKV** and **ChalkZone S01E04 H.264 MP4** render video,
  decode audio buffers and seek to one minute with the new player. Audio track
  counts are 3 and 1; the MP4 also exposes its subtitle track. Full source SHA-256
  hashes match before/after. See the delivery's Playback Verification.json.

UI layout was rendered and inspected with Qt offscreen. This environment has no
usable audio output device and cannot show a live Cocoa desktop, so audible hardware
output and live macOS window behavior remain unverified. Actual audio decoding and
playback timing are tested. The player uses Qt Multimedia 6.12.0 / FFmpeg 9.0.1;
the export engine continues to use PyAV 19.0.1 / FFmpeg 9.0.2. Matching official
macOS playback wheels and added runtime files are recorded by SHA-256.

## 0.3.1 About and menu identity

Three targeted desktop checks pass with the bundled macOS runtime: the About menu
opens twice and closes via its Close button, shows the current version, and has the
native About action role; startup sets the application name, display name and version
before QApplication construction and preserves file arguments with spaces; the existing
analysis/review/mode-switch desktop workflow still passes. The About window was rendered
and visually inspected using Qt offscreen.

Core Foundation reports an empty main-bundle information dictionary for bundled Python.
Qt Cocoa consequently uses argv[0] as its application-menu fallback. Startup now supplies
`Banana Split Redux` as argv[0], replacing `main.py` / `__main__.py`, before native menus
are initialized. The outer bundle's CFBundleName/CFBundleDisplayName are already correct.
This follows Qt's Cocoa application-name resolution and native About-role behavior:
https://github.com/qt/qtbase/blob/dev/src/plugins/platforms/cocoa/qcocoahelpers.mm
https://doc.qt.io/qt-6/qmenubar.html

Live Cocoa menu appearance cannot be inspected in this restricted environment; the
startup identity and Qt fallback inputs are verified. The media engine behavior and
native runtime binaries are unchanged from 0.3.0.

## 0.3.0 format expansion

Automated suite: **80 passed, 1 skipped** on macOS arm64. The existing skip is
an open-GOP fixture emitted as a closed GOP by the current encoder.

The matrix in SUPPORT_MATRIX.md covers 27 container/codec combinations, exercising
import, detection, previews, project restore, strict exports, exact exports and
black removal with independently validated outputs. Each matrix input also checks
that the actual desktop exact-cut mode is selectable. Additional variants cover
hvc1 HEVC, 720p 10-bit HEVC, AVI B-frames, interlaced MPEG-2, close cuts sharing a
GOP and high-quality CRF 1 H.264 boundaries.

Real-file checks on macOS:

- Original 1x01 MPEG-2 TFF/open-GOP episode: short segments 3:33.000–3:35.720 and
  3:35.720–3:39.000 preserve every frame and all three AC-3 tracks. At the main cut,
  only 0.32 seconds / 8 frames are encoded; including the two excerpt edges,
  0.52 seconds / 13 frames are encoded. All copied join frames match, audio sync
  checks pass, and source SHA-256 is unchanged. Reports/playable parts are in
  the delivery's Format Verification/MPEG-2 folder.
- Current ChalkZone S01E04 MP4: two short segments around 11:43.785 again pass
  packet, frame-order, join, quality, audio and subtitle checks. AAC and mov_text
  are copied; 1.126 seconds of video is encoded. Source SHA-256 is unchanged.
- The FFmpeg cross-check now ignores repetition-summary lines as summaries of
  already recorded messages; actual new decoder messages still fail validation.
  The MPEG-2 source and excerpts share the same existing picture-extension warning.

The 0.3.0 bundle reuses the original native runtime/binaries; only app code,
assets, docs and version metadata are updated. Tests and previews use Qt offscreen.
A live Cocoa window and external-player/hardware decoding are not established here.

## Historical 0.2.1 MP4 fix

43 tests passed, 1 skipped on macOS. MP4 long-GOP exports now pass independent decoding,
join identity, re-encoded frame quality, frame order and AAC synchronization checks.
The desktop regression test confirms that MP4 enables the re-encoding mode, MPEG-2 shows
its unsupported reason and the mode menu contains only two implemented entries.
Real ChalkZone S01E04 MP4 segments around 11:43.785 are validated with copied AAC and
mov_text subtitles; detailed reports accompany the delivery in MP4 Verification/.
The source is read-only, with its full SHA-256 compared before and after.

## Current 0.2.0 checks on macOS arm64

- Automated engine and desktop mode regression suite: 42 passed, 1 skipped. The skip is an
  existing open-GOP fixture that the current encoder emits as a closed GOP.
- Desktop workflow verified with the actual PySide6 widgets using Qt's offscreen platform:
  import, analyse, accept a cut after analysis, switch strict mode, switch between H.264
  and MPEG-2 files, review and export; both generated output parts passed validation.
- Real 1x02 cut at 9:47.720: two short segments exported on macOS with PyAV 19.0.1 /
  FFmpeg 9.0.2. One frame before the cut (0.04 s), 41 after (1.64 s), total 1.68 s of video
  re-encoded. Both audio tracks copied; A/V offset change 0.00 ms. Copied join frames are
  bit-identical, all planned frames present, FFmpeg CLI decode clean. Reports accompany
  the delivery in Verification/. Source SHA-256 checked before and after, unchanged.
- Interface screenshots rendered from the real episode and inspected for clipping and
  overlaps. The 0.2.0 bundle reuses the original macOS runtime, libraries and icon, and
  includes the updated engine, UI, docs and code hash manifest.
- A live Cocoa window could not be opened from the restricted execution environment:
  macOS reported “no screens available”. Window checks here use Qt offscreen. The original
  0.1.0 app had already been launched by the previous developer; that does not establish
  a live 0.2.0 launch. Player tests outside FFmpeg and Apple developer signing remain undone.

## Prior developer's 0.1.0 record

The material below is retained from the original delivery for context. Its test counts,
launch claims and unverified items describe 0.1.0; the current checks above supersede them.

## Source material (read-only; SHA-1 identical before and after all work)

| File | Container / video | Audio | Structure found |
|---|---|---|---|
| Mickey Mouse Works 1x01 [VHSRip].mkv (1.69 GB, 23:07.8) | MKV, MPEG-2 Main 720×576 interlaced tff, 25 fps, SAR 16:15, limited range | 3 × AC-3 2.0 (rus, rus, eng), Matroska lacing | 2,894 keyframes, every GOP after the first is **open** (2 leading B-frames); 0.48 s GOPs |
| Mickey Mouse Works 1x02 [VHSRip].mkv (1.00 GB, 22:04.4) | MKV, H.264 High@4.0 720×576 **MBAFF** bff, 25 fps, SAR 12:11, in-band SPS/PPS | 2 × AC-3 2.0 (rus, eng) | 230 IDR keyframes, closed GOPs of ~5 s (max 10 s), reorder depth 2; TV channel logo on every frame |

Black on these VHS captures measures Y≈10–28 (below nominal 16, noisy); default darkness
threshold 10 % and 98 % dark pixels detect transitions while tolerating the channel logo.
Full analysis (decode every frame) on the Linux VM of your Mac: 1x01 ≈ 25 s, 1x02 ≈ 120 s.

## Real exports (all validated; in `BSR Test Output/`)

| Test | Result |
|---|---|
| 1x01 full split, copy mode, 5 cuts at 3:35, 9:21, 11:01, 16:36, 22:23 → 6 parts | All pass. 34,686 of 34,694 frames written; the 8 missing are the reported undecodable open-GOP **black** frames (2 at each of 4 cuts; the 22:23 cut fell on a closed keyframe). All 43,368 frames of each audio track present once. A/V offset Δ 0.00 ms. |
| 1x01 small test, cuts at 1:00, 2:13, 3:35 → 3 parts | All pass (this run exposed and fixed a lacing bug, see below). |
| 1x02 copy mode, cuts at 1:13 and 2:34 → 2 parts | All pass; byte-identical packets, every frame. |
| 1x02 transition at 9:46 (no keyframe inside the black) | Correctly marked *Needs review*; export refused until resolved. With the explicit "cut 0.8 s after the black" choice, the part ending there (2:34.84–9:49.36, 434 s) was exported and validated. |
| 1x02 boundary re-encoding, exact cuts at 9:47.72 and 15:33.72 → parts 2, 3, 4 | All pass. Re-encoded: 1 frame (0.04 s) at the end of part 2, 41 frames (1.64 s) at the start of part 3, 132 frames (5.28 s) at the start of part 4; 0 audio samples. Copied frames across joins bit-identical (50, 466 and 260 frames checked). Re-encoded luma PSNR 43.8–50.1 dB. FFmpeg CLI decode clean. |

## Bugs found by validation during development (all fixed, all now covered by tests)

1. Matroska lacing: several AC-3 frames share one file position; packet identification by
   position wrote only one per block. Caught by the byte-identity check on the first real export.
2. Encoder delay (CodecDelay) cannot be cleared on copied streams with PyAV; now reported
   before export and checked.
3. x264 aspect ratio given through its option string was truncated (`12:11` → `12:1`).
4. Re-encoded piece with a different frame-reordering depth than the source made FFmpeg's
   decoder silently drop ~30 s of frames after the join, with no error message. Caught by the
   frame-count check; the encoder now matches the source's reorder depth.
5. Re-encoded tail packets could be written before the last copied video packets when audio
   ran ahead in the file (non-monotonic timestamps). Found on the real 1x02 export.

## Automated tests

37 tests (`tests/test_engine.py`): pass on Linux x86-64 (Python 3.13, PyAV 19.0.1, FFmpeg 9.0.2);
35 pass + 1 skipped on the arm64 Linux VM of your Mac (Python 3.10, PyAV 17.1.0, FFmpeg 8.1.1 —
the skip is a fixture whose open GOP that x264 build happened to close). UI driven end to end
headless (`tests/ui_smoke.py`) on fixtures and on real 1x01/1x02 excerpts, with screenshots.

## Not verified

- **macOS app bundle:** launches on your Mac (Apple silicon) — verified by launching it and
  capturing its main window. Your own session log from 15:54–16:01 shows the same build importing
  1x03 from an external drive, analysing 33,177 frames and resolving four open-GOP cuts on macOS.
  An **export on macOS has not been observed yet**; exports were verified on Linux with the same
  engine code and the same FFmpeg 9.0.2 libraries. The bundle is unsigned (no Apple developer ID).
- The desktop UI could not run on your Mac's VM (no OpenGL library there); it was tested on
  copies of real excerpts in the cloud workspace instead.
- Playback of outputs in players other than FFmpeg-based ones (QuickTime cannot open MKV;
  VideoToolbox hardware decoding of re-encoded joins) — untested.
- HEVC/AV1/VP9 sources, bitmap subtitles, Opus/FLAC audio, MP4 boundary re-encoding — not
  covered (see support matrix).
- Packaging as a signed `.app` bundle — not done; the launcher script is the supported way to run.
