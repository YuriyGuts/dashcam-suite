"""
Read the text overlay burned into the bottom of dashcam video frames.

The overlay uses a fixed-width font on a fixed character grid:

    [left field]                                                        [right field]
    46 KM/H N49.810205 E24.028992          VIOFO A119 V3             2026/09/23 18:42:06

The left field is empty when the camera has no GPS fix. The right field (camera clock)
is always present. The model name in the middle is optional and ignored.

Each character cell is compared against glyph templates using normalized cross-correlation,
allowing a small shift to absorb encoding jitter. Only the glyph fill and its thin dark outline
take part in the comparison, so the result does not depend on the background behind the text
(night sky, snow, sun glare). The fields are then decoded with their known formats, which rules
out misplaced characters.

The templates are stored as an image atlas next to this module and can be rebuilt with
`scripts/build_glyph_templates.py`.
"""

import dataclasses
import functools
import json
import re
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

# Frame width that all geometry below is measured in. The overlay scales with the frame width, so
# strips of other videos are scaled to this width before reading.
NOMINAL_FRAME_WIDTH = 2560

# Narrower frames lose so much detail that characters are misread with high scores.
MIN_FRAME_WIDTH = 1280

# Height of the strip cut from the bottom of each frame. All coordinates below are relative to it.
STRIP_HEIGHT = 64

# Character grid geometry (in pixels).
CELL_PITCH = 18
CELL_WIDTH = 18
CELL_HEIGHT = 28

# Maximum shift (in pixels) of a glyph inside its cell when matching templates.
MAX_GLYPH_SHIFT = 3

# Nominal top-left corners of the first cell of each field inside the strip.
NOMINAL_LEFT_FIELD_ORIGIN = (18, 18)
NOMINAL_RIGHT_FIELD_ORIGIN = (2200, 18)

# Number of cells in each field. The left field fits "999 KM/H S99.999999 W999.999999".
LEFT_FIELD_CELL_COUNT = 32
RIGHT_FIELD_CELL_COUNT = 19

# Pixels brighter than this in the averaged glyph image belong to the glyph fill.
GLYPH_FILL_MIN_BRIGHTNESS = 150

# Width (in pixels) of the dark outline around the glyph fill.
GLYPH_OUTLINE_WIDTH = 1

# A field contains text only if the mean correlation of its cells reaches this score.
MIN_FIELD_MEAN_SCORE = 0.5

# Text is reliable only if every cell reaches this score. Genuine overlay text scores above 0.8.
# Lower scores come from text that does not fit the expected format, such as garbage the camera
# occasionally displays for a second.
MIN_RELIABLE_CELL_SCORE = 0.6

# Glyph template atlas: one row of cells, with labels in the JSON file.
GLYPH_ATLAS_PATH = Path(__file__).parent / "overlay_glyphs.png"
GLYPH_LABELS_PATH = Path(__file__).parent / "overlay_glyphs.json"

LEFT_FIELD_PATTERN = re.compile(
    r"^(?P<speed>\d{1,3}) KM/H "
    r"(?P<lat_hemisphere>[NS])(?P<lat>\d{1,2}\.\d{6}) "
    r"(?P<lon_hemisphere>[EW])(?P<lon>\d{1,3}\.\d{6})$"
)

GrayImage = npt.NDArray[np.uint8]


@dataclasses.dataclass(frozen=True)
class FieldLayout:
    """Position of the first character cell of a field inside the strip."""

    x: int
    y: int
    cell_count: int


@dataclasses.dataclass(frozen=True)
class OverlayLayout:
    """Positions of both overlay fields for a particular video."""

    left_field: FieldLayout
    right_field: FieldLayout


@dataclasses.dataclass(frozen=True)
class GlyphTemplates:
    """Idealized glyphs (bright fill, dark outline) and masks that exclude the background."""

    labels: list[str]
    images: list[npt.NDArray[np.float32]]
    masks: list[npt.NDArray[np.float32]]


@dataclasses.dataclass(frozen=True)
class GpsReading:
    """Values parsed from the left overlay field."""

    speed_kmh: int
    lat: float
    lon: float


@dataclasses.dataclass(frozen=True)
class OverlayReading:
    """
    Text of both overlay fields, with the lowest cell score of each.

    An empty text means the field is blank. A text whose score is below
    `MIN_RELIABLE_CELL_SCORE` was seen but cannot be trusted.
    """

    left_text: str
    right_text: str
    left_min_score: float
    right_min_score: float

    @property
    def is_left_text_reliable(self) -> bool:
        return bool(self.left_text) and self.left_min_score >= MIN_RELIABLE_CELL_SCORE

    @property
    def is_right_text_reliable(self) -> bool:
        return bool(self.right_text) and self.right_min_score >= MIN_RELIABLE_CELL_SCORE

    @property
    def raw(self) -> str:
        return f"{self.left_text} | {self.right_text}"


