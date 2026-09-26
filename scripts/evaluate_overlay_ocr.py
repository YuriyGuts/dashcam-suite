r"""
Evaluate overlay OCR on sample videos using consistency checks instead of hand labels.

Every video is sampled at 2 fps. A reading
is flagged as a likely OCR error when it disagrees with both of its neighbors while the
neighbors agree with each other:

* Clock: `camera time - video offset` should stay constant between neighboring frames.
* GPS: latitude and longitude should change by less than `MAX_COORDINATE_STEP` per frame.
* Presence: the GPS text should not flicker on or off for a single frame.
* Speed: the displayed speed should match the speed implied by coordinates one second apart.
  Spoofed GPS and occasional GPS update lag also trigger this check.

Readings with text below the reliability threshold are listed as well.

Flagged strips are saved to `.scratch/ocr-review/` for visual review.

Usage:
> uv run python scripts/evaluate_overlay_ocr.py [VIDEO ...]
"""

import datetime
import logging
import math
import sys
import time
from pathlib import Path

import cv2

from dashcam import overlay
from dashcam.config import load_config
from dashcam.video import iter_overlay_strips
from dashcam.video import probe_video

# Sample videos to evaluate when none are given.
SAMPLE_VIDEO_DIR = Path(__file__).parents[1] / "video"

# Where to save the strips of flagged readings.
REVIEW_DIR = Path(__file__).parents[1] / ".scratch" / "ocr-review"

# Frames per second to sample.
SAMPLE_FPS = 2

# Largest plausible change of latitude or longitude between neighboring frames (degrees).
MAX_COORDINATE_STEP = 0.01

# Largest plausible mismatch (in seconds) between clock and video offset changes.
MAX_CLOCK_DRIFT_S = 1.5

# Largest plausible difference between displayed and implied speed (km/h).
MAX_SPEED_MISMATCH_KMH = 25

# How many speed mismatches to print per video.
MAX_REPORTED_MISMATCHES = 15

EARTH_RADIUS_M = 6_371_000

# pylint: disable=logging-fstring-interpolation
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)8s | %(message)s")
LOGGER = logging.getLogger(__name__)


