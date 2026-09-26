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
"""

import argparse
import logging
import sys
from pathlib import Path

from dashcam import encode
from dashcam.config import Config
from dashcam.config import load_config
from dashcam.system import configure_logging

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


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
    parser_trips_cmd.add_argument(
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
    parser_trips_cmd.add_argument(
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
        subparser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Print out the discovered trips or segments, but do not "
                "run ffmpeg for actual encoding."
            ),
        )
        subparser.add_argument(
            "--raw-video-dir",
            metavar="PATH",
            help=(
                f"Path to the raw video directory on the SD card "
                f"(default: '{config.raw_video_dir}')."
            ),
            type=Path,
            required=False,
            default=Path(config.raw_video_dir),
        )
        subparser.add_argument(
            "--output-dir",
            metavar="PATH",
            help="Directory to save the encoded videos to (default: current directory).",
            type=Path,
            required=False,
            default=Path.cwd(),
        )
        subparser.add_argument(
            "--skip-raw-video-validation",
            action="store_true",
            help=(
                "Do not check the raw videos for readability. This is faster, "
                "but may result in abruptly cut encoded videos in case of "
                "corrupted files."
            ),
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
    except (OSError, RuntimeError) as exc:
        LOGGER.error(exc)
        sys.exit(1)

    sys.exit(1 if failed_count else 0)


if __name__ == "__main__":
    main()
