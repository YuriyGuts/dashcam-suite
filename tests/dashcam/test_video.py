import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dashcam import overlay
from dashcam import video

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg not installed",
)

OVERLAY_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "overlay"
OVERLAY_LABELS = json.loads((OVERLAY_FIXTURE_DIR / "labels.json").read_text(encoding="utf-8"))


def make_video_from_strip(strip_path: Path, video_path: Path, width: int, height: int) -> None:
    """Make a 1-second video of a 2560x1440 frame with the strip at the bottom, scaled."""
    frame_filter = (
        f"pad=2560:1440:0:{1440 - overlay.STRIP_HEIGHT}:color=gray,scale={width}:{height}"
    )
    cmd = [
        "ffmpeg",
        *["-v", "error"],
        *["-loop", "1", "-framerate", "10", "-t", "1", "-i", str(strip_path)],
        *["-vf", frame_filter],
        *["-c:v", "libx264", "-pix_fmt", "yuv420p"],
        str(video_path),
    ]
    subprocess.run(cmd, check=True)


@pytest.fixture
def synthetic_video(tmp_path):
    path = tmp_path / "synthetic.mp4"
    cmd = [
        "ffmpeg",
        *["-v", "error"],
        *["-f", "lavfi", "-i", "color=c=gray:s=2560x200:r=10:d=3"],
        *["-c:v", "libx264", "-pix_fmt", "yuv420p"],
        str(path),
    ]
    subprocess.run(cmd, check=True)
    return path


@requires_ffmpeg
def test_probe_video(synthetic_video):
    # GIVEN a 3-second 2560x200 video

    # WHEN probing it
    video_info = video.probe_video(synthetic_video, "ffprobe")

    # THEN the resolution and duration are reported
    assert (video_info.width, video_info.height) == (2560, 200)
    assert video_info.duration_s == pytest.approx(3, abs=0.2)


@requires_ffmpeg
def test_probe_video_with_invalid_file(tmp_path):
    # GIVEN a file that is not a video
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"not a video")

    # WHEN probing it
    # THEN it fails with a readable error
    with pytest.raises(RuntimeError, match="Cannot read"):
        video.probe_video(path, "ffprobe")


@requires_ffmpeg
@pytest.mark.parametrize(
    "probe_output",
    [
        "not json",
        '{"streams": [{"width": 2560, "height": 1440}], "format": {}}',
        '{"streams": [{"width": "N/A", "height": 1440}], "format": {"duration": "1.0"}}',
        '{"streams": [{"height": 1440}], "format": {"duration": "1.0"}}',
    ],
)
def test_probe_video_with_incomplete_output(monkeypatch, probe_output):
    # GIVEN ffprobe succeeding with malformed or incomplete output (e.g. for a truncated video)
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=probe_output, stderr="")

    monkeypatch.setattr("dashcam.video.subprocess.run", fake_run)

    # WHEN probing the video
    # THEN a `RuntimeError` names the file
    with pytest.raises(RuntimeError, match="trip.mp4"):
        video.probe_video(Path("trip.mp4"), "ffprobe")


def test_probe_video_without_video_stream(monkeypatch):
    # GIVEN ffprobe finding no video stream
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout='{"streams": []}', stderr="")

    monkeypatch.setattr("dashcam.video.subprocess.run", fake_run)

    # WHEN probing the video
    # THEN the error says so
    with pytest.raises(RuntimeError, match="No video stream in 'trip.mp4'"):
        video.probe_video(Path("trip.mp4"), "ffprobe")


def test_probe_video_decodes_output_as_utf8(monkeypatch):
    # GIVEN ffprobe failing on a file with a Cyrillic name
    run_kwargs = {}

    def fake_run(cmd, **kwargs):
        run_kwargs.update(kwargs)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Поїздка.mp4: Invalid data")

    monkeypatch.setattr("dashcam.video.subprocess.run", fake_run)

    # WHEN probing the video
    # THEN the output is decoded as UTF-8 whatever the locale, and the error is reported
    with pytest.raises(RuntimeError, match="Invalid data"):
        video.probe_video(Path("Поїздка.mp4"), "ffprobe")
    assert run_kwargs["encoding"] == "utf-8"
    assert run_kwargs["errors"] == "replace"


