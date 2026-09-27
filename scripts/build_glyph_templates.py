"""
Build the overlay glyph templates from labeled overlay strips.

Reads the strips and labels in `tests/fixtures/overlay/`, cuts out every labeled character
cell, aligns all instances of each character, and averages them into one template per
character. The result is written to `src/dashcam/overlay_glyphs.png` (atlas) and
`src/dashcam/overlay_glyphs.json` (labels).

Afterwards, every labeled strip is read with the new templates. Any difference from its label
is reported and makes the script exit with an error. A difference usually indicates a labeling
mistake.

Usage:
> uv run python scripts/build_glyph_templates.py
"""

import json
import logging
import sys
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from dashcam import overlay

# Labeled overlay strips and their labels.
FIXTURE_DIR = Path(__file__).parents[1] / "tests" / "fixtures" / "overlay"
LABELS_PATH = FIXTURE_DIR / "labels.json"

# Position of the labeled texts in the fixture strips: the GPS text starts at the left margin,
# and the clock text ends at the right margin.
FIXTURE_GPS_TEXT_LEFT = 18
FIXTURE_CLOCK_TEXT_RIGHT = 2542

# How far (in pixels) a labeled cell may be shifted when aligning it with the others.
MAX_ALIGNMENT_SHIFT = 3

# Number of align-and-average passes.
REFINEMENT_PASS_COUNT = 3

# pylint: disable=logging-fstring-interpolation
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)8s | %(message)s")
LOGGER = logging.getLogger(__name__)

FloatImage = npt.NDArray[np.float32]


def cut_padded_cell(strip: npt.NDArray[np.uint8], x: int) -> FloatImage:
    """Cut the cell starting at `x`, with `MAX_ALIGNMENT_SHIFT` pixels of padding."""
    shift = MAX_ALIGNMENT_SHIFT
    padded_strip = cv2.copyMakeBorder(
        strip, shift, shift, shift, shift, cv2.BORDER_REPLICATE
    ).astype(np.float32)
    padded_height = overlay.CELL_HEIGHT + 2 * MAX_ALIGNMENT_SHIFT
    padded_width = overlay.CELL_WIDTH + 2 * MAX_ALIGNMENT_SHIFT
    return padded_strip[overlay.TEXT_TOP : overlay.TEXT_TOP + padded_height, x : x + padded_width]


def collect_labeled_cells() -> dict[str, list[tuple[str, FloatImage]]]:
    """
    Cut out every labeled non-blank character cell.

    Returns
    -------
    dict[str, list[tuple[str, FloatImage]]]
        Padded cells per character, each with a description of where it came from.
    """
    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    cells_by_character: dict[str, list[tuple[str, FloatImage]]] = {}

    for filename, label in labels.items():
        strip = overlay.read_strip_image(FIXTURE_DIR / filename)
        clock_text_left = FIXTURE_CLOCK_TEXT_RIGHT - len(label["clock"]) * overlay.CELL_PITCH
        for text_left, text in [
            (FIXTURE_GPS_TEXT_LEFT, label["gps"]),
            (clock_text_left, label["clock"]),
        ]:
            for cell_index, character in enumerate(text):
                if character == " ":
                    continue
                padded_cell = cut_padded_cell(strip, text_left + cell_index * overlay.CELL_PITCH)
                origin = f"{filename} cell {cell_index}"
                cells_by_character.setdefault(character, []).append((origin, padded_cell))

    return cells_by_character


def crop_center(padded_cell: FloatImage, dx: int = 0, dy: int = 0) -> FloatImage:
    """Crop an unpadded cell from a padded one, shifted by (dx, dy)."""
    top = MAX_ALIGNMENT_SHIFT + dy
    left = MAX_ALIGNMENT_SHIFT + dx
    return padded_cell[top : top + overlay.CELL_HEIGHT, left : left + overlay.CELL_WIDTH]


def build_template(padded_cells: list[FloatImage]) -> FloatImage:
    """Align the cells to each other and average them."""
    template = crop_center(padded_cells[0])
    for _ in range(REFINEMENT_PASS_COUNT):
        aligned_cells = []
        for padded_cell in padded_cells:
            response = cv2.matchTemplate(padded_cell, template, cv2.TM_CCOEFF_NORMED)
            _, _, _, (best_x, best_y) = cv2.minMaxLoc(response)
            dx = best_x - MAX_ALIGNMENT_SHIFT
            dy = best_y - MAX_ALIGNMENT_SHIFT
            aligned_cells.append(crop_center(padded_cell, dx, dy))
        template = np.mean(aligned_cells, axis=0).astype(np.float32)
    return template


def verify_labeled_strips() -> int:
    """
    Read every labeled strip with the bundled templates and report differences from the labels.

    Returns
    -------
    int
        The number of strips that were not read as labeled.
    """
    overlay.load_glyph_templates.cache_clear()
    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    mismatch_count = 0
    for filename, label in labels.items():
        reading = overlay.read_overlay(overlay.read_strip_image(FIXTURE_DIR / filename))
        if (reading.gps_text, reading.clock_text) != (label["gps"], label["clock"]):
            LOGGER.warning(
                f"{filename}: read {reading.raw!r}, labeled {label['gps']} | {label['clock']}"
            )
            mismatch_count += 1
    LOGGER.info(f"Verified {len(labels)} labeled strips, {mismatch_count} mismatches")
    return mismatch_count


def main() -> int:
    cells_by_character = collect_labeled_cells()
    labels = sorted(cells_by_character)
    LOGGER.info(f"Characters: {''.join(labels)}")

    templates = []
    for character in labels:
        instances = [padded_cell for _, padded_cell in cells_by_character[character]]
        templates.append(build_template(instances))
        LOGGER.info(f"'{character}': {len(instances)} instances")

    atlas = np.hstack(templates).clip(0, 255).astype(np.uint8)
    cv2.imwrite(str(overlay.GLYPH_ATLAS_PATH), atlas)
    overlay.GLYPH_LABELS_PATH.write_text(json.dumps(labels) + "\n", encoding="utf-8")
    LOGGER.info(f"Wrote {len(labels)} templates to '{overlay.GLYPH_ATLAS_PATH}'")

    mismatch_count = verify_labeled_strips()
    return 1 if mismatch_count else 0


if __name__ == "__main__":
    sys.exit(main())
