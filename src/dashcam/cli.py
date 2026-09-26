r"""
Command-line entry point of the `dashcam` tool.

Usage examples:
---------------

Encode all raw video files in the default location (see `dashcam.config`), group them into
trips, and save each trip as a separate output video file in the current directory:
> dashcam encode trips

Encode all raw video files in the specified location, group them into trips
where trips should be at least 8 hours apart, encode 3 trips in parallel:
> dashcam encode trips --raw-video-dir "/media/me/DASHCAM/DCIM/Movie" \
    --min-trip-gap-hours 8 --job-count 3

Encode raw video files labeled from #15 to #319 and save them as a single output video file
named "Road Trip.mp4" in the specified directory:
> dashcam encode range 15 319 --output-name "Road Trip" --output-dir ~/Videos/Dashcam

Download the OpenStreetMap data, then match all tracks in the default metadata directory
(see `dashcam.config`) to streets:
> dashcam enrich --update-osm

Suggest street-based names for the trips in ~/Videos/Dashcam still named "Trip HH-MM", and
rename them after confirmation:
> dashcam rename ~/Videos/Dashcam --suggest

Import new trips from the SD card into ~/Videos/Dashcam: encode, extract, match streets, and
suggest names:
> dashcam import --out ~/Videos/Dashcam

Browse the trips in ~/Videos/Dashcam on a map at http://127.0.0.1:8765/:
> dashcam serve ~/Videos/Dashcam
"""

import argparse
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
from dashcam.config import Config
from dashcam.config import load_config
from dashcam.system import configure_logging

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


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
        subparser.add_argument(
            "--output-dir",
            metavar="PATH",
            help="Directory to save the encoded videos to (default: current directory).",
            type=Path,
            required=False,
            default=Path.cwd(),
        )


def add_metadata_dir_argument(parser: argparse.ArgumentParser, config: Config) -> None:
    """Add the `--metadata-dir` option."""
    parser.add_argument(
        "--metadata-dir",
        metavar="PATH",
        help=f"Directory for tracks and the trip index (default: '{config.metadata_dir}').",
        type=Path,
        default=Path(config.metadata_dir),
    )


def add_video_dir_argument(parser: argparse.ArgumentParser) -> None:
    """Add the optional video directory argument."""
    parser.add_argument(
        "video_dir",
        metavar="DIR",
        help="Directory with trip videos (default: current directory).",
        type=Path,
        nargs="?",
        default=Path.cwd(),
    )


def add_extract_arguments(extract_parser: argparse.ArgumentParser, config: Config) -> None:
    """Add the arguments of the `extract` command."""
    add_video_dir_argument(extract_parser)
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
    add_video_dir_argument(rename_parser)
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
    import_parser.add_argument(
        "--out",
        metavar="DIR",
        help="Trip video directory to import into (default: current directory).",
        type=Path,
        default=Path.cwd(),
    )
    import_parser.add_argument(
        "--metadata-dir",
        metavar="PATH",
        help=(
            f"Directory for tracks and the trip index (default: '{config.metadata_dir}', "
            f"relative to the --out directory)."
        ),
        type=Path,
    )
    import_parser.add_argument(
        "--no-rename",
        action="store_true",
        help="Do not suggest new names for the imported trips.",
    )

    status_parser = subparsers.add_parser(
        name="status",
        help="List trips, unprocessed videos, and unreachable tracks.",
    )
    add_video_dir_argument(status_parser)
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
    add_metadata_dir_argument(forget_parser, config)

    doctor_parser = subparsers.add_parser(
        name="doctor",
        help="Check the metadata for problems and optionally fix the safe ones.",
    )
    add_video_dir_argument(doctor_parser)
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
    add_video_dir_argument(serve_parser)
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

    parsed_args = parser.parse_args(args)
    return parsed_args


def run_encode_command(parsed_args: argparse.Namespace, config: Config) -> int:
    """Entry point for the `encode` command."""
    check_readability = not parsed_args.skip_raw_video_validation
    parsed_args.output_dir.mkdir(parents=True, exist_ok=True)

    if parsed_args.encode_mode == "trips":
        return encode.encode_trips(
            raw_video_dir=parsed_args.raw_video_dir,
            output_dir=parsed_args.output_dir,
            config=config,
            min_trip_gap_hours=parsed_args.min_trip_gap_hours,
            job_count=parsed_args.job_count,
            dry_run=parsed_args.dry_run,
            check_readability=check_readability,
        )

    return encode.encode_range(
        raw_video_dir=parsed_args.raw_video_dir,
        output_dir=parsed_args.output_dir,
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
        video_dir=parsed_args.video_dir,
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
    metadata_dir = parsed_args.metadata_dir or parsed_args.out / config.metadata_dir
    parsed_args.out.mkdir(parents=True, exist_ok=True)
    return importer.import_trips(
        raw_video_dir=parsed_args.raw_video_dir,
        output_dir=parsed_args.out,
        metadata_dir=metadata_dir,
        config=config,
        min_trip_gap_hours=parsed_args.min_trip_gap_hours,
        job_count=parsed_args.job_count,
        dry_run=parsed_args.dry_run,
        check_readability=not parsed_args.skip_raw_video_validation,
        suggest_names=not parsed_args.no_rename,
    )


def main() -> None:
    configure_logging()
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
                video_dir=parsed_args.video_dir,
                metadata_dir=parsed_args.metadata_dir,
                config=config,
                include_all=parsed_args.include_all,
                interactive=parsed_args.suggest,
                assume_yes=parsed_args.yes,
            )
        elif parsed_args.command == "import":
            failed_count = run_import_command(parsed_args, config)
        elif parsed_args.command == "status":
            maintenance.print_status(
                parsed_args.video_dir, parsed_args.metadata_dir, config.max_interpolation_gap_s
            )
        elif parsed_args.command == "forget":
            failed_count = maintenance.forget_trips(
                parsed_args.names, parsed_args.metadata_dir, config.max_interpolation_gap_s
            )
        elif parsed_args.command == "doctor":
            failed_count = maintenance.run_doctor(
                video_dir=parsed_args.video_dir,
                metadata_dir=parsed_args.metadata_dir,
                max_interpolation_gap_s=config.max_interpolation_gap_s,
                apply_fixes=parsed_args.fix,
            )
        elif parsed_args.command == "serve":
            serve.serve(
                video_dir=parsed_args.video_dir,
                metadata_dir=parsed_args.metadata_dir,
                max_interpolation_gap_s=config.max_interpolation_gap_s,
                host=parsed_args.host,
                port=parsed_args.port,
            )
    except (OSError, RuntimeError) as exc:
        LOGGER.error(exc)
        sys.exit(1)

    sys.exit(1 if failed_count else 0)


if __name__ == "__main__":
    main()
