import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from dashcam import overlay

OVERLAY_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "overlay"
OVERLAY_LABELS = json.loads((OVERLAY_FIXTURE_DIR / "labels.json").read_text(encoding="utf-8"))

# Background brightness of synthetic strips.
BACKGROUND = 90


@pytest.fixture
def render_strip():
    """Draw texts with the glyph atlas onto a blank strip, each starting at its own x."""
    atlas = overlay.read_strip_image(overlay.GLYPH_ATLAS_PATH)
    labels = overlay.load_glyph_templates().labels

    def _render_strip(*texts_at_x, width=2560, top=overlay.TEXT_TOP):
        strip = np.full((overlay.STRIP_HEIGHT, width), BACKGROUND, dtype=np.uint8)
        for x, text in texts_at_x:
            for index, character in enumerate(text):
                if character == " ":
                    continue
                glyph_index = labels.index(character)
                glyph_left = glyph_index * overlay.CELL_WIDTH
                glyph = atlas[:, glyph_left : glyph_left + overlay.CELL_WIDTH]
                cell_x = x + index * overlay.CELL_PITCH
                strip[top : top + overlay.CELL_HEIGHT, cell_x : cell_x + overlay.CELL_WIDTH] = glyph
        return strip

    return _render_strip


@pytest.mark.parametrize("filename", sorted(OVERLAY_LABELS))
def test_read_overlay_with_labeled_strip(filename):
    # GIVEN a labeled overlay strip (day, night, rain, snow, glare, no fix, spoofed, day-first)
    strip = overlay.read_strip_image(OVERLAY_FIXTURE_DIR / filename)
    label = OVERLAY_LABELS[filename]

    # WHEN reading it
    reading = overlay.read_overlay(strip)

    # THEN both texts match the label and are reliable
    assert reading.gps_text == label["gps"]
    assert reading.clock_text == label["clock"]
    assert reading.is_clock_text_reliable
    assert reading.is_gps_text_reliable == bool(label["gps"])


@pytest.mark.parametrize(
    ("texts_at_x", "expected_gps_text", "expected_clock_text"),
    [
        # The VIOFO layout.
        (
            [(18, "46 KM/H N49.810205 E24.028992"), (2200, "2026/09/23 18:42:06")],
            "46 KM/H N49.810205 E24.028992",
            "2026/09/23 18:42:06",
        ),
        # Day-first date.
        (
            [(18, "46 KM/H N49.810205 E24.028992"), (2200, "27/04/2024 07:34:49")],
            "46 KM/H N49.810205 E24.028992",
            "27/04/2024 07:34:49",
        ),
        # Unpadded date and hour, dots as date separators.
        (
            [(18, "46 KM/H N49.810205 E24.028992"), (2254, "7.4.2024 7:34:49")],
            "46 KM/H N49.810205 E24.028992",
            "7.4.2024 7:34:49",
        ),
        # Clock on the left, GPS on the right, off the usual grid.
        (
            [(40, "2026/09/23 18:42:06"), (1901, "N49.810205 E24.028992 46 KM/H")],
            "46 KM/H N49.810205 E24.028992",
            "2026/09/23 18:42:06",
        ),
        # Time before date, extra spaces everywhere.
        (
            [(18, "46  KM/H   N49.810205    E24.028992"), (1500, "18:42:06    2026/09/23")],
            "46 KM/H N49.810205 E24.028992",
            "2026/09/23 18:42:06",
        ),
        # No spaces within the tokens.
        (
            [(18, "46KM/H N49.810205 E24.028992"), (2200, "2026/09/23 18:42:06")],
            "46 KM/H N49.810205 E24.028992",
            "2026/09/23 18:42:06",
        ),
        # Hemisphere letters after the numbers, and separated by a space.
        (
            [(18, "46 KM/H 49.810205N 24.028992E"), (2200, "2026/09/23 18:42:06")],
            "46 KM/H N49.810205 E24.028992",
            "2026/09/23 18:42:06",
        ),
        (
            [(18, "S 12.046143 W 77.056266"), (2200, "2026/09/23 18:42:06")],
            "S12.046143 W77.056266",
            "2026/09/23 18:42:06",
        ),
        # Fewer decimals, no speed.
        (
            [(18, "N49.8102 E124.0289"), (2200, "2026/09/23 18:42:06")],
            "N49.8102 E124.0289",
            "2026/09/23 18:42:06",
        ),
        # No GPS text.
        ([(2200, "2026/09/23 18:42:06")], "", "2026/09/23 18:42:06"),
    ],
)
def test_read_overlay_with_rendered_layout(
    render_strip, texts_at_x, expected_gps_text, expected_clock_text
):
    # GIVEN a strip with the texts at the given positions
    strip = render_strip(*texts_at_x)

    # WHEN reading it
    reading = overlay.read_overlay(strip)

    # THEN the texts are found and composed in the canonical order
    assert reading.gps_text == expected_gps_text
    assert reading.clock_text == expected_clock_text
    assert reading.is_clock_text_reliable
    assert reading.is_gps_text_reliable == bool(expected_gps_text)


