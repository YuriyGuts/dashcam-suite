"""
Download OpenStreetMap data and keep only what map matching needs.

The Geofabrik extract (several hundred MB) is filtered down to named drivable roads and
localities (cities, towns, villages, hamlets), which are stored in a SQLite database with
R*Tree spatial indexes:

    meta                 Key-value pairs: format version, OSM data timestamp, source.
    roads                One row per distinct set of road tags (name, name:en, ref, highway).
    road_chunks          Road geometry: each OSM way is cut into short polylines, so that the
                         bounding boxes in the spatial index stay small.
    road_chunk_index     R*Tree over the chunk bounding boxes.
    localities           Place nodes with their names and population.
    locality_index       R*Tree over the locality points.

Coordinates in chunks are packed as little-endian int32 pairs (lat, lon) in units of 1e-7
degrees, the precision of OSM itself.
"""

import array
import dataclasses
import datetime
import logging
import os
import sqlite3
import ssl
import sys
import time
import typing as t
import urllib.request
from pathlib import Path

import osmium
import osmium.filter
import osmium.io
import osmium.osm
import truststore

from dashcam import metadata
from dashcam import terminal

# Version of the database layout. Databases of other versions must be rebuilt.
DATABASE_FORMAT_VERSION = 1

# Name of the OSM directory inside the metadata directory, and of the database in it.
OSM_DIR_NAME = "osm"
DATABASE_FILENAME = "roads.sqlite"

# Highway classes a car can drive on. Footways, cycleways, and tracks are ignored.
DRIVABLE_HIGHWAY_CLASSES = frozenset(
    [
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "primary",
        "primary_link",
        "secondary",
        "secondary_link",
        "tertiary",
        "tertiary_link",
        "unclassified",
        "residential",
        "living_street",
        "service",
        "road",
    ]
)

# Place types kept as localities.
LOCALITY_PLACE_TYPES = frozenset(["city", "town", "village", "hamlet"])

# The largest number of nodes in one road chunk.
CHUNK_NODE_COUNT = 8

# OSM coordinates are fixed-point numbers with 7 decimal places.
COORDINATE_SCALE = 10_000_000

# How often (in seconds) to log progress while filtering the extract.
PROGRESS_INTERVAL_S = 30

# Seconds without any data after which a download fails.
DOWNLOAD_TIMEOUT_S = 60

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Road:
    """Tags of a named road."""

    name: str | None
    name_en: str | None
    ref: str | None
    highway: str


@dataclasses.dataclass(frozen=True)
class RoadChunk:
    """A short polyline of a road, as `(lat, lon)` points."""

    road_id: int
    points: list[tuple[float, float]]


@dataclasses.dataclass(frozen=True)
class Locality:
    """A city, town, village, or hamlet."""

    name: str
    name_en: str | None
    place: str
    population: int | None
    lat: float
    lon: float


def get_database_path(metadata_dir: Path) -> Path:
    """Return the path of the filtered OSM database in a metadata directory."""
    return metadata_dir / OSM_DIR_NAME / DATABASE_FILENAME


def pack_points(points: list[tuple[float, float]]) -> bytes:
    """Pack `(lat, lon)` points into a chunk blob."""
    values = array.array("i")
    for lat, lon in points:
        values.append(round(lat * COORDINATE_SCALE))
        values.append(round(lon * COORDINATE_SCALE))
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def unpack_points(blob: bytes) -> list[tuple[float, float]]:
    """Unpack a chunk blob into `(lat, lon)` points."""
    values = array.array("i")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    return [
        (values[index] / COORDINATE_SCALE, values[index + 1] / COORDINATE_SCALE)
        for index in range(0, len(values), 2)
    ]


def parse_population(text: str | None) -> int | None:
    """Parse the OSM `population` tag, which sometimes has spaces or separators."""
    if not text:
        return None
    digits = "".join(char for char in text.split(";")[0] if char.isdigit())
    return int(digits) if digits else None


