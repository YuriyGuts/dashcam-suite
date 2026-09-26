r"""
Concatenate and encode dashcam videos stored on an SD card.

Assumes the raw videos are stored as `YYYYMMDDhhmmss_NNNNNN.MP4`, where the first part is
the recording start time and `NNNNNN` is a sequential index. Files named differently are
supported if their name ends with the index (e.g. `xxxx0042.avi`); their start time is then
taken from the file modification time.

The tool can operate in two modes:

1) "trips" mode: encodes all videos in the input directory, organizing them into trips
   according to the recording start times. Each logical trip will be encoded as a single
   output video named after the trip start time.

   Example (default trip gap = 3 hours):

   Filename                               Date           Inferred Trip   Output File
   /DCIM
   |---/Movie
   |-------/20230112152345_000001.MP4     Jan 12, 15:23  | => Trip 1  |  2023-01-12 Trip 15-23.mp4
   |-------/20230112152445_000002.MP4     Jan 12, 15:24  | => Trip 1  |
   |-------/20230112193820_000003.MP4     Jan 12, 19:38  | => Trip 2  |  2023-01-12 Trip 19-38.mp4
   |-------/20230113101200_000004.MP4     Jan 13, 10:12  | => Trip 3  |  2023-01-13 Trip 10-12.mp4
   |-------/20230113101300_000005.MP4     Jan 13, 10:13  | => Trip 3  |

2) "range" mode: selects all raw videos between the specified start/end index and
   encodes them as a single output video.

   Example ("range 3 6"):

   /DCIM
   |---/Movie
   |-------/20230112152345_000001.MP4
   |-------/20230112152445_000003.MP4     <= These videos
   |-------/20230112193820_000004.MP4     <= will be selected
   |-------/20230113101200_000006.MP4     <= and merged
   |-------/20230113101300_000007.MP4

Existing output videos are never overwritten. While a video is being encoded, it is written
under a temporary name, so an interrupted run never leaves an incomplete video behind.
"""

import dataclasses
import datetime
import logging
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from dashcam import terminal
from dashcam import video
from dashcam.config import Config
from dashcam.system import prevent_os_sleep
from dashcam.system import worker_pool

# Extensions of the video files to expect in the input directory.
EXPECTED_VIDEO_EXTENSIONS = (".avi", ".mp4", ".mov")

# Raw video filename written by the camera, e.g. `20260925111707_000271`.
RAW_VIDEO_STEM_PATTERN = re.compile(r"^(?P<start_time>\d{14})_(?P<index>\d+)$")
RAW_VIDEO_START_TIME_FORMAT = "%Y%m%d%H%M%S"

# Fallback for other naming schemes: the index is the trailing number of the filename.
TRAILING_INDEX_PATTERN = re.compile(r"(?P<index>\d+)$")

# Container format and extension of the output videos.
OUTPUT_FORMAT = "mp4"

# Suffix of the videos that are still being encoded.
PARTIAL_OUTPUT_SUFFIX = ".partial"

# How often (in seconds) to log encoding progress.
PROGRESS_INTERVAL_S = 10

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class RawVideoSegment:
    """Represents a single raw video file (e.g. a 1-minute segment) in the input directory."""

    index: int
    path: Path
    start_time: datetime.datetime


@dataclasses.dataclass
class Trip:
    """Video segments, logically grouped into a trip that should be encoded as a single file."""

    raw_segments: list[RawVideoSegment]

    @property
    def start_time(self) -> datetime.datetime:
        return self.raw_segments[0].start_time

    def get_placeholder_name(self) -> str:
        """Return the output video name (without extension), e.g. `2026-09-25 Trip 11-17`."""
        return get_placeholder_video_name(self.start_time)

    def __str__(self) -> str:
        start_index = self.raw_segments[0].index
        end_index = self.raw_segments[-1].index
        return (
            f"{self.get_placeholder_name()} / Segments: {start_index}-{end_index} "
            f"({len(self.raw_segments)} files)"
        )


@dataclasses.dataclass(frozen=True)
class EncodeJobDefinition:
    """Parameters for a single encoding job in the parallel encoding pool."""

    raw_segments: list[RawVideoSegment]
    output_path: Path
    config: Config


def get_placeholder_video_name(start_time: datetime.datetime) -> str:
    """Return the placeholder name of a trip video that starts at the given time."""
    return start_time.strftime("%Y-%m-%d Trip %H-%M")


