"""
Import trips from the SD card in one go: `encode trips`, `extract`, `enrich`, and `rename`.

Each step is incremental, so importing again after an interruption only does what is left.
Enrichment and renaming are skipped with a warning when there is no OSM data yet.
"""

import logging
from pathlib import Path

from dashcam import encode
from dashcam import enrich
from dashcam import extract
from dashcam import osm
from dashcam import rename
from dashcam import terminal
from dashcam.config import Config

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


def import_trips(
    raw_video_dir: Path,
    library_dir: Path,
    metadata_dir: Path,
    config: Config,
    min_trip_gap_hours: float,
    encode_job_count: int,
    extract_job_count: int,
    dry_run: bool,
    check_readability: bool,
    suggest_names: bool,
) -> int:
    """
    Encode new trips from the SD card, extract and enrich their tracks, and suggest names.

    Returns
    -------
    int
        The number of failed trips over all steps.
    """
    LOGGER.info("Step 1/4: encoding trips", extra=terminal.HEADING)
    failed_count = encode.encode_trips(
        raw_video_dir=raw_video_dir,
        library_dir=library_dir,
        metadata_dir=metadata_dir,
        config=config,
        min_trip_gap_hours=min_trip_gap_hours,
        job_count=encode_job_count,
        dry_run=dry_run,
        check_readability=check_readability,
    )
    if dry_run:
        return failed_count

    LOGGER.info("Step 2/4: extracting GPS tracks", extra=terminal.HEADING)
    failed_count += extract.extract_videos(
        library_dir=library_dir,
        metadata_dir=metadata_dir,
        config=config,
        include=[],
        exclude=[],
        only=[],
        force=False,
        make_previews=False,
        job_count=extract_job_count,
    )

    if osm.read_database_version(metadata_dir) is None:
        LOGGER.warning(
            "No OSM data: skipping street matching and renaming "
            "(run `dashcam enrich --update-osm`, then `dashcam rename --suggest`)"
        )
        return failed_count

    LOGGER.info("Step 3/4: matching streets", extra=terminal.HEADING)
    failed_count += enrich.enrich_tracks(
        metadata_dir=metadata_dir,
        config=config,
        update_osm=False,
        osm_file=None,
        force=False,
    )

    if not suggest_names:
        return failed_count
    LOGGER.info("Step 4/4: naming trips", extra=terminal.HEADING)
    failed_count += rename.rename_trips(
        library_dir=library_dir,
        metadata_dir=metadata_dir,
        config=config,
        include_all=False,
        interactive=True,
        assume_yes=False,
    )
    return failed_count
