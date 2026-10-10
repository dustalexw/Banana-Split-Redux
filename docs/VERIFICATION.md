# Verification record — 10 Oct 2026, engine 0.1.0

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

- **The app has not been launched on macOS itself.** Everything ran on Linux (cloud and the VM
  inside the Claude desktop app on your Mac). PyAV and PySide6 publish macOS arm64 wheels and
  the code has no Linux-specific parts, but the `.command` launcher, macOS file dialogs,
  Retina rendering and the Documents-folder permission prompt are untested.
- The desktop UI could not run on your Mac's VM (no OpenGL library there); it was tested on
  copies of real excerpts in the cloud workspace instead.
- Playback of outputs in players other than FFmpeg-based ones (QuickTime cannot open MKV;
  VideoToolbox hardware decoding of re-encoded joins) — untested.
- HEVC/AV1/VP9 sources, bitmap subtitles, Opus/FLAC audio, MP4 boundary re-encoding — not
  covered (see support matrix).
- Packaging as a signed `.app` bundle — not done; the launcher script is the supported way to run.