def parse_raw_video_segment(path: Path) -> RawVideoSegment | None:
    """
    Extract the index and start time of a raw video from its filename.

    E.g., /path/to/videos/20260925111707_000271.MP4 -> index 271, start 2026-09-25 11:17:07.

    Returns
    -------
    RawVideoSegment | None
        The parsed segment, or None if the filename does not contain an index.
    """
    camera_name_match = RAW_VIDEO_STEM_PATTERN.match(path.stem)
    if camera_name_match:
        start_time = datetime.datetime.strptime(
            camera_name_match["start_time"],
            RAW_VIDEO_START_TIME_FORMAT,
        )
        return RawVideoSegment(
            index=int(camera_name_match["index"]),
            path=path,
            start_time=start_time,
        )

    trailing_index_match = TRAILING_INDEX_PATTERN.search(path.stem)
    if trailing_index_match:
        modification_time = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        return RawVideoSegment(
            index=int(trailing_index_match["index"]),
            path=path,
            start_time=modification_time.replace(microsecond=0),
        )

    return None


def is_raw_video_readable(path: Path, ffmpeg_executable: str) -> bool:
    """Check whether the specified video file can be read by `ffmpeg`."""
    cmd = [
        ffmpeg_executable,
        "-nostdin",
        "-v",
        "error",
        "-t",
        "0.1",
        "-i",
        str(path),
        "-f",
        "null",
        "-",
    ]
    LOGGER.info(shlex.join(cmd), extra=terminal.COMMAND)
    result = subprocess.run(cmd, capture_output=True, check=False)
    return result.returncode == 0


def collect_raw_video_segments(
    raw_video_dir: Path,
    ffmpeg_executable: str,
    start_index: int | None = None,
    end_index: int | None = None,
    check_readability: bool = True,
) -> list[RawVideoSegment]:
    """
    Scan the raw video directory for files matching the input criteria.

    Returns
    -------
    list[RawVideoSegment]
        The matching segments, sorted by start time.
    """
    LOGGER.info(f"Collecting files from '{raw_video_dir}'")

    # Skip hidden files, such as `._*` metadata files that macOS writes to FAT volumes.
    video_paths = [
        path
        for path in raw_video_dir.iterdir()
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() in EXPECTED_VIDEO_EXTENSIONS
    ]
    LOGGER.info(f"Found {len(video_paths)} files with supported extensions")

    segments = []
    for path in video_paths:
        segment = parse_raw_video_segment(path)
        if segment is None:
            LOGGER.warning(f"Cannot find an index in the name of '{path.name}'; skipping")
            continue
        segments.append(segment)
    segments.sort(key=lambda segment: (segment.start_time, segment.index))

    if start_index is not None and end_index is not None:
        segments = [segment for segment in segments if start_index <= segment.index <= end_index]

    if check_readability:
        LOGGER.info("Checking raw video files for readability...")
        validated_segments = []
        for segment in segments:
            if not is_raw_video_readable(segment.path, ffmpeg_executable):
                LOGGER.warning(f"The input file '{segment.path}' is not readable; skipping")
            else:
                validated_segments.append(segment)
        segments = validated_segments

    if not segments:
        raise RuntimeError("Could not find any files matching the input criteria")

    LOGGER.info(f"Collected {len(segments)} files matching the input criteria")
    return segments


def group_segments_into_trips(
    segments: list[RawVideoSegment],
    min_trip_gap_hours: float,
) -> list[Trip]:
    """Given a list of video segments sorted by start time, bucket them into trips."""
    trips: list[Trip] = []
    min_trip_gap = datetime.timedelta(hours=min_trip_gap_hours)

    # Scan the files sequentially and create a new trip
    # each time the time difference is big enough.
    prev_start_time = None
    for segment in segments:
        is_new_trip = prev_start_time is None or segment.start_time - prev_start_time > min_trip_gap
        if is_new_trip:
            trips.append(Trip(raw_segments=[]))

        trips[-1].raw_segments.append(segment)
        prev_start_time = segment.start_time

    return trips


def get_partial_output_path(output_path: Path) -> Path:
    """Return the temporary path under which the output video is written during encoding."""
    return output_path.with_name(output_path.name + PARTIAL_OUTPUT_SUFFIX)


def format_ffmpeg_concat_list(segments: list[RawVideoSegment]) -> str:
    """Build the contents of an ffmpeg concat demuxer list containing the files to concatenate."""
    lines = []
    for segment in segments:
        # Inside single quotes, a single quote is written as `'\''`.
        escaped_path = str(segment.path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped_path}'\n")
    return "".join(lines)


