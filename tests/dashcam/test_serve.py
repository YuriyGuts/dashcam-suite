import contextlib
import http.client
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import PureWindowsPath

import pytest

from dashcam import metadata
from dashcam import serve


@pytest.fixture
def library_dir(tmp_path):
    library_dir = tmp_path / "videos"
    library_dir.mkdir()
    return library_dir


@pytest.fixture
def store(tmp_path):
    store = metadata.MetadataStore(tmp_path / ".metadata")
    store.ensure_dirs()
    return store


@pytest.fixture
def app(library_dir, store):
    return serve.VisualizerApp(library_dir, store.root)


@pytest.fixture
def add_track(store, make_track):
    def _add_track(video_filename="2026-09-25 Trip.mp4", **kwargs):
        track = make_track(video_filename=video_filename, **kwargs)
        store.save_track(track)
        return track

    return _add_track


@pytest.fixture
def server(app):
    """Run the server on a free port in a background thread and yield its base URL."""
    http_server = serve.VisualizerServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http_server.server_address[1]}"
    http_server.shutdown()
    http_server.server_close()
    thread.join()


def fetch(url, headers=None, method="GET"):
    """Return the status, headers, and body of a response, including error responses."""
    request = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


@pytest.mark.parametrize(
    ("range_header", "expected_range"),
    [
        (None, None),
        ("", None),
        ("bytes=0-99", (0, 99)),
        ("bytes=100-", (100, 999)),
        ("bytes=-100", (900, 999)),
        ("bytes=-5000", (0, 999)),
        ("bytes=900-5000", (900, 999)),
        ("BYTES = 1-2", (1, 2)),
        ("bytes=0-1,5-6", None),
        ("bytes=5-1", None),
        ("bytes=-", None),
        ("items=0-1", None),
        pytest.param("bytes=" + "9" * 5000 + "-", None, id="bytes=<5000 digits>-"),
        ("bytes=a-b", None),
    ],
)
def test_parse_byte_range(range_header, expected_range):
    # GIVEN a 1000-byte file and a range header

    # WHEN parsing the header
    byte_range = serve.parse_byte_range(range_header, 1000)

    # THEN the inclusive byte range is returned, or None for the whole file
    assert byte_range == expected_range


def test_parse_byte_range_beyond_end_of_file():
    # GIVEN a range starting after the last byte

    # WHEN parsing the header
    # THEN the range cannot be satisfied
    with pytest.raises(serve.RangeNotSatisfiableError):
        serve.parse_byte_range("bytes=1000-", 1000)


def test_parse_byte_range_suffix_of_empty_file():
    # GIVEN a suffix range for an empty file

    # WHEN parsing the header
    # THEN the range cannot be satisfied
    with pytest.raises(serve.RangeNotSatisfiableError):
        serve.parse_byte_range("bytes=-10", 0)


def test_parse_byte_range_zero_length_suffix():
    # GIVEN a suffix range of zero bytes

    # WHEN parsing the header
    # THEN the range cannot be satisfied
    with pytest.raises(serve.RangeNotSatisfiableError):
        serve.parse_byte_range("bytes=-0", 1000)


def test_safe_join_nested_path(tmp_path):
    # GIVEN a nested relative path

    # WHEN joining it
    path = serve.safe_join(tmp_path, "vendor/leaflet/leaflet.js")

    # THEN it resolves inside the root
    assert path == tmp_path / "vendor" / "leaflet" / "leaflet.js"


@pytest.mark.parametrize(
    "relative_path",
    ["../secret", "a/../../secret", ".hidden", "a//b", "", "a/", "a\\..\\b", "a\0b", "/etc/passwd"],
)
def test_safe_join_refuses_unsafe_paths(tmp_path, relative_path):
    # GIVEN a path that could leave the root or reach hidden files

    # WHEN joining it
    path = serve.safe_join(tmp_path, relative_path)

    # THEN it is refused
    assert path is None


@pytest.mark.parametrize("relative_path", ["D:secret.txt", "D:/secret.txt", "a/D:secret.txt"])
def test_safe_join_refuses_other_windows_drives(relative_path):
    # GIVEN a Windows root and a path with a drive segment
    root = PureWindowsPath("C:/site/web")

    # WHEN joining it
    path = serve.safe_join(root, relative_path)

    # THEN it is refused instead of switching to the other drive
    assert path is None


def test_get_content_type_is_case_insensitive(tmp_path):
    # GIVEN a video with an upper-case extension

    # WHEN looking up its content type
    content_type = serve.get_content_type(tmp_path / "TRIP.MP4")

    # THEN it is served as MP4 video
    assert content_type == "video/mp4"


def test_get_content_type_of_unknown_extension(tmp_path):
    # GIVEN a file with an unknown extension

    # WHEN looking up its content type
    content_type = serve.get_content_type(tmp_path / "notes.xyz")

    # THEN it is served as binary data
    assert content_type == "application/octet-stream"


