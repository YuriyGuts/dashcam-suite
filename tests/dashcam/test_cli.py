from pathlib import Path

import pytest

from dashcam import cli


@pytest.fixture
def encode_calls(monkeypatch):
    calls = []

    def fake_encode_trips(**kwargs):
        calls.append(("trips", kwargs))
        return 0

    def fake_encode_range(**kwargs):
        calls.append(("range", kwargs))
        return 0

    monkeypatch.setattr("dashcam.cli.encode.encode_trips", fake_encode_trips)
    monkeypatch.setattr("dashcam.cli.encode.encode_range", fake_encode_range)
    return calls


def test_parse_command_line_args_encode_trips_uses_config_defaults(config):
    # GIVEN no options

    # WHEN parsing `encode trips`
    parsed_args = cli.parse_command_line_args(["encode", "trips"], config)

    # THEN the defaults come from the config
    assert parsed_args.encode_mode == "trips"
    assert parsed_args.raw_video_dir == Path(config.raw_video_dir)
    assert parsed_args.min_trip_gap_hours == config.min_trip_gap_hours
    assert parsed_args.job_count == config.job_count
    assert parsed_args.output_dir == Path.cwd()


def test_parse_command_line_args_encode_range(config):
    # GIVEN a range with an output name

    # WHEN parsing `encode range`
    parsed_args = cli.parse_command_line_args(
        ["encode", "range", "15", "319", "--output-name", "Road Trip"], config
    )

    # THEN the indexes and name are parsed
    assert (parsed_args.start_index, parsed_args.end_index) == (15, 319)
    assert parsed_args.output_name == "Road Trip"


def test_parse_command_line_args_without_command(config):
    # GIVEN no command

    # WHEN parsing
    # THEN argparse exits with a usage error
    with pytest.raises(SystemExit):
        cli.parse_command_line_args([], config)


def test_run_encode_command_trips_creates_output_dir(config, tmp_path, encode_calls):
    # GIVEN an output directory that does not exist
    output_dir = tmp_path / "new" / "dir"
    parsed_args = cli.parse_command_line_args(
        ["encode", "trips", "--output-dir", str(output_dir), "--skip-raw-video-validation"],
        config,
    )

    # WHEN running the command
    cli.run_encode_command(parsed_args, config)

    # THEN the directory is created and the readability check is disabled
    assert output_dir.is_dir()
    mode, kwargs = encode_calls[0]
    assert mode == "trips"
    assert kwargs["check_readability"] is False


def test_run_encode_command_range(config, tmp_path, encode_calls):
    # GIVEN `encode range` arguments
    parsed_args = cli.parse_command_line_args(
        ["encode", "range", "2", "3", "--output-dir", str(tmp_path)], config
    )

    # WHEN running the command
    cli.run_encode_command(parsed_args, config)

    # THEN the range encoder receives the indexes
    mode, kwargs = encode_calls[0]
    assert mode == "range"
    assert (kwargs["start_index"], kwargs["end_index"]) == (2, 3)
    assert kwargs["check_readability"] is True


def test_main_exits_with_error_on_failed_jobs(monkeypatch, config):
    # GIVEN an encoding run with a failed job
    monkeypatch.setattr("dashcam.cli.load_config", lambda: config)
    monkeypatch.setattr("dashcam.cli.run_encode_command", lambda parsed_args, config: 1)
    monkeypatch.setattr("sys.argv", ["dashcam", "encode", "trips"])

    # WHEN running the tool
    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    # THEN the exit code signals failure
    assert exc_info.value.code == 1


def test_main_exits_with_error_on_missing_raw_video_dir(monkeypatch, config, tmp_path):
    # GIVEN a raw video directory that does not exist
    monkeypatch.setattr("dashcam.cli.load_config", lambda: config)
    monkeypatch.setattr(
        "sys.argv",
        ["dashcam", "encode", "trips", "--raw-video-dir", str(tmp_path / "missing")],
    )

    # WHEN running the tool
    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    # THEN it exits with an error instead of a traceback
    assert exc_info.value.code == 1


def test_main_exits_with_error_on_invalid_config(monkeypatch):
    # GIVEN a config that cannot be loaded
    def failing_load_config():
        raise ValueError("Unknown settings")

    monkeypatch.setattr("dashcam.cli.load_config", failing_load_config)
    monkeypatch.setattr("sys.argv", ["dashcam", "encode", "trips"])

    # WHEN running the tool
    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    # THEN it exits with an error
    assert exc_info.value.code == 1
