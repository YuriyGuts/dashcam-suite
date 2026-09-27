"""
Suggest and apply descriptive trip names built from the street lists of the tracks.

A suggested name looks like `2026-09-25 Ivana Franka, Vasylia Stusa (Car).mp4`, where the car
model suffix is left out if no car model is configured:

* The date comes from the current video filename.
* Streets are listed in travel order. Stretches under 300 m and consecutive repeats are dropped.
  If the name gets too long, the first, the last, and the longest streets in between are kept.
* A street is named by OSM `name:en`, or else by the KMU 2010 transliteration of `name`, with
  street-type words ("vulytsia", "Street", ...) dropped. Motorways, trunks, and roads without a
  name are named by their ref (e.g. `M06`).
* If the name is taken, later trips get `(1)`, `(2)`, ... before the car model.
* Trips without usable GPS get `YYYY-mm-dd Trip HH-MM (<car model>)`.

Renaming moves the video, its track, and its preview together.
"""

import dataclasses
import datetime
import logging
import re
import typing as t
import unicodedata
from pathlib import Path

from dashcam import enrich
from dashcam import extract
from dashcam import metadata
from dashcam import terminal
from dashcam.config import Config

# The longest allowed filename, including the extension. Names are ASCII, so this stays below
# `metadata.get_max_video_filename_bytes`. The web app has the same limit.
MAX_FILENAME_LENGTH = 140

# Separator between street names.
STREET_SEPARATOR = ", "

# Stretches shorter than this are too minor to name a trip after.
MIN_NAMED_STRETCH_M = 300.0

# Highway classes named by their ref, even when they have a name.
REF_HIGHWAY_CLASSES = frozenset(["motorway", "motorway_link", "trunk", "trunk_link"])

# Placeholder names given by `encode`, e.g. `Trip 11-17`, or by older versions, e.g. `Trip 3`.
PLACEHOLDER_NAME_PATTERN = re.compile(r"^Trip (\d{2}-\d{2}|\d+)$")

# Characters that are not allowed in filenames on common file systems.
FORBIDDEN_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')

# Street-type words dropped from names, in lowercase. Ukrainian words are dropped before
# transliteration, English words from `name:en`.
UKRAINIAN_STREET_TYPE_WORDS = frozenset(
    [
        "вулиця",
        "вул.",
        "проспект",
        "просп.",
        "пр.",
        "площа",
        "пл.",
        "провулок",
        "пров.",
        "бульвар",
        "бул.",
        "шосе",
        "дорога",
        "узвіз",
        "набережна",
        "проїзд",
        "тупик",
        "майдан",
        "алея",
        "шлях",
        "тракт",
        "траса",
        "міст",
        "спуск",
        "в'їзд",
        "в’їзд",
        "з'їзд",
        "з’їзд",
        "улица",
    ]
)
ENGLISH_STREET_TYPE_WORDS = frozenset(
    [
        "street",
        "st",
        "st.",
        "avenue",
        "ave",
        "ave.",
        "square",
        "lane",
        "boulevard",
        "blvd",
        "road",
        "rd",
        "highway",
        "drive",
        "passage",
        "embankment",
        "descent",
        "alley",
        "way",
        "bridge",
        "prospekt",
        "prospect",
        "vulytsia",
        "ploshcha",
        "provulok",
        "entrance",
        "exit",
        "blind",
    ]
)

# KMU 2010 transliteration of Ukrainian (Cabinet of Ministers resolution No. 55). Letters with
# a separate form at the start of a word map to (initial form, other form).
KMU_2010_LETTERS = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "h",
    "ґ": "g",
    "д": "d",
    "е": "e",
    "є": ("ye", "ie"),
    "ж": "zh",
    "з": "z",
    "и": "y",
    "і": "i",
    "ї": ("yi", "i"),
    "й": ("y", "i"),
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ь": "",
    "ю": ("yu", "iu"),
    "я": ("ya", "ia"),
    # Russian letters, which appear in some older OSM names.
    "ё": ("yo", "io"),
    "ъ": "",
    "ы": "y",
    "э": "e",
}

