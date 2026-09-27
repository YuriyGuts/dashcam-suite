import contextlib
import dataclasses
import datetime
import math
import subprocess

import osmium
import osmium.io
import pytest
from osmium.osm import mutable
from rich.console import Console

from dashcam import cleaning
from dashcam import enrich
from dashcam import metadata
from dashcam import osm
from dashcam import terminal
from dashcam import video
from dashcam.config import get_platform_defaults

KYIV_SUMMER = datetime.timezone(datetime.timedelta(hours=3))

# Origin of the synthetic map in `osm_pbf_path`, and its OSM data timestamp.
MAP_ORIGIN = (49.8, 24.0)
OSM_TIMESTAMP = "2026-09-25T20:24:36Z"

# Roads of the synthetic map, as (tags, points in meters east and north of the origin).
SYNTHETIC_ROADS = [
    ({"highway": "primary", "name": "Main", "name:en": "Main Street"}, [(0, 0), (500, 0)]),
    ({"highway": "primary", "name": "Main"}, [(500, 0), (1000, 0)]),
    ({"highway": "residential", "name": "Cross"}, [(500, -500), (500, 0), (500, 500)]),
    ({"highway": "residential", "name": "Parallel"}, [(0, 80), (1000, 80)]),
    ({"highway": "trunk", "ref": "M06"}, [(0, -1000), (1000, -1000)]),
    ({"highway": "footway", "name": "Path"}, [(0, -300), (1000, -300)]),
    ({"highway": "residential"}, [(0, -600), (1000, -600)]),
]

# Localities of the synthetic map, as (tags, point in meters east and north of the origin).
SYNTHETIC_LOCALITIES = [
    ({"place": "city", "name": "Київ", "name:en": "Kyiv", "population": "2 950 000"}, (0, 0)),
    ({"place": "village", "name": "Гатне"}, (0, -12000)),
    ({"place": "suburb", "name": "Оболонь"}, (1000, 0)),
]


def local_to_lat_lon(east_m, north_m):
    """Convert meters east and north of the synthetic map origin to latitude and longitude."""
    meters_per_degree = 6_371_000 * math.pi / 180
    lat = MAP_ORIGIN[0] + north_m / meters_per_degree
    lon = MAP_ORIGIN[1] + east_m / (meters_per_degree * math.cos(math.radians(MAP_ORIGIN[0])))
    return lat, lon


@pytest.fixture(autouse=True)
def local_cache_dir(tmp_path, monkeypatch):
    """Keep the user cache directory inside the test directory."""
    monkeypatch.setattr(
        osm.platformdirs, "user_cache_path", lambda name, appauthor: tmp_path / "cache" / name
    )
    return tmp_path / "cache" / "dashcam"


@pytest.fixture(autouse=True)
def plain_terminal_output(monkeypatch):
    """Print without styles, even if the environment forces colors (e.g. `FORCE_COLOR`)."""
    for console_name, is_stderr in (("STDOUT", False), ("STDERR", True)):
        plain_console = Console(
            stderr=is_stderr,
            force_terminal=False,
            color_system=None,
            theme=terminal.THEME,
            markup=False,
            highlight=False,
            emoji=False,
        )
        monkeypatch.setattr(terminal, console_name, plain_console)


@pytest.fixture
def to_lat_lon():
    return local_to_lat_lon


@pytest.fixture
def osm_timestamp():
    return OSM_TIMESTAMP


@pytest.fixture
def osm_pbf_path(tmp_path):
    """Write a small OSM file with the synthetic roads and localities."""
    pbf_path = tmp_path / "synthetic.osm.pbf"
    header = osmium.io.Header()
    header.set("osmosis_replication_timestamp", OSM_TIMESTAMP)
    writer = osmium.SimpleWriter(str(pbf_path), header=header)
    node_ids = {}
    ways = []
    for way_id, (tags, points) in enumerate(SYNTHETIC_ROADS, start=1):
        for point in points:
            if point not in node_ids:
                node_ids[point] = len(node_ids) + 1
        ways.append(mutable.Way(id=way_id, nodes=[node_ids[point] for point in points], tags=tags))
    for (east_m, north_m), node_id in node_ids.items():
        lat, lon = local_to_lat_lon(east_m, north_m)
        writer.add_node(mutable.Node(id=node_id, location=(lon, lat)))
    for index, (tags, (east_m, north_m)) in enumerate(SYNTHETIC_LOCALITIES):
        lat, lon = local_to_lat_lon(east_m, north_m)
        writer.add_node(mutable.Node(id=1000 + index, location=(lon, lat), tags=tags))
    for way in ways:
        writer.add_way(way)
    writer.close()
    return pbf_path


@pytest.fixture
def osm_metadata_dir(tmp_path, osm_pbf_path):
    """A metadata directory with the synthetic map as its OSM data."""
    metadata_dir = tmp_path / ".metadata"
    osm.build_database(osm_pbf_path, osm.get_database_path(metadata_dir), source="test")
    return metadata_dir


@pytest.fixture
def road_database(osm_metadata_dir):
    database = osm.open_database(osm_metadata_dir)
    assert database is not None
    yield database
    database.close()


@pytest.fixture
def config():
    return dataclasses.replace(
        get_platform_defaults(),
        ffmpeg_executable="ffmpeg",
        ffprobe_executable="ffprobe",
        hwaccel_options="-hwaccel videotoolbox",
        video_codec_options="-c:v libx265 -crf 30 -preset fast",
        audio_codec_options="-c:a aac -b:a 128k",
        encode_job_count=2,
        extract_job_count=4,
        min_trip_gap_hours=3,
    )


