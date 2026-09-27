"""
Extract GPS tracks from the overlay of trip videos.

For every video in the library directory:

* A video whose track is up to date is skipped. The size and modification time are compared
  first, and the content fingerprint only when they differ.
* A video without a track of the same name is fingerprinted. If an existing track has the same
  fingerprint, the video was renamed: the track (and its preview) are renamed, with no OCR.
* A video whose content changed gets a new track. The old one is moved to the trash.
* Otherwise, the video is probed for the overlay and, if it has one, fully decoded and read.

Each track is written as soon as its video is done, so an interrupted run resumes where it
stopped.
"""

import contextlib
import dataclasses
import datetime
import fnmatch
import logging
import os
import shlex
import subprocess
import time
import traceback
from pathlib import Path

import cv2

from dashcam import cleaning
from dashcam import metadata
from dashcam import overlay
from dashcam import terminal
from dashcam import video
from dashcam.config import Config
from dashcam.system import prevent_os_sleep
from dashcam.system import worker_pool

# Frames per second read from each video. The overlay changes once per second, so two samples
# per second see every value at least once.
SAMPLE_FPS = 2

# Overlay probe: how many frames to check, and how many must show a readable camera clock.
PROBE_FRAME_COUNT = 10
PROBE_MIN_CLOCK_COUNT = 3

# How often (in seconds) to log the progress of reading a video or making its preview. Several
# videos are extracted in parallel, so this is longer than for encoding.
PROGRESS_INTERVAL_S = 30

# Preview encoding for browsers that cannot play HEVC.
PREVIEW_VIDEO_OPTIONS = "-vf scale=-2:480 -c:v libx264 -preset veryfast -crf 28"
PREVIEW_AUDIO_OPTIONS = "-c:a aac -b:a 64k"

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ExtractJobDefinition:
    """Parameters for extracting one video in the parallel pool."""

    video_path: Path
    fingerprint: str
    metadata_dir: Path
    config: Config
    make_preview: bool

    # Manual overrides kept from the previous track of the same content.
    overrides: cleaning.Overrides = dataclasses.field(default_factory=cleaning.Overrides)

    # Street data kept from the previous track of the same content. `enrich` updates it if the
    # new samples differ.
    streets: list[dict] = dataclasses.field(default_factory=list)
    localities: dict[str, dict | None] = dataclasses.field(default_factory=dict)
    enrichment: metadata.Enrichment | None = None


@dataclasses.dataclass(frozen=True)
class PreviewJobDefinition:
    """Parameters for making the missing preview of an extracted video in the parallel pool."""

    video_path: Path
    metadata_dir: Path
    config: Config


def filter_videos(video_paths: list[Path], include: list[str], exclude: list[str]) -> list[Path]:
    """Keep the videos whose names match the include and exclude patterns."""
    return [
        path
        for path in video_paths
        if (not include or any(fnmatch.fnmatch(path.name, pattern) for pattern in include))
        and not any(fnmatch.fnmatch(path.name, pattern) for pattern in exclude)
    ]


def skip_too_long_names(video_paths: list[Path]) -> list[Path]:
    """Report and drop the videos whose names do not fit the filename length limit."""
    kept_paths = []
    for path in video_paths:
        problem = metadata.get_filename_length_problem(path.name)
        if problem is None:
            kept_paths.append(path)
        else:
            LOGGER.error(f"Skipping '{path.name}': the name is too long, {problem}")
    return kept_paths


def collapse_readings(
    readings: list[tuple[float, overlay.OverlayReading]],
) -> list[cleaning.RawSample]:
    """
    Reduce frame readings to one sample per camera clock tick.

    Consecutive readings with the same clock text are merged. The sample is placed at the first
    frame showing the clock value and takes the last reliable GPS reading of the tick, since the
    GPS text may update slightly after the clock.
    """
    raw_samples = []
    group: list[tuple[float, overlay.OverlayReading]] = []

    def flush_group() -> None:
        first_offset_s = group[0][0]
        reliable_readings = [reading for _, reading in group if reading.is_gps_text_reliable]
        chosen = reliable_readings[-1] if reliable_readings else group[-1][1]
        raw_samples.append(
            cleaning.RawSample(
                t=first_offset_s,
                gps_text=chosen.gps_text,
                clock_text=chosen.clock_text,
                gps_score=chosen.gps_min_score,
                clock_score=chosen.clock_min_score,
            )
        )

    for offset_s, reading in readings:
        if group and group[-1][1].clock_text != reading.clock_text:
            flush_group()
            group = []
        group.append((offset_s, reading))
    if group:
        flush_group()
    return raw_samples


