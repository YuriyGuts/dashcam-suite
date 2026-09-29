"""
Serve the trip visualizer: a static web app, the trip index, tracks, previews, and videos.

Files are served with HTTP range requests (`206 Partial Content`), which browsers need to seek
in videos.
"""

import http.server
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import threading
import typing as t
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pathlib import PurePath

from dashcam import metadata
from dashcam import rename
from dashcam import terminal

# Default bind address and port.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Directory with the web app.
WEB_DIR = Path(__file__).parent / "web"

# Size of the chunks in which files are sent.
CHUNK_SIZE = 256 * 1024

# A single byte range: `bytes=START-END`, `bytes=START-`, or `bytes=-SUFFIX_LENGTH`.
# Longer numbers are ignored: no file is that large, and `int` refuses very long numbers.
BYTE_RANGE_PATTERN = re.compile(
    r"^\s*bytes\s*=\s*(\d{0,18})\s*-\s*(\d{0,18})\s*$", re.IGNORECASE | re.ASCII
)

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

# The largest request body accepted, in bytes.
MAX_REQUEST_BODY_SIZE = 16 * 1024

# Seconds a connection may stay idle, or a client may take to receive data, before it is closed.
CONNECTION_TIMEOUT_S = 60

# Parallel file stats in the index staleness check. More threads were not faster on a NAS.
STAT_THREAD_COUNT = 16

# Host names that always refer to this machine.
LOOPBACK_HOST_NAMES = frozenset(["localhost"])

RENAME_DISABLED_MESSAGE = (
    "Renaming is only available when the server listens on localhost or runs with --allow-rename"
)

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


def safe_join[RootPath: PurePath](root: RootPath, relative_path: str) -> RootPath | None:
    """
    Join a URL path (already unquoted) to a directory, refusing anything that could leave it.

    Returns
    -------
    RootPath | None
        The joined path, or None if a segment is empty, hidden (including `..`), or unsafe.
    """
    segments = relative_path.split("/")
    for segment in segments:
        if not segment or segment.startswith(".") or "\\" in segment or "\0" in segment:
            return None
    joined_path = root.joinpath(*segments)
    # A segment with a drive (`D:secret.txt` on Windows) replaces the root instead of joining it.
    if not joined_path.is_relative_to(root):
        return None
    return joined_path


def is_loopback_host(host: str) -> bool:
    """Check whether a host name or address refers to this machine only."""
    host = host.strip("[]").lower()
    if host in LOOPBACK_HOST_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def get_machine_host_names() -> set[str]:
    """Return the lowercase host name of this machine, with and without its domain."""
    host_name = socket.gethostname().lower()
    return {host_name, host_name.split(".")[0]}


def is_trusted_host(host: str, allow_network_names: bool) -> bool:
    """
    Check whether a host name cannot belong to a DNS rebinding attack.

    Loopback names are always trusted. With `allow_network_names`, so are IP addresses, mDNS
    names (`*.local`) and this machine's host name, none of which a website can make its own.
    """
    host = host.strip("[]").lower().rstrip(".")
    if is_loopback_host(host):
        return True
    if not allow_network_names:
        return False
    if host.endswith(".local") or host in get_machine_host_names():
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def split_host_header(host_header: str) -> str:
    """Return the host part of a `Host` header (`name:port`, or `[address]:port` for IPv6)."""
    if host_header.startswith("["):
        return host_header[: host_header.find("]") + 1]
    return host_header.rsplit(":", 1)[0] if ":" in host_header else host_header


def get_content_type(path: Path) -> str:
    """Return the content type of a file from its extension."""
    return CONTENT_TYPES.get(path.suffix.lower(), DEFAULT_CONTENT_TYPE)


def list_filenames(directory: Path) -> set[str]:
    """Return the names of the regular files in a directory, or nothing if it is missing."""
    try:
        with os.scandir(directory) as entries:
            return {entry.name for entry in entries if entry.is_file()}
    except OSError:
        return set()