def parse_clock(right_text: str) -> datetime.datetime | None:
    """Parse the camera clock text, or return None if it is not a valid date."""
    try:
        return datetime.datetime.strptime(right_text, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def clock_offset(offset_s: float, reading: overlay.OverlayReading) -> float | None:
    """Return `camera time - video offset` in seconds, or None if the clock is unreadable."""
    clock = parse_clock(reading.right_text)
    if clock is None:
        return None
    return clock.timestamp() - offset_s


def is_isolated_outlier(previous_value, value, next_value, tolerance) -> bool:
    """Check whether a value disagrees with both neighbors while they agree with each other."""
    if previous_value is None or value is None or next_value is None:
        return False
    neighbors_agree = abs(previous_value - next_value) <= tolerance
    differs_from_previous = abs(value - previous_value) > tolerance
    differs_from_next = abs(value - next_value) > tolerance
    return neighbors_agree and differs_from_previous and differs_from_next


def find_flagged_readings(
    readings: list[tuple[float, overlay.OverlayReading]],
) -> list[tuple[int, str]]:
    """
    Find readings that look like OCR errors.

    Returns
    -------
    list[tuple[int, str]]
        Indexes of flagged readings with the reason.
    """
    flagged = []
    for index in range(1, len(readings) - 1):
        (previous_offset, previous), (offset, current), (next_offset, following) = readings[
            index - 1 : index + 2
        ]

        if not current.right_text:
            flagged.append((index, "clock unreadable"))
        elif is_isolated_outlier(
            clock_offset(previous_offset, previous),
            clock_offset(offset, current),
            clock_offset(next_offset, following),
            MAX_CLOCK_DRIFT_S,
        ):
            flagged.append((index, "clock outlier"))

        presence = [bool(reading.left_text) for reading in (previous, current, following)]
        if presence[0] == presence[2] != presence[1]:
            flagged.append((index, "GPS presence flicker"))
            continue

        gps_values = [
            overlay.parse_gps_text(reading.left_text) for reading in (previous, current, following)
        ]
        if current.left_text and gps_values[1] is None:
            flagged.append((index, "GPS text unparseable"))
            continue
        if None in gps_values:
            continue
        for attribute in ("lat", "lon"):
            previous_value, value, next_value = [
                getattr(gps_value, attribute) for gps_value in gps_values
            ]
            if is_isolated_outlier(previous_value, value, next_value, MAX_COORDINATE_STEP):
                flagged.append((index, f"{attribute} outlier"))

    return flagged


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two points in meters."""
    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    dlat = lat2_rad - lat1_rad
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def find_speed_mismatches(
    readings: list[tuple[float, overlay.OverlayReading]],
) -> list[tuple[float, float, int]]:
    """
    Compare the displayed speed with the speed implied by coordinates one second apart.

    Only readings whose clocks differ by exactly one second are compared, so each pair spans
    one GPS update.

    Returns
    -------
    list[tuple[float, float, int]]
        (video offset, implied speed, displayed speed) for pairs that disagree.
    """
    samples = []
    for offset_s, reading in readings:
        gps_value = overlay.parse_gps_text(reading.left_text)
        clock = parse_clock(reading.right_text)
        if gps_value is not None and clock is not None:
            samples.append((offset_s, clock, gps_value))

    # Keep the first reading of every clock second.
    first_by_clock = {}
    for sample in samples:
        first_by_clock.setdefault(sample[1], sample)

    mismatches = []
    for clock, (offset_s, _, gps_value) in first_by_clock.items():
        following = first_by_clock.get(clock + datetime.timedelta(seconds=1))
        if following is None:
            continue
        next_gps_value = following[2]
        implied_kmh = (
            haversine_m(gps_value.lat, gps_value.lon, next_gps_value.lat, next_gps_value.lon) * 3.6
        )
        displayed_kmh = (gps_value.speed_kmh + next_gps_value.speed_kmh) / 2
        if abs(implied_kmh - displayed_kmh) > MAX_SPEED_MISMATCH_KMH:
            mismatches.append((offset_s, implied_kmh, gps_value.speed_kmh))
    return mismatches


def evaluate_video(path: Path) -> None:
    """Read all sampled frames of a video with the OCR and report flagged readings."""
    config = load_config()
    video_info = probe_video(path, config.ffprobe_executable)
    templates = overlay.load_glyph_templates()
    layout = overlay.get_nominal_layout()

    strips = []
    readings: list[tuple[float, overlay.OverlayReading]] = []
    started_at = time.monotonic()
    for offset_s, strip in iter_overlay_strips(
        path,
        width=video_info.width,
        sample_fps=SAMPLE_FPS,
        ffmpeg_executable=config.ffmpeg_executable,
        hwaccel_options=config.hwaccel_options,
    ):
        strips.append(strip)
        readings.append((offset_s, overlay.read_overlay(strip, layout, templates)))
    elapsed_s = time.monotonic() - started_at
    LOGGER.info(f"{path.name}: {len(strips)} frames in {elapsed_s:.0f} s")

    flagged = find_flagged_readings(readings)
    unreliable = [
        (index, "unreliable text")
        for index, (_, reading) in enumerate(readings)
        if (reading.left_text and not reading.is_left_text_reliable)
        or not reading.is_right_text_reliable
    ]
    gps_count = sum(reading.is_left_text_reliable for _, reading in readings)
    LOGGER.info(
        f"  {len(flagged)} flagged, {len(unreliable)} unreliable, "
        f"{gps_count}/{len(readings)} with reliable GPS text"
    )

    speed_mismatches = find_speed_mismatches(readings)
    LOGGER.info(f"  {len(speed_mismatches)} speed mismatches")
    for offset_s, implied_kmh, displayed_kmh in speed_mismatches[:MAX_REPORTED_MISMATCHES]:
        LOGGER.info(
            f"    {offset_s:7.1f}s implied {implied_kmh:.0f} km/h, displayed {displayed_kmh}"
        )

    for index, reason in flagged + unreliable:
        offset_s, reading = readings[index]
        LOGGER.info(
            f"    {offset_s:7.1f}s {reason:<22} {reading.raw!r} "
            f"(scores {reading.left_min_score:.2f}/{reading.right_min_score:.2f})"
        )
        review_path = REVIEW_DIR / f"{path.stem[:10]}_{offset_s:07.1f}.png"
        cv2.imwrite(str(review_path), strips[index])


def main() -> None:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    video_paths = [Path(arg) for arg in sys.argv[1:]] or sorted(SAMPLE_VIDEO_DIR.glob("*.mp4"))
    for path in video_paths:
        evaluate_video(path)


if __name__ == "__main__":
    main()
