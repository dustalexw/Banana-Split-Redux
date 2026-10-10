"""Preview-only support for transport seeking and software AV1 display.

These paths never participate in cut planning or exporting.
"""
from __future__ import annotations
import os
import queue
import threading
import tempfile
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QVideoFrame, QVideoFrameFormat
from bsr import cache


def mapped_image(frame):
    """Qt's CPU toImage omits some 10-bit formats; convert their mapped planes."""
    formats = {QVideoFrameFormat.Format_YUV420P10: 'yuv420p10le',
               QVideoFrameFormat.Format_P010: 'p010le', QVideoFrameFormat.Format_P016: 'p016le'}
    pixel = formats.get(frame.pixelFormat())
    if not pixel or not frame.map(QVideoFrame.ReadOnly): return QImage()
    try:
        converted = av.VideoFrame(frame.width(), frame.height(), pixel)
        format_info = frame.surfaceFormat()
        color_spaces = {QVideoFrameFormat.ColorSpace_BT601: 5,
                        QVideoFrameFormat.ColorSpace_BT709: 1,
                        QVideoFrameFormat.ColorSpace_BT2020: 9}
        converted.colorspace = color_spaces.get(format_info.colorSpace(), 2)
        converted.color_range = 2 if format_info.colorRange() == QVideoFrameFormat.ColorRange_Full else 1
        for i, plane in enumerate(converted.planes):
            source_stride = frame.bytesPerLine(i)
            source = np.frombuffer(frame.bits(i), dtype=np.uint8, count=frame.mappedBytes(i))
            source = source.reshape(-1, source_stride)
            dest = np.zeros((plane.height, plane.line_size), dtype=np.uint8)
            rows = min(plane.height, source.shape[0]); cols = min(plane.line_size, source_stride)
            dest[:rows, :cols] = source[:rows, :cols]
            plane.update(dest.tobytes())
        rgb = converted.to_ndarray(format='rgb24')
        return QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()
    finally: frame.unmap()


class PreparationSignals(QObject):
    ready = Signal(int, str, str)


def prepare_seekable(info, origin_s, cancelled):
    """Remux a read-only MPEG/TS file into a separately cached, indexed MKV."""
    key = cache.key('playback-remux-v1', info.fingerprint, origin_s)
    dest = Path(cache.cache_dir())/'playback'/f'{key}.mkv'
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists(): return str(dest)
    fd, temp = tempfile.mkstemp(dir=dest.parent, suffix='.mkv'); os.close(fd)
    try:
        with av.open(info.path) as inp, av.open(temp, 'w', format='matroska', options={'avoid_negative_ts':'disabled'}) as out:
            mapping = {}
            for stream in inp.streams:
                if stream.type in ('audio','video') or (stream.type=='subtitle' and stream.codec_context.name in ('subrip','ass','ssa','webvtt')):
                    mapping[stream.index] = out.add_stream_from_template(stream)
            for packet in inp.demux([inp.streams[i] for i in mapping]):
                if cancelled.is_set(): return ''
                if packet.dts is None: continue
                offset = round(Fraction(str(origin_s))/packet.time_base)
                if packet.pts is not None: packet.pts -= offset
                packet.dts -= offset
                packet.stream = mapping[packet.stream.index]
                out.mux(packet)
        if not cancelled.is_set(): os.replace(temp,dest)
        return str(dest) if dest.exists() else ''
    finally:
        if os.path.exists(temp): os.unlink(temp)


class SoftwareVideo:
    """Bounded PyAV decoding for AV1; Qt continues to own audio and the playback clock."""
    def __init__(self, info, origin_s):
        self.info = info
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.seek_to = origin_s
        self.generation = 0
        self.frames = queue.Queue(maxsize=16)
        self.next_frame = None
        self.error = ''
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def seek(self, seconds):
        with self.lock:
            self.seek_to = seconds; self.generation += 1
        self.next_frame = None
        while True:
            try: self.frames.get_nowait()
            except queue.Empty: break

    def take(self, seconds):
        latest = None
        while True:
            if self.next_frame is None:
                try: self.next_frame = self.frames.get_nowait()
                except queue.Empty: break
            generation, t, image = self.next_frame
            if generation != self.generation:
                self.next_frame = None; continue
            if t > seconds+.015: break
            latest = (t, image); self.next_frame = None
        return latest

    def _run(self):
        try:
            with av.open(self.info.path) as container:
                stream = container.streams[self.info.video.index]; stream.thread_type='AUTO'
                decoder = iter(())
                seek_floor = float('-inf')
                while not self.stop.is_set():
                    with self.lock:
                        target = self.seek_to; self.seek_to = None; generation = self.generation
                    if target is not None:
                        seek_floor = target-.1
                        container.seek(max(0, round(target/float(stream.time_base))), stream=stream,
                                       any_frame=False, backward=True)
                        decoder = container.decode(stream)
                    try: frame = next(decoder)
                    except StopIteration:
                        self.stop.wait(.02); continue
                    if frame.pts is None: continue
                    seconds = float(frame.pts * frame.time_base)
                    if seconds < seek_floor: continue
                    if frame.width>960: frame = frame.reformat(width=960,height=max(2,round(frame.height*960/frame.width)))
                    rgb = frame.to_ndarray(format='rgb24')
                    image = QImage(rgb.data,rgb.shape[1],rgb.shape[0],rgb.strides[0],QImage.Format_RGB888).copy()
                    while not self.stop.is_set():
                        if generation != self.generation: break
                        try: self.frames.put((generation,seconds,image),timeout=.02); break
                        except queue.Full: continue
        except Exception as exc: self.error = str(exc)

    def close(self):
        self.stop.set(); self.thread.join(timeout=2)
