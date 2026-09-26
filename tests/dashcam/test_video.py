import shutil
import subprocess

import pytest

from dashcam import overlay
from dashcam import video

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg not installed",
)


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
    video_info = video.probe_video(synthetic_video)

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
        video.probe_video(path)


@requires_ffmpeg
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


def test_build_strip_reader_command():
    # GIVEN a hardware acceleration option and a time range

    # WHEN building the ffmpeg command
    cmd = video.build_strip_reader_command(
        path=video.Path("/videos/trip.mp4"),
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
