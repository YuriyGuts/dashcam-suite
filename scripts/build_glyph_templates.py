r"""
Build the overlay glyph templates from labeled overlay strips.

Reads the strips and labels in `tests/fixtures/overlay/`, cuts out every labeled character
cell, aligns all instances of each character, and averages them into one template per
character. The result is written to `src/dashcam/overlay_glyphs.png` (atlas) and
`src/dashcam/overlay_glyphs.json` (labels).

Afterwards, every labeled strip is read with the new templates, and any difference from its
label is reported. A difference usually indicates a labeling mistake.

Usage:
> uv run python scripts/build_glyph_templates.py
"""

import json
import logging
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from dashcam import overlay

# Labeled overlay strips and their labels.
FIXTURE_DIR = Path(__file__).parents[1] / "tests" / "fixtures" / "overlay"
LABELS_PATH = FIXTURE_DIR / "labels.json"

# Width of a character cell including the shift padding on both sides.
PADDED_CELL_WIDTH = overlay.CELL_WIDTH + 2 * overlay.MAX_GLYPH_SHIFT

# Number of align-and-average passes.
REFINEMENT_PASS_COUNT = 3

# pylint: disable=logging-fstring-interpolation
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)8s | %(message)s")
LOGGER = logging.getLogger(__name__)

FloatImage = npt.NDArray[np.float32]


def collect_labeled_cells() -> dict[str, list[tuple[str, FloatImage]]]:
    """
    Cut out every labeled non-blank character cell.

    Returns
    -------
    dict[str, list[tuple[str, FloatImage]]]
        Padded cells per character, each with a description of where it came from.
    """
    labels = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    layout = overlay.get_nominal_layout()
    cells_by_character: dict[str, list[tuple[str, FloatImage]]] = {}

    for filename, label in labels.items():
        strip = overlay.read_strip_image(FIXTURE_DIR / filename)
        for field, text in [
            (layout.left_field, label["left"]),
            (layout.right_field, label["right"]),
        ]:
            region = overlay.cut_field_region(strip, field)
            for cell_index, character in enumerate(text):
                if character == " ":
                    continue
                cell_left = cell_index * overlay.CELL_PITCH
                padded_cell = region[:, cell_left : cell_left + PADDED_CELL_WIDTH]
                origin = f"{filename} cell {cell_index}"
                cells_by_character.setdefault(character, []).append((origin, padded_cell))

    return cells_by_character


def crop_center(padded_cell: FloatImage, dx: int = 0, dy: int = 0) -> FloatImage:
    """Crop an unpadded cell from a padded one, shifted by (dx, dy)."""
    top = overlay.MAX_GLYPH_SHIFT + dy
    left = overlay.MAX_GLYPH_SHIFT + dx
    return padded_cell[top : top + overlay.CELL_HEIGHT, left : left + overlay.CELL_WIDTH]


def build_template(padded_cells: list[FloatImage]) -> FloatImage:
    """Align the cells to each other and average them."""
    template = crop_center(padded_cells[0])
    for _ in range(REFINEMENT_PASS_COUNT):
        aligned_cells = []
        for padded_cell in padded_cells:
            response = cv2.matchTemplate(padded_cell, template, cv2.TM_CCOEFF_NORMED)
            _, _, _, (best_x, best_y) = cv2.minMaxLoc(response)
            dx = best_x - overlay.MAX_GLYPH_SHIFT
            dy = best_y - overlay.MAX_GLYPH_SHIFT
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
    layout = overlay.get_nominal_layout()
    mismatch_count = 0
    for filename, label in labels.items():
        reading = overlay.read_overlay(overlay.read_strip_image(FIXTURE_DIR / filename), layout)
        if (reading.left_text, reading.right_text) != (label["left"], label["right"]):
            LOGGER.warning(
                f"{filename}: read {reading.raw!r}, labeled {label['left']} | {label['right']}"
            )
            mismatch_count += 1
    LOGGER.info(f"Verified {len(labels)} labeled strips, {mismatch_count} mismatches")
    return mismatch_count


def main() -> None:
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

    verify_labeled_strips()


if __name__ == "__main__":
    main()
