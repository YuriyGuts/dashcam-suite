import json
from pathlib import Path

import numpy as np
import pytest

from dashcam import overlay

OVERLAY_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "overlay"
OVERLAY_LABELS = json.loads((OVERLAY_FIXTURE_DIR / "labels.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("filename", sorted(OVERLAY_LABELS))
def test_read_overlay_with_labeled_strip(filename):
    # GIVEN a labeled overlay strip (day, night, rain, snow, glare, no fix, spoofed)
    strip = overlay.read_strip_image(OVERLAY_FIXTURE_DIR / filename)
    label = OVERLAY_LABELS[filename]

    # WHEN reading it
    reading = overlay.read_overlay(strip, overlay.get_nominal_layout())

    # THEN both fields match the label and are reliable
    assert reading.left_text == label["left"]
    assert reading.right_text == label["right"]
    assert reading.is_right_text_reliable
    assert reading.is_left_text_reliable == bool(label["left"])


def test_read_overlay_with_blank_strip():
    # GIVEN a strip without any text
    strip = np.full((overlay.STRIP_HEIGHT, 2560), 90, dtype=np.uint8)

    # WHEN reading it
    reading = overlay.read_overlay(strip, overlay.get_nominal_layout())

    # THEN both fields are blank
    assert reading.left_text == ""
    assert reading.right_text == ""
    assert not reading.is_left_text_reliable
    assert not reading.is_right_text_reliable


def test_read_overlay_with_narrow_strip():
    # GIVEN a strip narrower than the overlay layout (e.g. a 1080p video)
    strip = np.full((overlay.STRIP_HEIGHT, 1920), 90, dtype=np.uint8)

    # WHEN reading it
    reading = overlay.read_overlay(strip, overlay.get_nominal_layout())

    # THEN it does not fail and finds no clock
    assert not reading.is_right_text_reliable


def test_overlay_reading_with_low_score_is_unreliable():
    # GIVEN a reading with text whose worst cell matched poorly
    reading = overlay.OverlayReading(
        left_text="9 KM/H N23.999585 E0.000000",
        right_text="2026/01/08 12:15:58",
        left_min_score=0.43,
        right_min_score=0.98,
    )

    # WHEN checking reliability
    # THEN the left field is unreliable and the right one is reliable
    assert not reading.is_left_text_reliable
    assert reading.is_right_text_reliable


@pytest.mark.parametrize(
    ("left_text", "expected"),
    [
        ("46 KM/H N49.810205 E24.028992", overlay.GpsReading(46, 49.810205, 24.028992)),
        ("203 KM/H S12.034249 W77.042741", overlay.GpsReading(203, -12.034249, -77.042741)),
        ("0 KM/H N1.000000 E123.456789", overlay.GpsReading(0, 1.0, 123.456789)),
        ("46 KM/H N49.8102 E24.028992", None),
        ("", None),
    ],
)
def test_parse_gps_text(left_text, expected):
    # GIVEN a left field text

    # WHEN parsing it
    gps = overlay.parse_gps_text(left_text)

    # THEN the values (or None for malformed text) are returned
    assert gps == expected


def test_build_left_field_patterns():
    # GIVEN speeds of 1-3 digits, latitudes of 1-2 digits, and longitudes of 1-3 digits

    # WHEN enumerating the patterns
    patterns = overlay.build_left_field_patterns()

    # THEN all 18 combinations fit in the left field
    assert len(patterns) == 18
    assert "ddd KM/H hdd.dddddd vddd.dddddd" in patterns
    assert max(len(pattern) for pattern in patterns) <= overlay.LEFT_FIELD_CELL_COUNT


def test_get_allowed_characters():
    # GIVEN a pattern with digits, hemispheres, and literal characters
    pattern = "d h/v"

    # WHEN expanding it
    allowed = overlay.get_allowed_characters(pattern)

    # THEN placeholders are expanded and literals stay as they are
    assert allowed == ["0123456789", " ", "NS", "/", "EW"]


def test_load_glyph_templates_covers_all_pattern_characters():
    # GIVEN the bundled templates

    # WHEN loading them
    templates = overlay.load_glyph_templates()

    # THEN every character used in the patterns has a template of the cell size
    pattern_characters = set("0123456789NSEW./:KMH")
    assert pattern_characters <= set(templates.labels)
    assert all(
        image.shape == (overlay.CELL_HEIGHT, overlay.CELL_WIDTH) for image in templates.images
    )


def test_read_strip_image_with_missing_file(tmp_path):
    # GIVEN a path that does not exist

    # WHEN reading it
    # THEN it fails
    with pytest.raises(FileNotFoundError):
        overlay.read_strip_image(tmp_path / "missing.png")