# Apostrophes are not transliterated.
APOSTROPHES = frozenset("'’ʼ`")

# pylint: disable=logging-fstring-interpolation
LOGGER = logging.getLogger(__name__)


class RenameError(ValueError):
    """Raised when a trip cannot be renamed as requested."""


@dataclasses.dataclass(frozen=True)
class NamedStretch:
    """A street label with the distance driven on it."""

    label: str
    distance_m: float


@dataclasses.dataclass
class RenamePlan:
    """A video and its suggested new filename."""

    video_path: Path
    new_filename: str


def transliterate_kmu_2010(text: str) -> str:
    """Transliterate Ukrainian text to Latin letters using the KMU 2010 system."""
    result = []
    for index, char in enumerate(text):
        if char in APOSTROPHES:
            continue
        lower_char = char.lower()
        mapping = KMU_2010_LETTERS.get(lower_char)
        if mapping is None:
            result.append(char)
            continue
        previous_char = text[index - 1] if index > 0 else ""
        is_word_start = not previous_char.isalpha() and previous_char not in APOSTROPHES
        if isinstance(mapping, tuple):
            mapping = mapping[0] if is_word_start else mapping[1]
        # The combination "зг" is written as "zgh" to tell it apart from "ж".
        if lower_char == "г" and previous_char.lower() == "з":
            mapping = "gh"
        if char.isupper() and mapping:
            next_char = text[index + 1] if index + 1 < len(text) else ""
            is_all_caps_word = next_char.isupper()
            mapping = mapping.upper() if is_all_caps_word else mapping[0].upper() + mapping[1:]
        result.append(mapping)
    return "".join(result)


def to_ascii(text: str) -> str:
    """Drop diacritics and any remaining non-ASCII characters."""
    decomposed = unicodedata.normalize("NFKD", text)
    return decomposed.encode("ascii", "ignore").decode("ascii")


def clean_filename_part(text: str) -> str:
    """Remove characters not allowed in filenames and collapse whitespace."""
    text = FORBIDDEN_FILENAME_CHARS.sub(" ", text)
    return " ".join(text.split())


def drop_street_type_words(name: str, street_type_words: frozenset[str]) -> str:
    """Remove street-type words from a street name."""
    words = [word for word in name.split() if word.lower().rstrip(",:;") not in street_type_words]
    return " ".join(words)


def normalize_ref(ref: str) -> str:
    """Turn an OSM ref like `М-06` or `М 06;Е40` into `M06`."""
    first_ref = ref.split(";")[0]
    return re.sub(r"[\s\-]+", "", to_ascii(transliterate_kmu_2010(first_ref)))


def get_street_label(street: dict[str, t.Any]) -> str | None:
    """
    Return the ASCII label of a street entry of a track.

    Returns
    -------
    str | None
        The label, or None if nothing is left of the name (e.g. a name that is just "вулиця").
    """
    name = street.get("name")
    ref = street.get("ref")
    has_own_name = bool(name) and name != ref
    if ref and (street.get("highway") in REF_HIGHWAY_CLASSES or not has_own_name):
        label = normalize_ref(ref)
    elif street.get("name_en"):
        label = to_ascii(drop_street_type_words(street["name_en"], ENGLISH_STREET_TYPE_WORDS))
    elif name:
        label = to_ascii(
            transliterate_kmu_2010(drop_street_type_words(name, UKRAINIAN_STREET_TYPE_WORDS))
        )
    else:
        return None
    label = clean_filename_part(label)
    return label or None


def merge_repeats(stretches: list[NamedStretch]) -> list[NamedStretch]:
    """Merge consecutive stretches with the same label."""
    merged: list[NamedStretch] = []
    for stretch in stretches:
        if merged and merged[-1].label == stretch.label:
            merged[-1] = NamedStretch(stretch.label, merged[-1].distance_m + stretch.distance_m)
        else:
            merged.append(stretch)
    return merged


