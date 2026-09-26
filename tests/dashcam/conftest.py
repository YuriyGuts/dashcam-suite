import contextlib
import dataclasses
import datetime
import subprocess

import pytest

from dashcam import cleaning
from dashcam import metadata
from dashcam.config import get_platform_defaults

KYIV_SUMMER = datetime.timezone(datetime.timedelta(hours=3))


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


@pytest.fixture
def make_track():
    def _make_track(
        video_filename="2026-09-25 Trip 11-17.mp4", fingerprint="100:abc", sample_count=3
    ):
        raw_samples = []
        clean_samples = []
        for index in range(sample_count):
            lat = round(49.8 + index * 0.0003, 6)
            raw_samples.append(
                cleaning.RawSample(
                    t=float(index),
                    left_text=f"30 KM/H N{lat:.6f} E24.000000",
                    right_text=f"2026/09/25 11:17:{index:02d}",
                    left_score=0.97,
                    right_score=0.99,
                )
            )
            clean_samples.append(
                cleaning.CleanSample(
                    t=float(index),
                    time=datetime.datetime(2026, 9, 25, 11, 17, index, tzinfo=KYIV_SUMMER),
                    lat=lat,
                    lon=24.0,
                    kmh=30,
                    status=cleaning.STATUS_OK,
                )
            )
        return metadata.Track(
            video_filename=video_filename,
            fingerprint=fingerprint,
            video_size=100,
            video_mtime=1790000000.5,
            extraction_status=metadata.EXTRACTION_OK,
            extracted_at="2026-09-26T16:00:00+03:00",
            extractor_version=metadata.EXTRACTOR_VERSION,
            cleaning_version=metadata.CLEANING_VERSION,
            video_width=2560,
            video_height=1440,
            duration_s=float(sample_count),
            overrides=cleaning.Overrides(bad_ranges_s=[(1.0, 2.0)]),
            streets=[],
            raw_samples=raw_samples,
            clean_samples=clean_samples,
        )

    return _make_track
