"""Read video metadata and overlay strips with `ffprobe` and `ffmpeg`."""

import dataclasses
import hashlib
import json
import math
import shlex
import subprocess
import tempfile
import typing as t
from pathlib import Path

import numpy as np

from dashcam.overlay import NOMINAL_FRAME_WIDTH
from dashcam.overlay import STRIP_HEIGHT
from dashcam.overlay import GrayImage

# How many bytes from the start and from the end of a file go into its fingerprint.
FINGERPRINT_CHUNK_SIZE = 1024 * 1024

# How many of the last ffmpeg error lines to report when it fails.
ERROR_TAIL_LINE_COUNT = 20


@dataclasses.dataclass(frozen=True)
class VideoInfo:
    """Basic properties of a video file."""

    width: int
    height: int
    duration_s: float


def probe_video(path: Path, ffprobe_executable: str) -> VideoInfo:
    """
    Read the resolution and duration of a video.

    Raises
    ------
    RuntimeError
        If the file cannot be read or has no video stream.
    """
    cmd = [
        ffprobe_executable,
        *["-v", "error"],
        *["-select_streams", "v:0"],
        *["-show_entries", "stream=width,height:format=duration"],
        *["-of", "json"],
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Cannot read '{path}': {result.stderr.strip()}")

    probe_output = json.loads(result.stdout)
    streams = probe_output.get("streams", [])
    if not streams:
        raise RuntimeError(f"No video stream in '{path}'")
    return VideoInfo(
        width=int(streams[0]["width"]),
        height=int(streams[0]["height"]),
        duration_s=float(probe_output["format"]["duration"]),
    )


def probe_duration(path: Path, ffprobe_executable: str) -> float:
    """
    Read the duration of a media file in seconds.

    Raises
    ------
    RuntimeError
        If the file cannot be read or has no duration.
    """
    cmd = [
        ffprobe_executable,
        *["-v", "error"],
        *["-show_entries", "format=duration"],
        *["-of", "default=noprint_wrappers=1:nokey=1"],
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        return float(result.stdout)
    except ValueError:
        raise RuntimeError(
            f"Cannot read the duration of '{path}': {result.stderr.strip()}"
        ) from None


@dataclasses.dataclass(frozen=True)
class FfmpegProgress:
    """A progress report of a running ffmpeg command."""

    # Time (in seconds) of the output written so far.
    output_time_s: float

    # Seconds of output written per second of processing, or None before it is known.
    speed: float | None


def parse_speed(value: str) -> float | None:
    """Parse an ffmpeg speed such as `1.23x`. `N/A` and zero speeds give None."""
    try:
        speed = float(value.removesuffix("x"))
    except ValueError:
        return None
    return speed or None


def get_error_tail(stderr: str) -> str:
    """Return the last lines of ffmpeg's error output."""
    return "\n".join(stderr.strip().splitlines()[-ERROR_TAIL_LINE_COUNT:])


def iter_ffmpeg_progress(cmd: list[str]) -> t.Generator[FfmpegProgress]:
    """
    Run an ffmpeg command that has `-progress pipe:1` and yield its progress reports.

    ffmpeg writes a report of `key=value` lines about twice per second, each ending with a
    `progress` line.

    Yields
    ------
    FfmpegProgress
        The output time and speed at each report.

    Raises
    ------
    subprocess.CalledProcessError
        If ffmpeg fails. Its `stderr` holds the last lines of the error output.
    """
    # Collect errors in a file: a pipe could fill up and block ffmpeg.
    with tempfile.TemporaryFile() as stderr_file:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file, text=True) as proc:
            assert proc.stdout is not None
            output_time_s = 0.0
            speed = None
            try:
                for line in proc.stdout:
                    key, _, value = line.strip().partition("=")
                    # The values are `N/A` until the first frame is written.
                    if key == "out_time_us" and value.isdigit():
                        output_time_s = int(value) / 1e6
                    elif key == "speed":
                        speed = parse_speed(value)
                    elif key == "progress":
                        yield FfmpegProgress(output_time_s=output_time_s, speed=speed)
            except GeneratorExit:
                # The caller stopped reading early.
                proc.kill()
                raise
            proc.wait()

        if proc.returncode != 0:
            stderr_file.seek(0)
            stderr = stderr_file.read().decode(errors="replace")
            raise subprocess.CalledProcessError(proc.returncode, cmd, stderr=get_error_tail(stderr))


def build_strip_filter(frame_width: int) -> str:
    """
    Build the ffmpeg filter that cuts the bottom strip of a frame, scaled to the nominal width.

    For other widths, a strip of the proportional height is cut first, so that the strip after
    scaling matches the overlay geometry of `NOMINAL_FRAME_WIDTH`.
    """
    crop_filter = f"crop=iw:{STRIP_HEIGHT}:0:ih-{STRIP_HEIGHT}"
    if frame_width == NOMINAL_FRAME_WIDTH:
        return crop_filter

    scale_factor = NOMINAL_FRAME_WIDTH / frame_width
    # Round up, so that the scaled strip is never shorter than `STRIP_HEIGHT`.
    source_height = math.ceil(STRIP_HEIGHT / scale_factor)
    scaled_height = round(source_height * scale_factor)
    return (
        f"crop=iw:{source_height}:0:ih-{source_height},"
        f"scale={NOMINAL_FRAME_WIDTH}:{scaled_height}:flags=lanczos,"
        f"{crop_filter}"
    )


def build_strip_reader_command(
    path: Path,
    frame_width: int,
    sample_fps: float,
    ffmpeg_executable: str,
    hwaccel_options: str,
    start_s: float = 0.0,
    duration_s: float | None = None,
) -> list[str]:
    """
    Build the ffmpeg command that outputs bottom strips of sampled frames as raw grayscale.

    The strips are `NOMINAL_FRAME_WIDTH` pixels wide, whatever the frame width.
    """
    strip_filter = build_strip_filter(frame_width)
    input_options = []
    if start_s > 0:
        input_options += ["-ss", f"{start_s:.3f}"]
    if duration_s is not None:
        input_options += ["-t", f"{duration_s:.3f}"]

    return [
        ffmpeg_executable,
        "-nostdin",
        *["-v", "error"],
        *shlex.split(hwaccel_options),
        *input_options,
        *["-i", str(path)],
        *["-an", "-sn"],
        *["-vf", f"fps={sample_fps},format=gray,{strip_filter}"],
        *["-f", "rawvideo", "-"],
    ]


def iter_overlay_strips(
    path: Path,
    width: int,
    sample_fps: float,
    ffmpeg_executable: str,
    hwaccel_options: str,
    start_s: float = 0.0,
    duration_s: float | None = None,
) -> t.Generator[tuple[float, GrayImage]]:
    """
    Decode the video and yield the bottom strip of frames sampled at `sample_fps`.

    Strips are scaled to `NOMINAL_FRAME_WIDTH`, so `width` (the frame width) only selects the part
    of the frame to cut.

    Yields
    ------
    tuple[float, GrayImage]
        The video offset of the frame (in seconds) and its bottom strip.
    """
    cmd = build_strip_reader_command(
        path=path,
        frame_width=width,
        sample_fps=sample_fps,
        ffmpeg_executable=ffmpeg_executable,
        hwaccel_options=hwaccel_options,
        start_s=start_s,
        duration_s=duration_s,
    )
    frame_size = NOMINAL_FRAME_WIDTH * STRIP_HEIGHT

    # Collect errors in a file: a pipe could fill up and block ffmpeg.
    with tempfile.TemporaryFile() as stderr_file:
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file) as proc:
            assert proc.stdout is not None
            frame_index = 0
            try:
                while True:
                    frame_bytes = proc.stdout.read(frame_size)
                    if len(frame_bytes) < frame_size:
                        break
                    strip_pixels = np.frombuffer(frame_bytes, dtype=np.uint8)
                    strip = strip_pixels.reshape(STRIP_HEIGHT, NOMINAL_FRAME_WIDTH)
                    yield start_s + frame_index / sample_fps, strip
                    frame_index += 1
            except GeneratorExit:
                # The caller stopped reading early.
                proc.kill()
                raise
            proc.wait()

        if proc.returncode != 0:
            stderr_file.seek(0)
            stderr = stderr_file.read().decode(errors="replace").strip()
            raise RuntimeError(f"ffmpeg failed to decode '{path}': {stderr}")


def compute_fingerprint(path: Path) -> str:
    """
    Compute a content fingerprint that identifies a video regardless of its name.

    The fingerprint is `<size>:<sha256 of the first and last 1 MB>`, which is fast to compute
    even for large files on slow drives.
    """
    size = path.stat().st_size
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        digest.update(fp.read(FINGERPRINT_CHUNK_SIZE))
        if size > FINGERPRINT_CHUNK_SIZE:
            fp.seek(max(FINGERPRINT_CHUNK_SIZE, size - FINGERPRINT_CHUNK_SIZE))
            digest.update(fp.read(FINGERPRINT_CHUNK_SIZE))
    return f"{size}:{digest.hexdigest()[:16]}"