def test_is_index_stale_without_index(store, add_track):
    # GIVEN a track but no index
    add_track()

    # WHEN checking the index
    # THEN it is stale
    assert serve.is_index_stale(store)


def test_is_index_stale_with_current_index(store, add_track):
    # GIVEN an index built after the last track change
    add_track()
    store.rebuild_index()

    # WHEN checking the index
    # THEN it is current
    assert not serve.is_index_stale(store)


def test_is_index_stale_after_track_changes(store, add_track):
    # GIVEN a track modified after the index was built
    track = add_track()
    store.rebuild_index()
    path = store.track_path(track.stem)
    index_mtime_ns = store.index_path.stat().st_mtime_ns
    os.utime(path, ns=(index_mtime_ns, index_mtime_ns + 1_000_000_000))

    # WHEN checking the index
    # THEN it is stale
    assert serve.is_index_stale(store)


def test_is_index_stale_after_track_removal(store, add_track):
    # GIVEN an index that lists a track that is gone
    track = add_track()
    store.rebuild_index()
    store.track_path(track.stem).unlink()

    # WHEN checking the index
    # THEN it is stale
    assert serve.is_index_stale(store)


def test_is_index_stale_after_new_preview(store, add_track):
    # GIVEN a preview made after the index was built
    track = add_track()
    store.rebuild_index()
    store.previews_dir.mkdir()
    preview_path = store.preview_path(track.stem)
    preview_path.write_bytes(b"preview")
    index_mtime_ns = store.index_path.stat().st_mtime_ns
    os.utime(preview_path, ns=(index_mtime_ns, index_mtime_ns + 1_000_000_000))

    # WHEN checking the index
    # THEN it is stale, because the index records `has_preview`
    assert serve.is_index_stale(store)


def test_is_index_stale_with_other_format_version(store, add_track):
    # GIVEN an index written by another version
    add_track()
    index = store.rebuild_index()
    index["format_version"] = metadata.INDEX_FORMAT_VERSION - 1
    store.index_path.write_text(json.dumps(index), encoding="utf-8")

    # WHEN checking the index
    # THEN it is stale
    assert serve.is_index_stale(store)


def test_is_index_stale_without_geometry(store, add_track):
    # GIVEN an index whose geometry file is gone
    add_track()
    store.rebuild_index()
    store.geometry_path.unlink()

    # WHEN checking the index
    # THEN it is stale
    assert serve.is_index_stale(store)


def test_is_index_stale_with_corrupt_index(store, add_track):
    # GIVEN an index that is not valid JSON
    add_track()
    store.index_path.write_text("{", encoding="utf-8")

    # WHEN checking the index
    # THEN it is stale
    assert serve.is_index_stale(store)


def test_load_index_rebuilds_stale_index(app, store, add_track, caplog):
    # GIVEN a track without an index
    caplog.set_level(logging.INFO)
    add_track()

    # WHEN loading the index
    index = app.load_index()

    # THEN it is rebuilt and written
    assert [trip["id"] for trip in index["trips"]] == ["2026-09-25 Trip"]
    assert store.index_path.is_file()
    assert "Rebuilding the trip index" in caplog.text


def test_get_trips_with_available_video(app, library_dir, add_track):
    # GIVEN a track whose video is in the library directory
    add_track("2026-09-25 Trip #1.mp4")
    (library_dir / "2026-09-25 Trip #1.mp4").write_bytes(b"video")

    # WHEN listing the trips
    trip = app.get_trips()["trips"][0]

    # THEN the video URL is quoted and there is no preview
    assert trip["video_url"] == "/videos/2026-09-25%20Trip%20%231.mp4"
    assert trip["track_url"] == "/tracks/2026-09-25%20Trip%20%231.json"
    assert trip["preview_url"] is None
    assert trip["has_preview"] is False
    assert trip["streets"] == []


def test_get_trips_with_unreachable_video_and_preview(app, store, add_track):
    # GIVEN a track with a preview but without its video
    track = add_track()
    store.previews_dir.mkdir()
    store.preview_path(track.stem).write_bytes(b"preview")

    # WHEN listing the trips
    trip = app.get_trips()["trips"][0]

    # THEN only the preview can be played
    assert trip["video_url"] is None
    assert trip["preview_url"] == "/previews/2026-09-25%20Trip.mp4"
    assert trip["has_preview"] is True


def test_get_trips_includes_street_names(app, store, make_track):
    # GIVEN a track with streets
    track = make_track()
    track.streets = [{"name": "Lesi Ukrainky", "distance_m": 700}, {"name": "Hrushevskoho"}]
    store.save_track(track)

    # WHEN listing the trips
    trip = app.get_trips()["trips"][0]

    # THEN the street names are listed
    assert trip["streets"] == ["Lesi Ukrainky", "Hrushevskoho"]