def select_named_stretches(streets: list[dict[str, t.Any]]) -> list[NamedStretch]:
    """Label the streets of a track and drop short stretches and consecutive repeats."""
    stretches = []
    for street in streets:
        label = get_street_label(street)
        if label is not None:
            stretches.append(NamedStretch(label, float(street.get("distance_m") or 0)))
    # Merging before dropping short stretches keeps a street whose OSM ways have different
    # names that end up with the same label (e.g. "Ivana Franka Square" and "Street").
    long_stretches = [
        stretch for stretch in merge_repeats(stretches) if stretch.distance_m >= MIN_NAMED_STRETCH_M
    ]
    return merge_repeats(long_stretches)


def truncate_words(text: str, max_length: int) -> str:
    """Shorten text to at most `max_length` characters, cutting at a word boundary if possible."""
    if len(text) <= max_length:
        return text
    cut_text = text[:max_length]
    if " " in cut_text:
        cut_text = cut_text.rsplit(" ", 1)[0]
    return cut_text.rstrip(" ,-")


def fit_street_labels(stretches: list[NamedStretch], max_length: int) -> str:
    """
    Join street labels, dropping streets until the text fits into `max_length` characters.

    The first and the last streets are kept, then the longest ones in between.
    """
    labels = [stretch.label for stretch in stretches]
    full_text = STREET_SEPARATOR.join(labels)
    if len(full_text) <= max_length or len(stretches) <= 1:
        return truncate_words(full_text, max_length)

    kept_indexes = {0, len(stretches) - 1}
    if len(STREET_SEPARATOR.join([labels[0], labels[-1]])) > max_length:
        return truncate_words(labels[0], max_length)
    middle_indexes = sorted(
        range(1, len(stretches) - 1), key=lambda index: -stretches[index].distance_m
    )
    for index in middle_indexes:
        candidate_indexes = sorted(kept_indexes | {index})
        candidate_text = STREET_SEPARATOR.join(labels[i] for i in candidate_indexes)
        if len(candidate_text) <= max_length:
            kept_indexes.add(index)
    return STREET_SEPARATOR.join(labels[index] for index in sorted(kept_indexes))


def format_fallback_name(current_name: str, start_time: datetime.datetime | None) -> str:
    """Return `Trip HH-MM` from the trip start time, or else the current placeholder name."""
    if start_time is not None:
        return start_time.strftime("Trip %H-%M")
    if PLACEHOLDER_NAME_PATTERN.match(current_name):
        return current_name
    return "Trip"


def build_filename(
    date_text: str,
    streets_text: str,
    stretches: list[NamedStretch],
    car_model: str,
    collision_number: int,
    extension: str,
) -> str:
    """Assemble a filename, fitting the streets into the length limit."""
    collision_suffix = f" ({collision_number})" if collision_number else ""
    car_suffix = f" ({car_model})" if car_model else ""
    fixed_length = len(f"{date_text} ") + len(collision_suffix) + len(car_suffix) + len(extension)
    if stretches:
        streets_text = fit_street_labels(stretches, MAX_FILENAME_LENGTH - fixed_length)
    return f"{date_text} {streets_text}{collision_suffix}{car_suffix}{extension}"


def suggest_filename(
    track: metadata.Track,
    car_model: str,
    extension: str,
    taken_stems: set[str],
) -> str:
    """
    Suggest a filename for a trip that is not in `taken_stems` (compared in lowercase).

    Returns
    -------
    str
        The filename, including the extension.
    """
    trip_name = metadata.parse_trip_name(track.video_filename)
    assert trip_name.date is not None
    date_text = trip_name.date.isoformat()
    stretches = select_named_stretches(track.streets)
    fallback_text = format_fallback_name(trip_name.name, get_start_time(track))
    clean_car_model = clean_filename_part(to_ascii(car_model))

    collision_number = 0
    while True:
        filename = build_filename(
            date_text=date_text,
            streets_text=fallback_text,
            stretches=stretches,
            car_model=clean_car_model,
            collision_number=collision_number,
            extension=extension,
        )
        if Path(filename).stem.lower() not in taken_stems:
            return filename
        collision_number += 1


