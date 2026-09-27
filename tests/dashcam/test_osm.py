import _thread
import io
import logging
import sqlite3
import sys
import threading
import time
import urllib.error
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


def test_build_database_builds_in_the_cache_and_leaves_nothing_there(
    tmp_path, osm_pbf_path, local_cache_dir, monkeypatch
):
    # GIVEN a build that records where the database is filtered
    build_paths = []
    original_filter_into_database = osm.filter_into_database

    def recording_filter_into_database(pbf_path, database_path, source):
        build_paths.append(database_path)
        original_filter_into_database(pbf_path, database_path, source)

    monkeypatch.setattr(osm, "filter_into_database", recording_filter_into_database)
    database_path = tmp_path / ".metadata" / "osm" / osm.DATABASE_FILENAME

    # WHEN building the database
    osm.build_database(osm_pbf_path, database_path, source="test")

    # THEN it is filtered in the cache directory and only the finished database is kept
    assert [path.parent for path in build_paths] == [local_cache_dir]
    assert list(local_cache_dir.iterdir()) == []
    assert sorted(path.name for path in database_path.parent.iterdir()) == [osm.DATABASE_FILENAME]
    database = osm.open_database(tmp_path / ".metadata")
    assert database is not None
    database.close()


def test_build_database_cleans_up_after_failure(tmp_path, local_cache_dir, monkeypatch):
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
    assert list(local_cache_dir.iterdir()) == []


def test_build_database_cleans_up_after_failed_copy(
    tmp_path, osm_pbf_path, local_cache_dir, monkeypatch
):
    # GIVEN an existing database and a copy to the metadata directory that fails
    database_path = tmp_path / "osm" / osm.DATABASE_FILENAME
    database_path.parent.mkdir()
    database_path.write_bytes(b"old database")

    def failing_copyfileobj(source_fp, target_fp, length):
        target_fp.write(b"partial")
        raise OSError("network drive disconnected")

    monkeypatch.setattr(osm.shutil, "copyfileobj", failing_copyfileobj)

    # WHEN building the database
    with pytest.raises(OSError, match="disconnected"):
        osm.build_database(osm_pbf_path, database_path, source="test")

    # THEN the old database is kept and no partial file is left in either directory
    assert database_path.read_bytes() == b"old database"
    assert sorted(path.name for path in database_path.parent.iterdir()) == [osm.DATABASE_FILENAME]
    assert list(local_cache_dir.iterdir()) == []


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

    def fake_urlopen(request, context, timeout):
        requests.append((request, context, timeout))
        return FakeResponse(content)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    target_path = tmp_path / "extract.osm.pbf"

    # WHEN downloading a file
    osm.download_file("https://example.com/extract.osm.pbf", target_path)

    # THEN the content is saved, the certificate is verified with the OS trust store, and a
    # stalled connection times out
    assert target_path.read_bytes() == content
    [(request, ssl_context, timeout)] = requests
    assert request.full_url == "https://example.com/extract.osm.pbf"
    assert isinstance(ssl_context, truststore.SSLContext)
    assert timeout == osm.DOWNLOAD_TIMEOUT_S


def test_download_file_with_truncated_response(tmp_path, monkeypatch):
    # GIVEN a server that announces more content than it sends
    response = FakeResponse(b"x" * 1000)
    response.headers = {"Content-Length": "5000000"}
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, context, timeout: response)

    # WHEN downloading a file
    # THEN the download fails
    with pytest.raises(RuntimeError, match="stopped after 0 of 5 MB"):
        osm.download_file("https://example.com/extract.osm.pbf", tmp_path / "extract.osm.pbf")


def test_open_database_in_directory_with_uri_characters(tmp_path, osm_pbf_path):
    # GIVEN OSM data in a metadata directory whose path contains URI special characters.
    # Windows does not allow `?` in file names.
    directory_name = "trips #1 %" if sys.platform == "win32" else "trips #1 ?%"
    metadata_dir = tmp_path / directory_name
    osm.update_osm_data(metadata_dir, "unused", pbf_path=osm_pbf_path)

    # WHEN opening the database
    database = osm.open_database(metadata_dir)

    # THEN it opens
    assert database is not None
    database.close()


def test_open_database_with_corrupt_file(tmp_path):
    # GIVEN a road database file that is not a database
    database_path = osm.get_database_path(tmp_path)
    database_path.parent.mkdir(parents=True)
    database_path.write_bytes(b"not a database" * 100)

    # WHEN opening it
    # THEN the error names the file and how to rebuild it
    with pytest.raises(RuntimeError, match="roads.sqlite.*--update-osm"):
        osm.open_database(tmp_path)


@pytest.fixture
def download_path(local_cache_dir):
    """Download to the cache directory inside the test directory."""
    return local_cache_dir / osm.DOWNLOAD_FILENAME


@pytest.fixture
def fake_download(monkeypatch, osm_pbf_path, download_path):
    """Replace the download with a copy of the synthetic OSM file."""
    downloads = []

    def _fake_download_file(url, target_path):
        downloads.append((url, target_path))
        target_path.write_bytes(osm_pbf_path.read_bytes())

    monkeypatch.setattr(osm, "download_file", _fake_download_file)
    return downloads


