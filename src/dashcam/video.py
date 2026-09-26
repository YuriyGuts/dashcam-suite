"""Read video metadata and overlay strips with `ffprobe` and `ffmpeg`."""

import dataclasses
import hashlib
import json
import shlex
import subprocess
import tempfile
import typing as t
from pathlib import Path

import numpy as np

from dashcam.overlay import STRIP_HEIGHT
from dashcam.overlay import GrayImage

# Which FFprobe executable to run.
FFPROBE_EXECUTABLE = "ffprobe"

# How many bytes from the start and from the end of a file go into its fingerprint.
FINGERPRINT_CHUNK_SIZE = 1024 * 1024


@dataclasses.dataclass(frozen=True)
class VideoInfo:
    """Basic properties of a video file."""

    width: int
    height: int
    duration_s: float


def probe_video(path: Path) -> VideoInfo:
    """
    Read the resolution and duration of a video.

    Raises
    ------
    RuntimeError
        If the file cannot be read or has no video stream.
    """
    cmd = [
        FFPROBE_EXECUTABLE,
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


def build_strip_reader_command(
    path: Path,
    sample_fps: float,
    ffmpeg_executable: str,
    hwaccel_options: str,
    start_s: float = 0.0,
    duration_s: float | None = None,
) -> list[str]:
    """Build the ffmpeg command that outputs bottom strips of sampled frames as raw grayscale."""
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
        *["-vf", f"fps={sample_fps},crop=iw:{STRIP_HEIGHT}:0:ih-{STRIP_HEIGHT},format=gray"],
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

    Yields
    ------
    tuple[float, GrayImage]
        The video offset of the frame (in seconds) and its bottom strip.
    """
    cmd = build_strip_reader_command(
        path=path,
        sample_fps=sample_fps,
        ffmpeg_executable=ffmpeg_executable,
        hwaccel_options=hwaccel_options,
        start_s=start_s,
        duration_s=duration_s,
    )
    frame_size = width * STRIP_HEIGHT

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
                    strip = np.frombuffer(frame_bytes, dtype=np.uint8).reshape(STRIP_HEIGHT, width)
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