def probe_overlay(video_path: Path, video_info: video.VideoInfo, config: Config) -> bool:
    """
    Check a few frames spread over the video for a readable camera clock.

    Raises
    ------
    RuntimeError
        If no clock was found but some frames could not be decoded, so the video may still have
        the overlay.
    """
    if video_info.width < overlay.MIN_FRAME_WIDTH:
        LOGGER.warning(
            f"{video_path.name}: the video is {video_info.width} pixels wide, but the overlay "
            f"can only be read at {overlay.MIN_FRAME_WIDTH} pixels or more"
        )
        return False

    clock_count = 0
    missing_frame_count = 0
    for frame_number in range(PROBE_FRAME_COUNT):
        start_s = video_info.duration_s * (frame_number + 0.5) / PROBE_FRAME_COUNT
        strips = video.iter_overlay_strips(
            video_path,
            width=video_info.width,
            sample_fps=SAMPLE_FPS,
            ffmpeg_executable=config.ffmpeg_executable,
            hwaccel_options=config.hwaccel_options,
            start_s=start_s,
            duration_s=1 / SAMPLE_FPS,
        )
        first_frame = next(strips, None)
        strips.close()
        if first_frame is None:
            missing_frame_count += 1
            continue
        _, strip = first_frame
        if overlay.read_overlay(strip).is_clock_text_reliable:
            clock_count += 1
        if clock_count >= PROBE_MIN_CLOCK_COUNT:
            return True

    if missing_frame_count > 0:
        raise RuntimeError(
            f"ffmpeg returned no frame at {missing_frame_count} of {PROBE_FRAME_COUNT} probe "
            f"points, so the overlay could not be checked"
        )
    return False


def read_all_frames(
    video_path: Path,
    video_info: video.VideoInfo,
    config: Config,
) -> list[tuple[float, overlay.OverlayReading]]:
    """Decode the whole video and read the overlay of every sampled frame."""
    readings = []
    progress_logger = terminal.ProgressLogger(
        LOGGER, video_path.name, video_info.duration_s, "read", PROGRESS_INTERVAL_S
    )
    strips = video.iter_overlay_strips(
        video_path,
        width=video_info.width,
        sample_fps=SAMPLE_FPS,
        ffmpeg_executable=config.ffmpeg_executable,
        hwaccel_options=config.hwaccel_options,
    )
    with contextlib.closing(strips):
        for offset_s, strip in strips:
            readings.append((offset_s, overlay.read_overlay(strip)))
            progress_logger.update(offset_s)
    return readings


def make_preview(
    video_path: Path, preview_path: Path, config: Config, duration_s: float | None
) -> None:
    """Encode a low-resolution H.264 preview of the video, logging the progress."""
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = metadata.get_partial_path(preview_path)
    cmd = [
        config.ffmpeg_executable,
        "-nostdin",
        # Report errors only, and the progress as `key=value` lines on stdout.
        *["-v", "error", "-nostats", "-progress", "pipe:1"],
        *video.split_options(config.hwaccel_options),
        *["-i", str(video_path)],
        *video.split_options(PREVIEW_VIDEO_OPTIONS),
        *video.split_options(PREVIEW_AUDIO_OPTIONS),
        *["-movflags", "+faststart", "-f", "mp4", "-y", str(partial_path)],
    ]
    LOGGER.info(shlex.join(cmd), extra=terminal.COMMAND)
    progress_logger = terminal.ProgressLogger(
        LOGGER, f"{video_path.name} (preview)", duration_s, "encoded", PROGRESS_INTERVAL_S
    )
    try:
        with contextlib.closing(video.iter_ffmpeg_progress(cmd)) as progress_reports:
            for progress in progress_reports:
                progress_logger.update(progress.output_time_s, progress.speed)
        # Replaces an older preview, which a plain rename refuses to do on Windows.
        os.replace(partial_path, preview_path)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"ffmpeg failed to make the preview (exit code {exc.returncode})\n"
            f"{video.get_error_tail(exc.stderr or '')}"
        ) from exc
    finally:
        # Left behind by a failure or an interruption. A saved preview no longer has this name.
        partial_path.unlink(missing_ok=True)