def get_start_time(track: metadata.Track) -> datetime.datetime | None:
    """Return the earliest known time of a trip."""
    times = [sample.time for sample in track.clean_samples if sample.time is not None]
    return min(times) if times else None


def get_trip_order_key(video_path: Path, track: metadata.Track) -> tuple[str, str, str]:
    """Order trips by date, then start time, then filename."""
    trip_date = metadata.parse_trip_name(video_path.name).date
    start_time = get_start_time(track)
    return (
        trip_date.isoformat() if trip_date else "",
        start_time.isoformat() if start_time else "",
        video_path.name,
    )


def is_rename_target(track: metadata.Track, include_all: bool) -> bool:
    """Check whether a trip is still named with a placeholder, or all trips are targeted."""
    return include_all or bool(
        PLACEHOLDER_NAME_PATTERN.match(metadata.parse_trip_name(track.video_filename).name)
    )


def has_usable_street_list(track: metadata.Track) -> bool:
    """Check whether a track can be named: enriched and current, or without located samples."""
    has_located_samples = any(
        sample.status in enrich.LOCATED_STATUSES for sample in track.clean_samples
    )
    if track.extraction_status != metadata.EXTRACTION_OK or not has_located_samples:
        return True
    return enrich.is_enrichment_current(track, osm_timestamp=None)


def find_taken_stems(video_paths: list[Path], track_stems: t.Iterable[str]) -> set[str]:
    """Return the names (stems, in lowercase) used by videos and tracks."""
    return {path.stem.lower() for path in video_paths} | {stem.lower() for stem in track_stems}


def find_taken_stems_in_library(library_dir: Path, store: metadata.MetadataStore) -> set[str]:
    """Return the names used by videos and tracks, from the file names only."""
    video_paths = extract.find_videos(library_dir, include=[], exclude=[])
    track_stems = [path.stem for path in store.list_track_paths()]
    return find_taken_stems(video_paths, track_stems)


def load_trip(
    library_dir: Path, store: metadata.MetadataStore, stem: str
) -> tuple[Path, metadata.Track]:
    """
    Load the track of a trip and find its video.

    Raises
    ------
    RenameError
        If the track is missing or unreadable, or the video is not in the library directory.
    """
    try:
        track = store.load_track(stem)
    except FileNotFoundError as exc:
        raise RenameError(f"No track for '{stem}'") from exc
    except (OSError, metadata.TrackFormatError) as exc:
        raise RenameError(f"Cannot read the track of '{stem}': {exc}") from exc
    video_path = library_dir / track.video_filename
    if not video_path.is_file():
        raise RenameError(f"'{track.video_filename}' is not in the library directory")
    return video_path, track


def suggest_trip_filename(
    library_dir: Path, store: metadata.MetadataStore, stem: str, car_model: str
) -> str:
    """
    Suggest a new filename for one trip.

    Raises
    ------
    RenameError
        If the trip cannot be named: no video, no date, or an outdated street list.
    """
    video_path, track = load_trip(library_dir, store, stem)
    if metadata.parse_trip_name(video_path.name).date is None:
        raise RenameError("The current name does not start with a date")
    if not has_usable_street_list(track):
        raise RenameError("The street list is outdated (run `dashcam enrich`)")
    taken_stems = find_taken_stems_in_library(library_dir, store)
    return suggest_filename(
        track, car_model, extension=video_path.suffix, taken_stems=taken_stems - {stem.lower()}
    )


