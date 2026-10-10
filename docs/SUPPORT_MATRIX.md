# Support matrix — Banana Split Redux 0.3.0

Verified means files were generated with real encoders, then imported, analysed,
previewed, saved/restored, split, exported and independently decoded. All rows below
exercise strict copy, automatic exact cuts (when needed) and black removal. The
actual desktop mode is checked as selectable for every matrix input.

| Video | Verified inputs | Boundary treatment | Output |
|---|---|---|---|
| H.264 8-bit 4:2:0 | MKV, MP4, M4V, MOV, AVI, TS, FLV | libx264 only near cuts; otherwise copy | MP4/MOV retained; other inputs MKV |
| H.264 10-bit 4:2:0 | MKV | libx264 only near cuts | MKV |
| H.264 MBAFF interlaced 8-bit, TFF/BFF | MKV | Same field order, SAR and pixel format | MKV |
| HEVC/H.265 8-bit 4:2:0 | MKV, MP4, MOV, TS | libx265 only near cuts | MP4/MOV retained; other inputs MKV |
| HEVC/H.265 10-bit SDR 4:2:0 | MKV, MP4, TS | 10-bit retained; libx265 only near cuts | MP4 retained; other inputs MKV |
| MPEG-2 4:2:0, closed/open GOP | MKV, MPG/MPEG, TS | Encode boundary frames, including otherwise undecodable leading B-frames | MKV |
| MPEG-2 interlaced TFF | MKV | Field order/SAR retained; all frames preserved in exact mode | MKV |
| MPEG-4 Part 2 (Xvid/DivX codec family) 4:2:0 | AVI, MP4, MKV | MPEG-4 encoder only near cuts; AVI B-frame timing recovered | MP4 retained; other inputs MKV |
| VP8 8-bit 4:2:0 | WebM | libvpx near cuts | MKV |
| VP9 8-bit 4:2:0 | WebM | Lossless libvpx-vp9 near cuts | MKV |
| AV1 8-bit 4:2:0 | MKV, MP4 | libsvtav1 near cuts | MKV/MP4 retained |
| WMV2 8-bit 4:2:0 | WMV/ASF | WMV2 encoder near cuts | WMV/ASF |
| MJPEG full-range 4:2:0 | AVI | Verified per-frame copy cuts | MKV |
| ProRes 422, 10-bit | MOV | Verified per-frame copy cuts | MOV |
| FFV1 8-bit 4:2:0 | MKV | Lossless FFV1 at exact boundaries | MKV |

Additional tests cover HEVC MP4 `hvc1` input (boundary exports use `hev1` to carry
in-band parameter sets), 720p 10-bit HEVC, non-square pixels, H.264 variable frame
rate and nonzero starts, single-frame black transitions, and two cuts sharing a GOP.
MTS/M2TS and VOB are accepted extensions of the tested transport/program-stream
families; separate Blu-ray/DVD authoring and navigation have not been tested.

The app retains the source codec and pixel depth. A format extension alone does
not guarantee that every codec/profile stored inside it can be boundary encoded.
A copy boundary is usable only after independent decoder proofs on both sides.
Exact cuts replace only the needed frames; audio is always copied. The amount
can be one frame or several seconds, depending on GOP spacing.

## Audio and additional tracks

AC-3 (including Matroska lacing and multiple language tracks), AAC, MP2, MP3,
Opus, FLAC, PCM and WMA2 are covered by export/decode/sync checks. Packet-sized
cuts retain the source audio offset within 1.1 ms. AAC/Opus overlap or preroll
at the very first packet remains a reported codec limitation of copying audio.

ASS subtitles, chapters and font attachments are tested in MKV; mov_text captions
are preserved in the real ChalkZone MP4 exports. Subtitle cues crossing a cut
are clipped while text payloads remain unchanged. Languages, titles, dispositions,
field order, SAR, pixel format and colour properties are checked after export.

## Explicit exceptions

- HDR10/HLG/Dolby Vision exact boundary encoding is disabled: mastering/dynamic
  metadata preservation has not been verified. Decoded analysis, previews and
  verified keyframe copy cuts remain available.
- Untested pixel formats (including H.264 4:2:2/4:4:4, VP9 10-bit and AV1 10-bit)
  retain strict copy with a visible explanation. The UI never silently converts them.
- Interlaced boundary encoding is tested for H.264 8-bit and MPEG-2; other
  interlaced codecs retain strict copy.
- Packed AVI packets containing multiple pictures are rejected with an unpacking
  explanation. The tested AVI B-frame cases use one picture per packet.
- PGS/VobSub/DVB bitmap subtitle cutting is not implemented. These tracks are
  excluded by default and identified in the export review.
- Multiple independent video tracks and alternative WMV codecs have not been
  included in this matrix. Export validation remains mandatory by default.
- Full re-encoding is not implemented. Raw elementary streams, disc menus,
  damaged/encrypted media and external hardware/player interoperability are not
  established by these tests.

## What every export checks

Copied compressed payloads and their order must match the source. Lossless
Annex-B/length-prefix, ADTS and AV1 temporal-delimiter framing changes are
normalized for comparison. Smart joins explicitly record any restored source
parameter headers. No copied picture or audio payload is re-encoded.

Every planned frame must decode in order. Copied frames around joins must be
bit-identical; encoded frames must reach at least 35 dB luma PSNR against their
source, measured at the correct bit depth. Track properties, subtitle timing,
chapters, audio decode/sync and seeking are also checked. When installed, the
FFmpeg command-line decoder adds a separate cross-check. Failed outputs stay in
a hidden diagnostic folder and are never published as completed parts.

Tests run on Apple-silicon macOS with Python 3.12.13, PyAV 19.0.1, FFmpeg 9.0.2
and PySide6 6.12.0. UI checks use Qt offscreen; live Cocoa and separate player
checks remain outside this execution environment.

## In-app playback (0.4.0)

The 27 matrix inputs above plus two-track H.264 MP4 pass actual Qt player video
rendering, decoded audio, pause/seeking, speed, volume/mute and audio selection.
HEVC/H.264 10-bit display uses CPU plane conversion where needed. MPEG/TS uses a
separately cached MKV made by packet copying; AV1 uses PyAV video with Qt's audio
clock. Playback preparation does not change the source or export preservation rules.
Real MPEG-2 MKV and H.264 MP4 playback/seek checks pass with unchanged source hashes.
Physical audio devices, live Cocoa display and HDR tone-mapping are not verified here.
