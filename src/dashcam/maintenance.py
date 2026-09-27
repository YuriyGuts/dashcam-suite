"""Inspect and repair the metadata directory: `status`, `forget`, and `doctor`."""

import dataclasses
import json
import logging
import time
import typing as t
from pathlib import Path

from dashcam import cleaning
from dashcam import enrich
from dashcam import metadata
from dashcam import osm
from dashcam import terminal
from dashcam import video

# Severity levels of doctor findings.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# GPS coverage (in percent) from which `status` shows a trip in green, or in yellow.
GOOD_COVERAGE_PERCENT = 90
FAIR_COVERAGE_PERCENT = 50

# Temporary files modified more recently than this (in seconds) may belong to a running job, so
# they are not deleted.
MIN_LEFTOVER_AGE_S = 3600

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class Finding:
    """A problem found by `doctor`, with an optional automatic fix."""

    severity: str
    message: str
    fix_description: str | None = None
    fix: t.Callable[[], None] | None = None


@dataclasses.dataclass
class LibraryScan:
    """Videos and tracks, matched to each other by name and fingerprint."""

    tracks_by_stem: dict[str, metadata.Track]
    unreadable_tracks: dict[str, str]
    video_paths_by_stem: dict[str, Path]
    fingerprints_by_stem: dict[str, str]

    # Groups of videos whose names differ only in the extension or the letter case.
    videos_sharing_a_track: list[list[Path]] = dataclasses.field(default_factory=list)

    def get_video_fingerprint(self, stem: str) -> str:
        """Fingerprint a video in the directory, caching the result."""
        if stem not in self.fingerprints_by_stem:
            self.fingerprints_by_stem[stem] = video.compute_fingerprint(
                self.video_paths_by_stem[stem]
            )
        return self.fingerprints_by_stem[stem]


def scan_library(library_dir: Path, store: metadata.MetadataStore) -> LibraryScan:
    """Load all tracks and list all videos."""
    tracks_by_stem = {}
    unreadable_tracks = {}
    for track_file in store.iter_track_files():
        if track_file.track is None:
            unreadable_tracks[track_file.path.stem] = str(track_file.error)
        else:
            tracks_by_stem[track_file.path.stem] = track_file.track

    video_paths = metadata.find_videos(library_dir) if library_dir.is_dir() else []
    return LibraryScan(
        tracks_by_stem=tracks_by_stem,
        unreadable_tracks=unreadable_tracks,
        video_paths_by_stem={path.stem: path for path in video_paths},
        fingerprints_by_stem={},
        videos_sharing_a_track=metadata.find_videos_sharing_a_track(video_paths),
    )


def format_duration(duration_s: float | None) -> str:
    """Format a duration as `H:MM:SS`."""
    if duration_s is None:
        return "-"
    total_s = int(duration_s)
    return f"{total_s // 3600}:{total_s % 3600 // 60:02d}:{total_s % 60:02d}"


def get_coverage_style(coverage_percent: float) -> str:
    """Return the color of a GPS coverage value in `status`."""
    if coverage_percent >= GOOD_COVERAGE_PERCENT:
        return "green"
    if coverage_percent >= FAIR_COVERAGE_PERCENT:
        return "yellow"
    return "red"


def print_section_title(title: str, count: int) -> None:
    """Print a bold section title with a dim item count."""
    terminal.print_line((title, "bold"), (f" ({count})", "dim"))


def print_status(library_dir: Path, metadata_dir: Path) -> None:
    """Print the trips, unprocessed videos, videos without an overlay, and unreachable tracks."""
    store = metadata.MetadataStore(metadata_dir)
    scan = scan_library(library_dir, store)

    trip_rows = []
    no_overlay_names = []
    unreachable_names = []
    for stem, track in sorted(scan.tracks_by_stem.items()):
        if stem not in scan.video_paths_by_stem:
            unreachable_names.append(track.video_filename)
        if track.extraction_status == metadata.EXTRACTION_NO_OVERLAY:
            no_overlay_names.append(track.video_filename)
            continue
        stats = metadata.compute_trip_stats(track)
        coverage_percent = stats["coverage"] * 100
        trip_rows.append(
            (
                f"  {stem:<60} ",
                (f"{format_duration(stats['duration_s']):>8} ", "dim"),
                (f"{stats['distance_km']:>7.1f} km ", "dim"),
                (f"{coverage_percent:>4.0f}%", get_coverage_style(coverage_percent)),
                (" GPS", "dim"),
            )
        )

    unprocessed_names = sorted(
        path.name
        for stem, path in scan.video_paths_by_stem.items()
        if stem not in scan.tracks_by_stem and stem not in scan.unreadable_tracks
    )

    print_section_title("Trips", len(trip_rows))
    for row in trip_rows:
        terminal.print_line(*row)
    if not trip_rows:
        terminal.print_line(("  (none)", "dim"))
    sections = [
        ("Videos not extracted yet (run `dashcam extract`)", unprocessed_names),
        ("Videos without an overlay (skipped)", no_overlay_names),
        (f"Tracks whose video is not in '{library_dir}'", unreachable_names),
        ("Unreadable tracks (run `dashcam doctor`)", sorted(scan.unreadable_tracks)),
    ]
    for title, names in sections:
        if names:
            terminal.print_line()
            print_section_title(title, len(names))
            for name in names:
                terminal.print_line(f"  {name}")


