import dataclasses
import json
from pathlib import Path

import pytest

from dashcam import cli


@pytest.fixture
def library_dir(tmp_path):
    return tmp_path / "Library"


@pytest.fixture
def config(config, library_dir):
    return dataclasses.replace(config, library_dir=str(library_dir))


@pytest.fixture
def config_without_library_dir(config):
    return dataclasses.replace(config, library_dir=None)


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


def test_parse_command_line_args_encode_trips_uses_config_defaults(config, library_dir):
    # GIVEN no options

    # WHEN parsing `encode trips`
    parsed_args = cli.parse_command_line_args(["encode", "trips"], config)

    # THEN the defaults come from the config
    assert parsed_args.encode_mode == "trips"
    assert parsed_args.raw_video_dir == Path(config.raw_video_dir)
    assert parsed_args.min_trip_gap_hours == config.min_trip_gap_hours
    assert parsed_args.job_count == config.encode_job_count
    assert parsed_args.library_dir == library_dir


def test_parse_command_line_args_encode_range_with_short_library_dir_option(config):
    # GIVEN a library directory passed with `-d`

    # WHEN parsing `encode range`
    parsed_args = cli.parse_command_line_args(["encode", "range", "1", "2", "-d", "out"], config)

    # THEN the option overrides the config
    assert parsed_args.library_dir == Path("out")


def test_parse_command_line_args_without_library_dir(config_without_library_dir, capsys):
    # GIVEN no library directory in the config or on the command line

    # WHEN parsing `serve`
    # THEN argparse exits with an error that points to both ways of setting it
    with pytest.raises(SystemExit) as exc_info:
        cli.parse_command_line_args(["serve"], config_without_library_dir)
    error_text = capsys.readouterr().err
    assert exc_info.value.code == 2
    assert "--library-dir" in error_text
    assert "library_dir" in error_text
    assert str(cli.get_config_path()) in error_text


def test_parse_command_line_args_encode_without_library_dir(config_without_library_dir):
    # GIVEN no library directory in the config or on the command line

    # WHEN parsing `encode trips`
    # THEN argparse exits with an error
    with pytest.raises(SystemExit):
        cli.parse_command_line_args(["encode", "trips"], config_without_library_dir)


def test_parse_command_line_args_metadata_dir_inside_library_dir(config, library_dir):
    # GIVEN a relative metadata directory in the config

    # WHEN parsing `status`
    parsed_args = cli.parse_command_line_args(["status"], config)

    # THEN the metadata directory is inside the library directory
    assert parsed_args.library_dir == library_dir
    assert parsed_args.metadata_dir == library_dir / config.metadata_dir


def test_parse_command_line_args_metadata_dir_inside_given_library_dir(config):
    # GIVEN a library directory on the command line

    # WHEN parsing `doctor`
    parsed_args = cli.parse_command_line_args(["doctor", "--library-dir", "videos"], config)

    # THEN the metadata directory is inside that library directory
    assert parsed_args.metadata_dir == Path("videos") / config.metadata_dir


def test_parse_command_line_args_absolute_metadata_dir_in_config(config, tmp_path):
    # GIVEN an absolute metadata directory in the config
    config = dataclasses.replace(config, metadata_dir=str(tmp_path / "meta"))

    # WHEN parsing `serve`
    parsed_args = cli.parse_command_line_args(["serve"], config)

    # THEN it is used as it is
    assert parsed_args.metadata_dir == tmp_path / "meta"


def test_parse_command_line_args_relative_metadata_dir_option(config):
    # GIVEN a relative metadata directory on the command line

    # WHEN parsing `rename`
    parsed_args = cli.parse_command_line_args(["rename", "--metadata-dir", "meta"], config)

    # THEN it stays relative to the current directory
    assert parsed_args.metadata_dir == Path("meta")


def test_parse_command_line_args_enrich_with_metadata_dir_only(config_without_library_dir):
    # GIVEN no library directory, but a metadata directory on the command line

    # WHEN parsing `enrich`
    parsed_args = cli.parse_command_line_args(
        ["enrich", "--metadata-dir", "meta"], config_without_library_dir
    )

    # THEN no library directory is needed
    assert parsed_args.metadata_dir == Path("meta")
    assert parsed_args.library_dir is None