def test_update_osm_data_downloads_and_deletes_extract(
    tmp_path, fake_download, download_path, osm_timestamp
):
    # GIVEN a metadata directory without OSM data
    metadata_dir = tmp_path / ".metadata"

    # WHEN updating the OSM data
    osm.update_osm_data(metadata_dir, extract_url="https://example.org/ukraine.osm.pbf")

    # THEN the extract is downloaded to the cache directory, filtered, and deleted
    assert fake_download == [("https://example.org/ukraine.osm.pbf", download_path)]
    assert list(download_path.parent.iterdir()) == []
    database = osm.open_database(metadata_dir)
    assert database is not None
    assert database.osm_timestamp == osm_timestamp
    database.close()
    assert sorted(path.name for path in (metadata_dir / "osm").iterdir()) == [osm.DATABASE_FILENAME]


def test_update_osm_data_deletes_extract_after_failure(
    tmp_path, fake_download, download_path, monkeypatch
):
    # GIVEN a download that cannot be filtered
    metadata_dir = tmp_path / ".metadata"

    def failing_build_database(pbf_path, database_path, source):
        raise RuntimeError("corrupt file")

    monkeypatch.setattr(osm, "build_database", failing_build_database)

    # WHEN updating the OSM data
    with pytest.raises(RuntimeError):
        osm.update_osm_data(metadata_dir, extract_url="https://example.org/ukraine.osm.pbf")

    # THEN the download is deleted
    assert list(download_path.parent.iterdir()) == []
    assert list((metadata_dir / "osm").iterdir()) == []


def test_download_file_stops_at_once_on_ctrl_c_while_connecting(tmp_path, monkeypatch):
    # GIVEN a connection that blocks (as the certificate check of the OS trust store can)
    connection_released = threading.Event()

    def blocking_urlopen(request, context, timeout):
        connection_released.wait()
        raise urllib.error.URLError("released")

    monkeypatch.setattr(urllib.request, "urlopen", blocking_urlopen)
    threading.Timer(0.3, _thread.interrupt_main).start()

    # WHEN Ctrl+C is pressed while connecting
    started_at = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            osm.download_file("https://example.com/extract.osm.pbf", tmp_path / "extract.osm.pbf")
        elapsed_s = time.monotonic() - started_at
    finally:
        connection_released.set()

    # THEN the download stops without waiting for the connection, and leaves no file
    assert elapsed_s < 1
    assert not (tmp_path / "extract.osm.pbf").exists()


def test_download_file_closes_its_file_on_ctrl_c_while_downloading(tmp_path, monkeypatch):
    # GIVEN a server that sends the file slowly
    class SlowResponse(FakeResponse):
        def read(self, size=-1):
            time.sleep(0.05)
            return super().read(1000)

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, context, timeout: SlowResponse(b"x" * 1_000_000)
    )
    threading.Timer(0.3, _thread.interrupt_main).start()
    target_path = tmp_path / "extract.osm.pbf"

    # WHEN Ctrl+C is pressed while downloading
    with pytest.raises(KeyboardInterrupt):
        osm.download_file("https://example.com/extract.osm.pbf", target_path)

    # THEN the partial file is closed, so that it can be deleted (which Windows requires)
    size_after_stop = target_path.stat().st_size
    time.sleep(0.2)
    assert 0 < size_after_stop == target_path.stat().st_size
    target_path.unlink()


def test_download_file_reports_errors_of_the_download(tmp_path, monkeypatch):
    # GIVEN a server that cannot be reached
    def failing_urlopen(request, context, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)

    # WHEN downloading a file
    # THEN the error of the download is raised
    with pytest.raises(OSError, match="no route to host"):
        osm.download_file("https://example.com/extract.osm.pbf", tmp_path / "extract.osm.pbf")


def test_download_file_logs_progress(tmp_path, monkeypatch, caplog):
    # GIVEN a server that sends the file slowly, and frequent progress reports
    class SlowResponse(FakeResponse):
        def read(self, size=-1):
            time.sleep(0.02)
            return super().read(1000)

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, context, timeout: SlowResponse(b"x" * 20000)
    )
    monkeypatch.setattr(osm, "DOWNLOAD_PROGRESS_INTERVAL_S", 0.1)
    monkeypatch.setattr(osm, "DOWNLOAD_POLL_INTERVAL_S", 0.01)
    caplog.set_level(logging.INFO)

    # WHEN downloading a file
    osm.download_file("https://example.com/extract.osm.pbf", tmp_path / "extract.osm.pbf")

    # THEN the progress is logged while it downloads
    assert "Downloaded 0 of 0 MB (" in caplog.text
    assert (tmp_path / "extract.osm.pbf").stat().st_size == 20000


@pytest.mark.parametrize(
    ("downloaded_size", "total_size", "elapsed_s", "expected_text"),
    [
        (120_000_000, 780_000_000, 5.0, "Downloaded 120 of 780 MB (15%, 24.0 MB/s)"),
        (120_000_000, 0, 10.0, "Downloaded 120 MB (12.0 MB/s)"),
    ],
)
def test_format_download_progress(downloaded_size, total_size, elapsed_s, expected_text):
    assert osm.format_download_progress(downloaded_size, total_size, elapsed_s) == expected_text


def test_get_download_path_is_in_the_user_cache_directory(local_cache_dir):
    # GIVEN a user cache directory
    # WHEN getting the download path
    download_path = osm.get_download_path()

    # THEN the extract goes there, not to the metadata directory
    assert download_path == local_cache_dir / osm.DOWNLOAD_FILENAME
