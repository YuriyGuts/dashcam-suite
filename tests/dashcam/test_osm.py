import io
import logging
import sqlite3
import urllib.request

import pytest
import truststore

from dashcam import osm


def test_pack_points_round_trip():
    # GIVEN points with OSM precision
    points = [(49.8397745, 24.0296057), (-33.8688, 151.2093), (0.0, -0.0000001)]

    # WHEN packing and unpacking them
    unpacked = osm.unpack_points(osm.pack_points(points))

    # THEN the points are unchanged
    assert unpacked == pytest.approx(points, abs=1e-9)


def test_split_into_chunks_shares_ends():
    # GIVEN a polyline longer than one chunk
    points = [(float(index), 0.0) for index in range(osm.CHUNK_NODE_COUNT + 3)]

    # WHEN splitting it
    chunks = osm.split_into_chunks(points)

    # THEN consecutive chunks share a point and together cover the polyline
    assert len(chunks) == 2
    assert len(chunks[0]) == osm.CHUNK_NODE_COUNT
    assert chunks[0][-1] == chunks[1][0]
    assert chunks[1][-1] == points[-1]


def test_split_into_chunks_with_two_points():
    # GIVEN a single segment
    points = [(0.0, 0.0), (1.0, 1.0)]

    # WHEN splitting it
    chunks = osm.split_into_chunks(points)

    # THEN it is one chunk
    assert chunks == [points]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("720000", 720000),
        ("720 000", 720000),
        ("1,234", 1234),
        ("500;600", 500),
        ("unknown", None),
        (None, None),
    ],
)
def test_parse_population(text, expected):
    assert osm.parse_population(text) == expected


def test_build_database_keeps_named_drivable_roads(road_database):
    # WHEN reading all roads of the synthetic map
    rows = road_database.connection.execute("SELECT name, ref, highway FROM roads").fetchall()

    # THEN footways and unnamed roads are dropped, and ref-only roads are kept
    assert sorted(rows, key=str) == sorted(
        [
            ("Main", None, "primary"),
            ("Main", None, "primary"),
            ("Cross", None, "residential"),
            ("Parallel", None, "residential"),
            (None, "M06", "trunk"),
        ],
        key=str,
    )


def test_build_database_records_osm_timestamp(road_database, osm_timestamp):
    assert road_database.osm_timestamp == osm_timestamp
    assert road_database.format_version == osm.DATABASE_FORMAT_VERSION


def test_find_road_chunks_returns_nearby_roads_only(road_database, to_lat_lon):
    # GIVEN a small box around the middle of `Cross`, north of `Main`
    min_lat, min_lon = to_lat_lon(490, 200)
    max_lat, max_lon = to_lat_lon(510, 210)

    # WHEN searching for road chunks
    chunks = road_database.find_road_chunks(min_lat, max_lat, min_lon, max_lon)

    # THEN only `Cross` is found, with its full geometry
    assert [road_database.get_road(chunk.road_id).name for chunk in chunks] == ["Cross"]
    assert chunks[0].points[1] == pytest.approx(to_lat_lon(500, 0), abs=1e-6)


def test_find_localities_skips_suburbs(road_database, to_lat_lon):
    # GIVEN a box around the city and the suburb
    min_lat, min_lon = to_lat_lon(-100, -100)
    max_lat, max_lon = to_lat_lon(1100, 100)

    # WHEN searching for localities
    localities = road_database.find_localities(min_lat, max_lat, min_lon, max_lon)

    # THEN only the city is found, with its parsed population
    assert [(locality.name, locality.population) for locality in localities] == [("Київ", 2950000)]
    assert localities[0].name_en == "Kyiv"


def test_open_database_without_data(tmp_path):
    assert osm.open_database(tmp_path) is None


def test_open_database_with_other_format_version(osm_metadata_dir):
    # GIVEN a database written in another format version
    database_path = osm.get_database_path(osm_metadata_dir)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE meta SET value = '0' WHERE key = 'format_version'")

    # WHEN opening it
    database = osm.open_database(osm_metadata_dir)

    # THEN it is treated as missing
    assert database is None


