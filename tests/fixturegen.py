"""Generated test media with known frame identities, black intervals and audio markers.

Every fixture is produced with PyAV's bundled encoders, so tests need no
command-line ffmpeg. Ground truth (which frames are black, where audio beeps are)
is returned alongside each file.

Timeline (frame numbers at 25 fps, 20 s):
    0- 99  scene A
  100-109  hard black, 10 frames (silent)              4.00-4.40 s
  110-199  scene B
  200      single black frame (flash)                   8.00 s
  201-299  scene C, frames 240-270 a *dark scene* with visible detail
  300-311  fade-out, 312-317 black (6), 318-329 fade-in  12.48-12.72 s black
  330-449  scene D
  450-452  black with a small bright logo (3 frames)    18.00-18.12 s
  453-499  scene E
Audio: a scene-specific tone, silence over black, plus a 1 kHz 10 ms beep at
every whole second (frames 0, 25, 50, ...), aligned with a white sync square in
the picture on those frames.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from fractions import Fraction

import av
import numpy as np

W, H, FPS, N = 320, 240, 25, 500
SR = 48000

BLACK = set(range(100, 110)) | {200} | set(range(312, 318)) | set(range(450, 453))
LOGO = set(range(450, 453))
DARK = set(range(240, 271))
FADE_OUT = list(range(300, 312))
FADE_IN = list(range(318, 330))
BLACK_INTERVALS = [(100, 110), (200, 201), (312, 318), (450, 453)]   # half-open frame ranges

_DIGITS = {
    "0": ["111", "101", "101", "101", "111"], "1": ["010", "110", "010", "010", "111"],
    "2": ["111", "001", "111", "100", "111"], "3": ["111", "001", "111", "001", "111"],
    "4": ["101", "101", "111", "001", "001"], "5": ["111", "100", "111", "001", "111"],
    "6": ["111", "100", "111", "101", "111"], "7": ["111", "001", "010", "010", "010"],
    "8": ["111", "101", "111", "101", "111"], "9": ["111", "101", "111", "001", "111"],
}


def scene_of(n):
    for lo, hi, s in ((0, 100, 0), (110, 200, 1), (201, 300, 2), (330, 450, 3), (453, 500, 4)):
        if lo <= n < hi:
            return s
    return None


def _draw_number(y, n, x0=20, y0=20, scale=6, val=235):
    for k, ch in enumerate(f"{n:03d}"):
        for r, row in enumerate(_DIGITS[ch]):
            for c, bit in enumerate(row):
                if bit == "1":
                    yy = y0 + r * scale; xx = x0 + k * 4 * scale + c * scale
                    y[yy:yy + scale, xx:xx + scale] = val


def frame_planes(n, w=W, h=H, letterbox=0):
    rng = np.random.default_rng(n)
    y = np.full((h, w), 16, np.uint8)
    u = np.full((h // 2, w // 2), 128, np.uint8)
    v = np.full((h // 2, w // 2), 128, np.uint8)
    top, bot = letterbox, h - letterbox
    if n in BLACK:
        y[top:bot] = np.clip(14 + rng.integers(-3, 5, (bot - top, w)), 0, 255)   # VHS-like noisy black
        if n in LOGO:
            y[top + 10:top + 26, w - 40:w - 12] = 200
        return y, u, v
    s = scene_of(n)
    base = [120, 90, 150, 70, 110][s if s is not None else 0]
    gain = 1.0
    if n in FADE_OUT:
        gain = 1 - (FADE_OUT.index(n) + 1) / (len(FADE_OUT) + 1)
    if n in FADE_IN:
        gain = (FADE_IN.index(n) + 1) / (len(FADE_IN) + 1)
    yy = np.linspace(0, 40, w)[None, :] + np.linspace(0, 30, bot - top)[:, None]
    img = base + yy
    x = (n * 4) % w
    img[:, x:x + 12] = 220
    _ = img
    if n in DARK:
        img = np.full_like(img, 22.0)
        img[(bot - top) // 2 - 20:(bot - top) // 2 + 20, 40:w - 40] = 140   # visible detail in a dark scene
        img[:, x:x + 6] = 60
    region = 16 + (img - 16) * gain
    y[top:bot] = np.clip(region + rng.integers(-2, 3, region.shape), 16, 235).astype(np.uint8)
    if gain == 1.0:
        _draw_number(y, n, y0=top + 20)
        if n % FPS == 0:
            y[top + 4:top + 16, w - 16:w - 4] = 235   # sync square on beep frames
    u[:] = 128 + (s or 0) * 6 * gain
    v[:] = 128 - (s or 0) * 5 * gain
    return y, u, v


def audio_signal(n_samples, sr=SR, freq_scale=1.0):
    t = np.arange(n_samples) / sr
    frame = (t * FPS).astype(int)
    sig = np.zeros(n_samples, np.float32)
    for s, f in enumerate((220, 277, 330, 392, 440)):
        mask = np.array([scene_of(int(k)) == s and int(k) not in BLACK for k in range(N)])[np.clip(frame, 0, N - 1)]
        sig += (0.15 * np.sin(2 * np.pi * f * freq_scale * t) * mask).astype(np.float32)
    beep_len = int(0.010 * sr)
    for sec in range(int(n_samples / sr) + 1):
        a = sec * sr
        sig[a:a + beep_len] += 0.6 * np.sin(2 * np.pi * 1000 * np.arange(min(beep_len, max(0, n_samples - a))) / sr)
    return sig


def beep_times(duration_s):
    return [float(s) for s in range(int(duration_s) + 1) if s < duration_s]


@dataclass
class Fixture:
    path: str
    name: str
    start_offset_s: float = 0.0
    vfr: bool = False
    notes: dict = field(default_factory=dict)


ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 240

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Fixture Sans,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,First line
Dialogue: 0,0:00:03.50,0:00:05.00,Default,,0,0,0,,Crosses the first black cut
Dialogue: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,Middle line
Dialogue: 0,0:00:12.00,0:00:13.50,Default,,0,0,0,,Crosses the fade
"""