def test_get_trips_does_not_read_tracks_when_index_is_current(app, store, add_track, monkeypatch):
    # GIVEN a current index
    add_track()
    store.rebuild_index()
    monkeypatch.setattr(
        "dashcam.metadata.MetadataStore.load_track",
        lambda self, stem: pytest.fail(f"Track '{stem}' was read"),
    )
    monkeypatch.setattr(
        "dashcam.metadata.MetadataStore.iter_track_files",
        lambda self: pytest.fail("Tracks were read"),
    )

    # WHEN listing the trips and loading the geometry
    trips = app.get_trips()["trips"]
    geometry = app.get_geometry()["trips"]

    # THEN both come from the index files
    assert [trip["id"] for trip in trips] == ["2026-09-25 Trip"]
    assert list(geometry) == ["2026-09-25 Trip"]


def test_get_geometry_skips_trips_without_gps(app, store, add_track, make_track):
    # GIVEN a trip with GPS and a video without an overlay
    add_track("2026-09-25 Trip.mp4")
    no_overlay_track = make_track(video_filename="2020-01-01 Old.mp4", sample_count=0)
    no_overlay_track.extraction_status = metadata.EXTRACTION_NO_OVERLAY
    store.save_track(no_overlay_track)

    # WHEN loading the coverage geometry
    geometry = app.get_geometry()["trips"]

    # THEN only the trip with GPS has routes
    assert list(geometry) == ["2026-09-25 Trip"]
    assert geometry["2026-09-25 Trip"] == [[[49.8, 24.0], [49.8006, 24.0]]]


def test_warn_about_library_reports_mismatches(app, library_dir, add_track, caplog):
    # GIVEN a video without a track, a track without a video, and a complete trip
    caplog.set_level(logging.INFO)
    add_track("2026-09-25 Trip.mp4")
    add_track("2026-09-24 Complete.mp4")
    (library_dir / "2026-09-24 Complete.mp4").write_bytes(b"video")
    (library_dir / "2026-09-26 New.mp4").write_bytes(b"video")

    # WHEN running the startup checks
    app.warn_about_library()

    # THEN both are reported
    assert "1 videos have no track yet" in caplog.text
    assert "1 tracks have no video" in caplog.text


def test_warn_about_library_without_tracks_or_library_dir(tmp_path, store, caplog):
    # GIVEN a missing library directory and no tracks
    app = serve.VisualizerApp(tmp_path / "missing", store.root)

    # WHEN running the startup checks
    app.warn_about_library()

    # THEN both problems are reported
    assert "Library directory not found" in caplog.text
    assert "No tracks in" in caplog.text


@pytest.mark.parametrize(
    ("host", "expected_url"),
    [
        ("127.0.0.1", "http://127.0.0.1:8765/"),
        ("0.0.0.0", "http://localhost:8765/"),
        ("::", "http://localhost:8765/"),
        ("::1", "http://[::1]:8765/"),
    ],
)
def test_format_server_url(host, expected_url):
    # GIVEN a bind address

    # WHEN formatting the URL to open
    url = serve.format_server_url(host, 8765)

    # THEN it is a browsable URL
    assert url == expected_url


def test_server_serves_web_app(server):
    # GIVEN a running server

    # WHEN requesting the root
    status, headers, body = fetch(f"{server}/")

    # THEN the web app page is returned
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b'<script src="app.js">' in body


def test_server_serves_vendored_leaflet(server):
    # GIVEN a running server

    # WHEN requesting the vendored Leaflet script
    status, headers, _ = fetch(f"{server}/vendor/leaflet/leaflet.js")

    # THEN it is served locally as JavaScript
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"


def test_server_api_trips(server, library_dir, add_track):
    # GIVEN a trip with its video
    add_track()
    (library_dir / "2026-09-25 Trip.mp4").write_bytes(b"video")

    # WHEN requesting the trip list
    status, headers, body = fetch(f"{server}/api/trips")

    # THEN the trips are returned as JSON, never cached
    trips = json.loads(body)["trips"]
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert trips[0]["id"] == "2026-09-25 Trip"
    assert trips[0]["video_url"] == "/videos/2026-09-25%20Trip.mp4"


def test_server_api_geometry(server, add_track):
    # GIVEN a trip with GPS
    add_track()

    # WHEN requesting the coverage geometry
    status, _, body = fetch(f"{server}/api/geometry")

    # THEN the routes are returned by trip ID
    assert status == 200
    assert list(json.loads(body)["trips"]) == ["2026-09-25 Trip"]