def split_into_chunks(points: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """Cut a polyline into chunks of at most `CHUNK_NODE_COUNT` nodes that share their ends."""
    chunks = []
    step = CHUNK_NODE_COUNT - 1
    for start in range(0, len(points) - 1, step):
        chunks.append(points[start : start + CHUNK_NODE_COUNT])
    return chunks


class RoadDatabaseWriter:
    """Write roads and localities to a new database."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.road_ids: dict[Road, int] = {}
        self.chunk_count = 0
        self.locality_count = 0
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE roads (
                id INTEGER PRIMARY KEY,
                name TEXT,
                name_en TEXT,
                ref TEXT,
                highway TEXT NOT NULL
            );
            CREATE TABLE road_chunks (
                id INTEGER PRIMARY KEY,
                road_id INTEGER NOT NULL,
                coords BLOB NOT NULL
            );
            CREATE VIRTUAL TABLE road_chunk_index USING rtree(
                id, min_lat, max_lat, min_lon, max_lon
            );
            CREATE TABLE localities (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                name_en TEXT,
                place TEXT NOT NULL,
                population INTEGER,
                lat REAL NOT NULL,
                lon REAL NOT NULL
            );
            CREATE VIRTUAL TABLE locality_index USING rtree(
                id, min_lat, max_lat, min_lon, max_lon
            );
            """
        )

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )

    def add_road(self, road: Road, points: list[tuple[float, float]]) -> None:
        """Add a road way, cut into chunks."""
        road_id = self.road_ids.get(road)
        if road_id is None:
            cursor = self.connection.execute(
                "INSERT INTO roads (name, name_en, ref, highway) VALUES (?, ?, ?, ?)",
                (road.name, road.name_en, road.ref, road.highway),
            )
            road_id = t.cast(int, cursor.lastrowid)
            self.road_ids[road] = road_id

        for chunk_points in split_into_chunks(points):
            cursor = self.connection.execute(
                "INSERT INTO road_chunks (road_id, coords) VALUES (?, ?)",
                (road_id, pack_points(chunk_points)),
            )
            lats = [lat for lat, _ in chunk_points]
            lons = [lon for _, lon in chunk_points]
            self.connection.execute(
                "INSERT INTO road_chunk_index VALUES (?, ?, ?, ?, ?)",
                (cursor.lastrowid, min(lats), max(lats), min(lons), max(lons)),
            )
            self.chunk_count += 1

    def add_locality(self, locality: Locality) -> None:
        cursor = self.connection.execute(
            "INSERT INTO localities (name, name_en, place, population, lat, lon) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                locality.name,
                locality.name_en,
                locality.place,
                locality.population,
                locality.lat,
                locality.lon,
            ),
        )
        self.connection.execute(
            "INSERT INTO locality_index VALUES (?, ?, ?, ?, ?)",
            (cursor.lastrowid, locality.lat, locality.lat, locality.lon, locality.lon),
        )
        self.locality_count += 1


class OsmFilterHandler(osmium.SimpleHandler):
    """Pass named drivable roads and localities from an OSM file to a database writer."""

    def __init__(self, writer: RoadDatabaseWriter):
        super().__init__()
        self.writer = writer
        self.next_progress_at = time.monotonic() + PROGRESS_INTERVAL_S

    def log_progress(self) -> None:
        if time.monotonic() < self.next_progress_at:
            return
        LOGGER.info(
            f"{len(self.writer.road_ids)} roads, {self.writer.chunk_count} chunks, "
            f"{self.writer.locality_count} localities so far",
            extra=terminal.PROGRESS,
        )
        self.next_progress_at = time.monotonic() + PROGRESS_INTERVAL_S

    def node(self, node: t.Any) -> None:
        place = node.tags.get("place")
        name = node.tags.get("name")
        if place not in LOCALITY_PLACE_TYPES or not name:
            return
        self.writer.add_locality(
            Locality(
                name=name,
                name_en=node.tags.get("name:en"),
                place=place,
                population=parse_population(node.tags.get("population")),
                lat=node.location.lat,
                lon=node.location.lon,
            )
        )

    def way(self, way: t.Any) -> None:
        highway = way.tags.get("highway")
        if highway not in DRIVABLE_HIGHWAY_CLASSES:
            return
        road = Road(
            name=way.tags.get("name"),
            name_en=way.tags.get("name:en"),
            ref=way.tags.get("ref"),
            highway=highway,
        )
        if not road.name and not road.ref:
            return
        points = [
            (node_ref.location.lat, node_ref.location.lon)
            for node_ref in way.nodes
            if node_ref.location.valid()
        ]
        if len(points) >= 2:
            self.writer.add_road(road, points)
        self.log_progress()


