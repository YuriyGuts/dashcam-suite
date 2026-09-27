"""
Store extracted tracks and the trip index.

Layout of the metadata directory:

    index.json                  Summary of all trips, rebuilt from the tracks. Never edit.
    geometry.json               Simplified routes of all trips, rebuilt with the index.
    encoded_segments.json       Raw videos already encoded (see `dashcam.encode`).
    tracks/<video stem>.json    One track per video. Hand-editable.
    previews/<video stem>.mp4   Optional low-resolution previews for browsers without HEVC.
    osm/                        Filtered OSM roads and localities (see `dashcam.osm`).
    trash/<timestamp>/          Replaced or forgotten tracks and previews.

A track file has a pretty-printed header and one sample per line, so it can be read, edited,
and diffed in a text editor. The video filename is the only source of truth for the trip name
and date. The content fingerprint reconnects a track to its video after a rename.
"""

import dataclasses
import datetime
import json
import logging
import math
import os
import re
import secrets
import shutil
import typing as t
from pathlib import Path

from dashcam import cleaning
from dashcam import geo
from dashcam import terminal

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)

# Versions of the extraction (OCR) and cleaning algorithms. Tracks made by older versions can
# be updated with `extract --force` (extraction) or `extract --reclean` (cleaning).
EXTRACTOR_VERSION = 1
CLEANING_VERSION = 2

# Extraction statuses of a track.
EXTRACTION_OK = "ok"
EXTRACTION_NO_OVERLAY = "no_overlay"

# Version of the index and geometry files. An index of another version is rebuilt.
INDEX_FORMAT_VERSION = 3

# Simplified routes: points closer than this to the previous kept point are dropped, points
# that deviate less than the tolerance from the simplified line are dropped too, and the kept
# coordinates are rounded to this many decimals (~1 m).
GEOMETRY_MIN_STEP_M = 10.0
GEOMETRY_TOLERANCE_M = 5.0
GEOMETRY_DECIMALS = 5

# Trip videos are named `YYYY-mm-dd <trip name>.<ext>`.
VIDEO_NAME_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s*(?P<name>.*)$")

TRACKS_DIR_NAME = "tracks"
PREVIEWS_DIR_NAME = "previews"
TRASH_DIR_NAME = "trash"
INDEX_FILENAME = "index.json"
GEOMETRY_FILENAME = "geometry.json"
ENCODED_SEGMENTS_FILENAME = "encoded_segments.json"
TRACK_EXTENSION = ".json"
PREVIEW_EXTENSION = ".mp4"

# The longest filename in the library and metadata directories, in UTF-8 bytes. This is the
# limit of eCryptfs, which encrypted NAS shared folders use.
MAX_FILENAME_BYTES = 143

# Files being written get a short random name, so that their length does not depend on the
# trip name. `doctor` deletes the ones left behind by an interrupted write.
PARTIAL_FILE_PREFIX = ".tmp-"
PARTIAL_FILE_SUFFIX = ".partial"
PARTIAL_FILE_GLOB = f"{PARTIAL_FILE_PREFIX}*{PARTIAL_FILE_SUFFIX}"

# Extensions of trip videos.
VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi")

# Characters that are not allowed in filenames on common file systems.
FORBIDDEN_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
FORBIDDEN_FILENAME_CHARS_TEXT = '/ \\ : * ? " < > |'

# Characters that would make a name span several path components, or on Windows, start from
# another drive (`C:name`).
PATH_SEPARATOR_CHARS = re.compile(r"[/\\:\x00]")


class TrackFormatError(ValueError):
    """Raised when a track file cannot be parsed."""


def get_partial_path(path: Path) -> Path:
    """Return a temporary path next to `path`, to write it and then rename it atomically."""
    return path.with_name(f"{PARTIAL_FILE_PREFIX}{secrets.token_hex(4)}{PARTIAL_FILE_SUFFIX}")


def write_text_atomically(path: Path, text: str) -> None:
    """
    Write a text file so that an interruption or a power loss never leaves it truncated.

    The text goes to a temporary file next to `path`, which is flushed to the disk and then
    renamed over `path`.
    """
    partial_path = get_partial_path(path)
    try:
        with partial_path.open("w", encoding="utf-8") as fp:
            fp.write(text)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(partial_path, path)
    except BaseException:
        partial_path.unlink(missing_ok=True)
        raise