def is_index_stale(store: metadata.MetadataStore) -> bool:
    """
    Check cheaply whether the index needs a rebuild: it or the geometry file is missing, it has
    another format version, is older than a track, or was built from other track files than
    there are.
    """
    try:
        index_mtime_ns = store.index_path.stat().st_mtime_ns
        index = store.load_index()
        if index.get("format_version") != metadata.INDEX_FORMAT_VERSION:
            return True
        indexed_stems = {trip["id"] for trip in index["trips"]}
        indexed_stems.update(index["skipped_track_stems"])
    except (OSError, ValueError, TypeError, KeyError):
        return True
    if not store.geometry_path.is_file():
        return True

    track_paths = store.list_track_paths()
    if {path.stem for path in track_paths} != indexed_stems:
        return True
    preview_paths = list(store.previews_dir.glob("*.mp4")) if store.previews_dir.is_dir() else []
    return is_any_file_newer([*track_paths, *preview_paths], index_mtime_ns)


def is_any_file_newer(paths: list[Path], mtime_ns: int) -> bool:
    """Check whether any of the files was modified after `mtime_ns`, or no longer exists."""

    def is_newer(path: Path) -> bool:
        try:
            return path.stat().st_mtime_ns > mtime_ns
        except OSError:
            # Removed since it was listed, e.g. by `forget` or a rename in another tab.
            return True

    # Each stat is a round trip on a network drive, so they run in parallel.
    with ThreadPoolExecutor(max_workers=STAT_THREAD_COUNT) as executor:
        return any(executor.map(is_newer, paths))