def read_osm_timestamp(pbf_path: Path) -> str:
    """Return the time of the OSM data in a PBF file, or an empty string if it is unknown."""
    reader = osmium.io.Reader(str(pbf_path), osmium.osm.osm_entity_bits.NOTHING)
    try:
        header = reader.header()
        return header.get("osmosis_replication_timestamp") or header.get("timestamp") or ""
    finally:
        reader.close()


def build_database(pbf_path: Path, database_path: Path, source: str) -> None:
    """
    Filter an OSM PBF file into a road database.

    The database is written under a temporary name and replaces the old one only when complete.
    """
    database_path.parent.mkdir(parents=True, exist_ok=True)
    partial_path = metadata.get_partial_path(database_path)

    started_at = time.monotonic()
    connection = sqlite3.connect(partial_path)
    try:
        writer = RoadDatabaseWriter(connection)
        writer.set_meta("format_version", str(DATABASE_FORMAT_VERSION))
        writer.set_meta("osm_timestamp", read_osm_timestamp(pbf_path))
        writer.set_meta("source", source)
        writer.set_meta(
            "built_at", datetime.datetime.now().astimezone().isoformat(timespec="seconds")
        )
        handler = OsmFilterHandler(writer)
        handler.apply_file(
            str(pbf_path),
            locations=True,
            filters=[osmium.filter.KeyFilter("highway", "place")],
        )
        connection.commit()
        LOGGER.info(
            f"Kept {len(writer.road_ids)} roads ({writer.chunk_count} chunks) and "
            f"{writer.locality_count} localities in {time.monotonic() - started_at:.0f} s",
            extra=terminal.SUCCESS,
        )
        connection.execute("VACUUM")
    except BaseException:
        connection.close()
        partial_path.unlink(missing_ok=True)
        raise
    connection.close()
    os.replace(partial_path, database_path)