def test_server_serves_track_file(server, add_track):
    # GIVEN a track
    add_track()

    # WHEN requesting its file with a quoted name
    status, headers, body = fetch(f"{server}/tracks/2026-09-25%20Trip.json")

    # THEN the track is served as JSON
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body)["video_filename"] == "2026-09-25 Trip.mp4"


def test_server_serves_preview(server, store):
    # GIVEN a preview
    store.previews_dir.mkdir()
    store.preview_path("2026-09-25 Trip").write_bytes(b"preview")

    # WHEN requesting it
    status, headers, body = fetch(f"{server}/previews/2026-09-25%20Trip.mp4")

    # THEN it is served as MP4 video
    assert status == 200
    assert headers["Content-Type"] == "video/mp4"
    assert body == b"preview"


def test_server_serves_whole_video_with_range_support(server, library_dir):
    # GIVEN a video
    (library_dir / "trip.mp4").write_bytes(bytes(range(100)))

    # WHEN requesting it without a range
    status, headers, body = fetch(f"{server}/videos/trip.mp4")

    # THEN the whole file is served, and range support is advertised
    assert status == 200
    assert headers["Accept-Ranges"] == "bytes"
    assert headers["Content-Length"] == "100"
    assert body == bytes(range(100))


def test_server_serves_video_byte_range(server, library_dir):
    # GIVEN a video
    (library_dir / "trip.mp4").write_bytes(bytes(range(100)))

    # WHEN requesting a byte range, as browsers do when seeking
    status, headers, body = fetch(f"{server}/videos/trip.mp4", headers={"Range": "bytes=10-19"})

    # THEN only that range is served
    assert status == 206
    assert headers["Content-Range"] == "bytes 10-19/100"
    assert headers["Content-Length"] == "10"
    assert body == bytes(range(10, 20))


def test_server_serves_large_video_range_in_chunks(server, library_dir, monkeypatch):
    # GIVEN a video larger than one send chunk
    monkeypatch.setattr("dashcam.serve.CHUNK_SIZE", 7)
    content = bytes(index % 256 for index in range(1000))
    (library_dir / "trip.mp4").write_bytes(content)

    # WHEN requesting an open-ended range
    status, _, body = fetch(f"{server}/videos/trip.mp4", headers={"Range": "bytes=500-"})

    # THEN the rest of the file arrives intact
    assert status == 206
    assert body == content[500:]


def test_server_rejects_range_beyond_end_of_video(server, library_dir):
    # GIVEN a video
    (library_dir / "trip.mp4").write_bytes(bytes(100))

    # WHEN requesting a range after its end
    status, headers, _ = fetch(f"{server}/videos/trip.mp4", headers={"Range": "bytes=100-"})

    # THEN the range is not satisfiable, and the file size is reported
    assert status == 416
    assert headers["Content-Range"] == "bytes */100"


def test_server_head_request_has_no_body(server, library_dir):
    # GIVEN a video
    (library_dir / "trip.mp4").write_bytes(bytes(100))

    # WHEN sending a HEAD request
    status, headers, body = fetch(f"{server}/videos/trip.mp4", method="HEAD")

    # THEN only the headers are sent
    assert status == 200
    assert headers["Content-Length"] == "100"
    assert body == b""


def test_server_refuses_files_that_are_not_videos(server, library_dir):
    # GIVEN a non-video file in the library directory
    (library_dir / "notes.txt").write_text("private", encoding="utf-8")

    # WHEN requesting it through the video route
    status, _, _ = fetch(f"{server}/videos/notes.txt")

    # THEN it is not found
    assert status == 404


@pytest.mark.parametrize(
    "url_path",
    [
        "/videos/..%2F.metadata%2Findex.json",
        "/tracks/..%2Findex.json",
        "/previews/..%2F..%2Fvideos%2Ftrip.mp4",
        "/..%2F..%2Fpyproject.toml",
        "/.hidden",
    ],
)
def test_server_refuses_paths_outside_served_directories(server, library_dir, url_path):
    # GIVEN a request that tries to escape a served directory
    (library_dir / "trip.mp4").write_bytes(b"video")

    # WHEN requesting it
    status, _, _ = fetch(f"{server}{url_path}")

    # THEN it is not found
    assert status == 404


def test_server_rejects_foreign_host(server, add_track):
    # GIVEN a server with a trip
    add_track()

    # WHEN a request addresses the server by another name (as after DNS rebinding)
    status, _, body = fetch(f"{server}/api/trips", headers={"Host": "attacker.example:8765"})

    # THEN it is refused
    assert status == 403
    assert "Host" in json.loads(body)["error"]


