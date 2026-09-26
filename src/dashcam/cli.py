r"""
Command-line entry point of the `dashcam` tool.

Usage examples:
---------------

The raw video directory, the library directory, and the metadata directory default to the
values in the config file (see `dashcam.config` and `dashcam config`).

Encode all raw video files in the default raw video directory, group them into trips, and save
each trip as a separate video file in the default library directory:
> dashcam encode trips

Encode all raw video files in the specified location, group them into trips
where trips should be at least 8 hours apart, encode 3 trips in parallel:
> dashcam encode trips --raw-video-dir "/media/me/DASHCAM/DCIM/Movie" \
    --min-trip-gap-hours 8 --job-count 3

Encode raw video files labeled from #15 to #319 and save them as a single video file named
"Road Trip.mp4" in the specified library directory:
> dashcam encode range 15 319 --output-name "Road Trip" --library-dir ~/Videos/Dashcam

Download the OpenStreetMap data, then match all tracks in the library to streets:
> dashcam enrich --update-osm

Suggest street-based names for the trips in ~/Videos/Dashcam still named "Trip HH-MM", and
rename them after confirmation:
> dashcam rename -d ~/Videos/Dashcam --suggest

Import new trips from the SD card into the library: encode, extract, match streets, and
suggest names:
> dashcam import

Browse the trips in the library on a map at http://127.0.0.1:8765/:
> dashcam serve

Show the config file path and the effective settings:
> dashcam config
"""

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

from dashcam import encode
from dashcam import enrich
from dashcam import extract
from dashcam import importer
from dashcam import maintenance
from dashcam import rename
from dashcam import serve
from dashcam import terminal
from dashcam.config import Config
from dashcam.config import get_config_path
from dashcam.config import get_platform_defaults
from dashcam.config import load_config
from dashcam.config import read_config_file

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)

# Commands that work without a library directory when the metadata directory is known.
METADATA_ONLY_COMMANDS = {"enrich", "forget"}


def add_trip_grouping_arguments(parser: argparse.ArgumentParser, config: Config) -> None:
    """Add the options that group raw videos into trips and set the parallelism."""
    parser.add_argument(
        "--min-trip-gap-hours",
        metavar="TG",
        help=(
            "The minimum time difference (in hours) between consecutive files "
            "for them to be considered separate trips."
        ),
        type=float,
        required=False,
        default=config.min_trip_gap_hours,
    )
    parser.add_argument(
        "--job-count",
        metavar="JC",
        help=(
            "The maximum number of trips allowed to be encoded in parallel. "
            "Adjust this number to change system resource utilization."
        ),
        type=int,
        required=False,
        default=config.job_count,
    )


def add_raw_video_arguments(parser: argparse.ArgumentParser, config: Config) -> None:
    """Add the options that locate and check the raw videos on the SD card."""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print out the discovered trips or segments, but do not run ffmpeg for actual encoding."
        ),
    )
    parser.add_argument(
        "--raw-video-dir",
        metavar="PATH",
        help=(
            f"Path to the raw video directory on the SD card (default: '{config.raw_video_dir}')."
        ),
        type=Path,
        required=False,
        default=Path(config.raw_video_dir),
    )
    parser.add_argument(
        "--skip-raw-video-validation",
        action="store_true",
        help=(
            "Do not check the raw videos for readability. This is faster, "
            "but may result in abruptly cut encoded videos in case of "
            "corrupted files."
        ),
    )


def add_encode_subparsers(encode_parser: argparse.ArgumentParser, config: Config) -> None:
    """Add the `trips` and `range` subcommands to the `encode` command parser."""
    subparsers = encode_parser.add_subparsers(dest="encode_mode", metavar="MODE", required=True)

    parser_trips_cmd = subparsers.add_parser(
        name="trips",
        help=(
            "Encode all videos in the input directory, organizing them into "
            "trips according to recording start times."
        ),
    )
    add_trip_grouping_arguments(parser_trips_cmd, config)

    parser_range_cmd = subparsers.add_parser(
        name="range",
        help=(
            "Concatenate input videos in the specified range "
            "and encode them into a single output video."
        ),
    )
    parser_range_cmd.add_argument(
        "start_index",
        metavar="START-INDEX",
        help=(
            "Index of the first video (inclusive). "
            "Example: for 20260925111707_000007.MP4, specify 7."
        ),
        type=int,
    )
    parser_range_cmd.add_argument(
        "end_index",
        metavar="END-INDEX",
        help=(
            "Index of the last video (inclusive). "
            "Example: for 20260925124512_000094.MP4, specify 94."
        ),
        type=int,
    )
    parser_range_cmd.add_argument(
        "--output-name",
        metavar="NAME",
        help=(
            "Name of the output video (without extension). "
            "If omitted, a name is generated from the start time of the first video."
        ),
        type=str,
        required=False,
    )

    for subparser in [parser_trips_cmd, parser_range_cmd]:
        add_raw_video_arguments(subparser, config)
        add_library_dir_argument(subparser, config)
        add_metadata_dir_argument(subparser, config)