def rename_trip(
    library_dir: Path, store: metadata.MetadataStore, stem: str, new_filename: str
) -> str:
    """
    Rename one trip to a filename chosen by the user.

    Returns
    -------
    str
        The new stem of the trip.

    Raises
    ------
    RenameError
        If the filename is invalid or taken, or the trip cannot be renamed.
    """
    video_path, _ = load_trip(library_dir, store, stem)
    problem = validate_filename(new_filename, video_path.suffix)
    if problem is not None:
        raise RenameError(f"Invalid name: {problem}")
    new_stem = Path(new_filename).stem
    taken_stems = find_taken_stems_in_library(library_dir, store)
    if new_stem.lower() in taken_stems - {stem.lower()}:
        raise RenameError(f"'{new_stem}' is already taken by another video or track")
    if new_filename != video_path.name:
        try:
            apply_rename(RenamePlan(video_path, new_filename), store)
        except (OSError, metadata.TrackFormatError) as exc:
            raise RenameError(f"Cannot rename '{video_path.name}': {exc}") from exc
        LOGGER.info(f"Renamed: {video_path.name} -> {new_filename}")
    return new_stem


def plan_renames(
    library_dir: Path, store: metadata.MetadataStore, car_model: str, include_all: bool
) -> list[RenamePlan]:
    """
    Suggest new names for the trips in the library directory, in the order of their dates.

    Trips without a dated name, without a track, or with an outdated street list are skipped.
    """
    tracks_by_stem = extract.load_tracks_by_stem(store)
    video_paths = extract.find_videos(library_dir, include=[], exclude=[])
    taken_stems = find_taken_stems(video_paths, tracks_by_stem)

    candidates = []
    for video_path in video_paths:
        track = tracks_by_stem.get(video_path.stem)
        if track is None or metadata.parse_trip_name(video_path.name).date is None:
            continue
        if not is_rename_target(track, include_all):
            continue
        if not has_usable_street_list(track):
            LOGGER.warning(
                f"Skipping '{video_path.name}': outdated street list (run `dashcam enrich`)"
            )
            continue
        candidates.append((video_path, track))

    plans = []
    for video_path, track in sorted(candidates, key=lambda item: get_trip_order_key(*item)):
        own_stem = video_path.stem.lower()
        new_filename = suggest_filename(
            track,
            car_model=car_model,
            extension=video_path.suffix,
            taken_stems=taken_stems - {own_stem},
        )
        if new_filename == video_path.name:
            continue
        taken_stems.add(Path(new_filename).stem.lower())
        plans.append(RenamePlan(video_path=video_path, new_filename=new_filename))
    return plans


def validate_filename(filename: str, extension: str) -> str | None:
    """
    Check a filename typed by the user.

    Returns
    -------
    str | None
        What is wrong with it, or None if it is fine.
    """
    if not filename.isascii():
        return "use ASCII characters only"
    if FORBIDDEN_FILENAME_CHARS.search(filename):
        return 'do not use any of / \\ : * ? " < > |'
    if len(filename) > MAX_FILENAME_LENGTH:
        return f"use at most {MAX_FILENAME_LENGTH} characters ({len(filename)} now)"
    if metadata.parse_trip_name(filename).date is None:
        return "start with the date (YYYY-mm-dd)"
    if Path(filename).suffix.lower() != extension.lower():
        return f"keep the {extension} extension"
    return None


def apply_rename(plan: RenamePlan, store: metadata.MetadataStore) -> None:
    """
    Rename a video with its track and preview.

    Raises
    ------
    FileExistsError
        If a video with the new name already exists.
    """
    new_path = plan.video_path.with_name(plan.new_filename)
    # On case-insensitive file systems, a rename that only changes the letter case finds the
    # video itself under the new name.
    if new_path.exists() and not new_path.samefile(plan.video_path):
        raise FileExistsError(f"'{new_path}' already exists")
    old_stem = plan.video_path.stem
    plan.video_path.rename(new_path)
    try:
        store.rename_trip(old_stem, plan.new_filename)
    except BaseException:
        new_path.rename(plan.video_path)
        raise


