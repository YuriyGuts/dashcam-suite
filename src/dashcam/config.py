"""
Per-machine configuration.

Defaults depend on the platform and can be overridden in a TOML file located at
`<user config dir>/dashcam/config.toml` (e.g. `~/.config/dashcam/config.toml` on Linux),
or at the path given by the `DASHCAM_CONFIG` environment variable.

Example config file:

    library_dir = "~/Videos/Dashcam"
    raw_video_dir = "/media/me/DASHCAM/DCIM/Movie"
    hwaccel_options = "-hwaccel vulkan"
    video_codec_options = "-c:v libx265 -crf 28 -preset medium"
    encode_job_count = 2
    extract_job_count = 6
    car_model = "Car"
"""

import dataclasses
import getpass
import os
import sys
import tomllib
import typing as t
from pathlib import Path

import platformdirs

# Environment variable that points to a custom config file.
CONFIG_PATH_ENV_VAR = "DASHCAM_CONFIG"

# Name of the config file inside the user config directory.
CONFIG_FILENAME = "config.toml"

# Directory settings in which `~` is expanded, and those of them that must be absolute.
PATH_SETTINGS = ("library_dir", "raw_video_dir", "metadata_dir")
ABSOLUTE_PATH_SETTINGS = ("library_dir", "raw_video_dir")


@dataclasses.dataclass(frozen=True)
class Config:
    """Settings that may differ between machines."""

    # Path to the raw video directory on the SD card.
    raw_video_dir: str

    # Path to the library directory with the trip videos and the metadata directory.
    # `None` means it must be passed on the command line.
    library_dir: str | None

    # Which FFmpeg and FFprobe executables to run.
    ffmpeg_executable: str
    ffprobe_executable: str

    # Hardware acceleration options for decoding. Leave blank ("") for none.
    hwaccel_options: str

    # Codec/quality settings for the encoded trip videos.
    video_codec_options: str
    audio_codec_options: str

    # The maximum number of trips encoded in parallel.
    encode_job_count: int

    # The maximum number of videos extracted in parallel.
    extract_job_count: int

    # The minimum time difference (in hours) between consecutive segments
    # for them to be considered separate trips.
    min_trip_gap_hours: float

    # Car model used in suggested trip names.
    car_model: str

    # Metadata directory for extracted tracks and the trip index. A relative path is relative
    # to the library directory.
    metadata_dir: str

    # Time zone of the camera clock.
    timezone: str

    # OpenStreetMap extract downloaded by `enrich --update-osm`.
    osm_extract_url: str


def get_platform_defaults() -> Config:
    """Return the default settings for the current platform."""
    common_settings: dict[str, t.Any] = {
        "ffmpeg_executable": "ffmpeg",
        "ffprobe_executable": "ffprobe",
        # Closed GOPs make every keyframe an IDR frame. Firefox only seeks efficiently to IDR
        # frames: with x265's default open GOPs, it decodes from the start of the video on every
        # seek, which takes up to a minute in a long trip.
        "video_codec_options": "-c:v libx265 -crf 30 -preset fast -x265-params open-gop=0",
        "audio_codec_options": "-c:a aac -b:a 128k",
        "encode_job_count": 1,
        "extract_job_count": 10,
        "min_trip_gap_hours": 3,
        "car_model": "Car",
        "metadata_dir": ".metadata",
        "library_dir": None,
        "timezone": "Europe/Kyiv",
        "osm_extract_url": "https://download.geofabrik.de/europe/ukraine-latest.osm.pbf",
    }

    if sys.platform == "darwin":
        return Config(
            raw_video_dir="/Volumes/DASHCAM/DCIM/Movie",
            hwaccel_options="-hwaccel videotoolbox",
            **common_settings,
        )

    if sys.platform == "linux":
        return Config(
            raw_video_dir=f"/media/{getpass.getuser()}/DASHCAM/DCIM/Movie",
            hwaccel_options="-hwaccel vulkan",
            **common_settings,
        )

    return Config(
        raw_video_dir="E:\\DCIM\\Movie",
        hwaccel_options="",
        **common_settings,
    )


def get_config_path() -> Path:
    """Return the path of the config file (which may not exist)."""
    custom_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if custom_path:
        return Path(custom_path)
    return platformdirs.user_config_path("dashcam", appauthor=False) / CONFIG_FILENAME


def read_config_file(config_path: Path) -> dict[str, t.Any]:
    """
    Read the settings overridden in the config file, with `~` expanded in paths.

    Returns
    -------
    dict[str, t.Any]
        The overridden settings, or an empty dict if the file does not exist.

    Raises
    ------
    ValueError
        If the config file contains unknown settings or relative directory paths.
    """
    if not config_path.is_file():
        return {}

    with config_path.open("rb") as fp:
        overrides = tomllib.load(fp)

    known_settings = {field.name for field in dataclasses.fields(Config)}
    unknown_settings = sorted(set(overrides) - known_settings)
    if unknown_settings:
        raise ValueError(f"Unknown settings in '{config_path}': {', '.join(unknown_settings)}")

    for setting in PATH_SETTINGS:
        if setting not in overrides:
            continue
        path = Path(overrides[setting]).expanduser()
        if setting in ABSOLUTE_PATH_SETTINGS and not path.is_absolute():
            raise ValueError(f"'{setting}' in '{config_path}' must be an absolute path")
        overrides[setting] = str(path)

    return overrides


def load_config(config_path: Path | None = None) -> Config:
    """
    Load the config, applying overrides from the config file on top of the platform defaults.

    Raises
    ------
    ValueError
        If the config file contains unknown settings or relative directory paths.
    """
    if config_path is None:
        config_path = get_config_path()
    return dataclasses.replace(get_platform_defaults(), **read_config_file(config_path))