def add_metadata_dir_argument(parser: argparse.ArgumentParser, config: Config) -> None:
    """Add the `--metadata-dir` option."""
    if Path(config.metadata_dir).is_absolute():
        default_text = f"'{config.metadata_dir}'"
    else:
        default_text = f"'{config.metadata_dir}' inside the library directory"
    parser.add_argument(
        "--metadata-dir",
        metavar="PATH",
        help=f"Metadata directory for tracks and the trip index (default: {default_text}).",
        type=Path,
    )


def add_library_dir_argument(parser: argparse.ArgumentParser, config: Config) -> None:
    """Add the `--library-dir` option."""
    if config.library_dir is None:
        default_text = "set `library_dir` in the config to omit this option"
    else:
        default_text = f"default: '{config.library_dir}'"
    parser.add_argument(
        "-d",
        "--library-dir",
        metavar="PATH",
        help=f"Library directory with the trip videos ({default_text}).",
        type=Path,
    )


def add_extract_arguments(extract_parser: argparse.ArgumentParser, config: Config) -> None:
    """Add the arguments of the `extract` command."""
    add_library_dir_argument(extract_parser, config)
    add_metadata_dir_argument(extract_parser, config)
    extract_parser.add_argument(
        "--include",
        metavar="GLOB",
        help="Only process videos whose filename matches this pattern (repeatable).",
        action="append",
        default=[],
    )
    extract_parser.add_argument(
        "--exclude",
        metavar="GLOB",
        help="Skip videos whose filename matches this pattern (repeatable).",
        action="append",
        default=[],
    )
    extract_parser.add_argument(
        "--only",
        metavar="VIDEO",
        help="Re-extract only this video, even if its track is up to date (repeatable).",
        action="append",
        default=[],
    )
    extract_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract all videos, including those without an overlay.",
    )
    extract_parser.add_argument(
        "--reclean",
        action="store_true",
        help="Re-run GPS cleaning on the stored readings of all tracks without decoding videos.",
    )
    extract_parser.add_argument(
        "--previews",
        action="store_true",
        help="Also make low-resolution H.264 previews for browsers that cannot play HEVC.",
    )


