"""
Match tracks to named roads and find the localities where trips start and end.

Map matching runs a hidden Markov model over the located samples of a track. The hidden state
of each sample is the street the car is on, or "off road" (an unnamed road, a parking lot, or
a road missing from OSM). Candidate streets come from the road segments near the sample:

* The emission cost grows with the squared distance from the sample to the segment, and with
  the angle between the segment and the direction of travel, so that the cross street at an
  intersection loses to the street the car is driving along.
* Switching streets has a fixed cost, so that GPS noise does not make the match flicker.

The Viterbi algorithm picks the cheapest sequence of streets. Consecutive samples on the same
street form a stretch; the street list of a trip is its stretches in travel order.

A track records which samples and which OSM data it was enriched from, so `enrich` only
processes tracks that changed, and `doctor` can report outdated street lists.
"""

import dataclasses
import datetime
import hashlib
import logging
import math
import typing as t
from pathlib import Path

from dashcam import cleaning
from dashcam import geo
from dashcam import metadata
from dashcam import osm
from dashcam import terminal
from dashcam.config import Config

# Version of the matching algorithm. Tracks enriched by an older version are enriched again.
ENRICHER_VERSION = 1

# Typical GPS error of a located sample, in meters.
GPS_SIGMA_M = 12.0

# Roads farther than this from a sample are not candidates for it.
SEARCH_RADIUS_M = 50.0

# Cost of a sample being off the named roads. Equal to the distance cost at about 30 m.
OFF_ROAD_COST = 3.0

# Cost of driving perpendicular to a road. Scaled by the squared sine of the angle.
HEADING_WEIGHT = 3.0

# Cost of switching from one street (or off road) to another.
SWITCH_COST = 4.0

# The direction of travel is taken from neighboring samples at least this far apart, looking
# at most this many samples in each direction. Below that, the car is considered stationary.
MIN_HEADING_DISPLACEMENT_M = 10.0
MAX_HEADING_WINDOW = 5

# Consecutive located samples farther apart than this (in video seconds or meters) belong to
# separate runs, which are matched independently.
MAX_RUN_STEP_S = 5.0
MAX_RUN_STEP_M = 150.0

# Stretches shorter than this are matching noise at intersections and are dropped.
MIN_STRETCH_M = 50.0

# Localities: a point belongs to the locality with the smallest distance relative to its
# radius, if that is at most 1. The radius depends on the place type and grows with the
# population (Lviv, 720,000 people, gets about 8.5 km).
LOCALITY_RADII_M = {"city": 5000.0, "town": 2500.0, "village": 1200.0, "hamlet": 500.0}
LOCALITY_POPULATION_RADIUS_FACTOR = 10.0
MAX_LOCALITY_RADIUS_M = 25000.0

# Meters per degree of latitude.
METERS_PER_DEGREE = geo.EARTH_RADIUS_M * math.pi / 180

# Sample statuses with a usable position.
LOCATED_STATUSES = frozenset([cleaning.STATUS_OK, cleaning.STATUS_INTERPOLATED])

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)

# A street is identified by its OSM name and ref, since a street consists of many OSM ways
# whose other tags (e.g. the highway class) may differ.
StreetKey = tuple[str | None, str | None]


