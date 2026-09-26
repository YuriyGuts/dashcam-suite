"""
Serve the trip visualizer: a static web app, the trip index, tracks, previews, and videos.

Routes:

    /                        The web app (`dashcam/web`).
    /api/trips               The trip index, with the video and preview availability of each trip.
    /api/geometry            Simplified routes of all trips, for the coverage mode.
    /tracks/<stem>.json      Track files.
    /previews/<stem>.mp4     Previews.
    /videos/<filename>       Trip videos from the video directory.

Files are served with HTTP range requests (`206 Partial Content`), which browsers need to seek
in videos.

The server binds to `127.0.0.1` by default, so it is only reachable from this machine.

Usage example:
> dashcam serve ~/Videos/Dashcam --port 8765
"""

import dataclasses
import http.server
import json
import logging
import os
import re
import sys
import threading
import typing as t
import urllib.parse
from pathlib import Path

from dashcam import cleaning
from dashcam import extract
from dashcam import geo
from dashcam import metadata

# Default bind address and port.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Directory with the web app.
WEB_DIR = Path(__file__).parent / "web"

# Size of the chunks in which files are sent.
CHUNK_SIZE = 256 * 1024

# Coverage geometry: points closer than this to the previous kept point are dropped, and the
# kept coordinates are rounded to this many decimals (~1 m).
GEOMETRY_MIN_STEP_M = 10.0
GEOMETRY_DECIMALS = 5

# A single byte range: `bytes=START-END`, `bytes=START-`, or `bytes=-SUFFIX_LENGTH`.
BYTE_RANGE_PATTERN = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE | re.ASCII)

# Content types by file extension. Other files are served as binary data.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"

# Sample statuses that have coordinates.
LOCATED_STATUSES = {cleaning.STATUS_OK, cleaning.STATUS_INTERPOLATED}

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


class RangeNotSatisfiableError(ValueError):
    """Raised when a byte range lies outside the file."""


def parse_byte_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    """
    Parse an HTTP `Range` header.

    Only a single byte range is supported. Headers that cannot be parsed, or ask for several
    ranges, are ignored, as HTTP allows, and the whole file is served.

    Returns
    -------
    tuple[int, int] | None
        The first and last byte (inclusive), or None to serve the whole file.

    Raises
    ------
    RangeNotSatisfiableError
        If the range starts beyond the end of the file.
    """
    match = BYTE_RANGE_PATTERN.match(range_header or "")
    if match is None:
        return None
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return None

    # A suffix range (`bytes=-500`) asks for the last bytes of the file.
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length == 0 or file_size == 0:
            raise RangeNotSatisfiableError(range_header)
        return max(file_size - suffix_length, 0), file_size - 1

    start = int(start_text)
    if end_text and int(end_text) < start:
        return None
    if start >= file_size:
        raise RangeNotSatisfiableError(range_header)
    end = min(int(end_text), file_size - 1) if end_text else file_size - 1
    return start, end


def safe_join(root: Path, relative_path: str) -> Path | None:
    """
    Join a URL path (already unquoted) to a directory, refusing anything that could leave it.

    Returns
    -------
    Path | None
        The joined path, or None if a segment is empty, hidden (including `..`), or unsafe.
    """
    segments = relative_path.split("/")
    for segment in segments:
        if not segment or segment.startswith(".") or "\\" in segment or "\0" in segment:
            return None
    return root.joinpath(*segments)


def get_content_type(path: Path) -> str:
    """Return the content type of a file from its extension."""
    return CONTENT_TYPES.get(path.suffix.lower(), DEFAULT_CONTENT_TYPE)


def simplify_track_geometry(samples: list[dict[str, t.Any]]) -> list[list[list[float]]]:
    """
    Reduce the samples of a track to runs of located points for the coverage mode.

    A run ends at every sample without a location. Points closer than `GEOMETRY_MIN_STEP_M` to
    the previous kept point are dropped, but the last point of a run is always kept.

    Returns
    -------
    list[list[list[float]]]
        Runs of `[lat, lon]` points. Runs with fewer than two points are left out.
    """
    runs = []
    current_run: list[list[float]] = []
    last_point: list[float] | None = None

    def finish_run() -> None:
        if last_point is not None and current_run[-1] is not last_point:
            current_run.append(last_point)
        if len(current_run) >= 2:
            runs.append(current_run)

    for sample in samples:
        is_located = (
            sample.get("status") in LOCATED_STATUSES
            and sample.get("lat") is not None
            and sample.get("lon") is not None
        )
        if not is_located:
            if current_run:
                finish_run()
            current_run = []
            last_point = None
            continue

        point = [
            round(sample["lat"], GEOMETRY_DECIMALS),
            round(sample["lon"], GEOMETRY_DECIMALS),
        ]
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


@dataclasses.dataclass(frozen=True)
class TrackSummary:
    """The parts of a track the web app needs for all trips at once."""

    geometry: list[list[list[float]]]
    street_names: list[str]