def clean_raw_samples(
    raw_samples: list[cleaning.RawSample],
    video_filename: str,
    overrides: cleaning.Overrides,
    config: Config,
) -> list[cleaning.CleanSample]:
    """Clean raw samples with the settings for this video."""
    trip_date = metadata.parse_trip_name(video_filename).date
    settings = cleaning.CleaningSettings.from_config(config, trip_date)
    return cleaning.clean_track(raw_samples, settings, overrides)


def extract_video(job_def: ExtractJobDefinition) -> metadata.Track:
    """Probe, decode, read, and clean one video."""
    video_path = job_def.video_path
    config = job_def.config
    video_info = video.probe_video(video_path, config.ffprobe_executable)
    stat = video_path.stat()

    has_overlay = probe_overlay(video_path, video_info, config)
    raw_samples: list[cleaning.RawSample] = []
    clean_samples: list[cleaning.CleanSample] = []
    if has_overlay:
        readings = read_all_frames(video_path, video_info, config)
        raw_samples = collapse_readings(readings)
        clean_samples = clean_raw_samples(raw_samples, video_path.name, job_def.overrides, config)

    return metadata.Track(
        video_filename=video_path.name,
        fingerprint=job_def.fingerprint,
        video_size=stat.st_size,
        video_mtime=stat.st_mtime,
        extraction_status=metadata.EXTRACTION_OK if has_overlay else metadata.EXTRACTION_NO_OVERLAY,
        extracted_at=datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        extractor_version=metadata.EXTRACTOR_VERSION,
        cleaning_version=metadata.CLEANING_VERSION,
        video_width=video_info.width,
        video_height=video_info.height,
        duration_s=video_info.duration_s,
        overrides=job_def.overrides,
        streets=job_def.streets,
        localities=job_def.localities,
        enrichment=job_def.enrichment,
        raw_samples=raw_samples,
        clean_samples=clean_samples,
    )


def run_extract_job(job_def: ExtractJobDefinition | PreviewJobDefinition) -> str | None:
    """
    Extract one video and save its track, or make the missing preview of an extracted video.

    Errors are reported, not raised.

    Returns
    -------
    str | None
        The error message, or None if the job succeeded.
    """
    # Each job decodes with its own ffmpeg process, so OpenCV must not add more threads.
    cv2.setNumThreads(1)
    started_at = time.monotonic()
    video_filename = job_def.video_path.name
    store = metadata.MetadataStore(job_def.metadata_dir)
    try:
        if isinstance(job_def, ExtractJobDefinition):
            LOGGER.info(f"Extracting: {video_filename}")
            track = extract_video(job_def)
            store.save_track(track)
            log_extracted_track(track, time.monotonic() - started_at)
            needs_preview = job_def.make_preview
        else:
            track = store.load_track(job_def.video_path.stem)
            needs_preview = True
        if needs_preview and track.extraction_status == metadata.EXTRACTION_OK:
            LOGGER.info(f"Making preview: {video_filename}")
            make_preview(
                job_def.video_path, store.preview_path(track.stem), job_def.config, track.duration_s
            )
            LOGGER.info(f"Preview made: {video_filename}", extra=terminal.SUCCESS)
    except (OSError, RuntimeError, subprocess.CalledProcessError, metadata.TrackFormatError) as exc:
        LOGGER.error(f"Extraction failed for '{video_filename}': {exc}")
        return str(exc)
    except Exception as exc:
        LOGGER.error(
            f"Extraction failed for '{video_filename}' with an unexpected error\n"
            f"{traceback.format_exc()}"
        )
        return repr(exc)
    return None


def log_extracted_track(track: metadata.Track, elapsed_s: float) -> None:
    """Log the outcome of an extraction, with how many samples got each status."""
    status_counts: dict[str, int] = {}
    for sample in track.clean_samples:
        status_counts[sample.status] = status_counts.get(sample.status, 0) + 1
    LOGGER.info(
        f"Extracted: {track.video_filename} ({track.extraction_status}, {elapsed_s:.0f} s) "
        f"{status_counts}",
        extra=terminal.SUCCESS,
    )


