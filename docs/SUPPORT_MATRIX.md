# Support matrix — engine 0.1.0

"Verified" means an automated test or a validated real-file export proved the claim by
packet inspection and decoding, not just that a file was produced. Anything not listed is
**not supported** yet, and the app says so before export instead of guessing.

## No re-encoding (strict copy) — default

| Video codec | Container (in → out) | Copy-safe cut points | Status |
|---|---|---|---|
| H.264 8-bit (progressive) | MKV → MKV | IDR keyframes, proven by decode | Verified (fixtures) |
| H.264 8-bit (MBAFF interlaced) | MKV → MKV | IDR keyframes, proven by decode | Verified (fixture + real 1x02) |
| H.264 10-bit | MKV → MKV | IDR keyframes, proven by decode | Verified (fixture) |
| H.264 open GOP (recovery-point I-frames) | MKV → MKV | Never used when a leading picture is visible (verified); black-leading-picture drop is implemented but only fixture-tested for MPEG-2 | Partly verified |
| H.264 | MP4 → MP4 | IDR keyframes | Verified (fixture) |
| MPEG-2 (closed GOP) | MKV → MKV | I-frames without leading B-frames | Verified (fixture) |
| MPEG-2 (open GOP, interlaced) | MKV → MKV | I-frames whose 2 leading B-frames are black: those frames are dropped (needs your acceptance or a tolerance setting) | Verified (fixture + real 1x01) |
| VFR timestamps, non-zero start times | MKV → MKV | as above | Verified (fixture) |
| HEVC, VP9, AV1, MPEG-4 Part 2, others | — | Keyframes are proven by decode before use, but no test fixture exists yet | **Untested** — treat results with care; validation still runs |

Every copy boundary is **proven by decoding**: frames on both sides of the cut are decoded
with fresh decoders that see only the packets the output files will contain, and must be
bit-identical to a continuous decode of the source.

## Audio (always copied)

| Codec | Cut granularity | Notes | Status |
|---|---|---|---|
| AC-3 | 1 packet (32 ms @ 48 kHz) | Each part gets the packets that start at/after its first picture | Verified (fixtures + real) |
| AC-3 with Matroska lacing | 1 frame | Laced blocks are split into frames | Verified (real 1x01) |
| AAC | 1 packet (21 ms) | First frame after a cut decodes without its predecessor's overlap (~21 ms) — reported before export | Verified (fixtures, MKV + MP4) |
| Opus, FLAC, others | packet | Copied; not covered by fixtures | Untested |

A/V offset in every part equals the source's offset at that point (tolerance 1.1 ms, one
Matroska tick); measured Δ = 0.00 ms on all fixtures and real exports. If the source declares
an encoder delay (Matroska CodecDelay), PyAV cannot clear it on copied streams, so later parts
inherit it: compliant players skip that many samples at the very start of each later part.
This is reported before export (AC-3 written by FFmpeg: 256 samples / 5.3 ms; the real
mkvmerge files declare none).

## Subtitles, chapters, attachments

| Item | Handling | Status |
|---|---|---|
| ASS/SSA, SRT (text) | Timing rewritten: cues crossing a cut are clipped to each part; text bytes unchanged | Verified (fixture) |
| Bitmap subtitles (PGS, VobSub, DVB) | Not cut yet — reported; deselect or keep the source | Not supported |
| Chapters | Clipped and re-based to each part | Verified (fixture) |
| Attachments (fonts) | Copied into every part (Matroska only) | Verified (fixture) |
| Track language, title, disposition | Preserved | Verified |
| mkvmerge statistics tags (BPS, DURATION, NUMBER_OF_*) | Dropped (they describe the whole source) | By design |

## Exact cuts with boundary re-encoding (opt-in)

| Video | Container | Encoder | Status |
|---|---|---|---|
| H.264 8-bit 4:2:0, progressive, reorder depth 0–2 | MKV → MKV | libx264 (bundled with PyAV), CRF 12 default | Verified (fixtures, incl. corrupted-join rejection) |
| H.264 8-bit 4:2:0, MBAFF interlaced (tff/bff), reorder depth 0–2 | MKV → MKV | libx264 | Verified (fixture + real 1x02, 3 joins on the full episode) |
| H.264 in MP4, 10-bit, 4:2:2, HEVC, MPEG-2, others | — | — | Not offered |

Scope: the later part re-encodes frames from the exact cut up to the next clean IDR; the
earlier part copies up to the last point where decode order and display order agree (usually
0–2 frames are re-encoded). Re-encoded pieces carry their own SPS/PPS ids and the same
frame-reordering depth as the source. Audio is never re-encoded. Re-encoded frames are **not
lossless** (measured luma PSNR on real 1x02: 43.8–50.1 dB).

Join verification (every export): copied frames after a head join (whole following GOP) and
before a tail join must decode bit-identically to the source; every planned frame must decode;
re-encoded frames must reach ≥ 35 dB PSNR; FFmpeg's command-line decoder (if installed) must
report nothing the source doesn't already report.

Players: joins are verified with FFmpeg's decoder (used by IINA, mpv, VLC on macOS). Hardware
decoders and other players have not been tested.

## Full re-encode

Not offered in this version.

## Tool versions tested

| Environment | Python | PyAV | FFmpeg libraries |
|---|---|---|---|
| Linux x86-64 (development) | 3.13 | 19.0.1 | 9.0.2 |
| Linux arm64 VM on your Mac (real episodes) | 3.10 | 17.1.0 | 8.1.1 |
| macOS arm64 (target) | — | — | **not executed yet** |