def get_nominal_layout() -> OverlayLayout:
    """Return the layout of VIOFO strips scaled to `NOMINAL_FRAME_WIDTH` (any frame height)."""
    return OverlayLayout(
        left_field=FieldLayout(*NOMINAL_LEFT_FIELD_ORIGIN, cell_count=LEFT_FIELD_CELL_COUNT),
        right_field=FieldLayout(*NOMINAL_RIGHT_FIELD_ORIGIN, cell_count=RIGHT_FIELD_CELL_COUNT),
    )


@functools.cache
def load_glyph_templates() -> GlyphTemplates:
    """Load the glyph templates bundled with the package."""
    atlas = cv2.imread(str(GLYPH_ATLAS_PATH), cv2.IMREAD_GRAYSCALE)
    if atlas is None:
        raise FileNotFoundError(f"Glyph atlas not found: '{GLYPH_ATLAS_PATH}'")
    labels = json.loads(GLYPH_LABELS_PATH.read_text(encoding="utf-8"))

    images = []
    masks = []
    outline_kernel = np.ones((3, 3), np.uint8)
    for index in range(len(labels)):
        averaged_image = atlas[:, index * CELL_WIDTH : (index + 1) * CELL_WIDTH]
        fill = (averaged_image > GLYPH_FILL_MIN_BRIGHTNESS).astype(np.uint8)
        fill_and_outline = cv2.dilate(fill, outline_kernel, iterations=GLYPH_OUTLINE_WIDTH)
        images.append((fill * 255).astype(np.float32))
        masks.append(fill_and_outline.astype(np.float32))

    return GlyphTemplates(labels=labels, images=images, masks=masks)


def read_strip_image(path: Path) -> GrayImage:
    """Read a saved overlay strip image as grayscale."""
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise FileNotFoundError(f"Cannot read image '{path}'")
    return np.asarray(image, dtype=np.uint8)


def cut_field_region(strip: GrayImage, field: FieldLayout) -> npt.NDArray[np.float32]:
    """Cut the region covering all cells of a field, with `MAX_GLYPH_SHIFT` pixels of padding."""
    left = field.x - MAX_GLYPH_SHIFT
    top = field.y - MAX_GLYPH_SHIFT
    region_width = (field.cell_count - 1) * CELL_PITCH + CELL_WIDTH + 2 * MAX_GLYPH_SHIFT
    region_height = CELL_HEIGHT + 2 * MAX_GLYPH_SHIFT

    # Pad the strip with edge pixels so that regions near the border keep their full size.
    pad_left = max(0, -left)
    pad_top = max(0, -top)
    pad_right = max(0, left + region_width - strip.shape[1])
    pad_bottom = max(0, top + region_height - strip.shape[0])
    padded_strip = cv2.copyMakeBorder(
        strip, pad_top, pad_bottom, pad_left, pad_right, cv2.BORDER_REPLICATE
    )
    left += pad_left
    top += pad_top

    region = padded_strip[top : top + region_height, left : left + region_width]
    return region.astype(np.float32)


def compute_cell_scores(
    strip: GrayImage,
    field: FieldLayout,
    templates: GlyphTemplates,
) -> npt.NDArray[np.float64]:
    """
    Compute how well every glyph template matches every cell of a field.

    Returns
    -------
    npt.NDArray[np.float64]
        Array of shape (cell count, template count) with correlation scores in [-1, 1].
    """
    region = cut_field_region(strip, field)
    shift_count = 2 * MAX_GLYPH_SHIFT + 1
    scores = np.empty((field.cell_count, len(templates.labels)))

    for template_index in range(len(templates.labels)):
        response = cv2.matchTemplate(
            region,
            templates.images[template_index],
            cv2.TM_CCOEFF_NORMED,
            mask=templates.masks[template_index],
        )
        # Flat image patches have no defined correlation.
        response = np.nan_to_num(response, nan=-1.0, posinf=-1.0, neginf=-1.0)

        for cell_index in range(field.cell_count):
            window_left = cell_index * CELL_PITCH
            window = response[:, window_left : window_left + shift_count]
            scores[cell_index, template_index] = window.max()

    return scores


def get_allowed_characters(pattern: str) -> list[str]:
    """
    Expand a cell pattern into the characters allowed in each cell.

    In the pattern, `d` means any digit, `h` means a latitude hemisphere (N/S), `v` means a
    longitude hemisphere (E/W), and any other character stands for itself.
    """
    expansions = {"d": "0123456789", "h": "NS", "v": "EW"}
    return [expansions.get(character, character) for character in pattern]