def resolve_stem(name: str) -> str:
    """Accept a video filename, a track filename, or a stem, and return the stem."""
    path = Path(name)
    if path.suffix.lower() in (*metadata.VIDEO_EXTENSIONS, ".json"):
        return path.stem
    return path.name


def forget_trips(names: list[str], metadata_dir: Path) -> int:
    """
    Move the tracks and previews of the given videos to the trash and rebuild the index.

    Returns
    -------
    int
        The number of names without a track.
    """
    store = metadata.MetadataStore(metadata_dir)
    missing_count = 0
    for name in names:
        stem = resolve_stem(name)
        moved_paths = store.move_to_trash(stem)
        if not moved_paths:
            LOGGER.error(f"No track found for '{name}'")
            missing_count += 1
            continue
        for moved_path in moved_paths:
            LOGGER.info(f"Moved to trash: {moved_path}")
    store.rebuild_index()
    return missing_count


def check_tracks(scan: LibraryScan, store: metadata.MetadataStore) -> list[Finding]:
    """Check track contents: format, names, versions, street lists, and suspicious results."""
    findings = []
    osm_data = osm.read_database_version(store.root)
    for stem, error in sorted(scan.unreadable_tracks.items()):
        findings.append(Finding(SEVERITY_ERROR, f"Track '{stem}.json' is unreadable: {error}"))

    for stem, track in sorted(scan.tracks_by_stem.items()):
        if track.stem != stem:
            target_path = store.track_path(track.stem)

            def rename_track_file(stem: str = stem, target_path: Path = target_path) -> None:
                metadata.rename_without_overwrite(store.track_path(stem), target_path)

            findings.append(
                Finding(
                    SEVERITY_ERROR,
                    f"Track '{stem}.json' belongs to '{track.video_filename}'",
                    fix_description=f"rename it to '{target_path.name}'",
                    fix=None if target_path.exists() else rename_track_file,
                )
            )

        is_extracted = track.extraction_status == metadata.EXTRACTION_OK
        if track.extractor_version < metadata.EXTRACTOR_VERSION:
            findings.append(
                Finding(
                    SEVERITY_INFO,
                    f"Track '{stem}' was made by an older extractor "
                    f"(run `dashcam extract --only '{track.video_filename}'`)",
                )
            )
        # Videos without an overlay have no readings to clean.
        elif is_extracted and track.cleaning_version < metadata.CLEANING_VERSION:
            findings.append(
                Finding(
                    SEVERITY_INFO,
                    f"Track '{stem}' was cleaned by an older version "
                    f"(run `dashcam extract --reclean`)",
                )
            )

        # Street lists are only expected once there is OSM data to make them from.
        has_outdated_streets = (
            is_extracted
            and osm_data is not None
            and not enrich.is_enrichment_current(track, osm_data)
        )
        if has_outdated_streets:
            reason = "no street list" if track.enrichment is None else "an outdated street list"
            findings.append(
                Finding(SEVERITY_INFO, f"Track '{stem}' has {reason} (run `dashcam enrich`)")
            )

        has_gps_text = any(sample.gps_text for sample in track.raw_samples)
        has_good_fix = any(sample.status == cleaning.STATUS_OK for sample in track.clean_samples)
        if has_gps_text and not has_good_fix:
            findings.append(
                Finding(
                    SEVERITY_WARNING,
                    f"Track '{stem}' has GPS text but no accepted fix. If the camera clock "
                    f"disagrees with the date in the filename, check the filename",
                )
            )

    stems_by_fingerprint: dict[str, list[str]] = {}
    for stem, track in scan.tracks_by_stem.items():
        stems_by_fingerprint.setdefault(track.fingerprint, []).append(stem)
    for fingerprint, stems in stems_by_fingerprint.items():
        if len(stems) > 1:
            findings.append(
                Finding(
                    SEVERITY_WARNING,
                    f"Tracks share the same video content ({fingerprint}): "
                    f"{', '.join(sorted(stems))}",
                )
            )
    return findings


