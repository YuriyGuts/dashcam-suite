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


@pytest.fixture
def recorded_calls(monkeypatch):
    calls = []

    def recorder(name, return_value=0):
        def record(*args, **kwargs):
            calls.append((name, args, kwargs))
            return return_value

        return record

    monkeypatch.setattr("dashcam.cli.extract.extract_videos", recorder("extract_videos"))
    monkeypatch.setattr("dashcam.cli.extract.reclean_tracks", recorder("reclean_tracks"))
    monkeypatch.setattr("dashcam.cli.maintenance.print_status", recorder("print_status", None))
    monkeypatch.setattr("dashcam.cli.maintenance.forget_trips", recorder("forget_trips"))
    monkeypatch.setattr("dashcam.cli.maintenance.run_doctor", recorder("run_doctor"))
    monkeypatch.setattr("dashcam.cli.serve.serve", recorder("serve", None))
    monkeypatch.setattr("dashcam.cli.enrich.enrich_tracks", recorder("enrich_tracks"))
    monkeypatch.setattr("dashcam.cli.rename.rename_trips", recorder("rename_trips"))
    monkeypatch.setattr("dashcam.cli.importer.import_trips", recorder("import_trips"))
    return calls


def run_main(monkeypatch, config, argv):
    monkeypatch.setattr("dashcam.cli.load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["dashcam", *argv])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    return exc_info.value.code


def test_parse_command_line_args_extract_defaults(config):
    # GIVEN no options

    # WHEN parsing `extract`
    parsed_args = cli.parse_command_line_args(["extract"], config)

    # THEN the current directory and the configured metadata directory are used
    assert parsed_args.video_dir == Path.cwd()
    assert parsed_args.metadata_dir == Path(config.metadata_dir)
    assert parsed_args.include == []
    assert not parsed_args.reclean


def test_main_runs_extract_with_patterns(monkeypatch, config, recorded_calls):
    # GIVEN `extract` with repeated patterns and previews

    # WHEN running the tool
    exit_code = run_main(
        monkeypatch,
        config,
        [
            "extract",
            "videos",
            "--include",
            "2026-*",
            "--include",
            "2025-*",
            "--exclude",
            "*old*",
            "--previews",
        ],
    )

    # THEN the extractor receives the options
    name, _, kwargs = recorded_calls[0]
    assert exit_code == 0
    assert name == "extract_videos"
    assert kwargs["video_dir"] == Path("videos")
    assert kwargs["include"] == ["2026-*", "2025-*"]
    assert kwargs["exclude"] == ["*old*"]
    assert kwargs["make_previews"] is True


def test_main_runs_reclean(monkeypatch, config, recorded_calls):
    # GIVEN `extract --reclean`

    # WHEN running the tool
    run_main(monkeypatch, config, ["extract", "--reclean", "--metadata-dir", "meta"])

    # THEN only recleaning runs
    assert [call[0] for call in recorded_calls] == ["reclean_tracks"]
    assert recorded_calls[0][1][0] == Path("meta")


def test_main_runs_status(monkeypatch, config, recorded_calls):
    # GIVEN `status` with a directory

    # WHEN running the tool
    exit_code = run_main(monkeypatch, config, ["status", "videos"])

    # THEN the status is printed for that directory
    assert exit_code == 0
    assert recorded_calls[0][0] == "print_status"
    assert recorded_calls[0][1][0] == Path("videos")


def test_main_runs_forget(monkeypatch, config, recorded_calls):
    # GIVEN `forget` with two names

    # WHEN running the tool
    run_main(monkeypatch, config, ["forget", "a.mp4", "b"])

    # THEN both names are forgotten
    assert recorded_calls[0][0] == "forget_trips"
    assert recorded_calls[0][1][0] == ["a.mp4", "b"]


def test_main_runs_doctor_with_fix(monkeypatch, config, recorded_calls):
    # GIVEN `doctor --fix`

    # WHEN running the tool
    run_main(monkeypatch, config, ["doctor", "--fix"])

    # THEN fixes are applied
    assert recorded_calls[0][0] == "run_doctor"
    assert recorded_calls[0][2]["apply_fixes"] is True


def test_main_runs_enrich_with_update_osm(monkeypatch, config, recorded_calls):
    # GIVEN `enrich --update-osm`

    # WHEN running the tool
    exit_code = run_main(monkeypatch, config, ["enrich", "--update-osm"])

    # THEN the OSM data is updated before enriching
    name, _, kwargs = recorded_calls[0]
    assert exit_code == 0
    assert name == "enrich_tracks"
    assert kwargs["update_osm"] is True
    assert kwargs["osm_file"] is None
    assert kwargs["force"] is False
    assert kwargs["metadata_dir"] == Path(config.metadata_dir)


def test_main_runs_enrich_with_osm_file_and_force(monkeypatch, config, recorded_calls):
    # GIVEN `enrich` with a local OSM file and `--force`

    # WHEN running the tool
    run_main(monkeypatch, config, ["enrich", "--osm-file", "lviv.osm.pbf", "--force"])

    # THEN the options are passed on
    kwargs = recorded_calls[0][2]
    assert kwargs["update_osm"] is False
    assert kwargs["osm_file"] == Path("lviv.osm.pbf")
    assert kwargs["force"] is True


def test_main_runs_rename_suggest_all(monkeypatch, config, recorded_calls):
    # GIVEN `rename --suggest --all` with a directory

    # WHEN running the tool
    exit_code = run_main(monkeypatch, config, ["rename", "videos", "--suggest", "--all"])

    # THEN all trips are offered for renaming interactively
    name, _, kwargs = recorded_calls[0]
    assert exit_code == 0
    assert name == "rename_trips"
    assert kwargs["video_dir"] == Path("videos")
    assert kwargs["include_all"] is True
    assert kwargs["interactive"] is True
    assert kwargs["assume_yes"] is False


def test_main_runs_rename_without_options(monkeypatch, config, recorded_calls):
    # GIVEN `rename` alone

    # WHEN running the tool
    run_main(monkeypatch, config, ["rename"])

    # THEN only placeholder names are targeted, without applying anything
    kwargs = recorded_calls[0][2]
    assert kwargs["include_all"] is False
    assert kwargs["interactive"] is False
    assert kwargs["assume_yes"] is False


def test_main_runs_rename_with_yes(monkeypatch, config, recorded_calls):
    run_main(monkeypatch, config, ["rename", "--yes"])

    assert recorded_calls[0][2]["assume_yes"] is True


def test_main_runs_import_with_metadata_under_out_dir(
    monkeypatch, config, recorded_calls, tmp_path
):
    # GIVEN `import` into a new directory, with encode options
    out_dir = tmp_path / "Dashcam"

    # WHEN running the tool
    exit_code = run_main(
        monkeypatch,
        config,
        ["import", "--out", str(out_dir), "--raw-video-dir", "sd", "--job-count", "3"],
    )

    # THEN the directory is created and the metadata lives inside it
    name, _, kwargs = recorded_calls[0]
    assert exit_code == 0
    assert name == "import_trips"
    assert out_dir.is_dir()
    assert kwargs["output_dir"] == out_dir
    assert kwargs["metadata_dir"] == out_dir / config.metadata_dir
    assert kwargs["raw_video_dir"] == Path("sd")
    assert kwargs["job_count"] == 3
    assert kwargs["suggest_names"] is True
    assert kwargs["check_readability"] is True


def test_main_runs_import_with_options(monkeypatch, config, recorded_calls, tmp_path):
    # GIVEN `import` with an explicit metadata directory and without renaming

    # WHEN running the tool
    run_main(
        monkeypatch,
        config,
        [
            "import",
            "--out",
            str(tmp_path),
            "--metadata-dir",
            "meta",
            "--no-rename",
            "--dry-run",
            "--skip-raw-video-validation",
        ],
    )

    # THEN the options are passed on
    kwargs = recorded_calls[0][2]
    assert kwargs["metadata_dir"] == Path("meta")
    assert kwargs["suggest_names"] is False
    assert kwargs["dry_run"] is True
    assert kwargs["check_readability"] is False


def test_main_exits_with_error_without_osm_data(monkeypatch, config, tmp_path):
    # GIVEN a metadata directory without OSM data

    # WHEN running `enrich`
    exit_code = run_main(monkeypatch, config, ["enrich", "--metadata-dir", str(tmp_path)])

    # THEN the tool fails
    assert exit_code == 1


def test_main_exits_with_error_when_doctor_finds_errors(monkeypatch, config):
    # GIVEN a doctor run that finds an error
    monkeypatch.setattr("dashcam.cli.maintenance.run_doctor", lambda **kwargs: 1)

    # WHEN running the tool
    exit_code = run_main(monkeypatch, config, ["doctor"])

    # THEN the exit code signals failure
    assert exit_code == 1


def test_main_runs_serve_with_defaults(monkeypatch, config, recorded_calls):
    # GIVEN `serve` without options

    # WHEN running the tool
    exit_code = run_main(monkeypatch, config, ["serve"])

    # THEN the server listens on the local address and default port
    name, _, kwargs = recorded_calls[0]
    assert exit_code == 0
    assert name == "serve"
    assert kwargs["video_dir"] == Path.cwd()
    assert kwargs["metadata_dir"] == Path(config.metadata_dir)
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 8765)


def test_main_runs_serve_with_options(monkeypatch, config, recorded_calls):
    # GIVEN `serve` with a directory, address, and port

    # WHEN running the tool
    run_main(monkeypatch, config, ["serve", "videos", "--host", "0.0.0.0", "--port", "9000"])

    # THEN the options are passed on
    kwargs = recorded_calls[0][2]
    assert kwargs["video_dir"] == Path("videos")
    assert (kwargs["host"], kwargs["port"]) == ("0.0.0.0", 9000)


def test_main_exits_with_error_when_port_is_taken(monkeypatch, config, caplog):
    # GIVEN a port that cannot be bound
    def failing_serve(**kwargs):
        raise OSError("[Errno 48] Address already in use")

    monkeypatch.setattr("dashcam.cli.serve.serve", failing_serve)

    # WHEN running the tool
    exit_code = run_main(monkeypatch, config, ["serve"])

    # THEN the error is reported
    assert exit_code == 1
    assert "Address already in use" in caplog.text