@dataclasses.dataclass(frozen=True)
class LocatedSample:
    """A sample with a usable position."""

    t: float
    lat: float
    lon: float


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A street near a sample, with the best-fitting road of that street."""

    street_key: StreetKey
    road_id: int
    cost: float


@dataclasses.dataclass
class Stretch:
    """Consecutive samples matched to the same street."""

    street_key: StreetKey | None
    start_t: float
    end_t: float
    distance_m: float = 0.0
    distance_by_road_id: dict[int, float] = dataclasses.field(default_factory=dict)


def compute_samples_digest(track: metadata.Track) -> str:
    """Return a digest of the located positions of a track, as stored in the track file."""
    digest = hashlib.sha256()
    for sample in track.clean_samples:
        if sample.status not in LOCATED_STATUSES:
            continue
        digest.update(f"{sample.t:.2f} {sample.lat:.6f} {sample.lon:.6f}\n".encode())
    return digest.hexdigest()[:16]


def is_enrichment_current(track: metadata.Track, osm_timestamp: str | None) -> bool:
    """
    Check whether the street list of a track matches its samples and the OSM data.

    Pass None as `osm_timestamp` to skip the OSM data check.
    """
    enrichment = track.enrichment
    if enrichment is None:
        return False
    return (
        enrichment.enricher_version >= ENRICHER_VERSION
        and enrichment.samples_digest == compute_samples_digest(track)
        and (osm_timestamp is None or enrichment.osm_timestamp == osm_timestamp)
    )


def split_into_runs(track: metadata.Track) -> list[list[LocatedSample]]:
    """Group the located samples of a track into runs without gaps or jumps."""
    runs: list[list[LocatedSample]] = []
    current_run: list[LocatedSample] = []
    for sample in track.clean_samples:
        if sample.status not in LOCATED_STATUSES or sample.lat is None or sample.lon is None:
            if current_run:
                runs.append(current_run)
                current_run = []
            continue
        located = LocatedSample(t=sample.t, lat=sample.lat, lon=sample.lon)
        if current_run:
            previous = current_run[-1]
            step_m = geo.haversine_m(previous.lat, previous.lon, located.lat, located.lon)
            if located.t - previous.t > MAX_RUN_STEP_S or step_m > MAX_RUN_STEP_M:
                runs.append(current_run)
                current_run = []
        current_run.append(located)
    if current_run:
        runs.append(current_run)
    return runs


def to_local_meters(
    origin_lat: float, origin_lon: float, lat: float, lon: float
) -> tuple[float, float]:
    """Project a point to meters east and north of an origin (accurate over short distances)."""
    east_m = (lon - origin_lon) * METERS_PER_DEGREE * math.cos(math.radians(origin_lat))
    north_m = (lat - origin_lat) * METERS_PER_DEGREE
    return east_m, north_m


def estimate_heading(run: list[LocatedSample], index: int) -> tuple[float, float] | None:
    """
    Estimate the direction of travel at a sample from its neighbors.

    Returns
    -------
    tuple[float, float] | None
        A unit vector (east, north), or None if the car is not moving.
    """
    sample = run[index]
    for window in range(1, MAX_HEADING_WINDOW + 1):
        before = run[max(0, index - window)]
        after = run[min(len(run) - 1, index + window)]
        start = to_local_meters(sample.lat, sample.lon, before.lat, before.lon)
        end = to_local_meters(sample.lat, sample.lon, after.lat, after.lon)
        east_m = end[0] - start[0]
        north_m = end[1] - start[1]
        length_m = math.hypot(east_m, north_m)
        if length_m >= MIN_HEADING_DISPLACEMENT_M:
            return east_m / length_m, north_m / length_m
    return None


def measure_segment(
    start: tuple[float, float], end: tuple[float, float]
) -> tuple[float, tuple[float, float] | None]:
    """
    Measure a segment in local meters relative to the sample at the origin.

    Returns
    -------
    tuple[float, tuple[float, float] | None]
        The distance from the origin to the segment, and the unit direction of the segment
        (None for a zero-length segment).
    """
    segment_east = end[0] - start[0]
    segment_north = end[1] - start[1]
    length_squared = segment_east**2 + segment_north**2
    if length_squared == 0:
        return math.hypot(*start), None
    # Position of the closest point along the segment, from 0 (start) to 1 (end).
    fraction = -(start[0] * segment_east + start[1] * segment_north) / length_squared
    fraction = min(1.0, max(0.0, fraction))
    closest_east = start[0] + fraction * segment_east
    closest_north = start[1] + fraction * segment_north
    length = math.sqrt(length_squared)
    return math.hypot(closest_east, closest_north), (
        segment_east / length,
        segment_north / length,
    )


def get_street_key(road: osm.Road) -> StreetKey:
    return road.name, road.ref


def find_candidates(
    database: osm.RoadDatabase, sample: LocatedSample, heading: tuple[float, float] | None
) -> list[Candidate]:
    """Find the streets near a sample, with the emission cost of each."""
    lat_margin = SEARCH_RADIUS_M / METERS_PER_DEGREE
    lon_margin = lat_margin / max(0.01, math.cos(math.radians(sample.lat)))
    chunks = database.find_road_chunks(
        min_lat=sample.lat - lat_margin,
        max_lat=sample.lat + lat_margin,
        min_lon=sample.lon - lon_margin,
        max_lon=sample.lon + lon_margin,
    )

    best_by_street: dict[StreetKey, Candidate] = {}
    for chunk in chunks:
        local_points = [
            to_local_meters(sample.lat, sample.lon, lat, lon) for lat, lon in chunk.points
        ]
        street_key = get_street_key(database.get_road(chunk.road_id))
        for start, end in zip(local_points, local_points[1:], strict=False):
            distance_m, direction = measure_segment(start, end)
            if distance_m > SEARCH_RADIUS_M:
                continue
            cost = 0.5 * (distance_m / GPS_SIGMA_M) ** 2
            if heading is not None and direction is not None:
                cosine = heading[0] * direction[0] + heading[1] * direction[1]
                cost += HEADING_WEIGHT * (1 - cosine**2)
            best = best_by_street.get(street_key)
            if best is None or cost < best.cost:
                best_by_street[street_key] = Candidate(street_key, chunk.road_id, cost)
    return list(best_by_street.values())


def match_run(database: osm.RoadDatabase, run: list[LocatedSample]) -> list[Candidate | None]:
    """
    Pick the most likely street for every sample of a run.

    Returns
    -------
    list[Candidate | None]
        The matched street of each sample, or None for off road.
    """
    # Each step maps a state (a street key, or None for off road) to its candidate, the total
    # cost of the best path ending in it, and the state it came from.
    steps: list[dict[StreetKey | None, tuple[Candidate | None, float, StreetKey | None]]] = []
    for index, sample in enumerate(run):
        candidates = find_candidates(database, sample, estimate_heading(run, index))
        emissions: dict[StreetKey | None, tuple[Candidate | None, float]] = {
            None: (None, OFF_ROAD_COST)
        }
        for candidate in candidates:
            emissions[candidate.street_key] = (candidate, candidate.cost)

        step: dict[StreetKey | None, tuple[Candidate | None, float, StreetKey | None]] = {}
        if not steps:
            for state, (candidate, emission_cost) in emissions.items():
                step[state] = (candidate, emission_cost, None)
        else:
            previous_step = steps[-1]
            cheapest_previous_state = min(previous_step, key=lambda state: previous_step[state][1])
            switch_total = previous_step[cheapest_previous_state][1] + SWITCH_COST
            for state, (candidate, emission_cost) in emissions.items():
                stay_total = previous_step[state][1] if state in previous_step else math.inf
                if stay_total <= switch_total:
                    step[state] = (candidate, stay_total + emission_cost, state)
                else:
                    step[state] = (
                        candidate,
                        switch_total + emission_cost,
                        cheapest_previous_state,
                    )
        steps.append(step)

    matches: list[Candidate | None] = []
    state = min(steps[-1], key=lambda state: steps[-1][state][1])
    for step in reversed(steps):
        candidate, _, previous_state = step[state]
        matches.append(candidate)
        state = previous_state
    matches.reverse()
    return matches


def build_stretches(run: list[LocatedSample], matches: list[Candidate | None]) -> list[Stretch]:
    """Group consecutive samples matched to the same street, measuring the distance driven."""
    stretches: list[Stretch] = []
    for index, (sample, match) in enumerate(zip(run, matches, strict=True)):
        street_key = match.street_key if match is not None else None
        if not stretches or stretches[-1].street_key != street_key:
            stretches.append(Stretch(street_key=street_key, start_t=sample.t, end_t=sample.t))
        stretch = stretches[-1]
        stretch.end_t = sample.t
        if index == 0:
            continue
        previous = run[index - 1]
        step_m = geo.haversine_m(previous.lat, previous.lon, sample.lat, sample.lon)
        stretch.distance_m += step_m
        if match is not None:
            road_distance_m = stretch.distance_by_road_id.get(match.road_id, 0.0)
            stretch.distance_by_road_id[match.road_id] = road_distance_m + step_m
    return stretches


def merge_stretches(stretches: list[Stretch]) -> list[Stretch]:
    """Drop off-road and very short stretches, then merge consecutive stretches of a street."""
    merged: list[Stretch] = []
    for stretch in stretches:
        if stretch.street_key is None or stretch.distance_m < MIN_STRETCH_M:
            continue
        if merged and merged[-1].street_key == stretch.street_key:
            merged[-1].end_t = stretch.end_t
            merged[-1].distance_m += stretch.distance_m
            for road_id, road_distance_m in stretch.distance_by_road_id.items():
                merged_distance_m = merged[-1].distance_by_road_id.get(road_id, 0.0)
                merged[-1].distance_by_road_id[road_id] = merged_distance_m + road_distance_m
            continue
        merged.append(stretch)
    return merged


def describe_stretch(database: osm.RoadDatabase, stretch: Stretch) -> dict[str, t.Any]:
    """
    Convert a stretch to the street entry stored in a track.

    `name` is the OSM name, or the ref for roads without a name. The other tags come from the
    road driven the longest in the stretch.
    """
    road_ids = sorted(
        stretch.distance_by_road_id, key=lambda road_id: -stretch.distance_by_road_id[road_id]
    )
    roads = [database.get_road(road_id) for road_id in road_ids]
    main_road = roads[0]
    name_en = next((road.name_en for road in roads if road.name_en), None)
    street: dict[str, t.Any] = {"name": main_road.name or main_road.ref}
    if name_en:
        street["name_en"] = name_en
    if main_road.ref:
        street["ref"] = main_road.ref
    street["highway"] = main_road.highway
    street["distance_m"] = round(stretch.distance_m)
    street["start_t"] = round(stretch.start_t, 2)
    street["end_t"] = round(stretch.end_t, 2)
    return street


def get_locality_radius_m(locality: osm.Locality) -> float:
    """Return how far from its center point a locality extends, roughly."""
    radius_m = LOCALITY_RADII_M.get(locality.place, 0.0)
    if locality.population:
        radius_m = max(radius_m, LOCALITY_POPULATION_RADIUS_FACTOR * math.sqrt(locality.population))
    return min(radius_m, MAX_LOCALITY_RADIUS_M)


def find_locality(database: osm.RoadDatabase, lat: float, lon: float) -> dict[str, t.Any] | None:
    """Find the locality a point is in, as the entry stored in a track."""
    lat_margin = MAX_LOCALITY_RADIUS_M / METERS_PER_DEGREE
    lon_margin = lat_margin / max(0.01, math.cos(math.radians(lat)))
    localities = database.find_localities(
        min_lat=lat - lat_margin,
        max_lat=lat + lat_margin,
        min_lon=lon - lon_margin,
        max_lon=lon + lon_margin,
    )

    best_locality = None
    best_score = 1.0
    for locality in localities:
        distance_m = geo.haversine_m(lat, lon, locality.lat, locality.lon)
        score = distance_m / get_locality_radius_m(locality)
        if score <= best_score:
            best_locality = locality
            best_score = score
    if best_locality is None:
        return None

    entry: dict[str, t.Any] = {"name": best_locality.name}
    if best_locality.name_en:
        entry["name_en"] = best_locality.name_en
    entry["place"] = best_locality.place
    return entry


def enrich_track(track: metadata.Track, database: osm.RoadDatabase) -> None:
    """Match a track to streets and localities, updating it in place."""
    runs = split_into_runs(track)
    stretches = []
    for run in runs:
        stretches += build_stretches(run, match_run(database, run))
    track.streets = [describe_stretch(database, stretch) for stretch in merge_stretches(stretches)]

    if runs:
        first_sample = runs[0][0]
        last_sample = runs[-1][-1]
        track.localities = {
            "start": find_locality(database, first_sample.lat, first_sample.lon),
            "end": find_locality(database, last_sample.lat, last_sample.lon),
        }
    else:
        track.localities = {"start": None, "end": None}

    track.enrichment = metadata.Enrichment(
        enricher_version=ENRICHER_VERSION,
        osm_timestamp=database.osm_timestamp,
        samples_digest=compute_samples_digest(track),
        enriched_at=datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def format_route(track: metadata.Track) -> str:
    """Describe the start and end localities of a track for log messages."""
    names = [(track.localities.get(key) or {}).get("name") or "?" for key in ("start", "end")]
    return " -> ".join(names)


def enrich_tracks(
    metadata_dir: Path,
    config: Config,
    update_osm: bool,
    osm_file: Path | None,
    force: bool,
) -> int:
    """
    Enrich the tracks whose street lists are missing or outdated, then rebuild the index.

    Returns
    -------
    int
        The number of tracks that could not be enriched.

    Raises
    ------
    RuntimeError
        If there is no OSM data.
    """
    if update_osm or osm_file is not None:
        osm.update_osm_data(metadata_dir, config.osm_extract_url, pbf_path=osm_file)

    database = osm.open_database(metadata_dir)
    if database is None:
        raise RuntimeError(
            f"No OSM data in '{osm.get_database_path(metadata_dir)}' "
            f"(run `dashcam enrich --update-osm`)"
        )

    store = metadata.MetadataStore(metadata_dir)
    failed_count = 0
    enriched_count = 0
    try:
        for track_file in store.iter_track_files():
            track = track_file.track
            if track is None:
                LOGGER.error(f"Cannot enrich '{track_file.path.name}': {track_file.error}")
                failed_count += 1
                continue
            if track_file.path.stem != track.stem:
                LOGGER.warning(
                    f"Skipped '{track_file.path.name}': it belongs to '{track.video_filename}' "
                    f"(run `dashcam doctor --fix`)"
                )
                continue
            if track.extraction_status != metadata.EXTRACTION_OK:
                continue
            if not force and is_enrichment_current(track, database.osm_timestamp):
                continue
            enrich_track(track, database)
            store.save_track(track)
            enriched_count += 1
            street_count_text = (
                "1 street" if len(track.streets) == 1 else f"{len(track.streets)} streets"
            )
            LOGGER.info(
                f"Enriched: {track.video_filename} ({street_count_text}, {format_route(track)})",
                extra=terminal.SUCCESS,
            )
    finally:
        database.close()

    if not enriched_count:
        LOGGER.info("All street lists are up to date", extra=terminal.SUCCESS)
    index = store.rebuild_index()
    LOGGER.info(f"Index: {len(index['trips'])} trips in '{store.index_path}'")
    return failed_count