def test_build_database_cleans_up_after_failure(tmp_path, monkeypatch):
    # GIVEN an existing database and a build that fails
    database_path = tmp_path / "osm" / osm.DATABASE_FILENAME
    database_path.parent.mkdir()
    database_path.write_bytes(b"old database")

    def failing_apply_file(self, *args, **kwargs):
        raise RuntimeError("corrupt file")

    monkeypatch.setattr(osm.OsmFilterHandler, "apply_file", failing_apply_file)
    monkeypatch.setattr(osm, "read_osm_timestamp", lambda pbf_path: "")

    # WHEN building the database
    with pytest.raises(RuntimeError):
        osm.build_database(tmp_path / "broken.osm.pbf", database_path, source="test")

    # THEN the old database is kept and no partial file is left
    assert database_path.read_bytes() == b"old database"
    assert sorted(path.name for path in database_path.parent.iterdir()) == [osm.DATABASE_FILENAME]


def test_update_osm_data_with_local_file_keeps_it(tmp_path, osm_pbf_path, caplog):
    # GIVEN a local OSM file
    caplog.set_level(logging.INFO)
    metadata_dir = tmp_path / ".metadata"

    # WHEN building the OSM data from it
    osm.update_osm_data(metadata_dir, extract_url="unused", pbf_path=osm_pbf_path)

    # THEN the database is built and the local file is kept
    assert osm.open_database(metadata_dir) is not None
    assert osm_pbf_path.exists()


class FakeResponse(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.headers = {"Content-Length": str(len(content))}


def test_download_file_verifies_certificate_with_os_trust_store(tmp_path, monkeypatch):
    # GIVEN a server that returns some content
    content = bytes(range(256)) * 10
    requests = []

    def fake_urlopen(request, context):
        requests.append((request, context))
        return FakeResponse(content)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    target_path = tmp_path / "extract.osm.pbf"

    # WHEN downloading a file
    osm.download_file("https://example.com/extract.osm.pbf", target_path)

    # THEN the content is saved, and the certificate is verified with the OS trust store
    assert target_path.read_bytes() == content
    [(request, ssl_context)] = requests
    assert request.full_url == "https://example.com/extract.osm.pbf"
    assert isinstance(ssl_context, truststore.SSLContext)


@pytest.fixture
def fake_download(monkeypatch, osm_pbf_path):
    """Replace the download with a copy of the synthetic OSM file."""
    downloads = []

    def _fake_download_file(url, target_path):
        downloads.append((url, target_path))
        target_path.write_bytes(osm_pbf_path.read_bytes())

    monkeypatch.setattr(osm, "download_file", _fake_download_file)
    return downloads


def test_update_osm_data_downloads_and_deletes_extract(tmp_path, fake_download, osm_timestamp):
    # GIVEN a metadata directory without OSM data
    metadata_dir = tmp_path / ".metadata"

    # WHEN updating the OSM data
    osm.update_osm_data(metadata_dir, extract_url="https://example.org/ukraine.osm.pbf")

    # THEN the extract is downloaded, filtered, and deleted
    assert [url for url, _ in fake_download] == ["https://example.org/ukraine.osm.pbf"]
    database = osm.open_database(metadata_dir)
    assert database is not None
    assert database.osm_timestamp == osm_timestamp
    database.close()
    assert sorted(path.name for path in (metadata_dir / "osm").iterdir()) == [osm.DATABASE_FILENAME]


def test_update_osm_data_deletes_extract_after_failure(tmp_path, fake_download, monkeypatch):
    # GIVEN a download that cannot be filtered
    metadata_dir = tmp_path / ".metadata"

    def failing_build_database(pbf_path, database_path, source):
        raise RuntimeError("corrupt file")

    monkeypatch.setattr(osm, "build_database", failing_build_database)

    # WHEN updating the OSM data
    with pytest.raises(RuntimeError):
        osm.update_osm_data(metadata_dir, extract_url="https://example.org/ukraine.osm.pbf")

    # THEN the download is deleted
    assert list((metadata_dir / "osm").iterdir()) == []