class VisualizerApp:
    """Data behind the HTTP routes."""

    def __init__(
        self,
        library_dir: Path,
        metadata_dir: Path,
        car_model: str = "",
        allow_rename: bool = False,
        allow_network_hosts: bool = False,
    ):
        self.library_dir = library_dir
        self.store = metadata.MetadataStore(metadata_dir)
        self.car_model = car_model
        self.allow_rename = allow_rename

        # Whether requests may address the server by a network name or IP address, not only by a
        # loopback name, so that other devices on the network can use it.
        self.allow_network_hosts = allow_network_hosts
        self.index_lock = threading.Lock()

    def load_index(self) -> dict[str, t.Any]:
        """Load the trip index, rebuilding it first if it is stale."""
        with self.index_lock:
            if is_index_stale(self.store):
                LOGGER.info(f"Rebuilding the trip index '{self.store.index_path}'")
                return self.store.rebuild_index()
            return self.store.load_index()

    def video_path(self, video_filename: str) -> Path | None:
        """Return the path of a trip video in the library directory, if the name is acceptable."""
        if Path(video_filename).suffix.lower() not in metadata.VIDEO_EXTENSIONS:
            return None
        return safe_join(self.library_dir, video_filename)

    def get_trips(self) -> dict[str, t.Any]:
        """
        Return the trip index with the URLs of the playable videos of each trip.

        `video_url` and `preview_url` are null when the file is not available. `streets` lists
        the street names of the trip in travel order. `library_dir` is where videos are looked up.
        It is left out when other devices may connect, since the path reveals the user name.
        """
        index = self.load_index()
        if not self.allow_network_hosts:
            index["library_dir"] = str(self.library_dir.resolve())
        index["can_rename"] = self.allow_rename
        self.add_file_urls(index["trips"])
        return index

    def add_file_urls(self, trips: list[dict[str, t.Any]]) -> None:
        """Add the URLs of the video, preview, and track files to index entries."""
        # One listing per directory, instead of a lookup per trip, which is slow on network drives.
        video_filenames = list_filenames(self.library_dir)
        preview_filenames = list_filenames(self.store.previews_dir)
        for trip in trips:
            is_video_available = (
                self.video_path(trip["video_filename"]) is not None
                and trip["video_filename"] in video_filenames
            )
            trip["video_url"] = (
                f"/videos/{urllib.parse.quote(trip['video_filename'])}"
                if is_video_available
                else None
            )
            is_preview_available = self.store.preview_path(trip["id"]).name in preview_filenames
            trip["has_preview"] = is_preview_available
            trip["preview_url"] = (
                f"/previews/{urllib.parse.quote(trip['id'])}.mp4" if is_preview_available else None
            )
            trip["track_url"] = f"/tracks/{urllib.parse.quote(trip['id'])}.json"

    def get_geometry(self) -> dict[str, t.Any]:
        """Return the simplified routes of all trips with GPS, keyed by trip ID."""
        # Loading the index rebuilds the geometry file with it if needed.
        self.load_index()
        return {"trips": self.store.load_geometry()["trips"]}

    def suggest_filename(self, trip_id: str) -> str:
        """
        Suggest a new filename for a trip.

        Raises
        ------
        rename.RenameError
            If the trip cannot be named.
        """
        with self.index_lock:
            return rename.suggest_trip_filename(
                self.library_dir, self.store, trip_id, self.car_model
            )

    def rename_trip(self, trip_id: str, new_filename: str) -> dict[str, t.Any]:
        """
        Rename a trip and update the index.

        Returns
        -------
        dict[str, t.Any]
            The index entry of the renamed trip, with the URLs that `get_trips` adds.

        Raises
        ------
        rename.RenameError
            If the trip cannot be renamed to this filename.
        """
        with self.index_lock:
            # Checked before the rename, which makes the renamed track newer than the index.
            was_index_stale = is_index_stale(self.store)
            new_id = rename.rename_trip(self.library_dir, self.store, trip_id, new_filename)
            if was_index_stale:
                index = self.store.rebuild_index()
            elif new_id != trip_id:
                index = self.store.update_index_after_rename(trip_id, new_id)
            else:
                index = self.store.load_index()
        trip = next(trip for trip in index["trips"] if trip["id"] == new_id)
        self.add_file_urls([trip])
        return trip

    def warn_about_library(self) -> None:
        """Run the cheap consistency checks and log what the user may want to fix."""
        track_stems = {path.stem for path in self.store.list_track_paths()}
        video_stems = set()
        if self.library_dir.is_dir():
            video_paths = metadata.find_videos(self.library_dir)
            video_stems = {path.stem for path in video_paths}
        else:
            LOGGER.warning(f"Library directory not found: '{self.library_dir}'")

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
                f"'{self.library_dir.resolve()}'. "
                f"Pass the library directory: `dashcam serve --library-dir DIR`"
            )
        elif tracks_without_videos:
            LOGGER.info(
                f"{len(tracks_without_videos)} tracks have no video in '{self.library_dir}'; "
                f"their video panel is disabled unless a preview exists"
            )


class VisualizerServer(http.server.ThreadingHTTPServer):
    """HTTP server that holds the visualizer data."""

    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], app: VisualizerApp):
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, VisualizerRequestHandler)
        self.app = app

    def handle_error(self, request: t.Any, client_address: t.Any) -> None:
        # Browsers drop connections all the time, e.g. when seeking in a video.
        exc = sys.exception()
        if isinstance(exc, ConnectionError | TimeoutError):
            return
        super().handle_error(request, client_address)


