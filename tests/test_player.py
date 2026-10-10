"""Real Qt FFmpeg playback: video frames, decoded audio, seeking and player controls."""
import os
import time
from fractions import Fraction
import pytest
from fixturegen import make
from test_formats import CASES
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QAudioBufferOutput, QMediaPlayer
from bsr.media import probe
from bsr_ui.player import VideoPlayer

_APP=None

def wait(app, predicate, timeout=10):
    start=time.monotonic()
    while not predicate():
        app.processEvents();time.sleep(.005)
        assert time.monotonic()-start<timeout,'Timed out waiting for Qt playback'
    app.processEvents()


@pytest.mark.parametrize('name,codec,container,ext,audio,ten_bit',
                         CASES+[('h264_mp4','h264','mp4','mp4',('aac','aac'),False)],ids=[x[0] for x in CASES]+['h264_mp4'])
def test_player_format(tmp_path,name,codec,container,ext,audio,ten_bit):
    global _APP
    _APP=QApplication.instance() or QApplication([]);app=_APP
    source=tmp_path/f'player.{ext}'
    make(str(source),codec=codec,container=container,audio=audio,ten_bit=ten_bit,
         gop=250,bframes=3 if codec in ('h264','hevc') else 0,frames=160,sar=Fraction(1,1))
    player=VideoPlayer();player.show()
    frames=[];buffers=[]
    player.framePresented.connect(lambda seconds: frames.append(round(seconds*1_000_000)))
    output=QAudioBufferOutput(player);player.media.setAudioBufferOutput(output)
    output.audioBufferReceived.connect(lambda buf:buffers.append(buf.byteCount()) if buf.isValid() else None)
    try:
        player.set_source(probe(str(source)));player.toggle()
        wait(app,lambda:len(frames)>=6 and buffers)
        assert player.media.error()==QMediaPlayer.NoError,player.media.errorString()
        assert player.video.image is not None and not player.video.image.isNull()
        assert player.audio_tracks.count()==len(audio)
        if len(audio)>1:
            player.audio_tracks.setCurrentIndex(1)
            assert player.media.activeAudioTrack()==1
        player.pause()
        assert not player.is_playing()
        frames.clear();player.seek_source(player.origin_s+4.8)
        wait(app,lambda:any(t>=4_700_000 for t in frames))
        assert abs(player.media.position()-4800)<100
        player.speed.setCurrentIndex(3);assert player.media.playbackRate()==2
        player.volume.setValue(40);assert player.audio.volume()==pytest.approx(.4)
        player.mute.setChecked(True);assert player.audio.isMuted()
    finally:player.shutdown();player.close();app.processEvents()


def test_player_loop_and_source_switch(fixtures):
    global _APP
    _APP=QApplication.instance() or QApplication([]);app=_APP
    player=VideoPlayer();player.show()
    try:
        info=probe(fixtures['h264_mp4_longgop']);player.set_source(info)
        wait(app,lambda:player.media.duration()>0)
        positions=[];player.positionMoved.connect(lambda p:positions.append(p))
        player.play_range(3.9,4.5)
        wait(app,lambda:sum(b<a-.3 for a,b in zip(positions,positions[1:]))>=2)
        player.loop.setChecked(False)
        wait(app,lambda:not player.is_playing())
        player.set_source(probe(fixtures['mpeg2_open']))
        assert player.range_ms is None and not player.is_playing()
        assert player.video.image is None
        player.toggle();wait(app,lambda:player.video.image is not None)
        assert player.media.error()==QMediaPlayer.NoError
    finally:player.shutdown();player.close();app.processEvents()


def test_expanded_player_starts_at_playhead(fixtures, monkeypatch):
    global _APP
    _APP=QApplication.instance() or QApplication([]);app=_APP
    from bsr_ui.dialogs import PlaybackDialog
    player=VideoPlayer();player.show();player.set_source(probe(fixtures['h264_mp4_longgop']))
    wait(app,lambda:player.media.duration()>0)
    player.seek_source(5)
    visited=[]
    def inspect(dialog):
        dialog.show()
        wait(app,lambda:dialog.player.video.image is not None and dialog.player.media.position()>=5000)
        visited.append(dialog.player.media.position())
        dialog.accept()
        return 1
    monkeypatch.setattr(PlaybackDialog,'exec',inspect)
    try:
        player.expand.click()
        assert visited and visited[0]<5500
        assert not player.is_playing()
    finally:player.shutdown();player.close()
