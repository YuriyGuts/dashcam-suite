"""
Per-machine configuration.

Defaults depend on the platform and can be overridden in a TOML file located at
`<user config dir>/dashcam/config.toml` (e.g. `~/.config/dashcam/config.toml` on Linux),
or at the path given by the `DASHCAM_CONFIG` environment variable.

Example config file:

    library_dir = "~/Videos/Dashcam"
    raw_video_dir = "/media/me/DASHCAM/DCIM"
    hwaccel_options = "-hwaccel vulkan"
    video_codec_options = "-c:v libx265 -crf 28 -preset medium"
    encode_job_count = 2
    extract_job_count = 6
    car_model = "Car"
    allowed_areas = [[44.0, 22.0, 52.5, 40.5]]
"""

import dataclasses
import getpass
import os
import sys
import tomllib
import typing as t
import zoneinfo
from pathlib import Path

import platformdirs

from dashcam import geo

# Environment variable that points to a custom config file.
CONFIG_PATH_ENV_VAR = "DASHCAM_CONFIG"

# Name of the config file inside the user config directory.
CONFIG_FILENAME = "config.toml"

# Directory settings in which `~` is expanded, and those of them that must be absolute.
PATH_SETTINGS = ("library_dir", "raw_video_dir", "metadata_dir")
ABSOLUTE_PATH_SETTINGS = ("library_dir", "raw_video_dir")

# How the expected type of a setting is described in error messages.
TYPE_DESCRIPTIONS = {str: "a string", int: "an integer", float: "a number"}

# Settings that must be at least 1.
POSITIVE_INT_SETTINGS = ("encode_job_count", "extract_job_count")


@dataclasses.dataclass(frozen=True)
class Config:
    """Settings that may differ between machines."""

    # Path to the `DCIM` directory on the SD card. Its subdirectories are searched too.
    raw_video_dir: str

    # Path to the library directory with the trip videos and the metadata directory.
    # `None` means it must be passed on the command line.
    library_dir: str | None

    # Which FFmpeg and FFprobe executables to run.
    ffmpeg_executable: str
    ffprobe_executable: str

    # Hardware acceleration options for decoding, e.g. `-hwaccel vulkan` on Linux,
    # `-hwaccel videotoolbox` on macOS, or `-hwaccel d3d12va` on Windows. Blank ("") for none.
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

    # Boxes of `[min_lat, min_lon, max_lat, max_lon]`. GPS fixes outside all of them are treated
    # as spoofed. Empty means anywhere.
    allowed_areas: list[list[float]]


def get_platform_defaults() -> Config:
    """Return the default settings for the current platform."""
    common_settings: dict[str, t.Any] = {
        "ffmpeg_executable": "ffmpeg",
        "ffprobe_executable": "ffprobe",
        "hwaccel_options": "",
        # Closed GOPs make every keyframe an IDR frame. Firefox only seeks efficiently to IDR
        # frames: with x265's default open GOPs, it decodes from the start of the video on every
        # seek, which takes up to a minute in a long trip.
        "video_codec_options": "-c:v libx265 -crf 30 -preset fast -x265-params open-gop=0",
        "audio_codec_options": "-c:a aac -b:a 128k",
        "encode_job_count": 1,
        "extract_job_count": 10,
        "min_trip_gap_hours": 3,
        "car_model": "",
        "metadata_dir": ".metadata",
        "library_dir": None,
        "timezone": "Europe/Kyiv",
        "osm_extract_url": "https://download.geofabrik.de/europe/ukraine-latest.osm.pbf",
        "allowed_areas": [],
    }

    if sys.platform == "darwin":
        return Config(
            raw_video_dir="/Volumes/DASHCAM/DCIM",
            **common_settings,
        )

    if sys.platform == "linux":
        return Config(
            raw_video_dir=f"/media/{getpass.getuser()}/DASHCAM/DCIM",
            **common_settings,
        )

    return Config(
        raw_video_dir="E:\\DCIM",
        **common_settings,
    )


def get_config_path() -> Path:
    """Return the path of the config file (which may not exist)."""
    custom_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if custom_path:
        return Path(custom_path)
    return platformdirs.user_config_path("dashcam", appauthor=False) / CONFIG_FILENAME