class VisualizerRequestHandler(http.server.BaseHTTPRequestHandler):
    """Routes requests to the web app, the API, and the files."""

    protocol_version = "HTTP/1.1"
    server_version = "dashcam"
    timeout = CONNECTION_TIMEOUT_S

    # Whether the status line of the current response has been sent.
    response_started = False

    @property
    def app(self) -> VisualizerApp:
        return t.cast(VisualizerServer, self.server).app

    def log_message(self, format: str, *args: t.Any) -> None:  # noqa: A002
        LOGGER.debug(f"{self.address_string()} {format % args}")

    def send_response(self, code: int, message: str | None = None) -> None:
        self.response_started = True
        super().send_response(code, message)

    def do_GET(self) -> None:
        self.handle_safely(lambda: self.handle_request(send_body=True), send_body=True)

    def do_HEAD(self) -> None:
        self.handle_safely(lambda: self.handle_request(send_body=False), send_body=False)

    def do_POST(self) -> None:
        # A rejected request leaves its body unread, which would be parsed as the next request.
        self.close_connection = True
        self.handle_safely(self.handle_post, send_body=True)

    def handle_safely(self, handler: t.Callable[[], None], send_body: bool) -> None:
        """Run a request handler, answering unexpected errors with a JSON error."""
        self.response_started = False
        try:
            handler()
        except (ConnectionError, TimeoutError):
            # Browsers abort video requests all the time, e.g. when seeking.
            self.close_connection = True
        except Exception:
            LOGGER.exception(f"Cannot handle {self.command} {self.path}")
            self.close_connection = True
            if not self.response_started:
                self.send_json_error(500, "Internal server error", send_body)

    def is_host_trusted(self) -> bool:
        """Check that the request addresses the server by a name that cannot be rebound."""
        host = split_host_header(self.headers.get("Host") or "")
        return is_trusted_host(host, self.app.allow_network_hosts)

    def is_cross_site(self) -> bool:
        """
        Check whether a browser sent the request for a page of another site.

        Such a page cannot read the response, but could learn from a `<video>` element whether a
        trip video exists and how long it is. `Sec-Fetch-Site` is sent by current browsers, and
        is `none` when the user opens a URL directly. Other clients (e.g. curl) do not send it.
        """
        fetch_site = self.headers.get("Sec-Fetch-Site")
        return fetch_site is not None and fetch_site not in ("same-origin", "none")

    def is_link_from_another_site(self) -> bool:
        """
        Check whether the user followed a link from another site, which opens the URL in a tab
        (`Sec-Fetch-Dest: document`). The other site cannot read the page, and cannot frame it.
        """
        return (
            self.headers.get("Sec-Fetch-Mode") == "navigate"
            and self.headers.get("Sec-Fetch-Dest") == "document"
        )

    def handle_post(self) -> None:
        """Dispatch a POST request."""
        if not self.is_host_trusted():
            self.send_json_error(403, "Unexpected Host header")
            return
        if self.is_cross_site():
            self.send_json_error(403, "Cross-site requests are not allowed")
            return
        url_path = urllib.parse.urlsplit(self.path).path
        if url_path != "/api/rename":
            self.send_json_error(404, "Not found")
            return
        rejection = self.check_rename_request()
        if rejection is not None:
            self.send_json_error(*rejection)
            return
        try:
            body_size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            body_size = -1
        if not 0 < body_size <= MAX_REQUEST_BODY_SIZE:
            self.send_json_error(400, "Invalid request body size")
            return
        try:
            request = json.loads(self.rfile.read(body_size))
            trip_id = request["id"]
            new_filename = request["filename"]
            if not isinstance(trip_id, str) or not isinstance(new_filename, str):
                raise TypeError("'id' and 'filename' must be strings")
        except (ValueError, KeyError, TypeError) as exc:
            self.send_json_error(400, f"Invalid request: {exc}")
            return
        try:
            trip = self.app.rename_trip(trip_id, new_filename.strip())
        except rename.RenameError as exc:
            self.send_json_error(409, str(exc))
            return
        self.send_json({"trip": trip}, send_body=True)

    def check_rename_request(self) -> tuple[int, str] | None:
        """
        Check that renaming is enabled and that the request comes from the web app.

        Other websites cannot send JSON without a CORS preflight, which the server does not allow.

        Returns
        -------
        tuple[int, str] | None
            The HTTP status and message of the rejection, or None to accept the request.
        """
        if not self.app.allow_rename:
            return 403, RENAME_DISABLED_MESSAGE
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{self.headers.get('Host')}":
            return 403, "Cross-origin requests are not allowed"
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return 415, "Expected a JSON request"
        return None

    def handle_request(self, send_body: bool) -> None:
        """Dispatch a GET or HEAD request."""
        if not self.is_host_trusted():
            self.send_json_error(403, "Unexpected Host header", send_body)
            return
        if self.is_cross_site() and not self.is_link_from_another_site():
            self.send_json_error(403, "Cross-site requests are not allowed", send_body)
            return
        url_path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if url_path == "/api/trips":
            self.send_json(self.app.get_trips(), send_body)
        elif url_path == "/api/suggestion":
            self.send_suggestion(send_body)
        elif url_path == "/api/geometry":
            self.send_json(self.app.get_geometry(), send_body)
        elif url_path.startswith("/videos/"):
            file_path = self.app.video_path(url_path.removeprefix("/videos/"))
            self.send_file(file_path, send_body)
        elif url_path.startswith("/tracks/"):
            file_path = safe_join(self.app.store.tracks_dir, url_path.removeprefix("/tracks/"))
            self.send_file(file_path, send_body)
        elif url_path.startswith("/previews/"):
            file_path = safe_join(self.app.store.previews_dir, url_path.removeprefix("/previews/"))
            self.send_file(file_path, send_body)
        else:
            relative_path = url_path.removeprefix("/") or "index.html"
            self.send_file(safe_join(WEB_DIR, relative_path), send_body)

    def send_suggestion(self, send_body: bool) -> None:
        """Send the suggested filename of the trip given by the `id` query parameter."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        trip_id = (query.get("id") or [""])[0]
        if not self.app.allow_rename:
            self.send_json_error(403, RENAME_DISABLED_MESSAGE, send_body)
            return
        try:
            filename = self.app.suggest_filename(trip_id)
        except rename.RenameError as exc:
            self.send_json_error(409, str(exc), send_body)
            return
        self.send_json({"filename": filename}, send_body)

    def send_json_error(self, status: int, message: str, send_body: bool = True) -> None:
        """Send an error as a JSON response with an `error` message."""
        self.send_json({"error": message}, send_body, status=status)

    def send_json(self, data: t.Any, send_body: bool, status: int = 200) -> None:
        """Send a JSON response."""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPES[".json"])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
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
    library_dir: Path,
    metadata_dir: Path,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    car_model: str = "",
    allow_network_rename: bool = False,
) -> None:
    """
    Run the visualizer server until interrupted.

    Renaming is enabled on a loopback address. `allow_network_rename` also enables it on other
    addresses, for anyone who can reach the server.
    """
    is_loopback = is_loopback_host(host)
    allow_rename = is_loopback or allow_network_rename
    app = VisualizerApp(
        library_dir,
        metadata_dir,
        car_model=car_model,
        allow_rename=allow_rename,
        allow_network_hosts=not is_loopback,
    )
    app.warn_about_library()
    index = app.load_index()
    trip_count = len(index["trips"])
    skipped_count = len(index["skipped_track_stems"])
    if skipped_count:
        LOGGER.warning(
            f"{skipped_count} track files are unreadable or misnamed and not shown "
            f"(run `dashcam doctor`)"
        )

    server = VisualizerServer((host, port), app)
    bound_port = server.server_address[1]
    LOGGER.info(
        f"Serving {trip_count} trips at {format_server_url(host, int(bound_port))}",
        extra=terminal.SUCCESS,
    )
    if not allow_rename:
        LOGGER.info(
            "Renaming trips in the browser is disabled on non-loopback addresses "
            "(use `--allow-rename` to enable it)"
        )
    elif not is_loopback:
        LOGGER.warning("Anyone who can reach this server can rename trips")
    LOGGER.info("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
    finally:
        server.server_close()