def check_videos(
    scan: LibraryScan, store: metadata.MetadataStore, library_dir: Path
) -> list[Finding]:
    """Match videos and tracks: missing tracks, renames, changed content, unreachable videos."""
    findings = []
    stems_by_fingerprint = {track.fingerprint: stem for stem, track in scan.tracks_by_stem.items()}

    # Tracks are matched to videos by the video they declare, so a track file with the wrong
    # name (reported by `check_tracks`) is not mistaken for a renamed video.
    declared_stems = {track.stem for track in scan.tracks_by_stem.values()}
    orphan_track_stems = {
        stem
        for stem, track in scan.tracks_by_stem.items()
        if track.stem not in scan.video_paths_by_stem
    }
    renamed_track_stems = set()

    # `extract` skips these videos, so their tracks are not checked against them.
    conflicting_stems = set()
    for paths in scan.videos_sharing_a_track:
        conflicting_stems.update(path.stem for path in paths)
        names = ", ".join(f"'{path.name}'" for path in paths)
        findings.append(
            Finding(SEVERITY_ERROR, f"Videos {names} would share one track (rename all but one)")
        )

    progress_logger = terminal.ItemProgressLogger(
        LOGGER, "Checking videos", len(scan.video_paths_by_stem)
    )
    for checked_count, (stem, video_path) in enumerate(
        sorted(scan.video_paths_by_stem.items()), start=1
    ):
        progress_logger.update(checked_count - 1)
        if stem in conflicting_stems:
            continue
        length_problem = metadata.get_filename_length_problem(video_path.name)
        if length_problem is not None:
            findings.append(
                Finding(
                    SEVERITY_ERROR,
                    f"Video '{video_path.name}' has a name that is too long: {length_problem}",
                )
            )
            continue
        # An unreadable track is reported by `check_tracks`, and `extract` leaves its video alone.
        if stem in scan.unreadable_tracks:
            continue
        if stem not in scan.tracks_by_stem and stem in declared_stems:
            continue
        track = scan.tracks_by_stem.get(stem)
        if track is not None:
            stat = video_path.stat()
            is_unchanged = track.video_size == stat.st_size and track.video_mtime == stat.st_mtime
            if not is_unchanged and scan.get_video_fingerprint(stem) != track.fingerprint:
                findings.append(
                    Finding(
                        SEVERITY_WARNING,
                        f"Video '{video_path.name}' changed since extraction "
                        f"(run `dashcam extract --only '{video_path.name}'`)",
                    )
                )
            continue

        old_stem = stems_by_fingerprint.get(scan.get_video_fingerprint(stem))
        if old_stem is not None and old_stem in orphan_track_stems:
            renamed_track_stems.add(old_stem)

            def reconnect(
                old_stem: str = old_stem,
                declared_stem: str = scan.tracks_by_stem[old_stem].stem,
                video_filename: str = video_path.name,
            ) -> None:
                # A fix of the track file name may have moved it to the stem it declares.
                track_stem = old_stem if store.track_path(old_stem).exists() else declared_stem
                store.rename_trip(track_stem, video_filename)

            findings.append(
                Finding(
                    SEVERITY_WARNING,
                    f"Video '{old_stem}' was renamed to '{stem}'",
                    fix_description="rename its track and preview",
                    fix=reconnect,
                )
            )
        elif old_stem is not None:
            findings.append(
                Finding(
                    SEVERITY_WARNING,
                    f"Video '{video_path.name}' has the same content as '{old_stem}', so "
                    f"`dashcam extract` skips it (delete one of them)",
                )
            )
        else:
            findings.append(
                Finding(
                    SEVERITY_INFO, f"Video '{video_path.name}' has no track (run `dashcam extract`)"
                )
            )

    for stem in sorted(orphan_track_stems - renamed_track_stems):
        findings.append(
            Finding(
                SEVERITY_INFO, f"Video of track '{stem}' is not in '{library_dir}' (unreachable)"
            )
        )
    return findings


