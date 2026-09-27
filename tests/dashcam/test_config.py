import pytest

from dashcam import config as config_module


@pytest.fixture
def platform(monkeypatch):
    def _set_platform(name):
        monkeypatch.setattr("dashcam.config.sys.platform", name)

    return _set_platform


def test_get_platform_defaults_on_macos(platform):
    # GIVEN macOS
    platform("darwin")

    # WHEN getting the defaults
    defaults = config_module.get_platform_defaults()

    # THEN the SD card is expected under /Volumes
    assert defaults.raw_video_dir.startswith("/Volumes/")


def test_get_platform_defaults_on_linux(platform):
    # GIVEN Linux
    platform("linux")

    # WHEN getting the defaults
    defaults = config_module.get_platform_defaults()

    # THEN the SD card is expected under /media
    assert defaults.raw_video_dir.startswith("/media/")


def test_get_platform_defaults_on_other_platforms(platform):
    # GIVEN Windows
    platform("win32")

    # WHEN getting the defaults
    defaults = config_module.get_platform_defaults()

    # THEN the SD card is expected on a drive
    assert defaults.raw_video_dir == "E:\\DCIM"


def test_get_platform_defaults_without_hardware_acceleration(platform):
    # GIVEN each supported platform
    hwaccel_options_by_platform = {}
    for platform_name in ("darwin", "linux", "win32"):
        platform(platform_name)

        # WHEN getting the defaults
        defaults = config_module.get_platform_defaults()
        hwaccel_options_by_platform[platform_name] = defaults.hwaccel_options

    # THEN hardware acceleration is off everywhere, so that ffmpeg works without a GPU
    assert hwaccel_options_by_platform == {"darwin": "", "linux": "", "win32": ""}


def test_load_config_without_file(tmp_path):
    # GIVEN a config path that does not exist

    # WHEN loading the config
    loaded_config = config_module.load_config(tmp_path / "missing.toml")

    # THEN the platform defaults are used
    assert loaded_config == config_module.get_platform_defaults()


def test_load_config_applies_overrides(tmp_path):
    # GIVEN a config file overriding some settings
    config_path = tmp_path / "config.toml"
    config_path.write_text('extract_job_count = 5\ncar_model = "Outback"\n')

    # WHEN loading the config
    loaded_config = config_module.load_config(config_path)

    # THEN the overrides are applied and the rest stays default
    defaults = config_module.get_platform_defaults()
    assert loaded_config.extract_job_count == 5
    assert loaded_config.car_model == "Outback"
    assert loaded_config.video_codec_options == defaults.video_codec_options


def test_load_config_with_unknown_setting(tmp_path):
    # GIVEN a config file with a misspelled setting
    config_path = tmp_path / "config.toml"
    config_path.write_text("job_cont = 5\n")

    # WHEN loading the config
    # THEN it fails and names the setting
    with pytest.raises(ValueError, match="job_cont"):
        config_module.load_config(config_path)


def test_load_config_with_invalid_toml(tmp_path):
    # GIVEN a config file with a syntax error
    config_path = tmp_path / "config.toml"
    config_path.write_text("library_dir = /videos\n")

    # WHEN loading the config
    # THEN it fails and names the file
    with pytest.raises(ValueError, match="Invalid TOML in .*config.toml"):
        config_module.load_config(config_path)


@pytest.mark.parametrize(
    ("setting_text", "message"),
    [
        ('encode_job_count = "2"', "'encode_job_count' .* must be an integer"),
        ("extract_job_count = 2.5", "'extract_job_count' .* must be an integer"),
        ("extract_job_count = true", "'extract_job_count' .* must be an integer"),
        ("encode_job_count = 0", "'encode_job_count' .* must be at least 1"),
        ('min_trip_gap_hours = "3"', "'min_trip_gap_hours' .* must be a number"),
        ("min_trip_gap_hours = 0", "'min_trip_gap_hours' .* must be positive"),
        ("library_dir = 5", "'library_dir' .* must be a string"),
        ("car_model = []", "'car_model' .* must be a string"),
        ('timezone = "Europe/Atlantis"', "Unknown time zone 'Europe/Atlantis'"),
        ('timezone = "../etc"', "Unknown time zone '../etc'"),
    ],
)
def test_load_config_with_invalid_value(tmp_path, setting_text, message):
    # GIVEN a config file with a value of the wrong type or out of range
    config_path = tmp_path / "config.toml"
    config_path.write_text(setting_text + "\n")

    # WHEN loading the config
    # THEN it fails and names the setting
    with pytest.raises(ValueError, match=message):
        config_module.load_config(config_path)


