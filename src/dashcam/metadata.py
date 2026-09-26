"""
Store extracted tracks and the trip index.

Layout of the metadata directory:

    index.json                  Summary of all trips, rebuilt from the tracks. Never edit.
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
import os
import re
import secrets
import shutil
import typing as t
from pathlib import Path

from dashcam import cleaning
from dashcam import geo

# Versions of the extraction (OCR) and cleaning algorithms. Tracks made by older versions can
# be updated with `extract --force` (extraction) or `extract --reclean` (cleaning).
EXTRACTOR_VERSION = 1
CLEANING_VERSION = 1

# Extraction statuses of a track.
EXTRACTION_OK = "ok"
EXTRACTION_NO_OVERLAY = "no_overlay"

# Trip videos are named `YYYY-mm-dd <trip name>.<ext>`.
VIDEO_NAME_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s*(?P<name>.*)$")

TRACKS_DIR_NAME = "tracks"
PREVIEWS_DIR_NAME = "previews"
TRASH_DIR_NAME = "trash"
INDEX_FILENAME = "index.json"
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


class TrackFormatError(ValueError):
    """Raised when a track file cannot be parsed."""


def get_partial_path(path: Path) -> Path:
    """Return a temporary path next to `path`, to write it and then rename it atomically."""
    return path.with_name(f"{PARTIAL_FILE_PREFIX}{secrets.token_hex(4)}{PARTIAL_FILE_SUFFIX}")


def get_max_video_filename_bytes(extension: str) -> int:
    """Return the longest video filename whose track and preview filenames fit the limit."""
    longest_extension_length = max(len(TRACK_EXTENSION), len(PREVIEW_EXTENSION), len(extension))
    return MAX_FILENAME_BYTES - longest_extension_length + len(extension)


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
    sample_dict["raw"] = f"{raw_sample.left_text} | {raw_sample.right_text}"
    sample_dict["scores"] = [round(raw_sample.left_score, 2), round(raw_sample.right_score, 2)]
    return sample_dict


def samples_from_dict(sample_dict: dict) -> tuple[cleaning.RawSample, cleaning.CleanSample]:
    """Convert a track file line back to a raw and a clean sample."""
    left_text, separator, right_text = sample_dict["raw"].partition(" | ")
    if not separator:
        raise TrackFormatError(f"Invalid raw text: {sample_dict['raw']!r}")
    left_score, right_score = sample_dict["scores"]
    raw_sample = cleaning.RawSample(
        t=float(sample_dict["t"]),
        left_text=left_text,
        right_text=right_text,
        left_score=float(left_score),
        right_score=float(right_score),
    )
    time_text = sample_dict.get("time")
    clean_sample = cleaning.CleanSample(
        t=float(sample_dict["t"]),
        time=datetime.datetime.fromisoformat(time_text) if time_text else None,
        lat=sample_dict.get("lat"),
        lon=sample_dict.get("lon"),
        kmh=sample_dict.get("kmh"),
        status=sample_dict["status"],
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


def compute_trip_stats(track: Track) -> dict[str, t.Any]:
    """Compute the trip summary shown in the visualizer."""
    samples = track.clean_samples
    located_statuses = {cleaning.STATUS_OK, cleaning.STATUS_INTERPOLATED}
    located = [sample.status in located_statuses for sample in samples]
    times = [sample.time for sample in samples if sample.time is not None]

    distance_m = 0.0
    moving_duration_s = 0.0
    for index in range(1, len(samples)):
        if not (located[index - 1] and located[index]):
            continue
        previous = samples[index - 1]
        current = samples[index]
        assert previous.lat is not None and previous.lon is not None
        assert current.lat is not None and current.lon is not None
        distance_m += geo.haversine_m(previous.lat, previous.lon, current.lat, current.lon)
        if previous.time is not None and current.time is not None:
            step_s = (current.time - previous.time).total_seconds()
            if 0 < step_s <= cleaning.MAX_INTERPOLATION_GAP_S:
                moving_duration_s += step_s

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


class MetadataStore:
    """Tracks, previews, and the trip index in a metadata directory."""

    def __init__(self, root: Path):
        self.root = root
        self.tracks_dir = root / TRACKS_DIR_NAME
        self.previews_dir = root / PREVIEWS_DIR_NAME
        self.trash_dir = root / TRASH_DIR_NAME
        self.index_path = root / INDEX_FILENAME
        self.encoded_segments_path = root / ENCODED_SEGMENTS_FILENAME

    def ensure_dirs(self) -> None:
        """Create the metadata directories if they do not exist."""
        self.tracks_dir.mkdir(parents=True, exist_ok=True)

    def track_path(self, stem: str) -> Path:
        return self.tracks_dir / f"{stem}{TRACK_EXTENSION}"

    def preview_path(self, stem: str) -> Path:
        return self.previews_dir / f"{stem}{PREVIEW_EXTENSION}"

    def list_track_paths(self) -> list[Path]:
        """List the track files, sorted by name."""
        if not self.tracks_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.tracks_dir.glob(f"*{TRACK_EXTENSION}")
            if not path.name.startswith(".")
        )

    def load_track(self, stem: str) -> Track:
        """Load the track of a video stem."""
        return load_track_text(self.track_path(stem).read_text(encoding="utf-8"))

    def save_track(self, track: Track) -> Path:
        """Write a track atomically, so an interruption never leaves a truncated file."""
        self.ensure_dirs()
        path = self.track_path(track.stem)
        self.write_track_file(track, path)
        return path

    def write_track_file(self, track: Track, path: Path) -> None:
        """Write a track to the given path atomically."""
        partial_path = get_partial_path(path)
        partial_path.write_text(dump_track(track), encoding="utf-8")
        os.replace(partial_path, path)

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
        """Rename a track (and its preview) after its video was renamed."""
        track = self.load_track(old_stem)
        track.video_filename = new_video_filename
        old_track_path = self.track_path(old_stem)
        # The track is updated under its old name and then renamed, so that a rename that only
        # changes the letter case works on case-insensitive file systems.
        self.write_track_file(track, old_track_path)
        if track.stem != old_stem:
            old_track_path.rename(self.track_path(track.stem))
            old_preview_path = self.preview_path(old_stem)
            if old_preview_path.exists():
                old_preview_path.rename(self.preview_path(track.stem))

    def build_index(self) -> dict[str, t.Any]:
        """
        Summarize all tracks. Unreadable tracks are skipped.

        Returns
        -------
        dict[str, t.Any]
            The index, ready to be written as JSON.
        """
        trips = []
        for path in self.list_track_paths():
            try:
                track = load_track_text(path.read_text(encoding="utf-8"))
            except (OSError, TrackFormatError):
                continue
            trip_name = parse_trip_name(track.video_filename)
            trip: dict[str, t.Any] = {
                "id": track.stem,
                "video_filename": track.video_filename,
                "date": trip_name.date.isoformat() if trip_name.date else None,
                "name": trip_name.name,
                "extraction_status": track.extraction_status,
                "has_preview": self.preview_path(track.stem).exists(),
                "street_count": len(track.streets),
                "start_locality": get_locality_name(track, "start"),
                "end_locality": get_locality_name(track, "end"),
            }
            if track.extraction_status == EXTRACTION_OK:
                trip.update(compute_trip_stats(track))
            trips.append(trip)

        return {
            "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "trips": trips,
        }

    def rebuild_index(self) -> dict[str, t.Any]:
        """Rebuild `index.json` from the tracks."""
        index = self.build_index()
        self.root.mkdir(parents=True, exist_ok=True)
        partial_path = get_partial_path(self.index_path)
        partial_path.write_text(
            json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(partial_path, self.index_path)
        return index