def check_files(
    store: metadata.MetadataStore, library_dir: Path, tracks_by_stem: dict[str, metadata.Track]
) -> list[Finding]:
    """Check the index against the tracks, and look for orphaned previews and leftover files."""
    findings = []

    expected_trips = store.build_index_entries(tracks_by_stem)
    try:
        current_index = json.loads(store.index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current_index = None
    is_index_current = current_index is not None and current_index.get("trips") == expected_trips
    if not is_index_current:

        def rebuild_index() -> None:
            store.rebuild_index()

        findings.append(
            Finding(
                SEVERITY_WARNING,
                "The trip index is missing or out of date",
                fix_description="rebuild it",
                fix=rebuild_index,
            )
        )

    track_stems = {path.stem for path in store.list_track_paths()}
    orphan_previews = []
    if store.previews_dir.is_dir():
        orphan_previews = [
            path
            for path in store.previews_dir.glob("*.mp4")
            if not path.name.startswith(".") and path.stem not in track_stems
        ]
    osm_dir = store.root / osm.OSM_DIR_NAME
    leftover_files = [
        path
        for directory in (store.root, store.tracks_dir, store.previews_dir, osm_dir)
        if directory.is_dir()
        for path in directory.glob(".*.partial*")
    ]
    if library_dir.is_dir():
        leftover_files += library_dir.glob(metadata.PARTIAL_FILE_GLOB)
    leftover_ages_s = {path: get_age_s(path) for path in leftover_files}
    recent_files = [
        path
        for path, age_s in leftover_ages_s.items()
        if age_s is not None and age_s < MIN_LEFTOVER_AGE_S
    ]
    old_leftover_files = [
        path
        for path, age_s in leftover_ages_s.items()
        if age_s is not None and age_s >= MIN_LEFTOVER_AGE_S
    ]
    for path in sorted(recent_files):
        findings.append(
            Finding(SEVERITY_INFO, f"Recent temporary file, possibly of a running job: {path}")
        )
    for path in sorted(orphan_previews + old_leftover_files):
        findings.append(
            Finding(
                SEVERITY_WARNING,
                f"Leftover file: {path}",
                fix_description="delete it",
                fix=lambda path=path: path.unlink(missing_ok=True),
            )
        )
    return findings


def get_age_s(path: Path) -> float | None:
    """Return how long ago (in seconds) a file was modified, or None if it is gone."""
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def apply_fixes_of(findings: list[Finding]) -> list[Finding]:
    """
    Apply the fixes of the findings that have one. A fix that fails is reported and skipped.

    Returns
    -------
    list[Finding]
        The findings that were fixed.
    """
    fixed_findings = []
    for finding in findings:
        if finding.fix is None:
            continue
        try:
            finding.fix()
        except (OSError, metadata.TrackFormatError) as exc:
            LOGGER.error(f"Cannot fix: {finding.message} ({finding.fix_description}): {exc}")
            continue
        LOGGER.info(f"Fixed: {finding.message} ({finding.fix_description})", extra=terminal.SUCCESS)
        fixed_findings.append(finding)
    return fixed_findings


def run_doctor(
    library_dir: Path,
    metadata_dir: Path,
    apply_fixes: bool,
) -> int:
    """
    Check the metadata directory for problems and optionally fix the safe ones.

    Returns
    -------
    int
        The number of errors left unfixed.
    """
    store = metadata.MetadataStore(metadata_dir)
    scan = scan_library(library_dir, store)
    findings = check_tracks(scan, store) + check_videos(scan, store, library_dir)

    fixed_findings = apply_fixes_of(findings) if apply_fixes else []

    # File checks run last. After fixes, the tracks are read again, so that the index is checked
    # against the renamed tracks.
    tracks_by_stem = store.load_tracks_by_stem() if fixed_findings else scan.tracks_by_stem
    file_findings = check_files(store, library_dir, tracks_by_stem)
    if apply_fixes:
        fixed_findings += apply_fixes_of(file_findings)

    all_findings = findings + file_findings
    fixed_finding_ids = {id(finding) for finding in fixed_findings}
    unfixed_findings = [finding for finding in all_findings if id(finding) not in fixed_finding_ids]
    for finding in unfixed_findings:
        suffix = f" [fixable: {finding.fix_description}]" if finding.fix is not None else ""
        log_level = {
            SEVERITY_ERROR: logging.ERROR,
            SEVERITY_WARNING: logging.WARNING,
        }.get(finding.severity, logging.INFO)
        LOGGER.log(log_level, f"{finding.message}{suffix}")

    if not all_findings:
        LOGGER.info("No problems found", extra=terminal.SUCCESS)
    elif not apply_fixes and any(finding.fix is not None for finding in all_findings):
        LOGGER.info("Run `dashcam doctor --fix` to apply the fixable changes")
    return sum(finding.severity == SEVERITY_ERROR for finding in unfixed_findings)