def get_max_video_filename_bytes(extension: str) -> int:
    """Return the longest video filename whose track and preview filenames fit the limit."""
    longest_extension_length = max(len(TRACK_EXTENSION), len(PREVIEW_EXTENSION), len(extension))
    return MAX_FILENAME_BYTES - longest_extension_length + len(extension)


def find_videos(library_dir: Path) -> list[Path]:
    """List the trip videos in the library directory, sorted by name."""
    LOGGER.info(f"Scanning '{library_dir}' for videos")
    return sorted(
        (
            path
            for path in library_dir.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=lambda path: path.name,
    )


def find_videos_sharing_a_track(video_paths: list[Path]) -> list[list[Path]]:
    """
    Group the videos whose names differ only in the extension or the letter case. Tracks are
    named after the video name without the extension, so such videos would share one track.
    """
    paths_by_stem: dict[str, list[Path]] = {}
    for path in video_paths:
        paths_by_stem.setdefault(path.stem.lower(), []).append(path)
    return [paths for paths in paths_by_stem.values() if len(paths) > 1]


def rename_without_overwrite(source_path: Path, target_path: Path) -> None:
    """
    Rename a file, refusing to replace another file (which POSIX would do silently).

    A target that is the source itself, as in a rename that only changes the letter case on a
    case-insensitive file system, is allowed.

    Raises
    ------
    FileExistsError
        If another file has the target name.
    """
    if target_path.exists() and not target_path.samefile(source_path):
        raise FileExistsError(f"'{target_path}' already exists")
    source_path.rename(target_path)


def is_single_path_component(name: str) -> bool:
    """Check that a name (e.g. a trip ID from a request) cannot leave its directory."""
    return bool(name) and not name.startswith(".") and not PATH_SEPARATOR_CHARS.search(name)


def get_filename_length_problem(video_filename: str) -> str | None:
    """
    Check that a video filename, and the names of its track and preview, fit the limit.

    Returns
    -------
    str | None
        What is wrong with it, or None if it is fine.
    """
    max_bytes = get_max_video_filename_bytes(Path(video_filename).suffix)
    filename_bytes = len(video_filename.encode("utf-8"))
    if filename_bytes <= max_bytes:
        return None
    if video_filename.isascii():
        return f"use at most {max_bytes} characters ({filename_bytes} now)"
    return f"use at most {max_bytes} bytes in UTF-8 ({filename_bytes} now)"


@dataclasses.dataclass(frozen=True)
class Enrichment:
    """What a street list was made from, to tell when it is outdated."""

    enricher_version: int
    osm_timestamp: str
    osm_source: str
    samples_digest: str
    enriched_at: str


@dataclasses.dataclass
class Track:
    """Everything extracted from one video."""

    video_filename: str
    fingerprint: str
    video_size: int
    video_mtime: float
    extraction_status: str
    extracted_at: str
    extractor_version: int
    cleaning_version: int
    video_width: int
    video_height: int
    duration_s: float
    overrides: cleaning.Overrides
    streets: list[dict[str, t.Any]]
    localities: dict[str, dict[str, t.Any] | None]
    enrichment: Enrichment | None
    raw_samples: list[cleaning.RawSample]
    clean_samples: list[cleaning.CleanSample]

    @property
    def stem(self) -> str:
        return Path(self.video_filename).stem


@dataclasses.dataclass(frozen=True)
class TripName:
    """Trip date and name, taken from the video filename."""

    date: datetime.date | None
    name: str


def parse_trip_name(video_filename: str) -> TripName:
    """Split a video filename into the trip date and name."""
    stem = Path(video_filename).stem
    match = VIDEO_NAME_PATTERN.match(stem)
    if match is None:
        return TripName(date=None, name=stem)
    try:
        trip_date = datetime.date.fromisoformat(match["date"])
    except ValueError:
        return TripName(date=None, name=stem)
    return TripName(date=trip_date, name=match["name"] or stem)


def sample_to_dict(raw_sample: cleaning.RawSample, clean_sample: cleaning.CleanSample) -> dict:
    """Convert a sample to the dictionary written to a track file line."""
    sample_dict: dict[str, t.Any] = {
        "t": round(clean_sample.t, 2),
        "time": clean_sample.time.isoformat() if clean_sample.time else None,
        "lat": round(clean_sample.lat, 6) if clean_sample.lat is not None else None,
        "lon": round(clean_sample.lon, 6) if clean_sample.lon is not None else None,
        "kmh": clean_sample.kmh,
        "status": clean_sample.status,
    }
    if clean_sample.time_estimated:
        sample_dict["time_estimated"] = True
    sample_dict["raw"] = f"{raw_sample.gps_text} | {raw_sample.clock_text}"
    sample_dict["scores"] = [round(raw_sample.gps_score, 2), round(raw_sample.clock_score, 2)]
    return sample_dict


def is_number(value: t.Any) -> bool:
    """Check that a JSON value is a number (JSON booleans load as `bool`, a subclass of `int`)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def samples_from_dict(sample_dict: dict) -> tuple[cleaning.RawSample, cleaning.CleanSample]:
    """Convert a track file line back to a raw and a clean sample."""
    gps_text, separator, clock_text = sample_dict["raw"].partition(" | ")
    if not separator:
        raise TrackFormatError(f"Invalid raw text: {sample_dict['raw']!r}")
    gps_score, clock_score = sample_dict["scores"]
    raw_sample = cleaning.RawSample(
        t=float(sample_dict["t"]),
        gps_text=gps_text,
        clock_text=clock_text,
        gps_score=float(gps_score),
        clock_score=float(clock_score),
    )
    status = sample_dict["status"]
    if status not in cleaning.ALL_STATUSES:
        raise TrackFormatError(f"Unknown status {status!r}")
    lat = sample_dict.get("lat")
    lon = sample_dict.get("lon")
    for coordinate in (lat, lon):
        if coordinate is not None and not is_number(coordinate):
            raise TrackFormatError(f"'lat' and 'lon' must be numbers or null, not {coordinate!r}")
    if status in cleaning.LOCATED_STATUSES and (lat is None or lon is None):
        raise TrackFormatError(f"A sample with status {status!r} needs 'lat' and 'lon'")
    time_text = sample_dict.get("time")
    sample_time = datetime.datetime.fromisoformat(time_text) if time_text else None
    if sample_time is not None and sample_time.tzinfo is None:
        raise TrackFormatError(f"'time' needs a UTC offset: {time_text!r}")
    clean_sample = cleaning.CleanSample(
        t=float(sample_dict["t"]),
        time=sample_time,
        lat=lat,
        lon=lon,
        kmh=sample_dict.get("kmh"),
        status=status,
        time_estimated=bool(sample_dict.get("time_estimated", False)),
    )
    return raw_sample, clean_sample


def dump_track(track: Track) -> str:
    """Serialize a track: pretty-printed header, one sample per line."""
    header = {
        "video_filename": track.video_filename,
        "fingerprint": track.fingerprint,
        "video_size": track.video_size,
        "video_mtime": track.video_mtime,
        "extraction_status": track.extraction_status,
        "extracted_at": track.extracted_at,
        "extractor_version": track.extractor_version,
        "cleaning_version": track.cleaning_version,
        "video": {
            "width": track.video_width,
            "height": track.video_height,
            "duration_s": track.duration_s,
        },
        "overrides": {
            "bad_ranges_s": [list(time_range) for time_range in track.overrides.bad_ranges_s],
            "good_ranges_s": [list(time_range) for time_range in track.overrides.good_ranges_s],
        },
        "streets": track.streets,
        "localities": track.localities,
        "enrichment": dataclasses.asdict(track.enrichment) if track.enrichment else None,
    }
    header_json = json.dumps(header, indent=2, ensure_ascii=False)

    sample_lines = [
        "    " + json.dumps(sample_to_dict(raw_sample, clean_sample), ensure_ascii=False)
        for raw_sample, clean_sample in zip(track.raw_samples, track.clean_samples, strict=True)
    ]
    samples_json = "[\n" + ",\n".join(sample_lines) + "\n  ]" if sample_lines else "[]"

    # Replace the closing brace of the header with the samples.
    return header_json[: -len("\n}")] + f',\n  "samples": {samples_json}\n}}\n'


def parse_time_ranges(value: t.Any, field_name: str) -> list[tuple[float, float]]:
    """Validate and convert a list of `[start, end]` ranges."""
    if not isinstance(value, list):
        raise TrackFormatError(f"'{field_name}' must be a list of [start, end] ranges")
    time_ranges = []
    for time_range in value:
        is_pair = isinstance(time_range, list) and len(time_range) == 2
        if not is_pair or not all(isinstance(bound, int | float) for bound in time_range):
            raise TrackFormatError(f"'{field_name}' has an invalid range: {time_range!r}")
        start, end = time_range
        if start > end:
            raise TrackFormatError(f"'{field_name}' has a range with start > end: {time_range!r}")
        time_ranges.append((float(start), float(end)))
    return time_ranges


def parse_enrichment(value: t.Any) -> Enrichment | None:
    """Validate and convert the `enrichment` field."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TrackFormatError("'enrichment' must be an object or null")
    return Enrichment(
        enricher_version=int(value["enricher_version"]),
        osm_timestamp=str(value["osm_timestamp"]),
        osm_source=str(value.get("osm_source", "")),
        samples_digest=str(value["samples_digest"]),
        enriched_at=str(value["enriched_at"]),
    )


def load_track_text(text: str) -> Track:
    """
    Parse a track file.

    Raises
    ------
    TrackFormatError
        If the text is not valid JSON or does not have the expected fields.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TrackFormatError(f"Invalid JSON at line {exc.lineno}: {exc.msg}") from exc

    try:
        overrides_data = data.get("overrides") or {}
        overrides = cleaning.Overrides(
            bad_ranges_s=parse_time_ranges(
                overrides_data.get("bad_ranges_s", []), "overrides.bad_ranges_s"
            ),
            good_ranges_s=parse_time_ranges(
                overrides_data.get("good_ranges_s", []), "overrides.good_ranges_s"
            ),
        )
        raw_samples = []
        clean_samples = []
        for sample_number, sample_dict in enumerate(data["samples"], start=1):
            try:
                raw_sample, clean_sample = samples_from_dict(sample_dict)
            except (KeyError, TypeError, ValueError) as exc:
                raise TrackFormatError(f"Invalid sample #{sample_number}: {exc}") from exc
            raw_samples.append(raw_sample)
            clean_samples.append(clean_sample)

        return Track(
            video_filename=data["video_filename"],
            fingerprint=data["fingerprint"],
            video_size=int(data["video_size"]),
            video_mtime=float(data["video_mtime"]),
            extraction_status=data["extraction_status"],
            extracted_at=data["extracted_at"],
            extractor_version=int(data["extractor_version"]),
            cleaning_version=int(data["cleaning_version"]),
            video_width=int(data["video"]["width"]),
            video_height=int(data["video"]["height"]),
            duration_s=float(data["video"]["duration_s"]),
            overrides=overrides,
            streets=list(data.get("streets") or []),
            localities=dict(data.get("localities") or {}),
            enrichment=parse_enrichment(data.get("enrichment")),
            raw_samples=raw_samples,
            clean_samples=clean_samples,
        )
    except KeyError as exc:
        raise TrackFormatError(f"Missing field: {exc}") from exc
    except (TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, TrackFormatError):
            raise
        raise TrackFormatError(str(exc)) from exc


def load_track_file(path: Path) -> Track:
    """
    Read and parse a track file. A byte order mark, as some Windows editors write, is accepted.

    Raises
    ------
    TrackFormatError
        If the file is not UTF-8 text or not a valid track.
    """
    data = path.read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TrackFormatError(f"Not UTF-8 text at byte {exc.start}") from exc
    return load_track_text(text)


def is_gap_between(previous: cleaning.CleanSample, current: cleaning.CleanSample) -> bool:
    """
    Check whether consecutive samples are further apart in time than cleaning interpolates,
    e.g. across a gap between merged segments. The route is not continuous there.
    """
    if previous.time is None or current.time is None:
        return False
    step_s = (current.time - previous.time).total_seconds()
    return not 0 <= step_s <= cleaning.MAX_INTERPOLATION_GAP_S


def compute_trip_stats(track: Track) -> dict[str, t.Any]:
    """Compute the trip summary shown in the visualizer. Gaps add neither distance nor time."""
    samples = track.clean_samples
    located = [sample.status in cleaning.LOCATED_STATUSES for sample in samples]
    times = [sample.time for sample in samples if sample.time is not None]

    distance_m = 0.0
    moving_duration_s = 0.0
    for index in range(1, len(samples)):
        if not (located[index - 1] and located[index]):
            continue
        previous = samples[index - 1]
        current = samples[index]
        if is_gap_between(previous, current):
            continue
        assert previous.lat is not None and previous.lon is not None
        assert current.lat is not None and current.lon is not None
        distance_m += geo.haversine_m(previous.lat, previous.lon, current.lat, current.lon)
        if previous.time is not None and current.time is not None:
            moving_duration_s += (current.time - previous.time).total_seconds()

    located_samples = [
        sample for sample, is_located in zip(samples, located, strict=True) if is_located
    ]
    speeds = [sample.kmh for sample in samples if sample.kmh is not None]
    lats = [sample.lat for sample in located_samples if sample.lat is not None]
    lons = [sample.lon for sample in located_samples if sample.lon is not None]
    status_counts: dict[str, int] = {}
    for sample in samples:
        status_counts[sample.status] = status_counts.get(sample.status, 0) + 1

    return {
        "start_time": min(times).isoformat() if times else None,
        "end_time": max(times).isoformat() if times else None,
        "duration_s": round((max(times) - min(times)).total_seconds()) if times else 0,
        "distance_km": round(distance_m / 1000, 2),
        "avg_kmh": round(distance_m / moving_duration_s * 3.6, 1) if moving_duration_s else None,
        "max_kmh": max(speeds) if speeds else None,
        "coverage": round(sum(located) / len(samples), 3) if samples else 0.0,
        "status_counts": status_counts,
        "bbox": [min(lats), min(lons), max(lats), max(lons)] if lats else None,
    }


def get_locality_name(track: Track, key: str) -> str | None:
    """Return the name of the start or end locality of a track, if known."""
    locality = track.localities.get(key)
    if not isinstance(locality, dict):
        return None
    return locality.get("name")


def get_street_names(track: Track) -> list[str]:
    """Return the street names of a track in travel order, each name once."""
    street_names = []
    for street in track.streets:
        name = street.get("name") if isinstance(street, dict) else street
        if isinstance(name, str) and name and name not in street_names:
            street_names.append(name)
    return street_names


def simplify_route(samples: list[cleaning.CleanSample]) -> list[list[list[float]]]:
    """
    Reduce the samples of a track to runs of located points, for drawing many trips at once.

    A run ends at every sample without a location and at every gap in time. Points closer than
    `GEOMETRY_MIN_STEP_M` to the previous kept point are dropped, but the last point of a run is
    always kept. Each run is then simplified with the Douglas-Peucker algorithm
    (`GEOMETRY_TOLERANCE_M`).

    Returns
    -------
    list[list[list[float]]]
        Runs of `[lat, lon]` points. Runs with fewer than two points are left out.
    """
    runs = []
    current_run: list[list[float]] = []
    last_point: list[float] | None = None
    previous_sample: cleaning.CleanSample | None = None

    def finish_run() -> None:
        if last_point is not None and current_run[-1] is not last_point:
            current_run.append(last_point)
        if len(current_run) >= 2:
            runs.append(simplify_polyline(current_run, GEOMETRY_TOLERANCE_M))

    for sample in samples:
        is_located = (
            sample.status in cleaning.LOCATED_STATUSES
            and sample.lat is not None
            and sample.lon is not None
        )
        is_after_gap = previous_sample is not None and is_gap_between(previous_sample, sample)
        previous_sample = sample
        if not is_located or is_after_gap:
            if current_run:
                finish_run()
            current_run = []
            last_point = None
        if not is_located:
            continue
        assert sample.lat is not None and sample.lon is not None

        point = [round(sample.lat, GEOMETRY_DECIMALS), round(sample.lon, GEOMETRY_DECIMALS)]
        last_point = point
        if not current_run:
            current_run.append(point)
            continue
        previous_point = current_run[-1]
        step_m = geo.haversine_m(previous_point[0], previous_point[1], point[0], point[1])
        if step_m >= GEOMETRY_MIN_STEP_M:
            current_run.append(point)

    if current_run:
        finish_run()
    return runs


def simplify_polyline(points: list[list[float]], tolerance_m: float) -> list[list[float]]:
    """
    Drop the points of a `[lat, lon]` polyline that deviate less than `tolerance_m` from the
    simplified line (Douglas-Peucker). The first and the last points are always kept.
    """
    if len(points) <= 2:
        return points
    # Local flat coordinates in meters, accurate enough over the length of a trip.
    lon_scale = math.cos(math.radians(points[0][0]))
    xs = [point[1] * lon_scale * geo.METERS_PER_DEGREE for point in points]
    ys = [point[0] * geo.METERS_PER_DEGREE for point in points]

    is_kept = [False] * len(points)
    is_kept[0] = is_kept[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        dx = xs[last] - xs[first]
        dy = ys[last] - ys[first]
        length = math.hypot(dx, dy)
        farthest_index = None
        farthest_distance = tolerance_m
        for index in range(first + 1, last):
            if length > 0:
                distance = abs(dy * (xs[index] - xs[first]) - dx * (ys[index] - ys[first])) / length
            else:
                distance = math.hypot(xs[index] - xs[first], ys[index] - ys[first])
            if distance > farthest_distance:
                farthest_index = index
                farthest_distance = distance
        if farthest_index is not None:
            is_kept[farthest_index] = True
            stack.append((first, farthest_index))
            stack.append((farthest_index, last))
    return [point for point, kept in zip(points, is_kept, strict=True) if kept]


def build_index_entry(track: Track, has_preview: bool) -> dict[str, t.Any]:
    """Summarize one track for the index."""
    trip_name = parse_trip_name(track.video_filename)
    entry: dict[str, t.Any] = {
        "id": track.stem,
        "video_filename": track.video_filename,
        "date": trip_name.date.isoformat() if trip_name.date else None,
        "name": trip_name.name,
        "extraction_status": track.extraction_status,
        "has_preview": has_preview,
        "streets": get_street_names(track),
        "start_locality": get_locality_name(track, "start"),
        "end_locality": get_locality_name(track, "end"),
    }
    if track.extraction_status == EXTRACTION_OK:
        entry.update(compute_trip_stats(track))
    return entry


def make_index(trips: list[dict[str, t.Any]], skipped_track_stems: list[str]) -> dict[str, t.Any]:
    """
    Wrap index entries into the index file content, with the stems of the track files that
    were left out (unreadable or misnamed), so that a cheap check can tell the index is current.
    """
    return {
        "format_version": INDEX_FORMAT_VERSION,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "trips": trips,
        "skipped_track_stems": skipped_track_stems,
    }


@dataclasses.dataclass(frozen=True)
class TrackFile:
    """A track file with its track, or the reason it could not be loaded."""

    path: Path
    track: Track | None
    error: str | None

    @property
    def is_misnamed(self) -> bool:
        """Check whether the file is named after another video than the one its track declares."""
        return self.track is not None and self.track.stem != self.path.stem

    def get_skip_message(self) -> str:
        """Describe why an unreadable or misnamed track file is skipped."""
        if self.track is None:
            return (
                f"Skipping unreadable track '{self.path.name}': {self.error} (see `dashcam doctor`)"
            )
        return (
            f"Skipping '{self.path.name}': it belongs to '{self.track.video_filename}' "
            f"(run `dashcam doctor --fix`)"
        )


class MetadataStore:
    """Tracks, previews, and the trip index in a metadata directory."""

    def __init__(self, root: Path):
        self.root = root
        self.tracks_dir = root / TRACKS_DIR_NAME
        self.previews_dir = root / PREVIEWS_DIR_NAME
        self.trash_dir = root / TRASH_DIR_NAME
        self.index_path = root / INDEX_FILENAME
        self.geometry_path = root / GEOMETRY_FILENAME
        self.encoded_segments_path = root / ENCODED_SEGMENTS_FILENAME

    def ensure_dirs(self) -> None:
        """Create the metadata directories if they do not exist."""
        self.tracks_dir.mkdir(parents=True, exist_ok=True)

    def track_path(self, stem: str) -> Path:
        return self.tracks_dir / f"{stem}{TRACK_EXTENSION}"

    def preview_path(self, stem: str) -> Path:
        return self.previews_dir / f"{stem}{PREVIEW_EXTENSION}"

    def list_track_paths(self) -> list[Path]:
        """List the track files, sorted by name (case-sensitively on every platform)."""
        if not self.tracks_dir.is_dir():
            return []
        return sorted(
            (
                path
                for path in self.tracks_dir.glob(f"*{TRACK_EXTENSION}")
                if not path.name.startswith(".")
            ),
            key=lambda path: path.name,
        )

    def iter_track_files(self) -> t.Generator[TrackFile, None, None]:
        """Load the track files one by one in name order, logging the progress."""
        paths = self.list_track_paths()
        LOGGER.info(f"Loading {len(paths)} tracks from '{self.tracks_dir}'")
        progress_logger = terminal.ItemProgressLogger(LOGGER, "Loading tracks", len(paths))
        for loaded_count, path in enumerate(paths, start=1):
            try:
                track = load_track_file(path)
            except (OSError, TrackFormatError) as exc:
                yield TrackFile(path=path, track=None, error=str(exc))
            else:
                yield TrackFile(path=path, track=track, error=None)
            progress_logger.update(loaded_count)

    def load_tracks_by_stem(self) -> dict[str, Track]:
        """
        Load all readable tracks by file stem. Unreadable track files, and files named after
        another video than their track declares, are reported and skipped.
        """
        tracks = {}
        for track_file in self.iter_track_files():
            if track_file.track is None or track_file.is_misnamed:
                LOGGER.warning(track_file.get_skip_message())
                continue
            tracks[track_file.path.stem] = track_file.track
        return tracks

    def load_track(self, stem: str) -> Track:
        """Load the track of a video stem."""
        return load_track_file(self.track_path(stem))

    def save_track(self, track: Track) -> Path:
        """Write a track atomically, so an interruption never leaves a truncated file."""
        self.ensure_dirs()
        path = self.track_path(track.stem)
        self.write_track_file(track, path)
        return path

    def write_track_file(self, track: Track, path: Path) -> None:
        """Write a track to the given path atomically."""
        write_text_atomically(path, dump_track(track))

    def move_to_trash(self, stem: str) -> list[Path]:
        """
        Move the track and preview of a video stem to a timestamped folder in the trash.

        The files keep their names, so that they fit the filename length limit.

        Returns
        -------
        list[Path]
            The new locations of the moved files.
        """
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        paths = [path for path in (self.track_path(stem), self.preview_path(stem)) if path.exists()]
        if not paths:
            return []
        trash_dir = self.get_free_trash_dir(timestamp, [path.name for path in paths])
        trash_dir.mkdir(parents=True, exist_ok=True)
        moved_paths = []
        for path in paths:
            trash_path = trash_dir / path.name
            shutil.move(path, trash_path)
            moved_paths.append(trash_path)
        return moved_paths

    def get_free_trash_dir(self, timestamp: str, filenames: list[str]) -> Path:
        """Return a trash folder for the timestamp that does not have any of the files yet."""
        trash_dir = self.trash_dir / timestamp
        collision_number = 0
        while any((trash_dir / filename).exists() for filename in filenames):
            collision_number += 1
            trash_dir = self.trash_dir / f"{timestamp}-{collision_number}"
        return trash_dir

    def rename_trip(self, old_stem: str, new_video_filename: str) -> None:
        """
        Rename a track (and its preview) after its video was renamed.

        If a step fails, the steps done so far are undone.

        Raises
        ------
        FileExistsError
            If another track or preview already has the new name.
        """
        original_track = self.load_track(old_stem)
        track = dataclasses.replace(original_track, video_filename=new_video_filename)
        old_track_path = self.track_path(old_stem)
        new_track_path = self.track_path(track.stem)
        old_preview_path = self.preview_path(old_stem)
        new_preview_path = self.preview_path(track.stem)
        undo_steps: list[t.Callable[[], object]] = []
        try:
            # The track is updated under its old name and then renamed, so that a rename that
            # only changes the letter case works on case-insensitive file systems.
            self.write_track_file(track, old_track_path)
            undo_steps.append(lambda: self.write_track_file(original_track, old_track_path))
            if track.stem != old_stem:
                rename_without_overwrite(old_track_path, new_track_path)
                undo_steps.append(lambda: new_track_path.rename(old_track_path))
                if old_preview_path.exists():
                    rename_without_overwrite(old_preview_path, new_preview_path)
        except BaseException:
            for undo_step in reversed(undo_steps):
                undo_step()
            raise

    def build_index_entries(self, tracks_by_stem: dict[str, Track]) -> list[dict[str, t.Any]]:
        """
        Summarize loaded tracks (keyed by file stem) like a full rebuild: in its order, and
        without the misnamed track files.
        """
        return [
            build_index_entry(track, has_preview=self.preview_path(track.stem).exists())
            for stem, track in sorted(
                tracks_by_stem.items(), key=lambda item: item[0] + TRACK_EXTENSION
            )
            if track.stem == stem
        ]

    def build_index_and_geometry(self) -> tuple[dict[str, t.Any], dict[str, t.Any]]:
        """
        Summarize all tracks and simplify their routes, reading each track once. Unreadable
        and misnamed track files are reported and left out.
        """
        trips = []
        routes = {}
        skipped_track_stems = []
        for track_file in self.iter_track_files():
            track = track_file.track
            if track is None or track_file.is_misnamed:
                LOGGER.warning(track_file.get_skip_message())
                skipped_track_stems.append(track_file.path.stem)
                continue
            trip = build_index_entry(track, has_preview=self.preview_path(track.stem).exists())
            trips.append(trip)
            route = simplify_route(track.clean_samples) if trip.get("bbox") else []
            if route:
                routes[track.stem] = route
        index = make_index(trips, skipped_track_stems)
        return index, {"format_version": INDEX_FORMAT_VERSION, "trips": routes}

    def rebuild_index(self) -> dict[str, t.Any]:
        """Rebuild `index.json` and `geometry.json` from the tracks."""
        index, geometry = self.build_index_and_geometry()
        self.write_index_files(index, geometry)
        return index

    def load_index(self) -> dict[str, t.Any]:
        """Load `index.json` as it is on disk."""
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def load_geometry(self) -> dict[str, t.Any]:
        """Load `geometry.json` as it is on disk."""
        return json.loads(self.geometry_path.read_text(encoding="utf-8"))

    def update_index_after_rename(self, old_stem: str, new_stem: str) -> dict[str, t.Any]:
        """
        Update the index and geometry files for one renamed trip, without reading other tracks.

        Returns
        -------
        dict[str, t.Any]
            The updated index.
        """
        track = self.load_track(new_stem)
        index = self.load_index()
        geometry = self.load_geometry()
        trips = [trip for trip in index["trips"] if trip["id"] != old_stem]
        entry = build_index_entry(track, has_preview=self.preview_path(new_stem).exists())
        trips.append(entry)
        # Same order as a full rebuild, which reads the track files in name order.
        trips.sort(key=lambda trip: trip["id"] + TRACK_EXTENSION)
        routes = geometry["trips"]
        routes.pop(old_stem, None)
        route = simplify_route(track.clean_samples) if entry.get("bbox") else []
        if route:
            routes[new_stem] = route
        index = make_index(trips, index["skipped_track_stems"])
        self.write_index_files(index, geometry)
        return index

    def write_index_files(self, index: dict[str, t.Any], geometry: dict[str, t.Any]) -> None:
        """Write the geometry and then the index, both atomically."""
        self.root.mkdir(parents=True, exist_ok=True)
        # The index is written last: it is newer than the geometry whenever both are current.
        for path, text in [
            (self.geometry_path, json.dumps(geometry, ensure_ascii=False, separators=(",", ":"))),
            (self.index_path, json.dumps(index, indent=2, ensure_ascii=False)),
        ]:
            write_text_atomically(path, text + "\n")