def build_ffmpeg_command(
    concat_list_path: Path,
    partial_output_path: Path,
    config: Config,
) -> list[str]:
    """Build the ffmpeg command that concatenates and encodes the listed files."""
    return [
        config.ffmpeg_executable,
        "-nostdin",
        # Report errors only, and the progress as `key=value` lines on stdout.
        *["-v", "error", "-nostats", "-progress", "pipe:1"],
        # Allow ffmpeg to use absolute input paths (-safe 0).
        *["-f", "concat", "-safe", "0"],
        *shlex.split(config.hwaccel_options),
        *["-i", str(concat_list_path)],
        # Video and audio codec parameters.
        *shlex.split(config.video_codec_options),
        *shlex.split(config.audio_codec_options),
        # The partial output file has an unusual extension, so specify the format explicitly.
        *["-f", OUTPUT_FORMAT, "-y", str(partial_output_path)],
    ]


def get_total_duration(segments: list[RawVideoSegment]) -> float | None:
    """
    Sum the durations of the raw videos.

    Returns
    -------
    float | None
        The total duration in seconds, or None if a duration cannot be read.
    """
    try:
        return sum(video.probe_duration(segment.path) for segment in segments)
    except (OSError, RuntimeError) as exc:
        LOGGER.warning(f"Cannot compute the encoding progress: {exc}")
        return None


def format_video_time(time_s: float) -> str:
    """Format a video time as `M:SS`, or `H:MM:SS` from one hour."""
    total_s = int(time_s)
    hours, minutes, seconds = total_s // 3600, total_s % 3600 // 60, total_s % 60
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def format_encode_progress(
    output_name: str,
    progress: video.FfmpegProgress,
    total_duration_s: float | None,
) -> str:
    """
    Describe the progress of an encoding job.

    E.g. `Trip.mp4: 23% (1:23 of 6:00, 1.2x, ~4 min left)`, or `Trip.mp4: 1:23 encoded, 1.2x`
    when the total duration is unknown.
    """
    output_time_text = format_video_time(progress.output_time_s)
    speed_text = "" if progress.speed is None else f", {progress.speed:.1f}x"
    if not total_duration_s:
        return f"{output_name}: {output_time_text} encoded{speed_text}"

    percent = min(100, round(progress.output_time_s / total_duration_s * 100))
    time_left_text = ""
    if progress.speed is not None:
        remaining_s = max(0.0, total_duration_s - progress.output_time_s) / progress.speed
        if remaining_s < 60:
            time_left_text = ", <1 min left"
        else:
            time_left_text = f", ~{round(remaining_s / 60)} min left"
    return (
        f"{output_name}: {percent}% ({output_time_text} of {format_video_time(total_duration_s)}"
        f"{speed_text}{time_left_text})"
    )


def run_ffmpeg_with_progress(
    cmd: list[str], output_name: str, total_duration_s: float | None
) -> None:
    """Run the encoding command, logging its progress every `PROGRESS_INTERVAL_S`."""
    next_progress_at = time.monotonic() + PROGRESS_INTERVAL_S
    for progress in video.iter_ffmpeg_progress(cmd):
        if time.monotonic() < next_progress_at:
            continue
        LOGGER.info(
            format_encode_progress(output_name, progress, total_duration_s),
            extra=terminal.PROGRESS,
        )
        next_progress_at = time.monotonic() + PROGRESS_INTERVAL_S


def run_encode_job(job_def: EncodeJobDefinition) -> bool:
    """
    Run video concatenation and encoding for a single output video.

    Returns
    -------
    bool
        True if the video was encoded, False if encoding failed.
    """
    output_path = job_def.output_path
    partial_output_path = get_partial_output_path(output_path)
    total_duration_s = get_total_duration(job_def.raw_segments)
    duration_text = "" if total_duration_s is None else f", {total_duration_s / 60:.0f} min"
    LOGGER.info(f"Encoding: {output_path.name} ({len(job_def.raw_segments)} files{duration_text})")

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="dashcam-encode-",
        suffix=".txt",
        delete=False,
    ) as fp:
        fp.write(format_ffmpeg_concat_list(job_def.raw_segments))
        concat_list_path = Path(fp.name)

    cmd = build_ffmpeg_command(concat_list_path, partial_output_path, job_def.config)
    try:
        LOGGER.info(shlex.join(cmd), extra=terminal.COMMAND)
        run_ffmpeg_with_progress(cmd, output_path.name, total_duration_s)
        partial_output_path.rename(output_path)
    except subprocess.CalledProcessError as exc:
        error_tail = f"\n{exc.stderr}" if exc.stderr else ""
        LOGGER.error(
            f"Encoding failed for '{output_path.name}' (ffmpeg exit code {exc.returncode})"
            f"{error_tail}"
        )
        partial_output_path.unlink(missing_ok=True)
        return False
    finally:
        concat_list_path.unlink(missing_ok=True)

    LOGGER.info(f"Encoding completed: {output_path.name}", extra=terminal.SUCCESS)
    return True