def test_iter_overlay_strips(synthetic_video):
    # GIVEN a 3-second video

    # WHEN reading strips at 2 frames per second
    strips = list(
        video.iter_overlay_strips(
            synthetic_video,
            width=2560,
            sample_fps=2,
            ffmpeg_executable="ffmpeg",
            hwaccel_options="",
        )
    )

    # THEN there is one bottom strip per half second, with offsets
    assert [offset_s for offset_s, _ in strips] == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    assert strips[0][1].shape == (overlay.STRIP_HEIGHT, 2560)


@requires_ffmpeg
def test_iter_overlay_strips_with_start_and_duration(synthetic_video):
    # GIVEN a 3-second video

    # WHEN reading one second starting at 1 s
    strips = list(
        video.iter_overlay_strips(
            synthetic_video,
            width=2560,
            sample_fps=2,
            ffmpeg_executable="ffmpeg",
            hwaccel_options="",
            start_s=1.0,
            duration_s=1.0,
        )
    )

    # THEN offsets are relative to the video start
    assert [offset_s for offset_s, _ in strips] == [1.0, 1.5]


@requires_ffmpeg
def test_iter_overlay_strips_stops_early(synthetic_video):
    # GIVEN a strip iterator

    # WHEN the caller stops after the first strip
    strips = video.iter_overlay_strips(
        synthetic_video,
        width=2560,
        sample_fps=2,
        ffmpeg_executable="ffmpeg",
        hwaccel_options="",
    )
    first_offset_s, _ = next(strips)
    strips.close()

    # THEN ffmpeg is stopped without an error
    assert first_offset_s == 0.0


@requires_ffmpeg
def test_iter_overlay_strips_with_invalid_file(tmp_path):
    # GIVEN a file that is not a video
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"not a video")

    # WHEN reading strips
    # THEN decoding fails with a readable error
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        list(video.iter_overlay_strips(path, 2560, 2, "ffmpeg", ""))


@requires_ffmpeg
@pytest.mark.parametrize("width, height", [(2560, 1440), (1920, 1080), (1280, 720)])
@pytest.mark.parametrize("filename", ["2024-02-11_60s.png", "2026-09-25_100s.png"])
def test_iter_overlay_strips_reads_scaled_videos(tmp_path, filename, width, height):
    # GIVEN a video with a labeled overlay, scaled down from 2560 pixels wide
    video_path = tmp_path / "scaled.mp4"
    make_video_from_strip(OVERLAY_FIXTURE_DIR / filename, video_path, width, height)

    # WHEN reading its first strip
    strips = video.iter_overlay_strips(
        video_path,
        width=width,
        sample_fps=2,
        ffmpeg_executable="ffmpeg",
        hwaccel_options="",
    )
    _, strip = next(strips)
    strips.close()
    reading = overlay.read_overlay(strip)

    # THEN the strip has the nominal size and both texts match the label
    assert strip.shape == (overlay.STRIP_HEIGHT, overlay.NOMINAL_FRAME_WIDTH)
    assert reading.gps_text == OVERLAY_LABELS[filename]["gps"]
    assert reading.clock_text == OVERLAY_LABELS[filename]["clock"]
    assert reading.is_gps_text_reliable
    assert reading.is_clock_text_reliable


@pytest.mark.parametrize(
    "frame_width, expected_filter",
    [
        (2560, "crop=iw:64:0:ih-64"),
        (1920, "crop=iw:48:0:ih-48,scale=2560:64:flags=lanczos,crop=iw:64:0:ih-64"),
        (1366, "crop=iw:35:0:ih-35,scale=2560:66:flags=lanczos,crop=iw:64:0:ih-64"),
        (3840, "crop=iw:96:0:ih-96,scale=2560:64:flags=lanczos,crop=iw:64:0:ih-64"),
    ],
)
def test_build_strip_filter(frame_width, expected_filter):
    # GIVEN a frame width

    # WHEN building the strip filter
    strip_filter = video.build_strip_filter(frame_width)

    # THEN strips of other widths are cut in proportion and scaled to the nominal width
    assert strip_filter == expected_filter


def test_build_strip_reader_command():
    # GIVEN a hardware acceleration option and a time range

    # WHEN building the ffmpeg command
    cmd = video.build_strip_reader_command(
        path=video.Path("/videos/trip.mp4"),
        frame_width=2560,
        sample_fps=2,
        ffmpeg_executable="ffmpeg",
        hwaccel_options="-hwaccel vulkan",
        start_s=10,
        duration_s=0.5,
    )

    # THEN the options come before the input and the strip is cropped from the bottom
    assert cmd[:10] == [
        "ffmpeg",
        "-nostdin",
        *["-v", "error"],
        *["-hwaccel", "vulkan"],
        *["-ss", "10.000"],
        *["-t", "0.500"],
    ]
    assert (
        f"crop=iw:{overlay.STRIP_HEIGHT}:0:ih-{overlay.STRIP_HEIGHT}" in cmd[cmd.index("-vf") + 1]
    )