@pytest.mark.parametrize("host_header", ["192.168.1.5:8765", "studio.local:8765"])
def test_server_accepts_network_names_when_listening_on_the_network(
    library_dir, store, add_track, host_header
):
    # GIVEN a server that accepts network names, with a trip
    add_track()
    app = serve.VisualizerApp(library_dir, store.root, allow_network_hosts=True)
    http_server = serve.VisualizerServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()

    # WHEN a device on the network addresses it by IP address or mDNS name
    try:
        status, _, body = fetch(
            f"http://127.0.0.1:{http_server.server_address[1]}/api/trips",
            headers={"Host": host_header},
        )
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join()

    # THEN the request is served
    assert status == 200
    assert len(json.loads(body)["trips"]) == 1


def test_server_answers_unexpected_errors_with_json(server, app, monkeypatch, caplog):
    # GIVEN a trip index that fails to load for an unexpected reason
    def broken_get_trips():
        raise KeyError("trips")

    monkeypatch.setattr(app, "get_trips", broken_get_trips)

    # WHEN requesting the trips
    status, headers, body = fetch(f"{server}/api/trips")

    # THEN the server answers with a JSON error and logs the traceback
    assert status == 500
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"error": "Internal server error"}
    assert "Cannot handle GET /api/trips" in caplog.text
    assert "KeyError" in caplog.text


def test_server_closes_connection_after_rejected_post(server):
    # GIVEN a connection to the server
    connection = http.client.HTTPConnection(server.removeprefix("http://"))

    # WHEN a POST to an unknown path is rejected without reading its body
    connection.request("POST", "/api/unknown", body=b"GET /api/trips HTTP/1.1\r\n\r\n")
    response = connection.getresponse()
    response.read()
    connection.close()

    # THEN the connection is closed, so the body is not parsed as the next request
    assert response.status == 404
    assert response.getheader("Connection") == "close"


def test_server_head_suggestion_when_disabled_has_no_body(server):
    # GIVEN a connection to a server without renaming
    connection = http.client.HTTPConnection(server.removeprefix("http://"))

    # WHEN asking for a suggestion with HEAD, then with GET on the same connection
    connection.request("HEAD", "/api/suggestion?id=x")
    head_response = connection.getresponse()
    head_response.read()
    connection.request("GET", "/api/suggestion?id=x")
    get_response = connection.getresponse()
    get_body = get_response.read()
    connection.close()

    # THEN both are refused, and the HEAD response has no body to confuse the next response
    assert head_response.status == 403
    assert get_response.status == 403
    assert "Renaming" in json.loads(get_body)["error"]


def test_server_missing_file(server):
    # GIVEN a running server

    # WHEN requesting a missing video
    status, _, _ = fetch(f"{server}/videos/missing.mp4")

    # THEN it is not found
    assert status == 404


def test_serve_logs_url_and_stops_on_interrupt(library_dir, store, add_track, monkeypatch, caplog):
    # GIVEN a server that is interrupted right after starting
    caplog.set_level(logging.INFO)
    add_track()

    def interrupted_serve_forever(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(serve.VisualizerServer, "serve_forever", interrupted_serve_forever)

    # WHEN running it on a free port
    serve.serve(library_dir, store.root, port=0)

    # THEN it reports the trips and the URL, then stops cleanly
    assert "Serving 1 trips at http://127.0.0.1:" in caplog.text
    assert "Stopped" in caplog.text


def test_get_trips_reports_library_dir(app, library_dir, add_track):
    # GIVEN a track
    add_track()

    # WHEN listing the trips
    index = app.get_trips()

    # THEN the absolute library directory is included for error messages
    assert index["library_dir"] == str(library_dir.resolve())


def test_warn_about_library_when_no_video_is_found(app, add_track, caplog):
    # GIVEN tracks, none of whose videos is in the library directory
    add_track("2026-09-25 Trip.mp4")
    add_track("2026-09-26 Trip.mp4")

    # WHEN running the startup checks
    app.warn_about_library()

    # THEN the user is told to pass the right directory
    assert "None of the 2 tracks has its video in" in caplog.text
    assert "`dashcam serve --library-dir DIR`" in caplog.text


def test_server_ignores_connection_aborted_while_sending(server, library_dir, monkeypatch, capsys):
    # GIVEN a video, and a client that aborts the connection while it is sent (as on Windows)
    (library_dir / "trip.mp4").write_bytes(bytes(100))

    def send_file_to_aborted_client(self, path, send_body):
        raise ConnectionAbortedError("Connection aborted")

    monkeypatch.setattr(serve.VisualizerRequestHandler, "send_file", send_file_to_aborted_client)

    # WHEN requesting the video
    with pytest.raises(OSError):
        fetch(f"{server}/videos/trip.mp4")

    # THEN the server closes the connection without printing a traceback
    assert "Traceback" not in capsys.readouterr().err


def test_server_ignores_reset_connections(app, capsys):
    # GIVEN a server
    http_server = serve.VisualizerServer(("127.0.0.1", 0), app)

    # WHEN a client resets the connection while a request is handled
    try:
        raise ConnectionResetError("Connection reset by peer")
    except ConnectionResetError:
        http_server.handle_error(None, ("127.0.0.1", 12345))
    http_server.server_close()

    # THEN no traceback is printed
    assert "Traceback" not in capsys.readouterr().err


def test_server_ignores_aborted_connections(app, capsys):
    # GIVEN a server
    http_server = serve.VisualizerServer(("127.0.0.1", 0), app)

    # WHEN a client aborts the connection while a request is handled (as on Windows)
    try:
        raise ConnectionAbortedError("Connection aborted")
    except ConnectionAbortedError:
        http_server.handle_error(None, ("127.0.0.1", 12345))
    http_server.server_close()

    # THEN no traceback is printed
    assert "Traceback" not in capsys.readouterr().err


def test_server_reports_other_errors(app, capsys):
    # GIVEN a server
    http_server = serve.VisualizerServer(("127.0.0.1", 0), app)

    # WHEN a request fails for another reason
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        http_server.handle_error(None, ("127.0.0.1", 12345))
    http_server.server_close()

    # THEN the traceback is printed
    assert "RuntimeError: boom" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("", False),
        ("192.168.1.5", False),
        ("example.com", False),
    ],
)
def test_is_loopback_host(host, expected):
    assert serve.is_loopback_host(host) is expected