def is_of_type(value: t.Any, expected_type: type) -> bool:
    """Check a TOML value against a setting type, where an integer also counts as a number."""
    if isinstance(value, bool):
        return False
    if expected_type is float:
        return isinstance(value, int | float)
    return isinstance(value, expected_type)


def validate_setting_values(overrides: dict[str, t.Any], config_path: Path) -> None:
    """
    Check the types and ranges of the scalar settings, and that the time zone exists.

    Raises
    ------
    ValueError
        If a setting has the wrong type or an invalid value.
    """
    field_types = t.get_type_hints(Config)
    for setting, value in overrides.items():
        # `str | None` settings may only be set to strings, since TOML has no null.
        expected_type = str if field_types[setting] == str | None else field_types[setting]
        if expected_type not in TYPE_DESCRIPTIONS:
            continue
        if not is_of_type(value, expected_type):
            raise ValueError(
                f"'{setting}' in '{config_path}' must be {TYPE_DESCRIPTIONS[expected_type]}"
            )

    for setting in POSITIVE_INT_SETTINGS:
        if setting in overrides and overrides[setting] < 1:
            raise ValueError(f"'{setting}' in '{config_path}' must be at least 1")
    if "min_trip_gap_hours" in overrides and overrides["min_trip_gap_hours"] <= 0:
        raise ValueError(f"'min_trip_gap_hours' in '{config_path}' must be positive")
    if "timezone" in overrides:
        try:
            zoneinfo.ZoneInfo(overrides["timezone"])
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"Unknown time zone '{overrides['timezone']}' in '{config_path}'"
            ) from exc


def validate_allowed_areas(allowed_areas: t.Any, config_path: Path) -> None:
    """
    Check that `allowed_areas` is a list of `[min_lat, min_lon, max_lat, max_lon]` boxes.

    Raises
    ------
    ValueError
        If any box is malformed or out of range.
    """
    if not isinstance(allowed_areas, list):
        raise ValueError(f"'allowed_areas' in '{config_path}' must be a list of boxes")
    for area in allowed_areas:
        is_four_numbers = (
            isinstance(area, list)
            and len(area) == 4
            and all(isinstance(value, int | float) for value in area)
        )
        if not is_four_numbers:
            raise ValueError(
                f"Allowed area {area} in '{config_path}' must be "
                "[min_lat, min_lon, max_lat, max_lon]"
            )
        min_lat, min_lon, max_lat, max_lon = area
        is_in_range = (
            -geo.MAX_ABS_LAT <= min_lat <= max_lat <= geo.MAX_ABS_LAT
            and -geo.MAX_ABS_LON <= min_lon <= max_lon <= geo.MAX_ABS_LON
        )
        if not is_in_range:
            raise ValueError(
                f"Allowed area {area} in '{config_path}' must have min <= max, "
                f"latitudes within +-{geo.MAX_ABS_LAT}, and longitudes within +-{geo.MAX_ABS_LON}"
            )


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
        If the config file is not valid TOML, or contains unknown settings, values of the wrong
        type, relative directory paths, or malformed allowed areas.
    """
    if not config_path.is_file():
        return {}

    with config_path.open("rb") as fp:
        try:
            overrides = tomllib.load(fp)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid TOML in '{config_path}': {exc}") from exc

    known_settings = {field.name for field in dataclasses.fields(Config)}
    unknown_settings = sorted(set(overrides) - known_settings)
    if unknown_settings:
        raise ValueError(f"Unknown settings in '{config_path}': {', '.join(unknown_settings)}")
    validate_setting_values(overrides, config_path)

    for setting in PATH_SETTINGS:
        if setting not in overrides:
            continue
        path = Path(overrides[setting]).expanduser()
        if setting in ABSOLUTE_PATH_SETTINGS and not path.is_absolute():
            raise ValueError(f"'{setting}' in '{config_path}' must be an absolute path")
        overrides[setting] = str(path)

    if "allowed_areas" in overrides:
        validate_allowed_areas(overrides["allowed_areas"], config_path)

    return overrides


def load_config(config_path: Path | None = None) -> Config:
    """
    Load the config, applying overrides from the config file on top of the platform defaults.

    Raises
    ------
    ValueError
        If the config file is invalid (see `read_config_file`).
    """
    if config_path is None:
        config_path = get_config_path()
    return dataclasses.replace(get_platform_defaults(), **read_config_file(config_path))