def plan_encode_jobs(
    segment_groups: list[tuple[str, list[RawVideoSegment]]],
    library_dir: Path,
    config: Config,
) -> list[EncodeJobDefinition]:
    """
    Turn (output name, segments) pairs into encoding jobs, skipping outputs that already exist.

    Returns
    -------
    list[EncodeJobDefinition]
        The jobs whose output does not exist yet.
    """
    job_defs = []
    for output_name, segments in segment_groups:
        output_path = library_dir / f"{output_name}.{OUTPUT_FORMAT}"
        if output_path.exists():
            LOGGER.warning(f"Output video '{output_path}' already exists; skipping")
            continue
        job_defs.append(
            EncodeJobDefinition(raw_segments=segments, output_path=output_path, config=config)
        )
    return job_defs


def run_encode_jobs(job_defs: list[EncodeJobDefinition], job_count: int) -> int:
    """
    Run the encoding jobs in parallel.

    Returns
    -------
    int
        The number of failed jobs.
    """
    if not job_defs:
        LOGGER.info("Nothing to encode")
        return 0

    process_count = min(job_count, len(job_defs))
    LOGGER.info(f"Encoding {len(job_defs)} videos with {process_count} parallel jobs")
    failed_count = 0
    with prevent_os_sleep():
        with worker_pool(process_count) as process_pool:
            for done_count, is_encoded in enumerate(
                process_pool.imap_unordered(run_encode_job, job_defs), start=1
            ):
                LOGGER.info(f"Progress: {done_count}/{len(job_defs)} videos")
                failed_count += not is_encoded

    summary = f"Encoded {len(job_defs) - failed_count} videos, {failed_count} failed"
    if failed_count:
        LOGGER.warning(summary)
    else:
        LOGGER.info(summary, extra=terminal.SUCCESS)
    return failed_count


def encode_trips(
    raw_video_dir: Path,
    library_dir: Path,
    config: Config,
    min_trip_gap_hours: float,
    job_count: int,
    dry_run: bool,
    check_readability: bool,
) -> int:
    """
    Encode all videos in the raw video directory, one output video per trip.

    Returns
    -------
    int
        The number of failed jobs.
    """
    segments = collect_raw_video_segments(
        raw_video_dir=raw_video_dir,
        ffmpeg_executable=config.ffmpeg_executable,
        check_readability=check_readability and not dry_run,
    )
    trips = group_segments_into_trips(segments, min_trip_gap_hours)

    LOGGER.info(f"Discovered {len(trips)} trips")
    for trip in trips:
        LOGGER.info(f"  {trip}")

    segment_groups = [(trip.get_placeholder_name(), trip.raw_segments) for trip in trips]
    job_defs = plan_encode_jobs(segment_groups, library_dir, config)

    if dry_run:
        LOGGER.warning("Dry run mode enabled. Not running any encoding jobs.")
        return 0

    return run_encode_jobs(job_defs, job_count)


def encode_range(
    raw_video_dir: Path,
    library_dir: Path,
    config: Config,
    start_index: int,
    end_index: int,
    output_name: str | None,
    dry_run: bool,
    check_readability: bool,
) -> int:
    """
    Encode the raw videos in the specified index range as a single output video.

    Returns
    -------
    int
        The number of failed jobs.
    """
    segments = collect_raw_video_segments(
        raw_video_dir=raw_video_dir,
        ffmpeg_executable=config.ffmpeg_executable,
        start_index=start_index,
        end_index=end_index,
        check_readability=check_readability and not dry_run,
    )
    if output_name is None:
        output_name = get_placeholder_video_name(segments[0].start_time)
    LOGGER.info(f"Output video: {output_name} ({len(segments)} files)")

    job_defs = plan_encode_jobs([(output_name, segments)], library_dir, config)

    if dry_run:
        LOGGER.warning("Dry run mode enabled. Not running any encoding jobs.")
        return 0

    return run_encode_jobs(job_defs, job_count=1)
