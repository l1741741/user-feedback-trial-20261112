"""Real-FFmpeg compatibility checks for the writer lifecycle changes.

All media is generated locally; nothing is downloaded.
"""

import os
import subprocess as sp

import numpy as np

import pytest

from moviepy.audio.AudioClip import AudioArrayClip
from moviepy.video.VideoClip import ColorClip


def _ffmpeg_available():
    try:
        return sp.run(
            ["ffmpeg", "-version"], capture_output=True, timeout=10
        ).returncode == 0
    except (OSError, sp.SubprocessError):
        return False


@pytest.mark.skipif(not _ffmpeg_available(), reason="real ffmpeg is not installed")
def test_real_video_and_audio_round_trip(tmp_path):
    mp4 = str(tmp_path / "color.mp4")
    clip = ColorClip(size=(96, 64), color=(30, 120, 200), duration=1.0).with_fps(12)
    clip.write_videofile(mp4, codec="libx264", logger=None)
    assert os.path.getsize(mp4) > 0
    probe = sp.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            mp4,
        ],
        capture_output=True,
        text=True,
    )
    assert "h264" in probe.stdout

    wav = str(tmp_path / "tone.wav")
    t = np.linspace(0, 0.5, 22050, endpoint=False)
    array = np.stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 220 * t)], axis=1)
    AudioArrayClip((array * 32767).astype(np.int16), fps=44100).write_audiofile(
        wav, codec="pcm_s16le", logger=None
    )
    assert os.path.getsize(wav) > 0


@pytest.mark.skipif(not _ffmpeg_available(), reason="real ffmpeg is not installed")
def test_real_masked_video_and_audio_mux(tmp_path):
    base = ColorClip(size=(64, 48), color=(255, 0, 0), duration=0.5).with_fps(10)
    mask = ColorClip(size=(64, 48), color=1.0, is_mask=True, duration=0.5).with_fps(10)
    base.with_mask(mask).write_videofile(
        str(tmp_path / "masked.mov"), codec="libx264", logger=None
    )
    assert os.path.getsize(str(tmp_path / "masked.mov")) > 0


@pytest.mark.skipif(not _ffmpeg_available(), reason="real ffmpeg is not installed")
def test_real_redirected_logfile_error_keeps_file_and_exit_code(tmp_path):
    clip = ColorClip(size=(64, 48), color=(1, 2, 3), duration=0.3).with_fps(10)
    logfile = str(tmp_path / "bad.mp4.log")
    with pytest.raises(IOError) as excinfo:
        clip.write_videofile(
            str(tmp_path / "bad.mp4"),
            codec="nonexistent-codec",
            write_logfile=True,
            logger=None,
        )
    message = str(excinfo.value)
    assert "bad.mp4" in message
    assert "redirected" in message
    assert "FFmpeg exited with code" in message
    assert os.path.exists(logfile) and os.path.getsize(logfile) > 0
