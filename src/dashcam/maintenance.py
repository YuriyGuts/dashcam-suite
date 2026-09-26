"""Inspect and repair the metadata directory: `status`, `forget`, and `doctor`."""

import dataclasses
import json
import logging
import typing as t
from pathlib import Path

from dashcam import cleaning
from dashcam import enrich
from dashcam import extract
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
    for path in store.list_track_paths():
        try:
            tracks_by_stem[path.stem] = metadata.load_track_text(path.read_text(encoding="utf-8"))
        except (OSError, metadata.TrackFormatError) as exc:
            unreadable_tracks[path.stem] = str(exc)

    video_paths = (
        extract.find_videos(library_dir, include=[], exclude=[]) if library_dir.is_dir() else []
    )
    return LibraryScan(
        tracks_by_stem=tracks_by_stem,
        unreadable_tracks=unreadable_tracks,
        video_paths_by_stem={path.stem: path for path in video_paths},
        fingerprints_by_stem={},
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
        if stem not in scan.tracks_by_stem
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
    if path.suffix.lower() in (*extract.VIDEO_EXTENSIONS, ".json"):
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
    osm_timestamp = osm.read_database_timestamp(store.root)
    for stem, error in sorted(scan.unreadable_tracks.items()):
        findings.append(Finding(SEVERITY_ERROR, f"Track '{stem}.json' is unreadable: {error}"))

    for stem, track in sorted(scan.tracks_by_stem.items()):
        if track.stem != stem:
            target_path = store.track_path(track.stem)

            def rename_track_file(stem: str = stem, target_path: Path = target_path) -> None:
                store.track_path(stem).rename(target_path)

            findings.append(
                Finding(
                    SEVERITY_ERROR,
                    f"Track '{stem}.json' belongs to '{track.video_filename}'",
                    fix_description=f"rename it to '{target_path.name}'",
                    fix=None if target_path.exists() else rename_track_file,
                )
            )

        is_outdated = (
            track.extractor_version < metadata.EXTRACTOR_VERSION
            or track.cleaning_version < metadata.CLEANING_VERSION
        )
        if is_outdated:
            findings.append(
                Finding(
                    SEVERITY_INFO,
                    f"Track '{stem}' was made by an older version "
                    f"(run `dashcam extract --reclean`, or `--only` to re-extract)",
                )
            )

        # Street lists are only expected once there is OSM data to make them from.
        is_extracted = track.extraction_status == metadata.EXTRACTION_OK
        has_outdated_streets = (
            is_extracted
            and osm_timestamp is not None
            and not enrich.is_enrichment_current(track, osm_timestamp)
        )
        if has_outdated_streets:
            reason = "no street list" if track.enrichment is None else "an outdated street list"
            findings.append(
                Finding(SEVERITY_INFO, f"Track '{stem}' has {reason} (run `dashcam enrich`)")
            )

        has_gps_text = any(sample.left_text for sample in track.raw_samples)
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

    for stem, video_path in sorted(scan.video_paths_by_stem.items()):
        length_problem = metadata.get_filename_length_problem(video_path.name)
        if length_problem is not None:
            findings.append(
                Finding(
                    SEVERITY_ERROR,
                    f"Video '{video_path.name}' has a name that is too long: {length_problem}",
                )
            )
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
        if old_stem in orphan_track_stems:
            renamed_track_stems.add(old_stem)

            def reconnect(old_stem: str = old_stem, video_filename: str = video_path.name) -> None:
                store.rename_trip(old_stem, video_filename)

            findings.append(
                Finding(
                    SEVERITY_WARNING,
                    f"Video '{old_stem}' was renamed to '{stem}'",
                    fix_description="rename its track and preview",
                    fix=reconnect,
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


def check_files(store: metadata.MetadataStore, library_dir: Path) -> list[Finding]:
    """Check the index, previews, and leftover temporary files."""
    findings = []

    expected_index = store.build_index()
    try:
        current_index = json.loads(store.index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current_index = None
    is_index_current = (
        current_index is not None and current_index.get("trips") == expected_index["trips"]
    )
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
    for path in sorted(orphan_previews + leftover_files):
        findings.append(
            Finding(
                SEVERITY_WARNING,
                f"Leftover file: {path}",
                fix_description="delete it",
                fix=lambda path=path: path.unlink(missing_ok=True),
            )
        )
    return findings


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

    fixable_findings = [finding for finding in findings if finding.fix is not None]
    if apply_fixes:
        for finding in fixable_findings:
            assert finding.fix is not None
            finding.fix()
            LOGGER.info(
                f"Fixed: {finding.message} ({finding.fix_description})", extra=terminal.SUCCESS
            )

    # File checks run last, so that the index reflects any renames made above.
    file_findings = check_files(store, library_dir)
    if apply_fixes:
        for finding in file_findings:
            if finding.fix is not None:
                finding.fix()
                LOGGER.info(
                    f"Fixed: {finding.message} ({finding.fix_description})",
                    extra=terminal.SUCCESS,
                )

    all_findings = findings + file_findings
    unfixed_findings = [
        finding for finding in all_findings if not (apply_fixes and finding.fix is not None)
    ]
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