def test_read_overlay_ignores_unrelated_digits(render_strip):
    # GIVEN a strip with the camera model and a license plate between the GPS text and the clock
    strip = render_strip(
        (18, "46 KM/H N49.810205 E24.028992"),
        (1300, "119 3"),
        (1500, "9688 12.5"),
        (2200, "2026/09/23 18:42:06"),
    )

    # WHEN reading it
    reading = overlay.read_overlay(strip)

    # THEN only the GPS text and the clock are read
    assert reading.raw == "46 KM/H N49.810205 E24.028992 | 2026/09/23 18:42:06"


def test_read_overlay_with_shifted_text(render_strip):
    # GIVEN a strip whose text sits a little lower than usual
    strip = render_strip(
        (18, "46 KM/H N49.810205 E24.028992"),
        (2200, "2026/09/23 18:42:06"),
        top=overlay.TEXT_TOP + overlay.MAX_VERTICAL_SHIFT,
    )

    # WHEN reading it
    reading = overlay.read_overlay(strip)

    # THEN both texts are read
    assert reading.raw == "46 KM/H N49.810205 E24.028992 | 2026/09/23 18:42:06"


def test_read_overlay_with_blank_strip(render_strip):
    # GIVEN a strip without any text
    strip = render_strip()

    # WHEN reading it
    reading = overlay.read_overlay(strip)

    # THEN both texts are blank
    assert reading.gps_text == ""
    assert reading.clock_text == ""
    assert not reading.is_gps_text_reliable
    assert not reading.is_clock_text_reliable


def test_read_overlay_with_narrow_strip(render_strip):
    # GIVEN a strip much narrower than a frame, with a clock
    strip = render_strip((10, "2026/09/23 18:42:06"), width=400)

    # WHEN reading it
    reading = overlay.read_overlay(strip)

    # THEN the clock is read
    assert reading.clock_text == "2026/09/23 18:42:06"


def test_read_overlay_with_tiny_strip(render_strip):
    # GIVEN a strip narrower than any token
    strip = render_strip(width=100)

    # WHEN reading it
    reading = overlay.read_overlay(strip)

    # THEN it does not fail and finds nothing
    assert reading.raw == " | "


def test_overlay_reading_with_low_score_is_unreliable():
    # GIVEN a reading with text whose worst cell matched poorly
    reading = overlay.OverlayReading(
        gps_text="9 KM/H N23.999585 E0.000000",
        clock_text="2026/01/08 12:15:58",
        gps_min_score=0.43,
        clock_min_score=0.98,
    )

    # WHEN checking reliability
    # THEN the GPS text is unreliable and the clock is reliable
    assert not reading.is_gps_text_reliable
    assert reading.is_clock_text_reliable


@pytest.mark.parametrize(
    ("gps_text", "expected"),
    [
        ("46 KM/H N49.810205 E24.028992", overlay.GpsReading(46, 49.810205, 24.028992)),
        ("203 KM/H S12.034249 W77.042741", overlay.GpsReading(203, -12.034249, -77.042741)),
        ("0 KM/H N1.000000 E123.456789", overlay.GpsReading(0, 1.0, 123.456789)),
        ("N49.8102 E24.0289", overlay.GpsReading(None, 49.8102, 24.0289)),
        ("46 KM/H N49.810205", None),
        ("46 KM/H N99.810205 E24.028992", None),
        ("46 KM/H N49.810205 E240.028992", None),
        ("0 KM/H S90.000000 W180.000000", overlay.GpsReading(0, -90.0, -180.0)),
        ("46 KM/H", None),
        ("", None),
    ],
)
def test_parse_gps_text(gps_text, expected):
    # GIVEN a GPS text

    # WHEN parsing it
    gps = overlay.parse_gps_text(gps_text)

    # THEN the values (or None without both coordinates in range) are returned
    assert gps == expected


def test_build_token_patterns():
    # GIVEN the token kinds

    # WHEN enumerating the patterns
    patterns = overlay.build_token_patterns()

    # THEN the common formats are included
    cells_by_kind = {}
    for pattern in patterns:
        cells_by_kind.setdefault(pattern.kind, set()).add(pattern.cells)
    assert "dd KM/H" in cells_by_kind["speed"]
    assert "hdd.dddddd" in cells_by_kind[f"lat-{overlay.PREFIX}"]
    assert "ddd.dddddd v" in cells_by_kind[f"lon-{overlay.SUFFIX}"]
    assert "dddd/dd/dd" in cells_by_kind["date"]
    assert "d.d.dddd" in cells_by_kind["date"]
    assert "dd:dd:dd" in cells_by_kind["time"]


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


def test_load_glyph_templates_rejects_atlas_of_wrong_width(tmp_path, monkeypatch):
    # GIVEN an atlas with one cell fewer than it has labels
    atlas_path = tmp_path / "glyphs.png"
    labels_path = tmp_path / "glyphs.json"
    cv2.imwrite(str(atlas_path), np.zeros((overlay.CELL_HEIGHT, overlay.CELL_WIDTH), np.uint8))
    labels_path.write_text('["0", "1"]', encoding="utf-8")
    monkeypatch.setattr(overlay, "GLYPH_ATLAS_PATH", atlas_path)
    monkeypatch.setattr(overlay, "GLYPH_LABELS_PATH", labels_path)
    overlay.load_glyph_templates.cache_clear()

    # WHEN loading the templates
    # THEN the mismatch is reported
    try:
        with pytest.raises(ValueError, match="18 pixels wide, but its 2 labels need 36"):
            overlay.load_glyph_templates()
    finally:
        overlay.load_glyph_templates.cache_clear()