def download_file(url: str, target_path: Path) -> None:
    """
    Download a file, logging the progress.

    Raises
    ------
    RuntimeError
        If the server sends less data than it announced.
    OSError
        If the download fails or stalls.
    """
    LOGGER.info(f"Downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "dashcam-suite"})
    # Verify the certificate with the OS trust store. OpenSSL's own store may lack the root
    # certificate, e.g. on Windows, which only fetches root certificates on demand.
    ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    next_progress_at = time.monotonic() + PROGRESS_INTERVAL_S
    with (
        urllib.request.urlopen(
            request, context=ssl_context, timeout=DOWNLOAD_TIMEOUT_S
        ) as response,
        target_path.open("wb") as fp,
    ):
        total_size = int(response.headers.get("Content-Length") or 0)
        downloaded_size = 0
        while block := response.read(1024 * 1024):
            fp.write(block)
            downloaded_size += len(block)
            if time.monotonic() >= next_progress_at:
                total_text = f" of {total_size / 1e6:.0f}" if total_size else ""
                LOGGER.info(
                    f"Downloaded {downloaded_size / 1e6:.0f}{total_text} MB",
                    extra=terminal.PROGRESS,
                )
                next_progress_at = time.monotonic() + PROGRESS_INTERVAL_S
    if total_size and downloaded_size != total_size:
        raise RuntimeError(
            f"The download of {url} stopped after {downloaded_size / 1e6:.0f} "
            f"of {total_size / 1e6:.0f} MB"
        )


def update_osm_data(metadata_dir: Path, extract_url: str, pbf_path: Path | None = None) -> None:
    """
    Build the road database from a local PBF file, or download the extract and build it.

    A downloaded extract is deleted afterwards. A local file is kept.
    """
    database_path = get_database_path(metadata_dir)
    if pbf_path is not None:
        LOGGER.info(f"Filtering '{pbf_path}'")
        build_database(pbf_path, database_path, source=pbf_path.name)
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        download_path = database_path.with_name(".download.partial.osm.pbf")
        try:
            download_file(extract_url, download_path)
            LOGGER.info("Filtering the downloaded extract")
            build_database(download_path, database_path, source=extract_url)
        finally:
            download_path.unlink(missing_ok=True)

    size_mb = database_path.stat().st_size / 1e6
    LOGGER.info(f"OSM data: '{database_path}' ({size_mb:.0f} MB)", extra=terminal.SUCCESS)


class RoadDatabase:
    """Read access to a road database."""

    def __init__(self, database_path: Path):
        """
        Open a road database read-only.

        Raises
        ------
        RuntimeError
            If the file is not a readable road database.
        """
        self.path = database_path
        database_uri = f"{database_path.resolve().as_uri()}?mode=ro"
        self.connection = sqlite3.connect(database_uri, uri=True)
        self.roads_by_id: dict[int, Road] = {}
        try:
            meta = dict(self.connection.execute("SELECT key, value FROM meta"))
        except sqlite3.DatabaseError as exc:
            self.connection.close()
            raise RuntimeError(
                f"Cannot read the OSM data in '{database_path}': {exc} "
                f"(run `dashcam enrich --update-osm`)"
            ) from exc
        self.format_version = int(meta.get("format_version", 0))
        self.osm_timestamp = meta.get("osm_timestamp", "")

    def close(self) -> None:
        self.connection.close()

    def get_road(self, road_id: int) -> Road:
        road = self.roads_by_id.get(road_id)
        if road is None:
            name, name_en, ref, highway = self.connection.execute(
                "SELECT name, name_en, ref, highway FROM roads WHERE id = ?", (road_id,)
            ).fetchone()
            road = Road(name=name, name_en=name_en, ref=ref, highway=highway)
            self.roads_by_id[road_id] = road
        return road

    def find_road_chunks(
        self, min_lat: float, max_lat: float, min_lon: float, max_lon: float
    ) -> list[RoadChunk]:
        """Return the road chunks whose bounding boxes intersect the given box."""
        rows = self.connection.execute(
            "SELECT road_chunks.road_id, road_chunks.coords "
            "FROM road_chunk_index JOIN road_chunks ON road_chunks.id = road_chunk_index.id "
            "WHERE road_chunk_index.max_lat >= ? AND road_chunk_index.min_lat <= ? "
            "AND road_chunk_index.max_lon >= ? AND road_chunk_index.min_lon <= ?",
            (min_lat, max_lat, min_lon, max_lon),
        )
        return [RoadChunk(road_id=road_id, points=unpack_points(blob)) for road_id, blob in rows]

    def find_localities(
        self, min_lat: float, max_lat: float, min_lon: float, max_lon: float
    ) -> list[Locality]:
        """Return the localities inside the given box."""
        rows = self.connection.execute(
            "SELECT name, name_en, place, population, lat, lon "
            "FROM locality_index JOIN localities ON localities.id = locality_index.id "
            "WHERE locality_index.max_lat >= ? AND locality_index.min_lat <= ? "
            "AND locality_index.max_lon >= ? AND locality_index.min_lon <= ?",
            (min_lat, max_lat, min_lon, max_lon),
        )
        return [
            Locality(
                name=name, name_en=name_en, place=place, population=population, lat=lat, lon=lon
            )
            for name, name_en, place, population, lat, lon in rows
        ]


def open_database(metadata_dir: Path) -> RoadDatabase | None:
    """
    Open the road database of a metadata directory.

    Returns
    -------
    RoadDatabase | None
        The database, or None if it does not exist or has an outdated format.
    """
    database_path = get_database_path(metadata_dir)
    if not database_path.is_file():
        return None
    database = RoadDatabase(database_path)
    if database.format_version != DATABASE_FORMAT_VERSION:
        database.close()
        return None
    return database


def read_database_timestamp(metadata_dir: Path) -> str | None:
    """Return the timestamp of the OSM data, or None if there is no usable OSM data."""
    database = open_database(metadata_dir)
    if database is None:
        return None
    try:
        return database.osm_timestamp
    finally:
        database.close()