def summarize_track(track_data: dict[str, t.Any]) -> TrackSummary:
    """Extract the coverage geometry and the street names from a parsed track file."""
    street_names = []
    for street in track_data.get("streets") or []:
        name = street.get("name") if isinstance(street, dict) else street
        if isinstance(name, str) and name and name not in street_names:
            street_names.append(name)
    return TrackSummary(
        geometry=simplify_track_geometry(track_data.get("samples") or []),
        street_names=street_names,
    )


class TrackSummaryCache:
    """Track summaries, recomputed only when a track file changes."""

    def __init__(self, store: metadata.MetadataStore):
        self.store = store
        self.lock = threading.Lock()
        self.entries: dict[str, tuple[tuple[int, int], TrackSummary]] = {}

    def get(self, stem: str) -> TrackSummary | None:
        """
        Return the summary of a track.

        Returns
        -------
        TrackSummary | None
            The summary, or None if the track is missing or unreadable.
        """
        path = self.store.track_path(stem)
        try:
            stat = path.stat()
        except OSError:
            return None
        file_key = (stat.st_mtime_ns, stat.st_size)
        with self.lock:
            cached = self.entries.get(stem)
        if cached is not None and cached[0] == file_key:
            return cached[1]

        try:
            track_data = json.loads(path.read_text(encoding="utf-8"))
            summary = summarize_track(track_data)
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            LOGGER.warning(f"Cannot read track '{path.name}': {exc} (see `dashcam doctor`)")
            return None
        with self.lock:
            self.entries[stem] = (file_key, summary)
        return summary


def is_index_stale(store: metadata.MetadataStore) -> bool:
    """
    Check cheaply whether the index needs a rebuild: it is missing, older than a track, or lists
    different trips than there are track files.
    """
    try:
        index_mtime_ns = store.index_path.stat().st_mtime_ns
        index = json.loads(store.index_path.read_text(encoding="utf-8"))
        indexed_stems = {trip["id"] for trip in index["trips"]}
    except (OSError, ValueError, TypeError, KeyError):
        return True

    track_paths = store.list_track_paths()
    if {path.stem for path in track_paths} != indexed_stems:
        return True
    for path in track_paths:
        try:
            if path.stat().st_mtime_ns > index_mtime_ns:
                return True
        except OSError:
            return True
    if store.previews_dir.is_dir():
        for path in store.previews_dir.glob("*.mp4"):
            if path.stat().st_mtime_ns > index_mtime_ns:
                return True
    return False


class VisualizerApp:
    """Data behind the HTTP routes."""

    def __init__(self, video_dir: Path, metadata_dir: Path, max_interpolation_gap_s: float):
        self.video_dir = video_dir
        self.store = metadata.MetadataStore(metadata_dir)
        self.max_interpolation_gap_s = max_interpolation_gap_s
        self.summaries = TrackSummaryCache(self.store)
        self.index_lock = threading.Lock()

    def load_index(self) -> dict[str, t.Any]:
        """Load the trip index, rebuilding it first if it is stale."""
        with self.index_lock:
            if is_index_stale(self.store):
                LOGGER.info(f"Rebuilding the trip index '{self.store.index_path}'")
                return self.store.rebuild_index(self.max_interpolation_gap_s)
            return json.loads(self.store.index_path.read_text(encoding="utf-8"))

    def video_path(self, video_filename: str) -> Path | None:
        """Return the path of a trip video in the video directory, if the name is acceptable."""
        if Path(video_filename).suffix.lower() not in extract.VIDEO_EXTENSIONS:
            return None
        return safe_join(self.video_dir, video_filename)

    def get_trips(self) -> dict[str, t.Any]:
        """
        Return the trip index with the URLs of the playable videos of each trip.

        `video_url` and `preview_url` are null when the file is not available. `streets` lists
        the street names of the trip in travel order. `video_dir` is where videos are looked up.
        """
        index = self.load_index()
        index["video_dir"] = str(self.video_dir.resolve())
        for trip in index["trips"]:
            video_path = self.video_path(trip["video_filename"])
            is_video_available = video_path is not None and video_path.is_file()
            trip["video_url"] = (
                f"/videos/{urllib.parse.quote(trip['video_filename'])}"
                if is_video_available
                else None
            )
            is_preview_available = self.store.preview_path(trip["id"]).is_file()
            trip["has_preview"] = is_preview_available
            trip["preview_url"] = (
                f"/previews/{urllib.parse.quote(trip['id'])}.mp4" if is_preview_available else None
            )
            trip["track_url"] = f"/tracks/{urllib.parse.quote(trip['id'])}.json"

            summary = self.summaries.get(trip["id"]) if trip.get("street_count") else None
            trip["streets"] = summary.street_names if summary is not None else []
        return index

    def get_geometry(self) -> dict[str, t.Any]:
        """Return the simplified routes of all trips with GPS, keyed by trip ID."""
        index = self.load_index()
        geometry = {}
        for trip in index["trips"]:
            if trip.get("extraction_status") != metadata.EXTRACTION_OK or not trip.get("bbox"):
                continue
            summary = self.summaries.get(trip["id"])
            if summary is not None:
                geometry[trip["id"]] = summary.geometry
        return {"trips": geometry}

    def warn_about_library(self) -> None:
        """Run the cheap consistency checks and log what the user may want to fix."""
        track_stems = {path.stem for path in self.store.list_track_paths()}
        video_stems = set()
        if self.video_dir.is_dir():
            video_paths = extract.find_videos(self.video_dir, include=[], exclude=[])
            video_stems = {path.stem for path in video_paths}
        else:
            LOGGER.warning(f"Video directory not found: '{self.video_dir}'")

        if not track_stems:
            LOGGER.warning(f"No tracks in '{self.store.tracks_dir}' (run `dashcam extract`)")
        videos_without_tracks = video_stems - track_stems
        if videos_without_tracks:
            LOGGER.warning(
                f"{len(videos_without_tracks)} videos have no track yet (run `dashcam extract`)"
            )
        tracks_without_videos = track_stems - video_stems
        if track_stems and tracks_without_videos == track_stems:
            LOGGER.warning(
                f"None of the {len(track_stems)} tracks has its video in "
                f"'{self.video_dir.resolve()}'. Pass the video directory: `dashcam serve DIR`"
            )
        elif tracks_without_videos:
            LOGGER.info(
                f"{len(tracks_without_videos)} tracks have no video in '{self.video_dir}'; "
                f"their video panel is disabled unless a preview exists"
            )