def test_compute_fingerprint_ignores_the_middle_of_large_files(tmp_path):
    # GIVEN two 3 MB files that differ only in the middle
    first_path = tmp_path / "first.bin"
    second_path = tmp_path / "second.bin"
    first_path.write_bytes(b"a" * 1024 * 1024 + b"b" * 1024 * 1024 + b"c" * 1024 * 1024)
    second_path.write_bytes(b"a" * 1024 * 1024 + b"x" * 1024 * 1024 + b"c" * 1024 * 1024)

    # WHEN fingerprinting them
    first_fingerprint = video.compute_fingerprint(first_path)
    second_fingerprint = video.compute_fingerprint(second_path)

    # THEN the fingerprints match and start with the size
    assert first_fingerprint == second_fingerprint
    assert first_fingerprint.startswith(f"{3 * 1024 * 1024}:")


def test_compute_fingerprint_detects_changed_end(tmp_path):
    # GIVEN two small files that differ at the end
    first_path = tmp_path / "first.bin"
    second_path = tmp_path / "second.bin"
    first_path.write_bytes(b"abc")
    second_path.write_bytes(b"abd")

    # WHEN fingerprinting them
    # THEN the fingerprints differ
    assert video.compute_fingerprint(first_path) != video.compute_fingerprint(second_path)


def test_probe_duration(monkeypatch, tmp_path):
    # GIVEN ffprobe reporting a duration
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="61.5\n", stderr="")

    monkeypatch.setattr("dashcam.video.subprocess.run", fake_run)

    # WHEN probing the duration with a custom ffprobe
    duration_s = video.probe_duration(tmp_path / "segment.MP4", "/opt/ffmpeg/bin/ffprobe")

    # THEN it is parsed, and the custom ffprobe is run
    assert duration_s == 61.5
    assert commands[0][0] == "/opt/ffmpeg/bin/ffprobe"


def test_probe_duration_of_unreadable_file(monkeypatch, tmp_path):
    # GIVEN ffprobe failing to read a file
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Invalid data found\n")

    monkeypatch.setattr("dashcam.video.subprocess.run", fake_run)

    # WHEN probing the duration
    # THEN the error names the file and the ffprobe error
    with pytest.raises(RuntimeError, match="segment.MP4.*Invalid data found"):
        video.probe_duration(tmp_path / "segment.MP4", "ffprobe")


def test_get_error_tail_keeps_last_lines():
    # GIVEN more error lines than are reported
    stderr = "".join(f"line {number}\n" for number in range(30))

    # WHEN taking the tail
    tail = video.get_error_tail(stderr)

    # THEN only the last lines are kept
    assert tail.splitlines() == [f"line {number}" for number in range(10, 30)]


def test_iter_ffmpeg_progress_yields_reports():
    # GIVEN a command that prints two ffmpeg progress reports
    progress_lines = (
        "out_time_us=N/A\nspeed=N/A\nprogress=continue\n"
        "frame=10\nout_time_us=1500000\nspeed=1.23x\nprogress=end\n"
    )
    cmd = ["sh", "-c", f"printf '{progress_lines}'"]

    # WHEN reading its progress
    reports = list(video.iter_ffmpeg_progress(cmd))

    # THEN one report is yielded per `progress` line, with the values known so far
    assert reports == [
        video.FfmpegProgress(output_time_s=0.0, speed=None),
        video.FfmpegProgress(output_time_s=1.5, speed=1.23),
    ]


def test_iter_ffmpeg_progress_raises_with_error_tail():
    # GIVEN a command that fails with error output
    cmd = ["sh", "-c", "echo 'Unknown encoder' >&2; exit 3"]

    # WHEN reading its progress
    # THEN the error carries the exit code and the error output
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        list(video.iter_ffmpeg_progress(cmd))
    assert exc_info.value.returncode == 3
    assert exc_info.value.stderr == "Unknown encoder"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.23x", 1.23),
        ("  12x", 12.0),
        ("N/A", None),
        ("0x", None),
    ],
)
def test_parse_speed(value, expected):
    assert video.parse_speed(value) == expected
