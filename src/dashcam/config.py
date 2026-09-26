"""
Per-machine configuration.

Defaults depend on the platform and can be overridden in a TOML file located at
`<user config dir>/dashcam/config.toml` (e.g. `~/.config/dashcam/config.toml` on Linux),
or at the path given by the `DASHCAM_CONFIG` environment variable.

Example config file:

    raw_video_dir = "/media/me/DASHCAM/DCIM/Movie"
    hwaccel_options = "-hwaccel vulkan"
    video_codec_options = "-c:v libx265 -crf 28 -preset medium"
    job_count = 3
    car_model = "CX-5"
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


@dataclasses.dataclass(frozen=True)
class Config:
    """Settings that may differ between machines."""

    # Path to the raw video directory on the SD card.
    raw_video_dir: str

    # Which FFmpeg executable to run.
    ffmpeg_executable: str

    # Hardware acceleration options for decoding. Leave blank ("") for none.
    hwaccel_options: str

    # Codec/quality settings for the encoded trip videos.
    video_codec_options: str
    audio_codec_options: str

    # The maximum number of trips encoded in parallel.
    job_count: int

    # The minimum time difference (in hours) between consecutive segments
    # for them to be considered separate trips.
    min_trip_gap_hours: float

    # Car model used in suggested trip names.
    car_model: str

    # Directory for extracted tracks and the trip index.
    metadata_dir: str

    # Time zone of the camera clock.
    timezone: str

    # GPS cleaning thresholds: the highest plausible speed, how far the camera clock date may
    # be from the date in the video filename, the longest gap inside a merged video, and the
    # longest gap in GPS data that is filled in by linear interpolation.
    max_speed_kmh: float
    max_clock_date_diff_days: float
    max_merge_gap_hours: float
    max_interpolation_gap_s: float

    # OpenStreetMap extract downloaded by `enrich --update-osm`.
    osm_extract_url: str


def get_platform_defaults() -> Config:
    """Return the default settings for the current platform."""
    common_settings: dict[str, t.Any] = {
        "ffmpeg_executable": "ffmpeg",
        # Closed GOPs make every keyframe an IDR frame. Firefox only seeks efficiently to IDR
        # frames: with x265's default open GOPs, it decodes from the start of the video on every
        # seek, which takes up to a minute in a long trip.
        "video_codec_options": "-c:v libx265 -crf 30 -preset fast -x265-params open-gop=0",
        "audio_codec_options": "-c:a aac -b:a 128k",
        "job_count": 2,
        "min_trip_gap_hours": 3,
        "car_model": "CX-5",
        "metadata_dir": ".metadata",
        "timezone": "Europe/Kyiv",
        "max_speed_kmh": 250,
        "max_clock_date_diff_days": 1,
        "max_merge_gap_hours": 12,
        "max_interpolation_gap_s": 60,
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
    return platformdirs.user_config_path("dashcam") / CONFIG_FILENAME


def load_config(config_path: Path | None = None) -> Config:
    """
    Load the config, applying overrides from the config file on top of the platform defaults.

    Raises
    ------
    ValueError
        If the config file contains unknown settings.
    """
    if config_path is None:
        config_path = get_config_path()

    defaults = get_platform_defaults()
    if not config_path.is_file():
        return defaults

    with config_path.open("rb") as fp:
        overrides = tomllib.load(fp)

    known_settings = {field.name for field in dataclasses.fields(Config)}
    unknown_settings = sorted(set(overrides) - known_settings)
    if unknown_settings:
        raise ValueError(f"Unknown settings in '{config_path}': {', '.join(unknown_settings)}")

    return dataclasses.replace(defaults, **overrides)
