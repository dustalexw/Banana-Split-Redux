"""Local video playback with audio; exact cut frames still come from the media engine."""
from __future__ import annotations
import os
import threading

# Use the bundled FFmpeg backend consistently, including AVI/MKV and legacy codecs.
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from PySide6.QtCore import Qt, QUrl, Signal, QTimer
from PySide6.QtGui import QPixmap, QShortcut, QKeySequence, QTextDocument
from PySide6.QtWidgets import (QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
                              QSlider, QComboBox, QCheckBox)
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink, QAudioOutput, QMediaMetaData
from bsr.media import fmt_time
from .playback_support import mapped_image, prepare_seekable, PreparationSignals, SoftwareVideo


class VideoCanvas(QLabel):
    def __init__(self):
        super().__init__("Add a video to watch and find your splits")
        self.setObjectName("preview")
        self.setAlignment(Qt.AlignCenter)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumSize(320, 180)
        self.setMaximumHeight(250)
        self.image = None

    def show_image(self, image):
        self.image = image
        self._draw()

    def _draw(self):
        if self.image is not None and not self.image.isNull():
            self.setPixmap(QPixmap.fromImage(self.image).scaled(
                self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._draw()

    def reset(self):
        self.image = None
        self.clear()
        self.setText("Add a video to watch and find your splits")


class VideoPlayer(QWidget):
    positionMoved = Signal(float)
    seekRequested = Signal(float)
    framePresented = Signal(float)

    def __init__(self, parent=None, *, expandable=True):
        super().__init__(parent)
        self.path = ""
        self.info = None
        self.origin_s = 0.0
        self.sar = 1.0
        self.range_ms = None
        self._precise_still = False
        self.software_video = None
        self._preparation_cancel = threading.Event()
        self._generation = 0
        self._loading = False
        self._play_when_ready = False
        self.preparation = PreparationSignals(self)
        self.preparation.ready.connect(self._prepared)
        self.video_timer = QTimer(self); self.video_timer.setInterval(16)
        self.video_timer.timeout.connect(self._software_frame)
        self.media = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.audio.setVolume(.75)
        self.media.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.media.setVideoSink(self.sink)
        self.setFocusPolicy(Qt.StrongFocus)
        lay = QVBoxLayout(self); lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(6)
        self.video = VideoCanvas(); lay.addWidget(self.video, 1)
        self.subtitle = QLabel(); self.subtitle.setAlignment(Qt.AlignCenter)
        self.subtitle.setTextFormat(Qt.PlainText); self.subtitle.setWordWrap(True)
        self.subtitle.hide(); lay.addWidget(self.subtitle)
        self.seek = QSlider(Qt.Horizontal); self.seek.setRange(0, 0)
        self.seek.sliderReleased.connect(self._scrub)
        self.seek.sliderMoved.connect(lambda ms: self.clock.setText(self._clock(ms)))
        # A click or keyboard move is a seek too; playback updates block these signals.
        self.seek.valueChanged.connect(lambda _: self._scrub() if not self.seek.isSliderDown() else None)
        lay.addWidget(self.seek)
        row = QHBoxLayout()
        self.play = QPushButton("▶ Play"); self.play.setObjectName("primary")
        self.play.clicked.connect(self.toggle); row.addWidget(self.play)
        for text, delta in (("−5 s", -5000), ("+5 s", 5000)):
            button = QPushButton(text); button.clicked.connect(lambda _, d=delta: self.jump(d)); row.addWidget(button)
        self.clock = QLabel("00:00 / 00:00"); self.clock.setObjectName("muted"); row.addWidget(self.clock, 1)
        self.speed = QComboBox()
        for rate in (.5, 1, 1.5, 2): self.speed.addItem(f"{rate:g}×", rate)
        self.speed.setCurrentIndex(1)
        self.speed.currentIndexChanged.connect(lambda _: self.media.setPlaybackRate(self.speed.currentData()))
        row.addWidget(self.speed)
        if expandable:
            self.expand = QPushButton("Expand")
            self.expand.clicked.connect(self.expand_player); row.addWidget(self.expand)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.mute = QPushButton("Sound"); self.mute.setCheckable(True)
        self.mute.setToolTip("Click to mute playback")
        self.mute.toggled.connect(self.audio.setMuted); row.addWidget(self.mute)
        self.mute.toggled.connect(lambda on: self.mute.setText("Muted" if on else "Sound"))
        self.volume = QSlider(Qt.Horizontal); self.volume.setRange(0, 100); self.volume.setValue(75)
        self.volume.setMaximumWidth(80); self.volume.setToolTip("Playback volume")
        self.volume.valueChanged.connect(lambda n: self.audio.setVolume(n/100)); row.addWidget(self.volume)
        self.audio_tracks = QComboBox(); self.audio_tracks.setToolTip("Audio to listen to while reviewing")
        self.audio_tracks.setMaximumWidth(150)
        self.audio_tracks.currentIndexChanged.connect(lambda _: self.media.setActiveAudioTrack(self.audio_tracks.currentData()) if self.audio_tracks.currentData() is not None else None)
        row.addWidget(self.audio_tracks)
        self.subtitle_tracks = QComboBox(); self.subtitle_tracks.setToolTip("Preview subtitles")
        self.subtitle_tracks.setMaximumWidth(130)
        self.subtitle_tracks.currentIndexChanged.connect(lambda _: self.media.setActiveSubtitleTrack(self.subtitle_tracks.currentData()) if self.subtitle_tracks.currentData() is not None else None)
        row.addWidget(self.subtitle_tracks)
        self.loop = QCheckBox("Loop preview"); self.loop.setChecked(True); row.addWidget(self.loop)
        lay.addLayout(row)
        self.error = QLabel(); self.error.setObjectName("muted"); self.error.setWordWrap(True)
        self.error.hide(); lay.addWidget(self.error)
        self.sink.videoFrameChanged.connect(self._frame)
        self.sink.subtitleTextChanged.connect(self._subtitle)
        self.media.positionChanged.connect(self._position)
        self.media.durationChanged.connect(self._duration)
        self.media.playbackStateChanged.connect(self._state)
        self.media.tracksChanged.connect(self._tracks)
        self.media.errorOccurred.connect(self._error)
        self.media.mediaStatusChanged.connect(self._status)
        shortcut = QShortcut(QKeySequence("Space"), self)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut); shortcut.activated.connect(self.toggle)
        self._enable(False)

    def _enable(self, on):
        for widget in (self.play, self.seek, self.speed, self.audio_tracks, self.subtitle_tracks): widget.setEnabled(on)

    def set_source(self, info=None):
        path = info.path if info else ""
        if path == self.path: return
        self._stop_helpers()
        self._generation += 1
        self._loading = False; self._play_when_ready = False
        self.media.stop()
        self.range_ms = None
        self._precise_still = False
        self.path = path
        self.info = info
        self.origin_s = min((float(s.start_pts * s.time_base) for s in info.streams
                             if s.type in ("video", "audio") and s.start_pts is not None), default=0) if info else 0
        self.sar = 1.0
        if info and info.video.sar:
            try:
                n, d = map(int, info.video.sar.replace(":", "/").split("/"))
                if n > 0 and d > 0: self.sar = n/d
            except (ValueError, ZeroDivisionError): pass
        self.video.reset(); self.subtitle.hide(); self.error.hide()
        self.audio_tracks.clear(); self.subtitle_tracks.clear()
        if info and info.video.codec == 'av1':
            self.software_video = SoftwareVideo(info, self.origin_s)
            self.video_timer.start()
        if info and info.format_name in ('mpeg', 'mpegts'):
            self.media.setSource(QUrl())
            self._loading = True
            self._preparation_cancel = threading.Event()
            generation, cancel = self._generation, self._preparation_cancel
            self.error.setText("Preparing a seekable playback preview… Original file stays unchanged.")
            self.error.show(); self._enable(False)
            def prepare():
                try: result, error = prepare_seekable(info, self.origin_s, cancel), ''
                except Exception as exc: result, error = '', str(exc)
                if not cancel.is_set(): self.preparation.ready.emit(generation, result, error)
            threading.Thread(target=prepare, daemon=True).start()
            return
        self.media.setSource(QUrl.fromLocalFile(path) if path else QUrl())
        self._enable(bool(path))

    def _prepared(self, generation, path, error):
        if generation != self._generation: return
        self._loading = False
        if error or not path:
            self.error.setText("Playback preview could not be prepared: " + error)
            return
        self.error.hide(); self._enable(True)
        self.media.setSource(QUrl.fromLocalFile(path))
        if self._play_when_ready: self._play_when_ready = False; self.media.play()

    def toggle(self):
        if not self.path: return
        if self._loading:
            self._play_when_ready = not self._play_when_ready
            return
        if self.is_playing(): self.media.pause()
        else:
            self._precise_still = False
            if self.media.mediaStatus() == QMediaPlayer.EndOfMedia: self._seek_backend(0)
            self.media.play()

    def is_playing(self):
        return self.media.playbackState() == QMediaPlayer.PlayingState

    def expand_player(self):
        if not self.path: return
        from .dialogs import PlaybackDialog
        self.pause()
        dialog = PlaybackDialog(self, self.path, at_s=self.media.position()/1000+self.origin_s)
        dialog.player.mute.setChecked(self.audio.isMuted())
        dialog.player.volume.setValue(round(self.audio.volume()*100))
        dialog.exec()

    def pause(self):
        self.media.pause()

    def seek_source(self, seconds, *, precise=False):
        self.pause(); self.range_ms = None; self._precise_still = precise
        ms = max(0, round((seconds-self.origin_s)*1000))
        self._seek_backend(min(ms, self.media.duration()) if self.media.duration() else ms)

    def _seek_backend(self, ms):
        if self.software_video: self.software_video.seek(ms/1000+self.origin_s)
        self.media.setPosition(ms)

    def play_range(self, start_s, end_s):
        self.range_ms = (max(0, round((start_s-self.origin_s)*1000)),
                         max(0, round((end_s-self.origin_s)*1000)))
        self._precise_still = False
        self._seek_backend(self.range_ms[0]); self.media.play()

    def show_still(self, image):
        self._precise_still = True
        self.video.show_image(image)

    def jump(self, delta):
        self.range_ms = None
        target = max(0, min(self.media.position()+delta, self.media.duration()))
        self.seekRequested.emit(target/1000+self.origin_s)

    def _scrub(self):
        if self.path:
            self.seekRequested.emit(self.seek.value()/1000+self.origin_s)

    def _clock(self, position):
        return f"{fmt_time(position/1000)[:-4]} / {fmt_time(self.media.duration()/1000)[:-4]}"

    def _position(self, ms):
        if not self.seek.isSliderDown():
            self.seek.blockSignals(True); self.seek.setValue(ms); self.seek.blockSignals(False)
            self.clock.setText(self._clock(ms))
        self.positionMoved.emit(ms/1000+self.origin_s)
        if self.range_ms and self.is_playing() and ms >= self.range_ms[1]:
            if self.loop.isChecked(): self._seek_backend(self.range_ms[0])
            else: self.pause(); self.range_ms = None

    def _duration(self, ms):
        self.seek.blockSignals(True); self.seek.setRange(0, max(0, ms)); self.seek.blockSignals(False)
        self.clock.setText(self._clock(self.media.position()))

    def _state(self, state):
        self.play.setText("Ⅱ Pause" if state == QMediaPlayer.PlayingState else "▶ Play")

    def _frame(self, frame):
        if not frame.isValid() or (self._precise_still and not self.is_playing()): return
        image = frame.toImage()
        if image.isNull(): image = mapped_image(frame)
        if image.isNull(): return
        self._show_image(image)
        self.framePresented.emit(frame.startTime()/1_000_000)

    def _show_image(self, image):
        if self.sar != 1:
            image = image.scaled(round(image.width()*self.sar), image.height(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.video.show_image(image)

    def _software_frame(self):
        if not self.software_video or (self._precise_still and not self.is_playing()): return
        result = self.software_video.take(self.media.position()/1000+self.origin_s)
        if result:
            seconds, image = result
            self._show_image(image); self.framePresented.emit(seconds-self.origin_s)
        if self.software_video.error:
            self.error.setText("Video playback unavailable: " + self.software_video.error); self.error.show()

    def _tracks(self):
        if self.software_video: self.media.setActiveVideoTrack(-1)
        for combo, tracks, active, label in (
            (self.audio_tracks, self.media.audioTracks(), self.media.activeAudioTrack(), "Audio"),
            (self.subtitle_tracks, self.media.subtitleTracks(), self.media.activeSubtitleTrack(), "Subtitles")):
            combo.blockSignals(True); combo.clear()
            if combo is self.subtitle_tracks: combo.addItem("Subtitles off", -1)
            for i, meta in enumerate(tracks):
                title = meta.stringValue(QMediaMetaData.Title)
                language = meta.stringValue(QMediaMetaData.Language)
                combo.addItem(title or f"{label} {i+1}" + (f" · {language}" if language else ""), i)
            combo.setCurrentIndex(max(0, combo.findData(active))); combo.blockSignals(False)
            combo.setVisible(bool(tracks))

    def _subtitle(self, text):
        doc = QTextDocument(); doc.setHtml(text)
        self.subtitle.setText(doc.toPlainText()); self.subtitle.setVisible(bool(text))

    def _error(self, *_):
        self.error.setText("Playback unavailable: " + self.media.errorString() + ". Frame-by-frame cut review remains available.")
        self.error.show()

    def _status(self, status):
        if status == QMediaPlayer.EndOfMedia and self.range_ms and self.loop.isChecked():
            self._seek_backend(self.range_ms[0]); self.media.play()

    def _stop_helpers(self):
        self._preparation_cancel.set()
        self.video_timer.stop()
        if self.software_video:
            self.software_video.close(); self.software_video = None

    def shutdown(self):
        self._stop_helpers(); self._generation += 1
        self.range_ms = None
        self.media.stop(); self.media.setSource(QUrl())
        # Release the backend's shared output references before Qt destroys
        # the widget's children. Leaving them attached can outlive the widgets
        # when several playback windows are closed during application exit.
        self.media.setVideoSink(None)
        self.media.setAudioOutput(None)
        self.video.reset()