def print_plans(plans: list[RenamePlan]) -> None:
    """Print the old and new names."""
    for number, plan in enumerate(plans, start=1):
        terminal.print_line((f"{number:>3}. ", "dim"), (plan.video_path.name, "dim"))
        terminal.print_line(f"     {terminal.ARROW} ", (plan.new_filename, "bold"))


def edit_filename(plan: RenamePlan, ask: t.Callable[[str], str]) -> str | None:
    """
    Ask the user for the new name of one video.

    Returns
    -------
    str | None
        The new filename, or None to keep the current one.
    """
    extension = plan.video_path.suffix
    terminal.print_line()
    terminal.print_line((plan.video_path.name, "dim"))
    terminal.print_line(("  suggested: ", "dim"), (plan.new_filename, "bold"))
    while True:
        answer = ask("  Enter to accept, '-' to skip, or type a new name: ").strip()
        if not answer:
            return plan.new_filename
        if answer == "-":
            return None
        if not answer.lower().endswith(extension.lower()):
            answer += extension
        problem = validate_filename(answer, extension)
        if problem is None:
            return answer
        terminal.print_line(("  ▲ Invalid name: ", "yellow"), problem)


def confirm_plans(plans: list[RenamePlan], ask: t.Callable[[str], str]) -> list[RenamePlan]:
    """
    Ask whether to apply all suggestions, none, or decide for each video.

    The end of input (e.g. no terminal) counts as "no".

    Returns
    -------
    list[RenamePlan]
        The renames to apply.
    """
    try:
        return ask_for_confirmed_plans(plans, ask)
    except EOFError:
        terminal.print_line()
        return []


def ask_for_confirmed_plans(
    plans: list[RenamePlan], ask: t.Callable[[str], str]
) -> list[RenamePlan]:
    """Ask the questions of `confirm_plans`."""
    while True:
        trip_count_text = "the trip" if len(plans) == 1 else f"{len(plans)} trips"
        answer = ask(f"Rename {trip_count_text}? [y]es, [n]o, [e]dit individually: ")
        answer = answer.strip().lower()
        if answer in ("y", "yes"):
            return plans
        if answer in ("n", "no", ""):
            return []
        if answer in ("e", "edit"):
            break

    confirmed_plans = []
    for plan in plans:
        new_filename = edit_filename(plan, ask)
        if new_filename is not None and new_filename != plan.video_path.name:
            confirmed_plans.append(RenamePlan(plan.video_path, new_filename))
    return confirmed_plans


def rename_trips(
    library_dir: Path,
    metadata_dir: Path,
    config: Config,
    include_all: bool,
    interactive: bool,
    assume_yes: bool,
    ask: t.Callable[[str], str] = terminal.ask,
) -> int:
    """
    Suggest trip names, then apply them after confirmation (or right away with `assume_yes`).

    Without `interactive` and `assume_yes`, the suggestions are only printed.

    Returns
    -------
    int
        The number of failed renames.
    """
    store = metadata.MetadataStore(metadata_dir)
    plans = plan_renames(library_dir, store, config.car_model, include_all)
    if not plans:
        LOGGER.info("No trips to rename")
        return 0

    print_plans(plans)
    if assume_yes:
        confirmed_plans = plans
    elif interactive:
        confirmed_plans = confirm_plans(plans, ask)
    else:
        LOGGER.info("Run with `--suggest` to rename interactively, or `--yes` to apply")
        return 0

    failed_count = 0
    renamed_count = 0
    for plan in confirmed_plans:
        try:
            apply_rename(plan, store)
        except (OSError, metadata.TrackFormatError) as exc:
            LOGGER.error(f"Cannot rename '{plan.video_path.name}': {exc}")
            failed_count += 1
            continue
        renamed_count += 1
        LOGGER.info(
            f"Renamed: {plan.video_path.name} {terminal.ARROW} {plan.new_filename}",
            extra=terminal.SUCCESS,
        )

    if renamed_count:
        store.rebuild_index()
    LOGGER.info(f"Renamed {renamed_count} of {len(plans)} trips")
    return failed_count