def build_left_field_patterns() -> list[str]:
    """Enumerate the cell patterns of the left field, e.g. `dd KM/H hdd.dddddd vdd.dddddd`."""
    patterns = []
    for speed_digit_count in (1, 2, 3):
        for lat_digit_count in (1, 2):
            for lon_digit_count in (1, 2, 3):
                speed = "d" * speed_digit_count
                lat = "h" + "d" * lat_digit_count + ".dddddd"
                lon = "v" + "d" * lon_digit_count + ".dddddd"
                patterns.append(f"{speed} KM/H {lat} {lon}")
    return patterns


# Cell patterns of the camera clock and of all possible GPS texts.
RIGHT_FIELD_CELL_PATTERN = "dddd/dd/dd dd:dd:dd"
LEFT_FIELD_CELL_PATTERNS = build_left_field_patterns()


def decode_with_pattern(
    scores: npt.NDArray[np.float64],
    pattern: str,
    labels: list[str],
) -> tuple[str, float, float]:
    """
    Pick the best allowed character for every non-blank cell of the pattern.

    Returns
    -------
    tuple[str, float, float]
        The text, the mean score, and the lowest score of the non-blank cells.
    """
    label_indexes = {label: index for index, label in enumerate(labels)}
    characters = []
    cell_scores = []
    for cell_index, allowed_characters in enumerate(get_allowed_characters(pattern)):
        if allowed_characters == " ":
            characters.append(" ")
            continue
        allowed_indexes = [label_indexes[character] for character in allowed_characters]
        best_index = max(allowed_indexes, key=lambda index: scores[cell_index, index])
        characters.append(labels[best_index])
        cell_scores.append(scores[cell_index, best_index])
    return "".join(characters), float(np.mean(cell_scores)), float(np.min(cell_scores))


def read_right_field(
    strip: GrayImage,
    field: FieldLayout,
    templates: GlyphTemplates,
) -> tuple[str, float]:
    """
    Read the camera clock.

    Returns
    -------
    tuple[str, float]
        The text (empty if the field is blank) and the lowest cell score.
    """
    scores = compute_cell_scores(strip, field, templates)
    text, mean_score, min_score = decode_with_pattern(
        scores, RIGHT_FIELD_CELL_PATTERN, templates.labels
    )
    if mean_score < MIN_FIELD_MEAN_SCORE:
        return "", min_score
    return text, min_score


def read_left_field(
    strip: GrayImage,
    field: FieldLayout,
    templates: GlyphTemplates,
) -> tuple[str, float]:
    """
    Read the speed and coordinates, choosing the pattern that matches best.

    Returns
    -------
    tuple[str, float]
        The text (empty if there is no GPS text) and the lowest cell score of the best pattern.
    """
    scores = compute_cell_scores(strip, field, templates)
    best_text = ""
    best_mean_score = -1.0
    best_min_score = -1.0
    for pattern in LEFT_FIELD_CELL_PATTERNS:
        text, mean_score, min_score = decode_with_pattern(scores, pattern, templates.labels)
        if mean_score > best_mean_score:
            best_text = text
            best_mean_score = mean_score
            best_min_score = min_score

    if best_mean_score < MIN_FIELD_MEAN_SCORE:
        return "", best_min_score
    return best_text, best_min_score


def read_overlay(
    strip: GrayImage,
    layout: OverlayLayout,
    templates: GlyphTemplates | None = None,
) -> OverlayReading:
    """Read both overlay fields from the bottom strip of a frame."""
    if templates is None:
        templates = load_glyph_templates()
    left_text, left_min_score = read_left_field(strip, layout.left_field, templates)
    right_text, right_min_score = read_right_field(strip, layout.right_field, templates)
    return OverlayReading(
        left_text=left_text,
        right_text=right_text,
        left_min_score=left_min_score,
        right_min_score=right_min_score,
    )


def parse_gps_text(left_text: str) -> GpsReading | None:
    """
    Parse the speed and coordinates from the left field text.

    Returns
    -------
    GpsReading | None
        The parsed values, or None if the text does not match the expected format.
    """
    match = LEFT_FIELD_PATTERN.match(left_text)
    if match is None:
        return None
    lat = float(match["lat"]) * (-1 if match["lat_hemisphere"] == "S" else 1)
    lon = float(match["lon"]) * (-1 if match["lon_hemisphere"] == "W" else 1)
    return GpsReading(speed_kmh=int(match["speed"]), lat=lat, lon=lon)