def parse_command_line_args(args: list[str], config: Config) -> argparse.Namespace:
    """Parse the arguments passed via the command line."""
    parser = argparse.ArgumentParser(
        prog="dashcam",
        description=(
            "Dashcam video toolkit: merge SD card segments, extract GPS tracks, "
            "and visualize routes."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    encode_parser = subparsers.add_parser(
        name="encode",
        help="Concatenate and encode dashcam videos stored on an SD card.",
    )
    add_encode_subparsers(encode_parser, config)

    extract_parser = subparsers.add_parser(
        name="extract",
        help="Extract GPS tracks from the overlay of trip videos.",
    )
    add_extract_arguments(extract_parser, config)

    enrich_parser = subparsers.add_parser(
        name="enrich",
        help="Match tracks to named roads and find where trips start and end.",
    )
    add_library_dir_argument(enrich_parser, config)
    add_metadata_dir_argument(enrich_parser, config)
    enrich_parser.add_argument(
        "--update-osm",
        action="store_true",
        help=(
            f"Download the OpenStreetMap extract ('{config.osm_extract_url}'), keep only named "
            f"roads and localities, and delete the download."
        ),
    )
    enrich_parser.add_argument(
        "--osm-file",
        metavar="PATH",
        help="Build the OSM data from this local .osm.pbf file instead of downloading it.",
        type=Path,
    )
    enrich_parser.add_argument(
        "--force",
        action="store_true",
        help="Enrich all tracks, including those whose street lists are up to date.",
    )

    rename_parser = subparsers.add_parser(
        name="rename",
        help="Name trips after the streets they follow.",
    )
    add_library_dir_argument(rename_parser, config)
    add_metadata_dir_argument(rename_parser, config)
    rename_parser.add_argument(
        "--suggest",
        action="store_true",
        help=(
            "Show the suggested names and ask whether to apply them, skip them, or edit them "
            "one by one. Without this option, the suggestions are only printed."
        ),
    )
    rename_parser.add_argument(
        "--all",
        action="store_true",
        dest="include_all",
        help="Suggest names for all trips, not only those still named 'Trip HH-MM'.",
    )
    rename_parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply the suggested names without asking.",
    )

    import_parser = subparsers.add_parser(
        name="import",
        help="Encode new trips from the SD card, extract and enrich them, and suggest names.",
    )
    add_trip_grouping_arguments(import_parser, config)
    add_raw_video_arguments(import_parser, config)
    add_library_dir_argument(import_parser, config)
    add_metadata_dir_argument(import_parser, config)
    import_parser.add_argument(
        "--no-rename",
        action="store_true",
        help="Do not suggest new names for the imported trips.",
    )

    status_parser = subparsers.add_parser(
        name="status",
        help="List trips, unprocessed videos, and unreachable tracks.",
    )
    add_library_dir_argument(status_parser, config)
    add_metadata_dir_argument(status_parser, config)

    forget_parser = subparsers.add_parser(
        name="forget",
        help="Move the tracks (and previews) of the given videos to the trash.",
    )
    forget_parser.add_argument(
        "names",
        metavar="VIDEO",
        help="Video filename or name without extension.",
        nargs="+",
    )
    add_library_dir_argument(forget_parser, config)
    add_metadata_dir_argument(forget_parser, config)

    doctor_parser = subparsers.add_parser(
        name="doctor",
        help="Check the metadata for problems and optionally fix the safe ones.",
    )
    add_library_dir_argument(doctor_parser, config)
    add_metadata_dir_argument(doctor_parser, config)
    doctor_parser.add_argument(
        "--fix",
        action="store_true",
        help="Apply safe fixes: reconnect renamed videos, rebuild the index, delete leftovers.",
    )

    serve_parser = subparsers.add_parser(
        name="serve",
        help="Browse the trips on a map with synchronized video playback.",
    )
    add_library_dir_argument(serve_parser, config)
    add_metadata_dir_argument(serve_parser, config)
    serve_parser.add_argument(
        "--host",
        metavar="ADDRESS",
        help=(
            f"Address to listen on (default: '{serve.DEFAULT_HOST}', reachable from this "
            f"machine only). Use '0.0.0.0' to allow other devices on the network."
        ),
        default=serve.DEFAULT_HOST,
    )
    serve_parser.add_argument(
        "--port",
        metavar="PORT",
        help=f"Port to listen on (default: {serve.DEFAULT_PORT}).",
        type=int,
        default=serve.DEFAULT_PORT,
    )

    subparsers.add_parser(
        name="config",
        help="Show the config file path and the effective settings.",
    )

    parsed_args = parser.parse_args(args)
    try:
        resolve_directories(parsed_args, config)
    except MissingLibraryDirError as exc:
        parser.error(str(exc))
    return parsed_args


class MissingLibraryDirError(Exception):
    """Neither the command line nor the config gives the library directory."""


def get_library_dir(parsed_args: argparse.Namespace, config: Config) -> Path:
    """
    Return the library directory from the command line, falling back to the config.

    Raises
    ------
    MissingLibraryDirError
        If neither sets it.
    """
    if parsed_args.library_dir is not None:
        return parsed_args.library_dir
    if config.library_dir is not None:
        return Path(config.library_dir)
    raise MissingLibraryDirError(
        f"No library directory: pass `--library-dir` or set `library_dir` in '{get_config_path()}'"
    )


def get_metadata_dir(parsed_args: argparse.Namespace, config: Config) -> Path:
    """
    Return the metadata directory from the command line, falling back to the config.

    Raises
    ------
    MissingLibraryDirError
        If the configured metadata directory is relative and there is no library directory.
    """
    if parsed_args.metadata_dir is not None:
        return parsed_args.metadata_dir
    configured_metadata_dir = Path(config.metadata_dir)
    if configured_metadata_dir.is_absolute():
        return configured_metadata_dir
    return get_library_dir(parsed_args, config) / configured_metadata_dir


def resolve_directories(parsed_args: argparse.Namespace, config: Config) -> None:
    """
    Replace the library and metadata directory options with the paths the command uses.

    A command that does not need the library directory keeps the option as given.

    Raises
    ------
    MissingLibraryDirError
        If a command needs the library directory and it is not set.
    """
    is_metadata_only = parsed_args.command in METADATA_ONLY_COMMANDS or (
        parsed_args.command == "extract" and parsed_args.reclean
    )
    if hasattr(parsed_args, "metadata_dir"):
        parsed_args.metadata_dir = get_metadata_dir(parsed_args, config)
    if hasattr(parsed_args, "library_dir") and not is_metadata_only:
        parsed_args.library_dir = get_library_dir(parsed_args, config)


def run_config_command(config_path: Path) -> None:
    """Print the config file path and the effective settings, marking the overridden ones."""
    overrides = read_config_file(config_path)
    config = dataclasses.replace(get_platform_defaults(), **overrides)
    file_state = "" if config_path.is_file() else " (not found)"
    terminal.print_line(("Config file: ", "bold"), f"'{config_path}'", (file_state, "dim"))
    for field in dataclasses.fields(Config):
        value = getattr(config, field.name)
        value_text = "(not set)" if value is None else json.dumps(value, ensure_ascii=False)
        source_note = ("  # from the config file", "green") if field.name in overrides else ""
        terminal.print_line(f"  {field.name} = {value_text}", source_note)


def run_encode_command(parsed_args: argparse.Namespace, config: Config) -> int:
    """Entry point for the `encode` command."""
    check_readability = not parsed_args.skip_raw_video_validation
    parsed_args.library_dir.mkdir(parents=True, exist_ok=True)

    if parsed_args.encode_mode == "trips":
        return encode.encode_trips(
            raw_video_dir=parsed_args.raw_video_dir,
            library_dir=parsed_args.library_dir,
            metadata_dir=parsed_args.metadata_dir,
            config=config,
            min_trip_gap_hours=parsed_args.min_trip_gap_hours,
            job_count=parsed_args.job_count,
            dry_run=parsed_args.dry_run,
            check_readability=check_readability,
        )

    return encode.encode_range(
        raw_video_dir=parsed_args.raw_video_dir,
        library_dir=parsed_args.library_dir,
        metadata_dir=parsed_args.metadata_dir,
        config=config,
        start_index=parsed_args.start_index,
        end_index=parsed_args.end_index,
        output_name=parsed_args.output_name,
        dry_run=parsed_args.dry_run,
        check_readability=check_readability,
    )


def run_extract_command(parsed_args: argparse.Namespace, config: Config) -> int:
    """Entry point for the `extract` command."""
    if parsed_args.reclean:
        return extract.reclean_tracks(parsed_args.metadata_dir, config)

    return extract.extract_videos(
        library_dir=parsed_args.library_dir,
        metadata_dir=parsed_args.metadata_dir,
        config=config,
        include=parsed_args.include,
        exclude=parsed_args.exclude,
        only=parsed_args.only,
        force=parsed_args.force,
        make_previews=parsed_args.previews,
    )


def run_import_command(parsed_args: argparse.Namespace, config: Config) -> int:
    """Entry point for the `import` command."""
    parsed_args.library_dir.mkdir(parents=True, exist_ok=True)
    return importer.import_trips(
        raw_video_dir=parsed_args.raw_video_dir,
        library_dir=parsed_args.library_dir,
        metadata_dir=parsed_args.metadata_dir,
        config=config,
        min_trip_gap_hours=parsed_args.min_trip_gap_hours,
        job_count=parsed_args.job_count,
        dry_run=parsed_args.dry_run,
        check_readability=not parsed_args.skip_raw_video_validation,
        suggest_names=not parsed_args.no_rename,
    )


def main() -> None:
    terminal.configure_logging()
    try:
        config = load_config()
    except (OSError, ValueError) as exc:
        LOGGER.error(f"Cannot load the config: {exc}")
        sys.exit(1)

    parsed_args = parse_command_line_args(sys.argv[1:], config)

    try:
        failed_count = 0
        if parsed_args.command == "encode":
            failed_count = run_encode_command(parsed_args, config)
        elif parsed_args.command == "extract":
            failed_count = run_extract_command(parsed_args, config)
        elif parsed_args.command == "enrich":
            failed_count = enrich.enrich_tracks(
                metadata_dir=parsed_args.metadata_dir,
                config=config,
                update_osm=parsed_args.update_osm,
                osm_file=parsed_args.osm_file,
                force=parsed_args.force,
            )
        elif parsed_args.command == "rename":
            failed_count = rename.rename_trips(
                library_dir=parsed_args.library_dir,
                metadata_dir=parsed_args.metadata_dir,
                config=config,
                include_all=parsed_args.include_all,
                interactive=parsed_args.suggest,
                assume_yes=parsed_args.yes,
            )
        elif parsed_args.command == "import":
            failed_count = run_import_command(parsed_args, config)
        elif parsed_args.command == "status":
            maintenance.print_status(parsed_args.library_dir, parsed_args.metadata_dir)
        elif parsed_args.command == "forget":
            failed_count = maintenance.forget_trips(parsed_args.names, parsed_args.metadata_dir)
        elif parsed_args.command == "doctor":
            failed_count = maintenance.run_doctor(
                library_dir=parsed_args.library_dir,
                metadata_dir=parsed_args.metadata_dir,
                apply_fixes=parsed_args.fix,
            )
        elif parsed_args.command == "config":
            run_config_command(get_config_path())
        elif parsed_args.command == "serve":
            serve.serve(
                library_dir=parsed_args.library_dir,
                metadata_dir=parsed_args.metadata_dir,
                host=parsed_args.host,
                port=parsed_args.port,
                car_model=config.car_model,
            )
    except (OSError, RuntimeError) as exc:
        LOGGER.error(exc)
        sys.exit(1)

    sys.exit(1 if failed_count else 0)


if __name__ == "__main__":
    main()
