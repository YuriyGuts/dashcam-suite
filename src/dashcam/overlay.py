"""
Read the text overlay burned into the bottom of dashcam video frames.

The overlay is one line of text in a fixed-width font, for example:

    46 KM/H N49.810205 E24.028992          VIOFO A119 V3             2026/09/23 18:42:06

The reader does not rely on where the values are, in which order they appear, or how they are
padded. It looks for these tokens anywhere along the line:

* speed, e.g. `46 KM/H` or `46KM/H`,
* latitude and longitude, e.g. `N49.810205`, `N 49.8102` or `49.810205N`,
* date, e.g. `2026/09/23`, `23/09/2026`, `9/23/2026` or `23.09.2026`,
* time, e.g. `18:42:06` or `8:42:06`.

Every character position along the line is compared against glyph templates using normalized
cross-correlation. Only the glyph fill and its thin dark outline take part in the comparison,
so the result does not depend on the background behind the text (night sky, snow, sun glare).
Each token pattern is then matched at every horizontal position, with its characters one cell
pitch apart and each cell allowed a small shift. Constraining every cell to the characters its
pattern allows there (a digit, a hemisphere letter, a separator) rules out misplaced
characters. Text that is not part of a token, such as the camera model name, is ignored.

Only the glyphs in the template atlas can be read. A format that needs other characters (for
example `MPH`, `AM`/`PM`, or degree signs) needs new templates.

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

from dashcam import geo

# Frame width that all geometry below is measured in. The overlay scales with the frame width, so
# strips of other videos are scaled to this width before reading.
NOMINAL_FRAME_WIDTH = 2560

# Narrower frames lose so much detail that characters are misread with high scores.
MIN_FRAME_WIDTH = 1280

# Height of the strip cut from the bottom of each frame. All coordinates below are relative to it.
STRIP_HEIGHT = 64

# Character cell geometry (in pixels). The font is fixed-width, so the characters of a token are
# `CELL_PITCH` apart.
CELL_PITCH = 18
CELL_WIDTH = 18
CELL_HEIGHT = 28

# Nominal top of the character cells inside the strip, and how far (in pixels) a glyph may be
# shifted from it.
TEXT_TOP = 18
MAX_VERTICAL_SHIFT = 3

# How far (in pixels) a glyph may be shifted horizontally from its position on the token grid.
MAX_HORIZONTAL_SHIFT = 2

# Pixels brighter than this in the averaged glyph image belong to the glyph fill.
GLYPH_FILL_MIN_BRIGHTNESS = 150

# Width (in pixels) of the dark outline around the glyph fill.
GLYPH_OUTLINE_WIDTH = 1

# A token candidate is considered only if every cell reaches this score. Cells of genuine text
# score well above it even in glare, while empty background scores well below it. This keeps
# a token from being extended over the blank cell next to it.
MIN_TOKEN_CELL_SCORE = 0.5

# A space in a token pattern must be blank: no glyph other than `.` may reach this score there.
# Blank cells next to genuine text score up to about 0.6, and glyph cells at least 0.75. The dot
# is left out because its small template also matches specks of background.
MAX_BLANK_CELL_SCORE = 0.67

# Most spaces allowed between the parts of a token, e.g. between the speed and its unit.
MAX_TOKEN_SPACES = 2

# Text is reliable only if every cell reaches this score. Genuine overlay text scores above 0.8.
# Lower scores come from text that is damaged or does not fit the expected format, such as
# garbage the camera occasionally displays for a second.
MIN_RELIABLE_CELL_SCORE = 0.6

# Number of decimal places a coordinate may have. Fewer than 4 places (about 10 m) would not be
# a usable track, and such short tokens are also easily matched by chance.
MIN_COORDINATE_DECIMALS = 4
MAX_COORDINATE_DECIMALS = 8

# Glyph template atlas: one row of cells, with labels in the JSON file.
GLYPH_ATLAS_PATH = Path(__file__).parent / "overlay_glyphs.png"
GLYPH_LABELS_PATH = Path(__file__).parent / "overlay_glyphs.json"

# Characters allowed by each placeholder of a token pattern. Any other character in a pattern
# stands for itself, and a space stands for a blank cell.
PLACEHOLDER_CHARACTERS = {"d": "0123456789", "h": "NS", "v": "EW"}

# Coordinate styles: the hemisphere letter comes before (`N49.81`) or after (`49.81N`) the
# number. Both coordinates of a strip are read in the same style: with mixed styles,
# `N49.81 E23.99` could also be read as `49.81 E`.
PREFIX = "prefix"
SUFFIX = "suffix"

# The GPS text as composed by `read_overlay`, with an optional speed.
GPS_TEXT_PATTERN = re.compile(
    r"^(?:(?P<speed>\d{1,3}) KM/H ?)?"
    r"(?:(?P<lat_hemisphere>[NS])(?P<lat>\d{1,2}\.\d+))? ?"
    r"(?:(?P<lon_hemisphere>[EW])(?P<lon>\d{1,3}\.\d+))?$"
)

GrayImage = npt.NDArray[np.uint8]

# The score and the label of the best allowed character of a pattern cell, at every position.
ClassScores = dict[str, tuple[npt.NDArray[np.float32], npt.NDArray[np.str_]]]


@dataclasses.dataclass(frozen=True)
class GlyphTemplates:
    """Idealized glyphs (bright fill, dark outline) and masks that exclude the background."""

    labels: list[str]
    images: list[npt.NDArray[np.float32]]
    masks: list[npt.NDArray[np.float32]]


@dataclasses.dataclass(frozen=True)
class TokenPattern:
    """A pattern of character cells, e.g. `dd KM/H` for the speed token."""

    kind: str
    cells: str

    @property
    def glyph_cell_count(self) -> int:
        return sum(cell != " " for cell in self.cells)


@dataclasses.dataclass(frozen=True)
class Token:
    """A token found in the strip, starting at `x`."""

    kind: str
    text: str
    x: int
    min_score: float
    mean_score: float

    @property
    def end_x(self) -> int:
        return self.x + len(self.text) * CELL_PITCH


@dataclasses.dataclass(frozen=True)
class GpsReading:
    """Values parsed from the GPS text. The speed is None if the camera does not show it."""

    speed_kmh: int | None
    lat: float
    lon: float


@dataclasses.dataclass(frozen=True)
class OverlayReading:
    """
    GPS and camera clock texts, with the lowest cell score of each.

    An empty text means that no such tokens were found. A text whose score is below
    `MIN_RELIABLE_CELL_SCORE` was seen but cannot be trusted.
    """

    gps_text: str
    clock_text: str
    gps_min_score: float
    clock_min_score: float

    @property
    def is_gps_text_reliable(self) -> bool:
        return bool(self.gps_text) and self.gps_min_score >= MIN_RELIABLE_CELL_SCORE

    @property
    def is_clock_text_reliable(self) -> bool:
        return bool(self.clock_text) and self.clock_min_score >= MIN_RELIABLE_CELL_SCORE

    @property
    def raw(self) -> str:
        return f"{self.gps_text} | {self.clock_text}"


def build_token_patterns() -> list[TokenPattern]:
    """Enumerate the cell patterns of all token kinds."""
    patterns = []
    separators = [" " * space_count for space_count in range(MAX_TOKEN_SPACES + 1)]
    for digit_count in (1, 2, 3):
        for separator in separators:
            patterns.append(TokenPattern("speed", "d" * digit_count + separator + "KM/H"))

    for kind, hemisphere, max_integer_digits in (("lat", "h", 2), ("lon", "v", 3)):
        for integer_digits in range(1, max_integer_digits + 1):
            for decimals in range(MIN_COORDINATE_DECIMALS, MAX_COORDINATE_DECIMALS + 1):
                number = "d" * integer_digits + "." + "d" * decimals
                for separator in separators:
                    prefix_cells = hemisphere + separator + number
                    suffix_cells = number + separator + hemisphere
                    patterns.append(TokenPattern(f"{kind}-{PREFIX}", prefix_cells))
                    patterns.append(TokenPattern(f"{kind}-{SUFFIX}", suffix_cells))

    for separator in "/.":
        for day_digits in (1, 2):
            for month_digits in (1, 2):
                day_and_month = "d" * day_digits + separator + "d" * month_digits
                patterns.append(TokenPattern("date", "dddd" + separator + day_and_month))
                patterns.append(TokenPattern("date", day_and_month + separator + "dddd"))

    for hour_digits in (1, 2):
        patterns.append(TokenPattern("time", "d" * hour_digits + ":dd:dd"))
    return patterns


TOKEN_PATTERNS = build_token_patterns()


@functools.cache
def load_glyph_templates() -> GlyphTemplates:
    """Load the glyph templates bundled with the package."""
    atlas = cv2.imread(str(GLYPH_ATLAS_PATH), cv2.IMREAD_GRAYSCALE)
    if atlas is None:
        raise FileNotFoundError(f"Glyph atlas not found: '{GLYPH_ATLAS_PATH}'")
    labels = json.loads(GLYPH_LABELS_PATH.read_text(encoding="utf-8"))
    expected_width = len(labels) * CELL_WIDTH
    if atlas.shape[1] != expected_width:
        raise ValueError(
            f"Glyph atlas '{GLYPH_ATLAS_PATH}' is {atlas.shape[1]} pixels wide, but its "
            f"{len(labels)} labels need {expected_width}"
        )

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


def compute_glyph_scores(strip: GrayImage, templates: GlyphTemplates) -> npt.NDArray[np.float32]:
    """
    Compute how well every glyph template matches a cell starting at every horizontal position.

    Returns
    -------
    npt.NDArray[np.float32]
        Array of shape (template count, strip width) with correlation scores in [-1, 1]. Each
        score is the best one within the allowed vertical and horizontal shift.
    """
    top = TEXT_TOP - MAX_VERTICAL_SHIFT
    band_height = CELL_HEIGHT + 2 * MAX_VERTICAL_SHIFT
    band = strip[top : top + band_height].astype(np.float32)
    if band.shape[0] < band_height:
        band = cv2.copyMakeBorder(band, 0, band_height - band.shape[0], 0, 0, cv2.BORDER_REPLICATE)

    strip_width = strip.shape[1]
    scores = np.full((len(templates.labels), strip_width), -1.0, dtype=np.float32)
    shift_kernel = np.ones((1, 2 * MAX_HORIZONTAL_SHIFT + 1), np.uint8)
    for template_index in range(len(templates.labels)):
        response = cv2.matchTemplate(
            band,
            templates.images[template_index],
            cv2.TM_CCOEFF_NORMED,
            mask=templates.masks[template_index],
        )
        # Flat image patches have no defined correlation.
        response = np.nan_to_num(response, nan=-1.0, posinf=-1.0, neginf=-1.0)
        best_over_rows = response.max(axis=0, keepdims=True)
        best_over_shifts = cv2.dilate(best_over_rows, shift_kernel)[0]
        scores[template_index, : len(best_over_shifts)] = best_over_shifts
    return scores


def compute_class_scores(
    glyph_scores: npt.NDArray[np.float32], labels: list[str], cell: str
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.str_]]:
    """
    Find the best character allowed in a pattern cell at every horizontal position.

    For a space, the score is how much the cell looks like any glyph other than `.`. The small
    `.` template also matches the bottom of other glyphs, so a cell that looks like another
    glyph cannot be a `.`, unless that glyph is `:`, which contains a dot.

    Returns
    -------
    tuple[npt.NDArray[np.float32], npt.NDArray[np.str_]]
        The score and the label of the best allowed character at every position.
    """
    glyph_indexes = [index for index, label in enumerate(labels) if label != "."]
    glyph_presence_scores = glyph_scores[glyph_indexes].max(axis=0)
    if cell == " ":
        return glyph_presence_scores, np.full(len(glyph_presence_scores), " ")

    allowed_characters = PLACEHOLDER_CHARACTERS.get(cell, cell)
    allowed_indexes = [labels.index(character) for character in allowed_characters]
    allowed_scores = glyph_scores[allowed_indexes]
    allowed_labels = np.array(list(allowed_characters))
    best_scores = allowed_scores.max(axis=0)
    if cell == ".":
        undotted_indexes = [index for index, label in enumerate(labels) if label not in ".:"]
        undotted_presence_scores = glyph_scores[undotted_indexes].max(axis=0)
        best_scores = np.where(undotted_presence_scores < MAX_BLANK_CELL_SCORE, best_scores, -1.0)
    return best_scores, allowed_labels[allowed_scores.argmax(axis=0)]


@dataclasses.dataclass(frozen=True)
class PatternScores:
    """Scores of one pattern at every horizontal position."""

    pattern: TokenPattern
    min_scores: npt.NDArray[np.float32]
    mean_scores: npt.NDArray[np.float32]

    def matches_at(self, x: int) -> bool:
        return 0 <= x < len(self.min_scores) and self.min_scores[x] >= MIN_TOKEN_CELL_SCORE


def score_pattern(pattern: TokenPattern, class_scores: ClassScores) -> PatternScores:
    """
    Compute the lowest and the mean glyph cell score of a pattern at every position.

    Positions where a space of the pattern is not blank get a lowest score of -1.
    """
    strip_width = len(class_scores[" "][0])
    position_count = max(0, strip_width - (len(pattern.cells) - 1) * CELL_PITCH)

    def scores_of_cell(index: int, cell: str) -> npt.NDArray[np.float32]:
        offset = index * CELL_PITCH
        return class_scores[cell][0][offset : offset + position_count]

    glyph_cell_scores = np.stack(
        [scores_of_cell(index, cell) for index, cell in enumerate(pattern.cells) if cell != " "]
    )
    min_scores = glyph_cell_scores.min(axis=0)
    for index, cell in enumerate(pattern.cells):
        if cell == " ":
            min_scores[scores_of_cell(index, cell) >= MAX_BLANK_CELL_SCORE] = -1.0
    return PatternScores(pattern, min_scores, glyph_cell_scores.mean(axis=0))


def read_cells(pattern: TokenPattern, x: int, class_scores: ClassScores) -> str:
    """Read the best allowed character of every cell of a pattern placed at `x`."""
    return "".join(
        " " if cell == " " else str(class_scores[cell][1][x + index * CELL_PITCH])
        for index, cell in enumerate(pattern.cells)
    )


def find_container(
    pattern_scores: PatternScores,
    x: int,
    all_pattern_scores: list[PatternScores],
    class_scores: ClassScores,
) -> tuple[PatternScores, int] | None:
    """
    Find the longest matching pattern that extends a match with more glyph cells.

    The extension must read the same text over the cells of the match, spaces included, and
    each added cell must reach `MIN_RELIABLE_CELL_SCORE`. Blank background next to a token
    scores below that, so a token is not extended over it.
    """
    pattern = pattern_scores.pattern
    text = read_cells(pattern, x, class_scores)
    best_container = None
    best_key = (pattern.glyph_cell_count, -1.0)
    for container_scores in all_pattern_scores:
        container = container_scores.pattern
        if container.glyph_cell_count <= pattern.glyph_cell_count:
            continue
        for cell_offset in range(len(container.cells) - len(pattern.cells) + 1):
            added_cells = [
                (index, cell)
                for index, cell in enumerate(container.cells)
                if cell != " " and not cell_offset <= index < cell_offset + len(pattern.cells)
            ]
            for shift in range(-MAX_HORIZONTAL_SHIFT, MAX_HORIZONTAL_SHIFT + 1):
                container_x = x - cell_offset * CELL_PITCH + shift
                if not container_scores.matches_at(container_x):
                    continue
                container_text = read_cells(container, container_x, class_scores)
                covered_text = container_text[cell_offset : cell_offset + len(pattern.cells)]
                are_added_cells_reliable = all(
                    class_scores[cell][0][container_x + index * CELL_PITCH]
                    >= MIN_RELIABLE_CELL_SCORE
                    for index, cell in added_cells
                )
                key = (
                    container.glyph_cell_count,
                    float(container_scores.mean_scores[container_x]),
                )
                if covered_text == text and are_added_cells_reliable and key > best_key:
                    best_key = key
                    best_container = (container_scores, container_x)
    return best_container


def find_token(kind: str, class_scores: ClassScores) -> Token | None:
    """
    Find the best token of one kind anywhere in the strip.

    A pattern matches at a position if every glyph cell reaches `MIN_TOKEN_CELL_SCORE`. The
    match with the best mean score is taken and then extended to the longest match that covers
    it, so that a token is read in full (`2026/09/23` rather than `026/09/23`).
    """
    all_pattern_scores = [
        score_pattern(pattern, class_scores) for pattern in TOKEN_PATTERNS if pattern.kind == kind
    ]
    best = None
    best_mean_score = -1.0
    for pattern_scores in all_pattern_scores:
        matching_xs = np.flatnonzero(pattern_scores.min_scores >= MIN_TOKEN_CELL_SCORE)
        if len(matching_xs) == 0:
            continue
        x = int(matching_xs[np.argmax(pattern_scores.mean_scores[matching_xs])])
        if pattern_scores.mean_scores[x] > best_mean_score:
            best = (pattern_scores, x)
            best_mean_score = float(pattern_scores.mean_scores[x])
    if best is None:
        return None

    while (container := find_container(*best, all_pattern_scores, class_scores)) is not None:
        best = container
    pattern_scores, x = best
    return Token(
        kind=kind,
        text=read_cells(pattern_scores.pattern, x, class_scores),
        x=x,
        min_score=float(pattern_scores.min_scores[x]),
        mean_score=float(pattern_scores.mean_scores[x]),
    )


def normalize_coordinate(text: str) -> str:
    """Write a coordinate token with the hemisphere first and without spaces: `N49.810205`."""
    hemisphere = next(character for character in text if character in "NSEW")
    number = text.replace(hemisphere, "").replace(" ", "")
    return hemisphere + number


def exclude_token_cells(class_scores: ClassScores, token: Token) -> ClassScores:
    """Return class scores in which no cell can start within the cells of the token."""
    first_x = max(0, token.x - CELL_WIDTH + 1)
    excluded_class_scores = {}
    for cell, (scores, labels) in class_scores.items():
        excluded_scores = scores.copy()
        excluded_scores[first_x : token.end_x] = -1.0
        excluded_class_scores[cell] = (excluded_scores, labels)
    return excluded_class_scores


def find_tokens(strip: GrayImage, templates: GlyphTemplates) -> dict[str, Token]:
    """
    Find the best token of every kind in the strip.

    The date and time are found first, and their cells are excluded from the search for the
    speed and coordinates, so that GPS tokens are not made up of clock digits. The coordinates
    are read in both styles, and the style that finds more of them, and then matches better, is
    kept.
    """
    glyph_scores = compute_glyph_scores(strip, templates)
    pattern_cells = {cell for pattern in TOKEN_PATTERNS for cell in pattern.cells}
    class_scores = {
        cell: compute_class_scores(glyph_scores, templates.labels, cell) for cell in pattern_cells
    }
    tokens = {}
    for kind in ("date", "time"):
        token = find_token(kind, class_scores)
        if token is not None:
            tokens[kind] = token
    for token in list(tokens.values()):
        class_scores = exclude_token_cells(class_scores, token)

    speed_token = find_token("speed", class_scores)
    if speed_token is not None:
        tokens["speed"] = speed_token

    best_coordinate_tokens: dict[str, Token] = {}
    best_key = (0, 0.0)
    for style in (PREFIX, SUFFIX):
        coordinate_tokens = {}
        for kind in ("lat", "lon"):
            token = find_token(f"{kind}-{style}", class_scores)
            if token is not None:
                coordinate_tokens[kind] = token
        key = (
            len(coordinate_tokens),
            sum(token.mean_score for token in coordinate_tokens.values()),
        )
        if key > best_key:
            best_key = key
            best_coordinate_tokens = coordinate_tokens
    tokens.update(best_coordinate_tokens)
    return tokens


def read_overlay(strip: GrayImage, templates: GlyphTemplates | None = None) -> OverlayReading:
    """
    Read the GPS text and the camera clock from the bottom strip of a frame.

    The GPS text is composed as `46 KM/H N49.810205 E24.028992` from the tokens found, and the
    clock text as `<date> <time>`, with the date as displayed.
    """
    if templates is None:
        templates = load_glyph_templates()
    tokens = find_tokens(strip, templates)

    gps_parts = []
    if "speed" in tokens:
        gps_parts.append(tokens["speed"].text.replace("KM/H", "").strip() + " KM/H")
    for kind in ("lat", "lon"):
        if kind in tokens:
            gps_parts.append(normalize_coordinate(tokens[kind].text))
    gps_scores = [tokens[kind].min_score for kind in ("speed", "lat", "lon") if kind in tokens]

    clock_parts = [tokens[kind].text for kind in ("date", "time") if kind in tokens]
    clock_scores = [tokens[kind].min_score for kind in ("date", "time") if kind in tokens]

    return OverlayReading(
        gps_text=" ".join(gps_parts),
        clock_text=" ".join(clock_parts),
        gps_min_score=min(gps_scores, default=-1.0),
        clock_min_score=min(clock_scores, default=-1.0),
    )


def parse_gps_text(gps_text: str) -> GpsReading | None:
    """
    Parse the speed and coordinates from the GPS text.

    Returns
    -------
    GpsReading | None
        The parsed values, or None if the text has no latitude or longitude, or one of them is
        out of range.
    """
    match = GPS_TEXT_PATTERN.match(gps_text)
    if match is None or match["lat"] is None or match["lon"] is None:
        return None
    lat = float(match["lat"]) * (-1 if match["lat_hemisphere"] == "S" else 1)
    lon = float(match["lon"]) * (-1 if match["lon_hemisphere"] == "W" else 1)
    if abs(lat) > geo.MAX_ABS_LAT or abs(lon) > geo.MAX_ABS_LON:
        return None
    speed_kmh = int(match["speed"]) if match["speed"] is not None else None
    return GpsReading(speed_kmh=speed_kmh, lat=lat, lon=lon)