def make(path: str, codec="h264", gop=25, bframes=2, open_gop=False, vfr=False, start_offset_s=0.0,
         audio=("ac3",), subs=False, attachment=False, chapters=False, letterbox=0, container="matroska",
         ten_bit=False, frames=N, interlaced=None, sar=None, tag=None):
    """Encode a fixture. Returns a Fixture."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    out = av.open(path, "w", format=container)
    if codec == "h264":
        vs = out.add_stream("libx264", rate=FPS)
        params = f"keyint={gop}:min-keyint={gop}:scenecut=0:bframes={bframes}:open-gop={1 if open_gop else 0}"
        params += ":b-pyramid=normal" if interlaced else ":b-pyramid=0"
        if interlaced:
            params += ":interlaced=1:" + ("tff=1" if interlaced == "tt" else "bff=1")
        vs.options = {"x264-params": params, "preset": "veryfast", "crf": "20"}
        vs.pix_fmt = "yuv420p10le" if ten_bit else "yuv420p"
    elif codec == "mpeg2video":
        vs = out.add_stream("mpeg2video", rate=FPS)
        vs.codec_context.gop_size = gop
        vs.codec_context.max_b_frames = bframes
        opts = {"sc_threshold": "1000000000", "b": "2M"}
        if not open_gop:
            opts["flags"] = "+cgop"
        if interlaced:
            opts["flags"] = opts.get("flags", "") + "+ildct+ilme"
            opts["top"] = "1" if interlaced == "tt" else "0"
        vs.options = opts
        vs.pix_fmt = "yuv420p"
    else:
        encoder = {"hevc": "libx265", "vp8": "libvpx", "vp9": "libvpx-vp9", "av1": "libsvtav1",
                   "prores": "prores_ks", "mjpeg": "mjpeg", "mpeg4": "mpeg4", "wmv2": "wmv2", "ffv1": "ffv1"}[codec]
        vs = out.add_stream(encoder, rate=FPS)
        vs.codec_context.gop_size = gop
        if codec == "mpeg4": vs.codec_context.max_b_frames = bframes
        vs.pix_fmt = "yuv420p10le" if ten_bit else "yuv420p"
        if codec == "hevc":
            vs.options = {"preset": "ultrafast", "x265-params": f"keyint={gop}:min-keyint={gop}:scenecut=0:open-gop=0:bframes={bframes}:log-level=error:pools=1", "crf": "18"}
        elif codec in ("vp8", "vp9"):
            vs.options = {"deadline": "realtime", "cpu-used": "6", "crf": "12", "b": "1M"}
        elif codec == "av1":
            vs.options = {"preset": "12", "crf": "18", "svtav1-params": "lp=2"}
        elif codec == "prores":
            vs.pix_fmt = "yuv422p10le"; vs.options = {"profile": "2"}
        elif codec == "mjpeg":
            vs.pix_fmt = "yuvj420p"; vs.codec_context.color_range = 2
        else:
            vs.options = {"b": "2M"}
    vs.width, vs.height = W, H
    if tag:
        vs.codec_context.codec_tag = tag
    if codec != "mjpeg":
        vs.codec_context.color_range = 1
    if sar:
        vs.codec_context.sample_aspect_ratio = sar
    if vfr:
        vs.codec_context.time_base = Fraction(1, 1000)
    vs.metadata["title"] = "Fixture video"
    vs.metadata["language"] = "und"
    astreams = []
    for k, ac in enumerate(audio):
        a = out.add_stream(ac, rate=SR)
        a.layout = "stereo"
        if ac == "wmav2":
            a.bit_rate = 192000
        a.metadata["language"] = ["eng", "jpn", "fra"][k % 3]
        a.metadata["title"] = f"Track {k+1} ({ac})"
        astreams.append(a)
    if attachment:
        out.add_attachment(name="FixtureSans.ttf", mimetype="application/x-truetype-font",
                           data=b"\x00\x01\x00\x00" + bytes(range(256)) * 8)
    if chapters:
        out.set_chapters([{"id": 1, "start": 0, "end": 6000, "time_base": Fraction(1, 1000), "metadata": {"title": "Intro"}},
                          {"id": 2, "start": 6000, "end": 14000, "time_base": Fraction(1, 1000), "metadata": {"title": "Middle"}},
                          {"id": 3, "start": 14000, "end": 20000, "time_base": Fraction(1, 1000), "metadata": {"title": "End"}}])
    sub_in = None
    if subs:
        ass_path = os.path.splitext(path)[0] + ".src.ass"
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ASS)
        sub_in = av.open(ass_path)
        ss = out.add_stream_from_template(sub_in.streams[0])
        ss.metadata["language"] = "eng"
        ss.metadata["title"] = "Fixture subs"
    # VFR: frames 0-199 at 40 ms, 200-299 alternate 20/60 ms, rest 40 ms (same total)
    times_ms = []
    t = 0
    for n in range(frames):
        times_ms.append(t)
        if vfr and 200 <= n < 300:
            t += 20 if n % 2 == 0 else 60
        else:
            t += 40
    off_ms = int(round(start_offset_s * 1000))
    for n in range(frames):
        y, u, v = frame_planes(n, letterbox=letterbox)
        if ten_bit:
            fr = av.VideoFrame(W, H, "yuv420p10le")
            for p, arr in zip(fr.planes, (y, u, v)):
                a16 = (arr.astype(np.uint16) << 2)
                buf = np.frombuffer(p, np.uint16).reshape(p.height, p.line_size // 2)
                buf[:, : arr.shape[1]] = a16
        else:
            fr = av.VideoFrame.from_ndarray(np.concatenate([y, u.reshape(-1, W), v.reshape(-1, W)]), format="yuv420p")
        fr.color_range = 1
        if vfr:
            fr.pts = times_ms[n] + off_ms
            fr.time_base = Fraction(1, 1000)
        else:
            fr.pts = n + int(round(start_offset_s * FPS))
            fr.time_base = Fraction(1, FPS)
        for pkt in vs.encode(fr):
            out.mux(pkt)
    for pkt in vs.encode(None):
        out.mux(pkt)
    dur = frames / FPS
    total = int(dur * SR)
    for k, a in enumerate(astreams):
        sig = audio_signal(total, freq_scale=1 + 0.25 * k)
        fs = a.codec_context.frame_size or 1024
        pos = 0
        a_off = int(round(start_offset_s * SR))
        while pos < total:
            chunk = sig[pos:pos + fs]
            if len(chunk) < fs:
                chunk = np.pad(chunk, (0, fs - len(chunk)))
            fr = av.AudioFrame.from_ndarray(np.stack([chunk, chunk]).astype(np.float32), format="fltp", layout="stereo")
            fr.sample_rate = SR
            fr.pts = pos + a_off
            fr.time_base = Fraction(1, SR)
            for pkt in a.encode(fr):
                out.mux(pkt)
            pos += fs
        for pkt in a.encode(None):
            out.mux(pkt)
    if sub_in is not None:
        st = sub_in.streams[0]
        for pkt in sub_in.demux(st):
            if pkt.size == 0:
                continue
            pkt.pts += int(round(start_offset_s / st.time_base))
            pkt.dts = pkt.pts
            pkt.stream = out.streams[len(out.streams) - 1] if False else [s for s in out.streams if s.type == "subtitle"][0]
            out.mux(pkt)
        sub_in.close()
    out.close()
    return Fixture(path=path, name=os.path.basename(path), start_offset_s=start_offset_s, vfr=vfr)


FIXTURES = {
    # name: kwargs
    "h264_closed": dict(codec="h264", gop=25, bframes=2),
    "h264_open": dict(codec="h264", gop=25, bframes=2, open_gop=True),
    "h264_longgop": dict(codec="h264", gop=250, bframes=3),
    "mpeg2_open": dict(codec="mpeg2video", gop=12, bframes=2, open_gop=True),
    "mpeg2_closed": dict(codec="mpeg2video", gop=12, bframes=2, open_gop=False),
    "h264_vfr_offset": dict(codec="h264", gop=25, bframes=2, vfr=True, start_offset_s=10.0),
    "h264_full": dict(codec="h264", gop=25, bframes=2, audio=("ac3", "aac"), subs=True, attachment=True, chapters=True),
    "h264_letterbox": dict(codec="h264", gop=25, bframes=2, letterbox=36),
    "h264_10bit": dict(codec="h264", gop=25, bframes=2, ten_bit=True),
    "h264_shortgop": dict(codec="h264", gop=4, bframes=2),
    # like the real 1x02 episode: MBAFF interlaced, bottom field first, B-pyramid, 12:11 SAR, long GOP
    "h264_interlaced_longgop": dict(codec="h264", gop=250, bframes=3, interlaced="bb", sar=Fraction(12, 11),
                                    audio=("ac3", "ac3")),
    "h264_mp4": dict(codec="h264", gop=25, bframes=2, audio=("aac",), container="mp4"),
    "h264_mp4_longgop": dict(codec="h264", gop=250, bframes=3, audio=("aac",), container="mp4", sar=Fraction(1, 1)),
}


def build_all(out_dir: str, names=None, force=False) -> dict:
    res = {}
    for name, kw in FIXTURES.items():
        if names and name not in names:
            continue
        ext = {"mp4": ".mp4", "mov": ".mov", "avi": ".avi", "mpegts": ".ts", "mpeg": ".mpg", "webm": ".webm", "asf": ".wmv"}.get(kw.get("container"), ".mkv")
        p = os.path.join(out_dir, name + ext)
        if force or not os.path.exists(p):
            make(p, **kw)
        res[name] = p
    return res


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures_out"
    for k, v in build_all(d, force=True).items():
        print(k, v, os.path.getsize(v))