@pytest.mark.parametrize(
    ("host", "allow_network_names", "expected"),
    [
        ("localhost", False, True),
        ("localhost.", False, True),
        ("127.0.0.1", False, True),
        ("192.168.1.5", False, False),
        ("192.168.1.5", True, True),
        ("[fe80::1]", True, True),
        ("mac.local", False, False),
        ("Mac.local", True, True),
        ("studio", False, False),
        ("studio", True, True),
        ("studio.lan", True, True),
        ("attacker.example", True, False),
        ("local.attacker.example", True, False),
    ],
)
def test_is_trusted_host(host, allow_network_names, expected, monkeypatch):
    # GIVEN a machine named `Studio.lan`
    monkeypatch.setattr(serve.socket, "gethostname", lambda: "Studio.lan")

    # WHEN checking the host
    is_trusted = serve.is_trusted_host(host, allow_network_names)

    # THEN only names that a website cannot make its own are trusted
    assert is_trusted is expected


@pytest.mark.parametrize(
    ("host_header", "expected_host"),
    [
        ("127.0.0.1:8765", "127.0.0.1"),
        ("[::1]:8765", "[::1]"),
        ("localhost", "localhost"),
    ],
)
def test_split_host_header(host_header, expected_host):
    assert serve.split_host_header(host_header) == expected_host


STUSA = {"name": "вулиця Василя Стуса", "distance_m": 967}


@contextlib.contextmanager
def run_rename_server(library_dir, store, allow_network_hosts=False):
    """Run a server that allows renaming, and yield its base URL."""
    app = serve.VisualizerApp(
        library_dir,
        store.root,
        car_model="Car",
        allow_rename=True,
        allow_network_hosts=allow_network_hosts,
    )
    http_server = serve.VisualizerServer(("127.0.0.1", 0), app)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http_server.server_address[1]}"
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join()


@pytest.fixture
def rename_server(library_dir, store):
    """Run a server that allows renaming from this machine, and yield its base URL."""
    with run_rename_server(library_dir, store) as base_url:
        yield base_url


@pytest.fixture
def network_rename_server(library_dir, store):
    """Run a server that allows renaming from the network, and yield its base URL."""
    with run_rename_server(library_dir, store, allow_network_hosts=True) as base_url:
        yield base_url


@pytest.fixture
def add_trip_with_video(library_dir, store, make_named_track):
    def _add_trip_with_video(video_filename="2026-09-25 Trip 11-17.mp4"):
        (library_dir / video_filename).write_bytes(b"video")
        store.save_track(make_named_track(video_filename, [STUSA]))

    return _add_trip_with_video


def post_rename(base_url, data, headers=None):
    """Send a rename request like the web app does, and return the status and parsed body."""
    body = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
    request_headers = {"Content-Type": "application/json", "Origin": base_url}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        f"{base_url}/api/rename", data=body, headers=request_headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_server_renames_trip(rename_server, library_dir, store, add_trip_with_video):
    # GIVEN a trip
    add_trip_with_video()

    # WHEN the web app renames it
    status, body = post_rename(
        rename_server, {"id": "2026-09-25 Trip 11-17", "filename": "2026-09-25 To Work.mp4"}
    )

    # THEN the files are renamed and the index lists the new trip ID
    assert (status, body) == (200, {"id": "2026-09-25 To Work"})
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 To Work.mp4"]
    _, _, trips_body = fetch(f"{rename_server}/api/trips")
    assert [trip["id"] for trip in json.loads(trips_body)["trips"]] == ["2026-09-25 To Work"]