def test_parse_command_line_args_forget_with_metadata_dir_only(config_without_library_dir):
    # GIVEN no library directory, but a metadata directory on the command line

    # WHEN parsing `forget`
    parsed_args = cli.parse_command_line_args(
        ["forget", "a.mp4", "--metadata-dir", "meta"], config_without_library_dir
    )

    # THEN no library directory is needed
    assert parsed_args.metadata_dir == Path("meta")


def test_parse_command_line_args_reclean_with_metadata_dir_only(config_without_library_dir):
    # GIVEN no library directory, but a metadata directory on the command line

    # WHEN parsing `extract --reclean`
    parsed_args = cli.parse_command_line_args(
        ["extract", "--reclean", "--metadata-dir", "meta"], config_without_library_dir
    )

    # THEN no library directory is needed
    assert parsed_args.metadata_dir == Path("meta")


def test_parse_command_line_args_extract_with_metadata_dir_only(config_without_library_dir):
    # GIVEN no library directory, but a metadata directory on the command line

    # WHEN parsing `extract`, which reads the videos
    # THEN argparse exits with an error
    with pytest.raises(SystemExit):
        cli.parse_command_line_args(
            ["extract", "--metadata-dir", "meta"], config_without_library_dir
        )


def test_parse_command_line_args_enrich_without_any_directory(config_without_library_dir):
    # GIVEN no library directory and a relative metadata directory in the config

    # WHEN parsing `enrich`
    # THEN argparse exits with an error, since the metadata directory cannot be located
    with pytest.raises(SystemExit):
        cli.parse_command_line_args(["enrich"], config_without_library_dir)


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


def test_run_encode_command_trips_creates_library_dir(config, tmp_path, encode_calls):
    # GIVEN a library directory that does not exist
    library_dir = tmp_path / "new" / "dir"
    parsed_args = cli.parse_command_line_args(
        ["encode", "trips", "--library-dir", str(library_dir), "--skip-raw-video-validation"],
        config,
    )

    # WHEN running the command
    cli.run_encode_command(parsed_args, config)

    # THEN the directory is created and the readability check is disabled
    assert library_dir.is_dir()
    mode, kwargs = encode_calls[0]
    assert mode == "trips"
    assert kwargs["check_readability"] is False


def test_run_encode_command_dry_run_does_not_create_library_dir(config, tmp_path, encode_calls):
    # GIVEN a library directory that does not exist
    library_dir = tmp_path / "new" / "dir"
    parsed_args = cli.parse_command_line_args(
        ["encode", "trips", "--library-dir", str(library_dir), "--dry-run"], config
    )

    # WHEN running the command as a dry run
    cli.run_encode_command(parsed_args, config)

    # THEN nothing is created
    assert not library_dir.exists()
    assert encode_calls[0][1]["dry_run"] is True


@pytest.mark.parametrize(
    "args",
    [
        ["serve", "--port", "70000"],
        ["serve", "--port", "-1"],
        ["extract", "--job-count", "0"],
        ["import", "--extract-job-count", "-2"],
        ["encode", "trips", "--min-trip-gap-hours", "0"],
    ],
)
def test_parse_command_line_args_rejects_out_of_range_numbers(config, args, capsys):
    # WHEN parsing a number out of its range
    # THEN argparse exits with a usage error that explains the range
    with pytest.raises(SystemExit) as exc_info:
        cli.parse_command_line_args(args, config)
    assert exc_info.value.code == 2
    assert "must be" in capsys.readouterr().err


def test_main_exits_quietly_on_ctrl_c(monkeypatch, config, caplog):
    # GIVEN a command interrupted with Ctrl+C
    def interrupted_encode(parsed_args, config):
        raise KeyboardInterrupt

    monkeypatch.setattr("dashcam.cli.load_config", lambda: config)
    monkeypatch.setattr("dashcam.cli.run_encode_command", interrupted_encode)
    monkeypatch.setattr("sys.argv", ["dashcam", "encode", "trips"])

    # WHEN running the tool
    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    # THEN it reports the interruption with a cleanup hint, and exits with the conventional code
    assert exc_info.value.code == cli.INTERRUPTED_EXIT_CODE
    assert "Interrupted (`dashcam doctor --fix` removes any partial files" in caplog.text


def test_run_encode_command_range(config, tmp_path, encode_calls):
    # GIVEN `encode range` arguments
    parsed_args = cli.parse_command_line_args(
        ["encode", "range", "2", "3", "--library-dir", str(tmp_path)], config
    )

    # WHEN running the command
    cli.run_encode_command(parsed_args, config)

    # THEN the range encoder receives the indexes and the metadata directory in the library
    mode, kwargs = encode_calls[0]
    assert mode == "range"
    assert (kwargs["start_index"], kwargs["end_index"]) == (2, 3)
    assert kwargs["check_readability"] is True
    assert kwargs["metadata_dir"] == tmp_path / ".metadata"


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