class VisualizerServer(http.server.ThreadingHTTPServer):
    """HTTP server that holds the visualizer data."""

    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], app: VisualizerApp):
        super().__init__(server_address, VisualizerRequestHandler)
        self.app = app

    def handle_error(self, request: t.Any, client_address: t.Any) -> None:
        # Browsers drop connections all the time, e.g. when seeking in a video.
        exc = sys.exception()
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class VisualizerRequestHandler(http.server.BaseHTTPRequestHandler):
    """Routes requests to the web app, the API, and the files."""

    protocol_version = "HTTP/1.1"
    server_version = "dashcam"

    @property
    def app(self) -> VisualizerApp:
        return t.cast(VisualizerServer, self.server).app

    def log_message(self, format: str, *args: t.Any) -> None:  # noqa: A002
        LOGGER.debug(f"{self.address_string()} {format % args}")

    def do_GET(self) -> None:
        self.handle_request(send_body=True)

    def do_HEAD(self) -> None:
        self.handle_request(send_body=False)

    def handle_request(self, send_body: bool) -> None:
        """Dispatch a GET or HEAD request."""
        url_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        try:
            if url_path == "/api/trips":
                self.send_json(self.app.get_trips(), send_body)
            elif url_path == "/api/geometry":
                self.send_json(self.app.get_geometry(), send_body)
            elif url_path.startswith("/videos/"):
                file_path = self.app.video_path(url_path.removeprefix("/videos/"))
                self.send_file(file_path, send_body)
            elif url_path.startswith("/tracks/"):
                file_path = safe_join(self.app.store.tracks_dir, url_path.removeprefix("/tracks/"))
                self.send_file(file_path, send_body)
            elif url_path.startswith("/previews/"):
                file_path = safe_join(
                    self.app.store.previews_dir, url_path.removeprefix("/previews/")
                )
                self.send_file(file_path, send_body)
            else:
                relative_path = url_path.removeprefix("/") or "index.html"
                self.send_file(safe_join(WEB_DIR, relative_path), send_body)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers abort video requests all the time, e.g. when seeking.
            self.close_connection = True

    def send_json(self, data: t.Any, send_body: bool) -> None:
        """Send a JSON response."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES[".json"])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def send_file(self, path: Path | None, send_body: bool) -> None:
        """Send a file, or the byte range of it that the request asks for."""
        if path is None or not path.is_file():
            self.send_error(404)
            return
        try:
            file = path.open("rb")
        except OSError:
            self.send_error(404)
            return

        with file:
            file_size = os.fstat(file.fileno()).st_size
            try:
                byte_range = parse_byte_range(self.headers.get("Range"), file_size)
            except RangeNotSatisfiableError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if byte_range is None:
                start, end = 0, file_size - 1
                self.send_response(200)
            else:
                start, end = byte_range
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            content_length = end - start + 1
            self.send_header("Content-Type", get_content_type(path))
            self.send_header("Content-Length", str(content_length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if not send_body:
                return

            file.seek(start)
            remaining_length = content_length
            while remaining_length > 0:
                chunk = file.read(min(CHUNK_SIZE, remaining_length))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining_length -= len(chunk)


def format_server_url(host: str, port: int) -> str:
    """Return the URL to open in the browser."""
    if host in ("", "0.0.0.0", "::"):
        host = "localhost"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def serve(
    video_dir: Path,
    metadata_dir: Path,
    max_interpolation_gap_s: float,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Run the visualizer server until interrupted."""
    app = VisualizerApp(video_dir, metadata_dir, max_interpolation_gap_s)
    app.warn_about_library()
    trip_count = len(app.load_index()["trips"])

    server = VisualizerServer((host, port), app)
    bound_port = server.server_address[1]
    LOGGER.info(f"Serving {trip_count} trips at {format_server_url(host, int(bound_port))}")
    LOGGER.info("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
    finally:
        server.server_close()