def test_rename_trip_updates_current_index_without_reading_other_tracks(
    app, store, add_trip_with_video, monkeypatch
):
    # GIVEN two trips and a current index
    add_trip_with_video("2026-09-25 Trip 11-17.mp4")
    add_trip_with_video("2026-09-24 Other.mp4")
    store.rebuild_index()
    monkeypatch.setattr(
        "dashcam.metadata.MetadataStore.iter_track_files",
        lambda self: pytest.fail("All tracks were read"),
    )

    # WHEN renaming one trip
    new_id = app.rename_trip("2026-09-25 Trip 11-17", "2026-09-25 To Work.mp4")

    # THEN only its index entry changes
    assert new_id == "2026-09-25 To Work"
    trip_ids = {trip["id"] for trip in store.load_index()["trips"]}
    assert trip_ids == {"2026-09-25 To Work", "2026-09-24 Other"}
    assert set(store.load_geometry()["trips"]) == trip_ids


def test_rename_trip_rebuilds_stale_index(app, store, add_trip_with_video, make_track):
    # GIVEN an index that misses a track added later
    add_trip_with_video("2026-09-25 Trip 11-17.mp4")
    store.rebuild_index()
    store.save_track(make_track("2026-09-24 Added Later.mp4"))

    # WHEN renaming a trip
    app.rename_trip("2026-09-25 Trip 11-17", "2026-09-25 To Work.mp4")

    # THEN the index is rebuilt with the added track too
    trip_ids = {trip["id"] for trip in store.load_index()["trips"]}
    assert trip_ids == {"2026-09-25 To Work", "2026-09-24 Added Later"}


def test_server_reports_rename_conflict(rename_server, add_trip_with_video):
    # GIVEN two trips
    add_trip_with_video("2026-09-25 Trip 11-17.mp4")
    add_trip_with_video("2026-09-25 To Work.mp4")

    # WHEN renaming one to the name of the other
    status, body = post_rename(
        rename_server, {"id": "2026-09-25 Trip 11-17", "filename": "2026-09-25 To Work.mp4"}
    )

    # THEN the conflict is explained
    assert status == 409
    assert "already taken" in body["error"]


def test_server_rejects_rename_when_disabled(server, add_trip_with_video):
    # GIVEN a server that does not allow renaming
    add_trip_with_video()

    # WHEN renaming
    status, body = post_rename(
        server, {"id": "2026-09-25 Trip 11-17", "filename": "2026-09-25 To Work.mp4"}
    )

    # THEN it is refused
    assert status == 403
    assert "localhost" in body["error"]


def test_server_rejects_rename_without_json(rename_server, add_trip_with_video):
    # GIVEN a trip
    add_trip_with_video()

    # WHEN a form or another website posts plain text, which needs no CORS preflight
    status, _ = post_rename(
        rename_server,
        {"id": "2026-09-25 Trip 11-17", "filename": "2026-09-25 To Work.mp4"},
        headers={"Content-Type": "text/plain"},
    )

    # THEN it is refused
    assert status == 415


def test_server_rejects_cross_origin_rename(rename_server, library_dir, add_trip_with_video):
    # GIVEN a trip
    add_trip_with_video()

    # WHEN another website sends the request
    status, _ = post_rename(
        rename_server,
        {"id": "2026-09-25 Trip 11-17", "filename": "2026-09-25 To Work.mp4"},
        headers={"Origin": "https://example.com"},
    )

    # THEN it is refused and nothing is renamed
    assert status == 403
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 Trip 11-17.mp4"]


def test_server_rejects_rename_with_foreign_host(rename_server, add_trip_with_video):
    # GIVEN a trip
    add_trip_with_video()

    # WHEN the request addresses the server by another name (as after DNS rebinding)
    status, body = post_rename(
        rename_server,
        {"id": "2026-09-25 Trip 11-17", "filename": "2026-09-25 To Work.mp4"},
        headers={"Host": "attacker.example:8765", "Origin": "http://attacker.example:8765"},
    )

    # THEN it is refused
    assert status == 403
    assert "Host" in body["error"]


def test_server_rejects_rename_by_ip_address_by_default(rename_server, add_trip_with_video):
    # GIVEN a trip on a server that only allows renaming from this machine
    add_trip_with_video()

    # WHEN a device on the network sends the request to the server's IP address
    status, _ = post_rename(
        rename_server,
        {"id": "2026-09-25 Trip 11-17", "filename": "2026-09-25 To Work.mp4"},
        headers={"Host": "192.168.1.5:8765", "Origin": "http://192.168.1.5:8765"},
    )

    # THEN it is refused
    assert status == 403