@pytest.fixture
def raw_video_dir(tmp_path):
    raw_video_dir = tmp_path / "DCIM" / "Movie"
    raw_video_dir.mkdir(parents=True)
    return raw_video_dir


@pytest.fixture
def make_raw_videos(raw_video_dir):
    def _make_raw_videos(*filenames):
        paths = []
        for filename in filenames:
            path = raw_video_dir / filename
            path.write_bytes(b"")
            paths.append(path)
        return paths

    return _make_raw_videos


class FakeFfmpeg:
    """Records ffmpeg invocations and simulates their outcome without running ffmpeg."""

    def __init__(self):
        self.calls = []
        self.encode_return_code = 0
        self.encode_stderr = ""
        self.progress_reports = [video.FfmpegProgress(output_time_s=0.0, speed=None)]
        self.unreadable_paths = set()

    def run(self, cmd, **kwargs):
        self.calls.append(cmd)
        is_readability_check = cmd[-3:] == ["-f", "null", "-"]
        if is_readability_check:
            input_path = cmd[cmd.index("-i") + 1]
            return_code = 1 if input_path in self.unreadable_paths else 0
            return subprocess.CompletedProcess(cmd, return_code)

        raise AssertionError(f"Unexpected ffmpeg command: {cmd}")

    def iter_progress(self, cmd):
        self.calls.append(cmd)
        output_path = cmd[-1]
        with open(output_path, "wb") as fp:
            fp.write(b"partial video")
        yield from self.progress_reports
        if self.encode_return_code != 0:
            raise subprocess.CalledProcessError(
                self.encode_return_code, cmd, stderr=self.encode_stderr
            )


@pytest.fixture
def fake_ffmpeg(monkeypatch):
    ffmpeg = FakeFfmpeg()
    monkeypatch.setattr("dashcam.encode.subprocess.run", ffmpeg.run)
    monkeypatch.setattr("dashcam.video.iter_ffmpeg_progress", ffmpeg.iter_progress)
    monkeypatch.setattr("dashcam.video.probe_duration", lambda path, ffprobe_executable: 60.0)
    return ffmpeg


@pytest.fixture
def no_os_sleep_prevention(monkeypatch):
    monkeypatch.setattr("dashcam.encode.prevent_os_sleep", contextlib.nullcontext)


class SerialPool:
    """Runs pool jobs in the test process so that monkeypatched functions stay in effect."""

    def imap_unordered(self, func, items):
        return (func(item) for item in items)


@contextlib.contextmanager
def serial_worker_pool(process_count):
    yield SerialPool()


@pytest.fixture
def serial_pool(monkeypatch):
    monkeypatch.setattr("dashcam.encode.worker_pool", serial_worker_pool)
    monkeypatch.setattr("dashcam.extract.worker_pool", serial_worker_pool)


@pytest.fixture
def make_track():
    def _make_track(
        video_filename="2026-09-25 Trip 11-17.mp4", fingerprint="100:abc", sample_count=3
    ):
        raw_samples = []
        clean_samples = []
        for index in range(sample_count):
            lat = round(49.8 + index * 0.0003, 6)
            raw_samples.append(
                cleaning.RawSample(
                    t=float(index),
                    gps_text=f"30 KM/H N{lat:.6f} E24.000000",
                    clock_text=f"2026/09/25 11:17:{index:02d}",
                    gps_score=0.97,
                    clock_score=0.99,
                )
            )
            clean_samples.append(
                cleaning.CleanSample(
                    t=float(index),
                    time=datetime.datetime(2026, 9, 25, 11, 17, index, tzinfo=KYIV_SUMMER),
                    lat=lat,
                    lon=24.0,
                    kmh=30,
                    status=cleaning.STATUS_OK,
                )
            )
        return metadata.Track(
            video_filename=video_filename,
            fingerprint=fingerprint,
            video_size=100,
            video_mtime=1790000000.5,
            extraction_status=metadata.EXTRACTION_OK,
            extracted_at="2026-09-26T16:00:00+03:00",
            extractor_version=metadata.EXTRACTOR_VERSION,
            cleaning_version=metadata.CLEANING_VERSION,
            video_width=2560,
            video_height=1440,
            duration_s=float(sample_count),
            overrides=cleaning.Overrides(bad_ranges_s=[(1.0, 2.0)]),
            streets=[],
            localities={},
            enrichment=None,
            raw_samples=raw_samples,
            clean_samples=clean_samples,
        )

    return _make_track


@pytest.fixture
def make_named_track(make_track):
    """Make a track with a street list, enriched from its current samples."""

    def _make_named_track(video_filename, streets, start_hour=11):
        track = make_track(video_filename=video_filename)
        for index, sample in enumerate(track.clean_samples):
            sample.time = datetime.datetime(2026, 9, 25, start_hour, 17, index, tzinfo=KYIV_SUMMER)
        track.streets = streets
        track.enrichment = metadata.Enrichment(
            enricher_version=enrich.ENRICHER_VERSION,
            osm_timestamp="2026-09-25T20:24:36Z",
            samples_digest=enrich.compute_samples_digest(track),
            enriched_at="2026-09-26T20:00:00+03:00",
        )
        return track

    return _make_named_track
