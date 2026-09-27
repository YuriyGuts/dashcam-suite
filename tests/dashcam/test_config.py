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

    # THEN VideoToolbox is used and the SD card is expected under /Volumes
    assert defaults.hwaccel_options == "-hwaccel videotoolbox"
    assert defaults.raw_video_dir.startswith("/Volumes/")


def test_get_platform_defaults_on_linux(platform):
    # GIVEN Linux
    platform("linux")

    # WHEN getting the defaults
    defaults = config_module.get_platform_defaults()

    # THEN Vulkan is used and the SD card is expected under /media
    assert defaults.hwaccel_options == "-hwaccel vulkan"
    assert defaults.raw_video_dir.startswith("/media/")


def test_get_platform_defaults_on_other_platforms(platform):
    # GIVEN an unsupported platform
    platform("win32")

    # WHEN getting the defaults
    defaults = config_module.get_platform_defaults()

    # THEN no hardware acceleration is used
    assert defaults.hwaccel_options == ""


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


def test_load_config_expands_home_in_paths(monkeypatch, tmp_path):
    # GIVEN a config file with paths under the home directory
    monkeypatch.setenv("HOME", str(tmp_path))
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