def test_parse_command_line_args_extract_defaults(config, library_dir):
    # GIVEN no options

    # WHEN parsing `extract`
    parsed_args = cli.parse_command_line_args(["extract"], config)

    # THEN the configured library and metadata directories are used
    assert parsed_args.library_dir == library_dir
    assert parsed_args.metadata_dir == library_dir / config.metadata_dir
    assert parsed_args.include == []
    assert parsed_args.job_count == config.extract_job_count
    assert not parsed_args.reclean


def test_main_runs_extract_with_patterns(monkeypatch, config, recorded_calls):
    # GIVEN `extract` with repeated patterns and previews

    # WHEN running the tool
    exit_code = run_main(
        monkeypatch,
        config,
        [
            "extract",
            "-d",
            "videos",
            "--include",
            "2026-*",
            "--include",
            "2025-*",
            "--exclude",
            "*old*",
            "--previews",
            "--job-count",
            "8",
        ],
    )

    # THEN the extractor receives the options
    name, _, kwargs = recorded_calls[0]
    assert exit_code == 0
    assert name == "extract_videos"
    assert kwargs["library_dir"] == Path("videos")
    assert kwargs["include"] == ["2026-*", "2025-*"]
    assert kwargs["exclude"] == ["*old*"]
    assert kwargs["make_previews"] is True
    assert kwargs["job_count"] == 8


def test_main_runs_reclean(monkeypatch, config, recorded_calls):
    # GIVEN `extract --reclean`

    # WHEN running the tool
    run_main(monkeypatch, config, ["extract", "--reclean", "--metadata-dir", "meta"])

    # THEN only recleaning runs
    assert [call[0] for call in recorded_calls] == ["reclean_tracks"]
    assert recorded_calls[0][2]["metadata_dir"] == Path("meta")


def test_main_runs_reclean_with_patterns(monkeypatch, config, recorded_calls):
    # GIVEN `extract --reclean` with include and exclude patterns

    # WHEN running the tool
    run_main(
        monkeypatch,
        config,
        ["extract", "--reclean", "--include", "2019-*", "--exclude", "*old*"],
    )

    # THEN the patterns are passed to recleaning
    _, _, kwargs = recorded_calls[0]
    assert kwargs["include"] == ["2019-*"]
    assert kwargs["exclude"] == ["*old*"]


def test_main_runs_status(monkeypatch, config, recorded_calls):
    # GIVEN `status` with a directory

    # WHEN running the tool
    exit_code = run_main(monkeypatch, config, ["status", "--library-dir", "videos"])

    # THEN the status is printed for that directory
    assert exit_code == 0
    assert recorded_calls[0][0] == "print_status"
    assert recorded_calls[0][2]["library_dir"] == Path("videos")


def test_main_runs_forget(monkeypatch, config, recorded_calls):
    # GIVEN `forget` with two names

    # WHEN running the tool
    run_main(monkeypatch, config, ["forget", "a.mp4", "b"])

    # THEN both names are forgotten
    assert recorded_calls[0][0] == "forget_trips"
    assert recorded_calls[0][2]["names"] == ["a.mp4", "b"]


def test_main_runs_doctor_with_fix(monkeypatch, config, recorded_calls):
    # GIVEN `doctor --fix`

    # WHEN running the tool
    run_main(monkeypatch, config, ["doctor", "--fix"])

    # THEN fixes are applied
    assert recorded_calls[0][0] == "run_doctor"
    assert recorded_calls[0][2]["apply_fixes"] is True


def test_main_runs_enrich_with_update_osm(monkeypatch, config, recorded_calls, library_dir):
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
    assert kwargs["metadata_dir"] == library_dir / config.metadata_dir


def test_main_runs_enrich_with_osm_file_and_force(monkeypatch, config, recorded_calls):
    # GIVEN `enrich` with a local OSM file and `--force`

    # WHEN running the tool
    run_main(monkeypatch, config, ["enrich", "--osm-file", "ukraine.osm.pbf", "--force"])

    # THEN the options are passed on
    kwargs = recorded_calls[0][2]
    assert kwargs["update_osm"] is False
    assert kwargs["osm_file"] == Path("ukraine.osm.pbf")
    assert kwargs["force"] is True