def test_server_renames_trip_from_the_network(
    network_rename_server, library_dir, add_trip_with_video
):
    # GIVEN a trip on a server that allows renaming from the network
    add_trip_with_video()

    # WHEN the web app on another device renames it through the server's IP address
    status, body = post_rename(
        network_rename_server,
        {"id": "2026-09-25 Trip 11-17", "filename": "2026-09-25 To Work.mp4"},
        headers={"Host": "192.168.1.5:8765", "Origin": "http://192.168.1.5:8765"},
    )

    # THEN the trip is renamed
    assert (status, body) == (200, {"id": "2026-09-25 To Work"})
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 To Work.mp4"]


def test_server_rejects_network_rename_with_foreign_host(
    network_rename_server, library_dir, add_trip_with_video
):
    # GIVEN a trip on a server that allows renaming from the network
    add_trip_with_video()

    # WHEN the request addresses the server by another name (as after DNS rebinding)
    status, _ = post_rename(
        network_rename_server,
        {"id": "2026-09-25 Trip 11-17", "filename": "2026-09-25 To Work.mp4"},
        headers={"Host": "attacker.example:8765", "Origin": "http://attacker.example:8765"},
    )

    # THEN it is refused and nothing is renamed
    assert status == 403
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 Trip 11-17.mp4"]


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        {"id": "2026-09-25 Trip 11-17"},
        {"id": 5, "filename": "2026-09-25 A.mp4"},
        b"",
    ],
)
def test_server_rejects_invalid_rename_request(rename_server, add_trip_with_video, data):
    add_trip_with_video()

    status, _ = post_rename(rename_server, data)

    assert status == 400


def test_server_rejects_post_to_other_paths(rename_server):
    request = urllib.request.Request(
        f"{rename_server}/api/trips",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request)

    assert exc_info.value.code == 404


def test_server_api_suggestion(rename_server, add_trip_with_video):
    # GIVEN a trip with streets
    add_trip_with_video()

    # WHEN asking for a suggested name
    status, _, body = fetch(f"{rename_server}/api/suggestion?id=2026-09-25%20Trip%2011-17")

    # THEN the suggestion is returned
    assert status == 200
    assert json.loads(body) == {"filename": "2026-09-25 Vasylia Stusa (Car).mp4"}


def test_server_api_suggestion_for_unknown_trip(rename_server):
    status, _, body = fetch(f"{rename_server}/api/suggestion?id=nope")

    assert status == 409
    assert "No track" in json.loads(body)["error"]


def test_server_api_suggestion_when_disabled(server):
    status, _, _ = fetch(f"{server}/api/suggestion?id=nope")

    assert status == 403


def test_get_trips_reports_whether_renaming_is_allowed(library_dir, store, add_track):
    # GIVEN apps with and without renaming
    add_track()
    rename_app = serve.VisualizerApp(library_dir, store.root, allow_rename=True)
    read_only_app = serve.VisualizerApp(library_dir, store.root)

    # WHEN listing the trips
    # THEN the web app learns whether to offer renaming
    assert rename_app.get_trips()["can_rename"] is True
    assert read_only_app.get_trips()["can_rename"] is False


@pytest.fixture
def started_apps(monkeypatch):
    """Replace the HTTP server with one that stops right away, and record the served apps."""
    apps = []

    class FakeServer:
        server_address = ("0.0.0.0", 8765)

        def __init__(self, server_address, app):
            apps.append(app)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(serve, "VisualizerServer", FakeServer)
    return apps


def test_serve_disables_renaming_on_all_interfaces(library_dir, store, started_apps, caplog):
    # GIVEN a server started on all interfaces
    caplog.set_level(logging.INFO)

    # WHEN running it
    serve.serve(library_dir, store.root, host="0.0.0.0")

    # THEN renaming is disabled, with a hint to enable it, and network names are accepted
    assert started_apps[0].allow_rename is False
    assert started_apps[0].allow_network_hosts is True
    assert "Renaming trips in the browser is disabled" in caplog.text
    assert "--allow-rename" in caplog.text


def test_serve_allows_network_renaming_on_request(library_dir, store, started_apps, caplog):
    # GIVEN a server started on all interfaces with network renaming
    caplog.set_level(logging.INFO)

    # WHEN running it
    serve.serve(library_dir, store.root, host="0.0.0.0", allow_network_rename=True)

    # THEN renaming is enabled from the network, with a warning
    assert started_apps[0].allow_rename is True
    assert started_apps[0].allow_network_hosts is True
    assert "Anyone who can reach this server can rename trips" in caplog.text


def test_serve_allows_local_renaming_by_default(library_dir, store, started_apps):
    # WHEN running a server on the loopback address
    serve.serve(library_dir, store.root, host="127.0.0.1")

    # THEN renaming is enabled, and only loopback names are accepted
    assert started_apps[0].allow_rename is True
    assert started_apps[0].allow_network_hosts is False