def test_load_config_accepts_integer_trip_gap_and_valid_time_zone(tmp_path):
    # GIVEN a config file with an integer trip gap and a valid time zone
    config_path = tmp_path / "config.toml"
    config_path.write_text('min_trip_gap_hours = 2\ntimezone = "America/New_York"\n')

    # WHEN loading the config
    config = config_module.load_config(config_path)

    # THEN both are applied
    assert config.min_trip_gap_hours == 2
    assert config.timezone == "America/New_York"


def test_load_config_with_allowed_areas(tmp_path):
    # GIVEN a config file with two allowed areas
    config_path = tmp_path / "config.toml"
    config_path.write_text("allowed_areas = [[44, 22, 52.5, 40.5], [49.0, 14.0, 55.0, 24.0]]\n")

    # WHEN loading the config
    loaded_config = config_module.load_config(config_path)

    # THEN the areas are loaded as given
    assert loaded_config.allowed_areas == [[44, 22, 52.5, 40.5], [49.0, 14.0, 55.0, 24.0]]


def test_get_platform_defaults_allows_any_area():
    # GIVEN no config file

    # WHEN getting the defaults
    defaults = config_module.get_platform_defaults()

    # THEN GPS fixes are allowed anywhere
    assert defaults.allowed_areas == []


@pytest.mark.parametrize(
    "allowed_areas_text",
    [
        "[44, 22, 52.5, 40.5]",
        '[[44, 22, 52.5, "40.5"]]',
        "[[44, 22, 52.5]]",
        "[[52.5, 22, 44, 40.5]]",
        "[[44, 40.5, 52.5, 22]]",
        "[[44, 22, 95, 40.5]]",
        "[[44, -190, 52.5, 40.5]]",
        '"Ukraine"',
    ],
)
def test_load_config_with_malformed_allowed_areas(tmp_path, allowed_areas_text):
    # GIVEN a config file with a malformed allowed area
    config_path = tmp_path / "config.toml"
    config_path.write_text(f"allowed_areas = {allowed_areas_text}\n")

    # WHEN loading the config
    # THEN it fails and names the setting or the area
    with pytest.raises(ValueError, match="[Aa]llowed.area"):
        config_module.load_config(config_path)


def test_get_config_path_from_environment(monkeypatch, tmp_path):
    # GIVEN the config path environment variable
    monkeypatch.setenv(config_module.CONFIG_PATH_ENV_VAR, str(tmp_path / "custom.toml"))

    # WHEN getting the config path
    config_path = config_module.get_config_path()

    # THEN the environment variable wins
    assert config_path == tmp_path / "custom.toml"


def test_get_config_path_default(monkeypatch):
    # GIVEN no config path environment variable
    monkeypatch.delenv(config_module.CONFIG_PATH_ENV_VAR, raising=False)

    # WHEN getting the config path
    config_path = config_module.get_config_path()

    # THEN it is inside the user config directory
    assert config_path.name == "config.toml"
    assert config_path.parent.name == "dashcam"
    assert config_path.parent.parent.name != "dashcam"


def test_load_config_expands_home_in_paths(monkeypatch, tmp_path):
    # GIVEN a config file with paths under the home directory
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'library_dir = "~/Videos"\nraw_video_dir = "~/SD"\nmetadata_dir = "~/meta"\n'
    )

    # WHEN loading the config
    loaded_config = config_module.load_config(config_path)

    # THEN `~` is expanded in every path
    assert loaded_config.library_dir == str(tmp_path / "Videos")
    assert loaded_config.raw_video_dir == str(tmp_path / "SD")
    assert loaded_config.metadata_dir == str(tmp_path / "meta")


def test_load_config_with_relative_library_dir(tmp_path):
    # GIVEN a config file with a relative library directory
    config_path = tmp_path / "config.toml"
    config_path.write_text('library_dir = "Videos"\n')

    # WHEN loading the config
    # THEN it fails and names the setting
    with pytest.raises(ValueError, match="'library_dir'.*absolute"):
        config_module.load_config(config_path)


def test_load_config_with_relative_raw_video_dir(tmp_path):
    # GIVEN a config file with a relative raw video directory
    config_path = tmp_path / "config.toml"
    config_path.write_text('raw_video_dir = "DCIM/Movie"\n')

    # WHEN loading the config
    # THEN it fails and names the setting
    with pytest.raises(ValueError, match="'raw_video_dir'.*absolute"):
        config_module.load_config(config_path)


def test_load_config_keeps_relative_metadata_dir(tmp_path):
    # GIVEN a config file with a relative metadata directory
    config_path = tmp_path / "config.toml"
    config_path.write_text('metadata_dir = "meta"\n')

    # WHEN loading the config
    loaded_config = config_module.load_config(config_path)

    # THEN it stays relative, to be resolved against the library directory
    assert loaded_config.metadata_dir == "meta"


def test_get_platform_defaults_without_library_dir():
    # GIVEN no config file

    # WHEN getting the defaults
    defaults = config_module.get_platform_defaults()

    # THEN the library directory is not set
    assert defaults.library_dir is None