@dataclasses.dataclass(frozen=True)
class PlannedExtraction:
    """A video to extract, with its fingerprint and the previous track of the same content."""

    video_path: Path
    fingerprint: str
    previous_track: metadata.Track | None = None


def plan_extraction(
    video_paths: list[Path],
    store: metadata.MetadataStore,
    force: bool,
    library_stems: set[str] | None = None,
) -> list[PlannedExtraction]:
    """
    Reconcile videos with existing tracks and decide which videos to extract.

    Renamed videos get their tracks renamed. Changed videos get their old tracks moved to the
    trash. A track is only taken over by a video with the same content if its own video is not
    among `library_stems`, the stems of all videos in the library (by default, of `video_paths`).
    Videos whose track file cannot be read, or declares another video, are skipped, even with
    `force`, so that a broken hand edit is never overwritten.

    Returns
    -------
    list[PlannedExtraction]
        The videos to extract.
    """
    tracks_by_stem = store.load_tracks_by_stem()
    skipped_stems = {path.stem for path in store.list_track_paths()} - tracks_by_stem.keys()
    present_stems = library_stems if library_stems is not None else {p.stem for p in video_paths}
    stems_by_fingerprint = {track.fingerprint: stem for stem, track in tracks_by_stem.items()}

    to_extract = []
    progress_logger = terminal.ItemProgressLogger(LOGGER, "Checking videos", len(video_paths))
    for checked_count, video_path in enumerate(video_paths, start=1):
        progress_logger.update(checked_count - 1)
        if video_path.stem in skipped_stems:
            LOGGER.warning(
                f"Not extracting '{video_path.name}', so that its unreadable or misnamed track "
                f"file is not overwritten (see `dashcam doctor`)"
            )
            continue
        stat = video_path.stat()
        track = tracks_by_stem.get(video_path.stem)

        if track is not None:
            is_unchanged = track.video_size == stat.st_size and track.video_mtime == stat.st_mtime
            if is_unchanged and not force:
                continue
            fingerprint = video.compute_fingerprint(video_path)
            if fingerprint == track.fingerprint and not force:
                # Same content with a new modification time (e.g. copied to another drive).
                track.video_mtime = stat.st_mtime
                store.save_track(track)
                continue
            if fingerprint != track.fingerprint:
                LOGGER.info(f"Content changed: {video_path.name} (old track moved to trash)")
                store.move_to_trash(video_path.stem)
                to_extract.append(PlannedExtraction(video_path, fingerprint))
            else:
                to_extract.append(PlannedExtraction(video_path, fingerprint, track))
            continue

        fingerprint = video.compute_fingerprint(video_path)
        old_stem = stems_by_fingerprint.get(fingerprint)
        if old_stem is not None and old_stem not in present_stems:
            LOGGER.info(
                f"Renamed: {old_stem} {terminal.ARROW} {video_path.stem} (track renamed, no OCR)"
            )
            store.rename_trip(old_stem, video_path.name)
            tracks_by_stem[video_path.stem] = tracks_by_stem.pop(old_stem)
            stems_by_fingerprint[fingerprint] = video_path.stem
            continue
        if old_stem is not None:
            LOGGER.warning(
                f"Duplicate: '{video_path.name}' has the same content as '{old_stem}'; skipping"
            )
            continue
        to_extract.append(PlannedExtraction(video_path, fingerprint))

    return to_extract


def make_extract_job(
    planned: PlannedExtraction, metadata_dir: Path, config: Config, make_preview: bool
) -> ExtractJobDefinition:
    """Define the job for a planned extraction, keeping what the previous track had."""
    previous_track = planned.previous_track
    if previous_track is None:
        return ExtractJobDefinition(
            video_path=planned.video_path,
            fingerprint=planned.fingerprint,
            metadata_dir=metadata_dir,
            config=config,
            make_preview=make_preview,
        )
    return ExtractJobDefinition(
        video_path=planned.video_path,
        fingerprint=planned.fingerprint,
        metadata_dir=metadata_dir,
        config=config,
        make_preview=make_preview,
        overrides=previous_track.overrides,
        streets=previous_track.streets,
        localities=previous_track.localities,
        enrichment=previous_track.enrichment,
    )


