"""Probing and packet indexing.

Nothing in this module decodes media. The packet index records every packet's
timestamps, flags, size and a payload hash, in file (decode) order, so later
stages can (a) plan cuts on exact integer timestamps and (b) prove that exported
packets are byte-identical copies of source packets.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, asdict
from fractions import Fraction
from typing import Callable, Optional

import av
import numpy as np

from . import cache

# Matroska statistics tags written by mkvmerge describe the *source* file and
# would be wrong on a segment, so they are never copied to outputs.
STALE_TAG_PREFIXES = ("BPS", "DURATION", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES",
                      "_STATISTICS_", "ENCODER")

TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
BITMAP_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


# Tags the muxer regenerates itself (MP4 handler names etc.).
MUXER_TAGS = {"HANDLER_NAME", "VENDOR_ID", "NAME", "COMPATIBLE_BRANDS", "MAJOR_BRAND", "MINOR_VERSION"}


def is_stale_tag(key: str) -> bool:
    k = key.upper()
    return k in MUXER_TAGS or any(k.startswith(p) for p in STALE_TAG_PREFIXES)


def disposition_names(flags) -> list[str]:
    try:
        return [f.name for f in type(flags) if f.value and (flags & f) == f]
    except Exception:  # pragma: no cover - very old PyAV
        return []


@dataclass
class StreamInfo:
    index: int
    type: str                    # video/audio/subtitle/attachment/data
    codec: str
    time_base: Fraction
    start_pts: Optional[int]
    duration_s: Optional[float]
    language: Optional[str]
    title: Optional[str]
    disposition: list[str]
    metadata: dict
    profile: Optional[str] = None
    # video
    width: int = 0
    height: int = 0
    pix_fmt: Optional[str] = None
    bit_depth: int = 8
    color_range: Optional[str] = None
    color_space: Optional[str] = None
    color_trc: Optional[str] = None
    color_primaries: Optional[str] = None
    field_order: Optional[str] = None
    frame_rate: Optional[Fraction] = None
    has_b_frames: int = 0
    sar: Optional[str] = None
    # audio
    sample_rate: int = 0
    channels: int = 0
    layout: Optional[str] = None
    frame_size: int = 0
    initial_padding: int = 0
    seek_preroll: int = 0
    # attachment
    filename: Optional[str] = None
    mimetype: Optional[str] = None

    @property
    def is_text_sub(self) -> bool:
        return self.type == "subtitle" and self.codec in TEXT_SUB_CODECS

    @property
    def is_hdr(self) -> bool:
        return (self.color_trc or "") in ("smpte2084", "arib-std-b67", "16", "18")

    def label(self) -> str:
        bits = [f"#{self.index}", self.type, self.codec]
        if self.language:
            bits.append(f"[{self.language}]")
        if self.title:
            bits.append(f"“{self.title}”")
        return " ".join(bits)

    def to_json(self):
        d = asdict(self)
        d["time_base"] = str(self.time_base)
        d["frame_rate"] = str(self.frame_rate) if self.frame_rate else None
        return d


@dataclass
class MediaInfo:
    path: str
    size: int
    format_name: str
    duration_s: float
    bit_rate: int
    metadata: dict
    streams: list[StreamInfo]
    chapters: list[dict] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def video(self) -> StreamInfo:
        vs = [s for s in self.streams if s.type == "video"
              and "attached_pic" not in s.disposition]
        if not vs:
            raise ValueError("No video stream")
        return vs[0]

    def of_type(self, t: str) -> list[StreamInfo]:
        return [s for s in self.streams if s.type == t]

    def to_json(self):
        return {"path": self.path, "size": self.size, "format": self.format_name,
                "duration_s": self.duration_s, "bit_rate": self.bit_rate,
                "metadata": self.metadata, "chapters": self.chapters,
                "fingerprint": self.fingerprint,
                "streams": [s.to_json() for s in self.streams]}


_RANGE = {0: None, 1: "tv", 2: "pc"}
_FIELD = {0: "unknown", 1: "progressive", 2: "tt", 3: "bb", 4: "tb", 5: "bt"}


def _enum_name(v, table=None):
    if v is None:
        return None
    if table and isinstance(v, int) and v in table:
        return table[v]
    n = getattr(v, "name", None)
    return n.lower() if n else str(v)


def fingerprint(path: str) -> str:
    """Cheap identity of a source file: size, mtime and head/tail bytes."""
    st = os.stat(path)
    h = hashlib.sha1()
    h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if st.st_size > (2 << 20):
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return h.hexdigest()[:20]


def open_input(path: str):
    try:
        c = av.open(path, metadata_errors="replace")
    except TypeError:          # PyAV versions without the keyword
        c = av.open(path)
    if c.format.name == "avi" and any(s.codec_context.has_b_frames for s in c.streams.video):
        try:
            return _AviPresentationInput(c, path)
        except Exception:
            c.close()
            raise
    return c


class _AviPresentationInput:
    """AVI has decode timestamps only. Recover B-frame PTS without changing payloads.

    COPY_OPAQUE links each displayed frame to its compressed packet. The cached
    map is also applied after seeks and during proof/preview/export, so all layers
    use the same presentation timeline. Packed multi-picture packets are rejected
    rather than assigning a misleading single timestamp.
    """
    def __init__(self, container, path):
        self._c = container
        key = cache.key("avi-presentation-v1", fingerprint(path))
        saved = cache.load_npz(key)
        if saved is None:
            positions, points, streams = [], [], []
            for video in container.streams.video:
                if not video.codec_context.has_b_frames: continue
                inp = av.open(path)
                st = inp.streams[video.index]
                st.codec_context.copy_opaque = True
                tb, rate = Fraction(st.time_base), Fraction(st.average_rate or st.guessed_rate)
                origin, count, seen = st.start_time or 0, 0, set()
                try:
                    for pkt in inp.demux(st):
                        if pkt.size: pkt.opaque = pkt.pos
                        for frame in pkt.decode():
                            if frame.opaque is None or frame.opaque in seen:
                                raise ValueError("AVI contains packed or unidentifiable B-frames; unpack the video losslessly before importing")
                            seen.add(frame.opaque)
                            positions.append(frame.opaque)
                            points.append(origin + round(Fraction(count)/rate/tb))
                            streams.append(st.index)
                            count += 1
                finally: inp.close()
            saved = {"pos": np.array(positions, np.int64), "pts": np.array(points, np.int64), "stream": np.array(streams, np.int64)}
            cache.save_npz(key, saved)
        self._pts = {(int(si), int(pos)): int(p) for si,pos,p in zip(saved["stream"], saved["pos"], saved["pts"])}

    def __getattr__(self, name):
        return getattr(self._c, name)

    def demux(self, *args, **kwargs):
        for pkt in self._c.demux(*args, **kwargs):
            p = self._pts.get((pkt.stream_index, pkt.pos))
            if p is not None: pkt.pts = p
            yield pkt

    def decode(self, *args, **kwargs):
        for pkt in self.demux(*args, **kwargs):
            yield from pkt.decode()


def probe(path: str) -> MediaInfo:
    c = open_input(path)
    try:
        streams = []
        for s in c.streams:
            md = dict(s.metadata)
            info = StreamInfo(
                index=s.index, type=s.type, codec="", time_base=Fraction(s.time_base or Fraction(1, 1000)),
                start_pts=s.start_time, duration_s=(float(s.duration * s.time_base) if s.duration and s.time_base else None),
                language=md.get("language"), title=md.get("title"),
                disposition=disposition_names(s.disposition), metadata=md)
            cc = None
            try:
                cc = s.codec_context
            except Exception:
                cc = None
            if cc is not None:
                info.codec = {"libdav1d": "av1", "libvpx-vp9": "vp9", "libvpx": "vp8",
                              "mp3float": "mp3", "mp2float": "mp2"}.get(cc.name, cc.name or "")
                info.profile = getattr(cc, "profile", None)
            if s.type == "video" and cc is not None:
                info.width, info.height = cc.width, cc.height
                info.pix_fmt = cc.format.name if cc.format else None
                if cc.format is not None:
                    try:
                        info.bit_depth = max(c_.bits for c_ in cc.format.components)
                    except Exception:
                        info.bit_depth = 8
                info.color_range = _enum_name(cc.color_range, _RANGE)
                info.color_space = _enum_name(cc.colorspace)
                info.color_trc = _enum_name(cc.color_trc)
                info.color_primaries = _enum_name(cc.color_primaries)
                info.field_order = _enum_name(getattr(cc, "field_order", None), _FIELD)
                info.frame_rate = Fraction(s.average_rate) if s.average_rate else (
                    Fraction(s.guessed_rate) if s.guessed_rate else None)
                # frame-reordering depth (PyAV's has_b_frames is only a boolean)
                rd = getattr(cc, "reorder_depth", None)
                info.has_b_frames = int(rd) if rd is not None else int(bool(cc.has_b_frames))
                sar = cc.sample_aspect_ratio
                info.sar = str(sar) if sar else None
            elif s.type == "audio" and cc is not None:
                info.sample_rate = cc.sample_rate
                info.channels = cc.channels
                info.layout = cc.layout.name if cc.layout else None
                info.frame_size = cc.frame_size or 0
                info.initial_padding = getattr(cc, "initial_padding", 0) or 0
                info.seek_preroll = getattr(cc, "seek_preroll", 0) or 0
            elif s.type == "subtitle" and cc is not None:
                pass
            elif s.type == "attachment":
                info.codec = info.codec or "attachment"
                info.filename = getattr(s, "name", None) or md.get("filename")
                info.mimetype = getattr(s, "mimetype", None) or md.get("mimetype")
            streams.append(info)
        # MPEG transport demuxers use the generic MP3 decoder for Layer II too.
        mpeg_audio = {s.index for s in streams if s.codec in ("mp3", "mp2")}
        if mpeg_audio:
            for pkt in c.demux():
                if pkt.stream_index in mpeg_audio and pkt.size >= 4:
                    data = bytes(pkt)
                    if data[0] == 255 and data[1] & 224 == 224:
                        streams[pkt.stream_index].codec = {1: "mp3", 2: "mp2", 3: "mp1"}.get((data[1] >> 1) & 3, streams[pkt.stream_index].codec)
                    mpeg_audio.remove(pkt.stream_index)
                if not mpeg_audio: break
        chapters = []
        try:
            for ch in c.chapters():
                tb = Fraction(ch["time_base"])
                chapters.append({"start_s": float(ch["start"] * tb), "end_s": float(ch["end"] * tb),
                                 "start": ch["start"], "end": ch["end"], "time_base": str(tb),
                                 "metadata": dict(ch.get("metadata", {}))})
        except Exception:
            pass
        return MediaInfo(path=os.path.abspath(path), size=os.path.getsize(path),
                         format_name=c.format.name, duration_s=(c.duration or 0) / 1e6,
                         bit_rate=c.bit_rate or 0, metadata=dict(c.metadata), streams=streams,
                         chapters=chapters, fingerprint=fingerprint(path))
    finally:
        c.close()


# ---------------------------------------------------------------- packet index

@dataclass
class StreamPackets:
    """All packets of one stream, in file/decode order."""
    stream_index: int
    time_base: Fraction
    pts: np.ndarray        # int64, NOPTS -> INT64_MIN
    dts: np.ndarray        # int64, NOPTS -> INT64_MIN
    duration: np.ndarray   # int64
    key: np.ndarray        # bool
    size: np.ndarray       # int64
    pos: np.ndarray        # int64 (-1 if unknown)
    hash: np.ndarray       # uint64 payload hash (blake2b-8)

    def __len__(self):
        return len(self.pts)

    def end_pts(self) -> int:
        valid = self.pts != NOPTS
        if not valid.any():
            return 0
        return int((self.pts[valid] + self.duration[valid]).max())


NOPTS = np.iinfo(np.int64).min


def payload_hash(data: bytes) -> int:
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "little")


def compressed_hash(codec, data, extradata=b""):
    """Hash compressed payload independently of lossless container framing."""
    if codec in ("h264", "hevc"):
        from .boundaries import _iter_nals, _nal_length_size, _hevc_nal_length_size
        ls = _nal_length_size(extradata) if codec == "h264" else _hevc_nal_length_size(extradata)
        nals = list(_iter_nals(data, ls))
        if nals:
            data = b''.join(len(n).to_bytes(4, 'big') + n for n in nals)
    elif (codec == "aac" and len(data) > 7 and data[0] == 255 and data[1] & 246 == 240
          and (((data[3] & 3) << 11) | (data[4] << 3) | (data[5] >> 5)) == len(data)):
        # ADTS headers carry framing/configuration, not encoded audio samples.
        data = data[7 if data[1] & 1 else 9:]
    elif codec in ("av1", "libdav1d"):
        while data.startswith(b'\x12\x00'):
            data = data[2:]  # temporal delimiter, removed by MP4/Matroska muxers
    return payload_hash(data)


def build_packet_index(path: str, progress: Optional[Callable[[float], None]] = None,
                       cancel=None) -> dict[int, StreamPackets]:
    fp = fingerprint(path)
    key = cache.key("packets-v3", fp)
    hit = cache.load_npz(key)
    if hit is not None:
        return _index_from_npz(hit)
    c = open_input(path)
    try:
        size = os.path.getsize(path) or 1
        cols: dict[int, dict[str, list]] = {}
        tbs = {}
        n = 0
        for s in c.streams:
            if s.type == "attachment":
                continue
            cols[s.index] = {k: [] for k in ("pts", "dts", "duration", "key", "size", "pos", "hash")}
            tbs[s.index] = Fraction(s.time_base)
        for pkt in c.demux():
            if pkt.size == 0 and pkt.pts is None and pkt.dts is None:
                continue  # flush packet
            col = cols.get(pkt.stream_index)
            if col is None:
                continue
            col["pts"].append(NOPTS if pkt.pts is None else pkt.pts)
            col["dts"].append(NOPTS if pkt.dts is None else pkt.dts)
            col["duration"].append(pkt.duration or 0)
            col["key"].append(bool(pkt.is_keyframe))
            col["size"].append(pkt.size)
            col["pos"].append(pkt.pos if pkt.pos is not None else -1)
            col["hash"].append(compressed_hash(pkt.stream.codec_context.name, bytes(pkt), pkt.stream.codec_context.extradata or b""))
            n += 1
            if n % 2000 == 0:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if progress and pkt.pos and pkt.pos > 0:
                    progress(min(1.0, pkt.pos / size))
        out = {}
        for si, col in cols.items():
            out[si] = StreamPackets(
                stream_index=si, time_base=tbs[si],
                pts=np.array(col["pts"], dtype=np.int64), dts=np.array(col["dts"], dtype=np.int64),
                duration=np.array(col["duration"], dtype=np.int64), key=np.array(col["key"], dtype=bool),
                size=np.array(col["size"], dtype=np.int64), pos=np.array(col["pos"], dtype=np.int64),
                hash=np.array(col["hash"], dtype=np.uint64))
        _fill_missing_durations(out)
        cache.save_npz(key, _index_to_npz(out))
        return out
    finally:
        c.close()


def _fill_missing_durations(idx: dict[int, StreamPackets]):
    for sp in idx.values():
        if len(sp) == 0:
            continue
        missing = sp.duration <= 0
        if not missing.any():
            continue
        valid = sp.pts != NOPTS
        order = np.sort(sp.pts[valid])
        if len(order) > 1:
            diffs = np.diff(order)
            med = int(np.median(diffs)) if len(diffs) else 0
            nxt = {int(a): int(b - a) for a, b in zip(order[:-1], order[1:])}
            for i in np.nonzero(missing & valid)[0]:
                sp.duration[i] = nxt.get(int(sp.pts[i]), med)


def _index_to_npz(idx):
    d = {}
    for si, sp in idx.items():
        for f in ("pts", "dts", "duration", "key", "size", "pos", "hash"):
            d[f"{si}_{f}"] = getattr(sp, f)
        d[f"{si}_tb"] = np.array([sp.time_base.numerator, sp.time_base.denominator], dtype=np.int64)
    return d


def _index_from_npz(d):
    out = {}
    sids = sorted({int(k.split("_")[0]) for k in d.files})
    for si in sids:
        tb = d[f"{si}_tb"]
        out[si] = StreamPackets(si, Fraction(int(tb[0]), int(tb[1])),
                                *(d[f"{si}_{f}"] for f in ("pts", "dts", "duration", "key", "size", "pos", "hash")))
    return out


class Cancelled(Exception):
    pass


def to_tb(value: int, src: Fraction, dst: Fraction, rounding: str = "floor") -> int:
    """Exact rational rescale of an integer timestamp."""
    q = Fraction(value) * src / dst
    if rounding == "floor":
        return q.numerator // q.denominator
    if rounding == "ceil":
        return -((-q.numerator) // q.denominator)
    return round(q)


def fmt_time(seconds: float) -> str:
    if seconds is None:
        return "—"
    sign = "-" if seconds < 0 else ""
    s = abs(seconds)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s - h * 3600 - m * 60
    return f"{sign}{h:d}:{m:02d}:{sec:06.3f}"
