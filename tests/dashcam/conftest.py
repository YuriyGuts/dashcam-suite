import contextlib
import dataclasses
import subprocess

import pytest

from dashcam.config import get_platform_defaults


@pytest.fixture
def config():
    return dataclasses.replace(
        get_platform_defaults(),
        ffmpeg_executable="ffmpeg",
        hwaccel_options="-hwaccel videotoolbox",
        video_codec_options="-c:v libx265 -crf 30 -preset fast",
        audio_codec_options="-c:a aac -b:a 128k",
        job_count=2,
        min_trip_gap_hours=3,
    )


@pytest.fixture
def raw_video_dir(tmp_path):
    raw_video_dir = tmp_path / "DCIM" / "Movie"
    raw_video_dir.mkdir(parents=True)
    return raw_video_dir


@pytest.fixture
def make_raw_videos(raw_video_dir):
    def _make_raw_videos(*filenames):
        paths = []
        for filename in filenames:
            path = raw_video_dir / filename
            path.write_bytes(b"")
            paths.append(path)
        return paths

    return _make_raw_videos


class FakeFfmpeg:
    """Records ffmpeg invocations and simulates their outcome without running ffmpeg."""

    def __init__(self):
        self.calls = []
        self.encode_return_code = 0
        self.unreadable_paths = set()

    def run(self, cmd, **kwargs):
        self.calls.append(cmd)
        is_readability_check = cmd[-3:] == ["-f", "null", "-"]
        if is_readability_check:
            input_path = cmd[cmd.index("-i") + 1]
            return_code = 1 if input_path in self.unreadable_paths else 0
            return subprocess.CompletedProcess(cmd, return_code)

        output_path = cmd[-1]
        with open(output_path, "wb") as fp:
            fp.write(b"partial video")
        if self.encode_return_code != 0:
            raise subprocess.CalledProcessError(self.encode_return_code, cmd)
        return subprocess.CompletedProcess(cmd, 0)


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    ffmpeg = FakeFfmpeg()
    monkeypatch.setattr("dashcam.encode.subprocess.run", ffmpeg.run)
    return ffmpeg


@pytest.fixture
def no_os_sleep_prevention(monkeypatch):
    monkeypatch.setattr("dashcam.encode.prevent_os_sleep", contextlib.nullcontext)


@pytest.fixture
def serial_pool(monkeypatch):
    """Run pool jobs in the test process so that monkeypatched functions stay in effect."""

    class SerialPool:
        def __init__(self, processes, initializer=None):
            self.processes = processes

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def map(self, func, items):
            return [func(item) for item in items]

    monkeypatch.setattr("dashcam.encode.multiprocessing.Pool", SerialPool)