def extract_videos(
    library_dir: Path,
    metadata_dir: Path,
    config: Config,
    include: list[str],
    exclude: list[str],
    only: list[str],
    force: bool,
    make_previews: bool,
    job_count: int,
) -> int:
    """
    Extract tracks for new and changed videos, then rebuild the index.

    Returns
    -------
    int
        The number of failed videos.
    """
    store = metadata.MetadataStore(metadata_dir)
    store.ensure_dirs()
    library_video_paths = metadata.find_videos(library_dir)
    video_paths = filter_videos(library_video_paths, include, exclude)
    if only:
        only_names = {Path(name).name for name in only}
        video_paths = [path for path in video_paths if path.name in only_names]
        missing_names = only_names - {path.name for path in video_paths}
        for name in sorted(missing_names):
            LOGGER.warning(f"Video not found in '{library_dir}': {name}")
        force = True
    LOGGER.info(f"Found {len(video_paths)} videos in '{library_dir}'")
    found_count = len(video_paths)
    video_paths = skip_too_long_names(video_paths)
    skipped_count = found_count - len(video_paths)

    library_stems = {path.stem for path in library_video_paths}
    to_extract = plan_extraction(video_paths, store, force, library_stems)
    job_defs: list[ExtractJobDefinition | PreviewJobDefinition] = [
        make_extract_job(planned, metadata_dir, config, make_previews) for planned in to_extract
    ]
    if make_previews:
        extracted_paths = {planned.video_path for planned in to_extract}
        remaining_paths = [path for path in video_paths if path not in extracted_paths]
        job_defs += plan_missing_previews(remaining_paths, store, config)

    failed_count = skipped_count
    if job_defs:
        process_count = min(job_count, len(job_defs))
        LOGGER.info(f"Processing {len(job_defs)} videos with {process_count} parallel jobs")
        with prevent_os_sleep():
            with worker_pool(process_count) as process_pool:
                for done_count, error in enumerate(
                    process_pool.imap_unordered(run_extract_job, job_defs), start=1
                ):
                    LOGGER.info(f"Progress: {done_count}/{len(job_defs)} videos")
                    failed_count += error is not None
    else:
        LOGGER.info("All tracks are up to date", extra=terminal.SUCCESS)

    index = store.rebuild_index()
    LOGGER.info(f"Index: {len(index['trips'])} trips in '{store.index_path}'")
    return failed_count


def plan_missing_previews(
    video_paths: list[Path],
    store: metadata.MetadataStore,
    config: Config,
) -> list[PreviewJobDefinition]:
    """
    Plan preview-only jobs for extracted videos that do not have a preview yet.

    Videos without an overlay get no preview, and unreadable tracks are skipped (the planning
    of the extraction reports them).
    """
    job_defs = []
    for video_path in video_paths:
        track_path = store.track_path(video_path.stem)
        if not track_path.exists() or store.preview_path(video_path.stem).exists():
            continue
        try:
            track = store.load_track(video_path.stem)
        except (OSError, metadata.TrackFormatError):
            continue
        if track.extraction_status != metadata.EXTRACTION_OK:
            continue
        job_defs.append(
            PreviewJobDefinition(video_path=video_path, metadata_dir=store.root, config=config)
        )
    return job_defs


def reclean_tracks(metadata_dir: Path, config: Config) -> int:
    """
    Re-run cleaning on the stored raw readings of all tracks, then rebuild the index.

    Returns
    -------
    int
        The number of tracks that could not be recleaned.
    """
    store = metadata.MetadataStore(metadata_dir)
    failed_count = 0
    for track_file in store.iter_track_files():
        track = track_file.track
        if track is None:
            LOGGER.error(f"Cannot reclean '{track_file.path.name}': {track_file.error}")
            failed_count += 1
            continue
        # Saving it would overwrite the track file of the video it declares.
        if track_file.is_misnamed:
            LOGGER.warning(track_file.get_skip_message())
            continue
        if track.extraction_status != metadata.EXTRACTION_OK:
            continue
        track.clean_samples = clean_raw_samples(
            track.raw_samples, track.video_filename, track.overrides, config
        )
        track.cleaning_version = metadata.CLEANING_VERSION
        store.save_track(track)
        LOGGER.info(f"Recleaned: {track.video_filename}", extra=terminal.SUCCESS)

    index = store.rebuild_index()
    LOGGER.info(f"Index: {len(index['trips'])} trips in '{store.index_path}'")
    return failed_count