def test_main_runs_rename_suggest_all(monkeypatch, config, recorded_calls):
    # GIVEN `rename --suggest --all` with a directory

    # WHEN running the tool
    exit_code = run_main(monkeypatch, config, ["rename", "-d", "videos", "--suggest", "--all"])

    # THEN all trips are offered for renaming interactively
    name, _, kwargs = recorded_calls[0]
    assert exit_code == 0
    assert name == "rename_trips"
    assert kwargs["library_dir"] == Path("videos")
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


def test_main_runs_import_with_metadata_inside_library_dir(
    monkeypatch, config, recorded_calls, tmp_path
):
    # GIVEN `import` into a new library directory, with encode options
    library_dir = tmp_path / "Dashcam"

    # WHEN running the tool
    exit_code = run_main(
        monkeypatch,
        config,
        [
            "import",
            *["-d", str(library_dir)],
            *["--raw-video-dir", "sd"],
            *["--encode-job-count", "3"],
            *["--extract-job-count", "7"],
        ],
    )

    # THEN the directory is created and the metadata lives inside it
    name, _, kwargs = recorded_calls[0]
    assert exit_code == 0
    assert name == "import_trips"
    assert library_dir.is_dir()
    assert kwargs["library_dir"] == library_dir
    assert kwargs["metadata_dir"] == library_dir / config.metadata_dir
    assert kwargs["raw_video_dir"] == Path("sd")
    assert kwargs["encode_job_count"] == 3
    assert kwargs["extract_job_count"] == 7
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
            "--library-dir",
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
    assert kwargs["encode_job_count"] == config.encode_job_count
    assert kwargs["extract_job_count"] == config.extract_job_count


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


def test_main_runs_serve_with_defaults(monkeypatch, config, recorded_calls, library_dir):
    # GIVEN `serve` without options

    # WHEN running the tool
    exit_code = run_main(monkeypatch, config, ["serve"])

    # THEN the server listens on the local address and default port
    name, _, kwargs = recorded_calls[0]
    assert exit_code == 0
    assert name == "serve"
    assert kwargs["library_dir"] == library_dir
    assert kwargs["metadata_dir"] == library_dir / config.metadata_dir
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 8765)
    assert kwargs["allow_network_rename"] is False


def test_main_runs_serve_with_options(monkeypatch, config, recorded_calls):
    # GIVEN `serve` with a directory, address, port, and network renaming

    # WHEN running the tool
    run_main(
        monkeypatch,
        config,
        ["serve", "-d", "videos", "--host", "0.0.0.0", "--port", "9000", "--allow-rename"],
    )

    # THEN the options are passed on
    kwargs = recorded_calls[0][2]
    assert kwargs["library_dir"] == Path("videos")
    assert (kwargs["host"], kwargs["port"]) == ("0.0.0.0", 9000)
    assert kwargs["allow_network_rename"] is True


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


def test_main_runs_config_without_file(monkeypatch, config, tmp_path, capsys):
    # GIVEN a config path that does not exist
    config_path = tmp_path / "missing.toml"
    monkeypatch.setenv("DASHCAM_CONFIG", str(config_path))

    # WHEN running `dashcam config`
    exit_code = run_main(monkeypatch, config, ["config"])

    # THEN the path and the default settings are printed
    output = capsys.readouterr().out
    assert exit_code == 0
    assert str(config_path) in output
    assert "(not found)" in output
    assert "library_dir = (not set)" in output
    assert 'metadata_dir = ".metadata"' in output
    assert "from the config file" not in output


def test_main_runs_config_with_file(monkeypatch, config, library_dir, tmp_path, capsys):
    # GIVEN a config file overriding the library directory
    config_path = tmp_path / "config.toml"
    config_path.write_text(f'library_dir = "{library_dir.as_posix()}"\n')
    monkeypatch.setenv("DASHCAM_CONFIG", str(config_path))

    # WHEN running `dashcam config`
    run_main(monkeypatch, config, ["config"])

    # THEN the overridden setting is marked
    output_lines = capsys.readouterr().out.splitlines()
    library_dir_line = next(line for line in output_lines if "library_dir" in line)
    job_count_line = next(line for line in output_lines if "encode_job_count" in line)
    assert json.dumps(str(library_dir)) in library_dir_line
    assert "from the config file" in library_dir_line
    assert "from the config file" not in job_count_line
