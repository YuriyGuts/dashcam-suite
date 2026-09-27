import datetime
import json
import logging

import pytest

from dashcam import cleaning
from dashcam import metadata
from dashcam import rename

KYIV_SUMMER = datetime.timezone(datetime.timedelta(hours=3))


@pytest.fixture(autouse=True)
def info_logs(caplog):
    caplog.set_level(logging.INFO)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("М06", "M06"),
        ("М-06", "M06"),
        ("М 06;Е40", "M06"),
        ("Т1418", "T1418"),
    ],
)
def test_normalize_ref(ref, expected):
    assert rename.normalize_ref(ref) == expected


def test_get_street_label_prefers_english_name():
    street = {
        "name": "вулиця Івана Франка",
        "name_en": "Ivana Franka Street",
        "highway": "secondary",
    }

    assert rename.get_street_label(street) == "Ivana Franka"


def test_get_street_label_transliterates_ukrainian_name():
    street = {"name": "проспект Червоної Калини", "highway": "secondary"}

    assert rename.get_street_label(street) == "Chervonoi Kalyny"


def test_get_street_label_uses_ref_of_trunk_road():
    street = {"name": "Київ — Чоп", "ref": "М06", "highway": "trunk"}

    assert rename.get_street_label(street) == "M06"


def test_get_street_label_uses_ref_of_road_without_name():
    street = {"name": "Т1418", "ref": "Т1418", "highway": "secondary"}

    assert rename.get_street_label(street) == "T1418"


def test_get_street_label_keeps_name_of_city_street_with_ref():
    street = {"name": "Велика Васильківська вулиця", "ref": "М11", "highway": "primary"}

    assert rename.get_street_label(street) == "Velyka Vasylkivska"


def test_get_street_label_with_only_street_type_words():
    street = {"name": "Вулиця", "highway": "residential"}

    assert rename.get_street_label(street) is None


def test_get_street_label_removes_forbidden_characters():
    street = {"name_en": "Rynok Square: North/South Side", "name": "x", "highway": "primary"}

    assert rename.get_street_label(street) == "Rynok North South Side"


def test_select_named_stretches_merges_labels_before_dropping_short_ones():
    # GIVEN a square and a street with the same label, each under 300 m, then a short detour
    streets = [
        {"name": "площа Івана Франка", "name_en": "Ivana Franka Square", "distance_m": 150},
        {"name": "вулиця Івана Франка", "name_en": "Ivana Franka Street", "distance_m": 200},
        {"name": "Снопківська вулиця", "distance_m": 80},
        {"name": "вулиця Івана Франка", "name_en": "Ivana Franka Street", "distance_m": 400},
        {"name": "вулиця Василя Стуса", "distance_m": 967},
    ]

    # WHEN selecting the named stretches
    stretches = rename.select_named_stretches(streets)

    # THEN the short detour is dropped and the repeats are merged
    assert stretches == [
        rename.NamedStretch("Ivana Franka", 750.0),
        rename.NamedStretch("Vasylia Stusa", 967.0),
    ]


def test_fit_street_labels_when_all_fit():
    stretches = [rename.NamedStretch("A", 500.0), rename.NamedStretch("B", 400.0)]

    assert rename.fit_street_labels(stretches, max_length=100) == "A, B"


def test_fit_street_labels_keeps_first_last_and_longest():
    # GIVEN five streets that do not fit together
    stretches = [
        rename.NamedStretch("First", 500.0),
        rename.NamedStretch("Short", 400.0),
        rename.NamedStretch("Longest", 5000.0),
        rename.NamedStretch("Middle", 900.0),
        rename.NamedStretch("Last", 500.0),
    ]

    # WHEN fitting them into 30 characters
    text = rename.fit_street_labels(stretches, max_length=30)

    # THEN the first, the last, and the longest streets that fit remain, in travel order
    assert text == "First, Longest, Middle, Last"


def test_fit_street_labels_when_first_and_last_do_not_fit():
    stretches = [
        rename.NamedStretch("Very Long First Street Name", 500.0),
        rename.NamedStretch("Very Long Last Street Name", 500.0),
    ]

    assert rename.fit_street_labels(stretches, max_length=20) == "Very Long First"


STUSA = {"name": "вулиця Василя Стуса", "distance_m": 967}
FRANKA = {"name": "вулиця Івана Франка", "name_en": "Ivana Franka Street", "distance_m": 401}


def test_suggest_filename_with_streets(make_named_track):
    track = make_named_track("2026-09-25 Trip 11-17.mp4", [FRANKA, STUSA])

    filename = rename.suggest_filename(track, "Car", ".mp4", taken_stems=set())

    assert filename == "2026-09-25 Ivana Franka, Vasylia Stusa (Car).mp4"


def test_suggest_filename_numbers_collisions(make_named_track):
    # GIVEN a trip whose name and first numbered name are taken
    track = make_named_track("2026-09-25 Trip 11-17.mp4", [STUSA])
    taken_stems = {"2026-09-25 vasylia stusa (car)", "2026-09-25 vasylia stusa (1) (car)"}

    # WHEN suggesting a name
    filename = rename.suggest_filename(track, "Car", ".mp4", taken_stems)

    # THEN the next free number goes before the car model
    assert filename == "2026-09-25 Vasylia Stusa (2) (Car).mp4"


def test_suggest_filename_without_streets_uses_start_time(make_named_track):
    track = make_named_track("2026-09-25 Some Trip.mp4", [], start_hour=8)

    filename = rename.suggest_filename(track, "Car", ".mp4", taken_stems=set())

    assert filename == "2026-09-25 Trip 08-17 (Car).mp4"


@pytest.mark.parametrize(
    ("streets", "taken_stems", "expected_filename"),
    [
        ([FRANKA, STUSA], set(), "2026-09-25 Ivana Franka, Vasylia Stusa.mp4"),
        ([STUSA], {"2026-09-25 vasylia stusa"}, "2026-09-25 Vasylia Stusa (1).mp4"),
        ([], set(), "2026-09-25 Trip 08-17.mp4"),
    ],
)
def test_suggest_filename_without_car_model(
    make_named_track, streets, taken_stems, expected_filename
):
    # GIVEN a trip and no configured car model
    track = make_named_track("2026-09-25 Trip 11-17.mp4", streets, start_hour=8)

    # WHEN suggesting a name
    filename = rename.suggest_filename(track, "", ".mp4", taken_stems)

    # THEN the name has no car model suffix
    assert filename == expected_filename


@pytest.mark.parametrize("current_name", ["Trip 11-17", "Trip 3", "Lake House"])
def test_suggest_filename_without_streets_or_time_keeps_current_name(make_track, current_name):
    # GIVEN a video without an overlay
    track = make_track(video_filename=f"2026-09-25 {current_name}.MOV", sample_count=0)

    # WHEN suggesting a name
    filename = rename.suggest_filename(track, "Car", ".MOV", taken_stems=set())

    # THEN the current name is kept as it is
    assert filename == f"2026-09-25 {current_name}.MOV"


def test_suggest_filename_stays_within_length_limit(make_named_track):
    # GIVEN a trip through many streets with long names
    streets = [
        {
            "name": f"Street {index} " + "x" * 20,
            "name_en": f"Street{index} " + "Long" * 5,
            "distance_m": 400 + index,
        }
        for index in range(12)
    ]
    track = make_named_track("2026-09-25 Trip 11-17.mp4", streets)

    # WHEN suggesting a name
    filename = rename.suggest_filename(track, "Car", ".mp4", taken_stems=set())

    # THEN it fits, starting with the first and ending with the last street
    assert len(filename) <= rename.MAX_FILENAME_LENGTH
    assert filename.startswith("2026-09-25 Street0 LongLongLongLongLong, ")
    assert filename.endswith(", Street11 LongLongLongLongLong (Car).mp4")


@pytest.fixture
def library(tmp_path):
    library_dir = tmp_path / "videos"
    library_dir.mkdir()
    store = metadata.MetadataStore(tmp_path / ".metadata")
    store.ensure_dirs()
    return library_dir, store


@pytest.fixture
def add_named_trip(library, make_named_track):
    library_dir, store = library

    def _add_named_trip(video_filename, streets, start_hour=11):
        video_path = library_dir / video_filename
        video_path.write_bytes(b"video")
        track = make_named_track(video_filename, streets, start_hour)
        store.save_track(track)
        return video_path, track

    return _add_named_trip


def test_plan_renames_targets_placeholders_only(library, add_named_trip):
    # GIVEN a trip with a placeholder name, and trips with real names
    library_dir, store = library
    add_named_trip("2026-09-24 Trip 3.mp4", [FRANKA])
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    add_named_trip("2026-09-25 Trip with Bad GPS.mp4", [FRANKA])

    # WHEN planning renames
    plans = rename.plan_renames(library_dir, store, "Car", include_all=False)

    # THEN only the placeholder is renamed
    assert [(plan.video_path.name, plan.new_filename) for plan in plans] == [
        ("2026-09-25 Trip 11-17.mp4", "2026-09-25 Vasylia Stusa (Car).mp4"),
    ]


def test_plan_renames_with_all(library, add_named_trip):
    # GIVEN a trip with a real name
    library_dir, store = library
    add_named_trip("2026-09-25 Trip with Bad GPS.mp4", [FRANKA])

    # WHEN planning renames of all trips
    plans = rename.plan_renames(library_dir, store, "Car", include_all=True)

    # THEN it is renamed too
    assert [plan.new_filename for plan in plans] == ["2026-09-25 Ivana Franka (Car).mp4"]


def test_plan_renames_numbers_same_route_in_time_order(library, add_named_trip):
    # GIVEN two trips on the same day along the same street, listed out of time order by name
    library_dir, store = library
    add_named_trip("2026-09-25 Trip 18-00.mp4", [STUSA], start_hour=18)
    add_named_trip("2026-09-25 Trip 09-00.mp4", [STUSA], start_hour=9)

    # WHEN planning renames
    plans = rename.plan_renames(library_dir, store, "Car", include_all=False)

    # THEN the earlier trip keeps the plain name
    assert {plan.video_path.name: plan.new_filename for plan in plans} == {
        "2026-09-25 Trip 09-00.mp4": "2026-09-25 Vasylia Stusa (Car).mp4",
        "2026-09-25 Trip 18-00.mp4": "2026-09-25 Vasylia Stusa (1) (Car).mp4",
    }


def test_plan_renames_avoids_names_of_existing_files_and_tracks(
    library, add_named_trip, make_named_track
):
    # GIVEN an existing video and an unreachable track that already use the suggested name
    library_dir, store = library
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    (library_dir / "2026-09-25 Vasylia Stusa (Car).mp4").write_bytes(b"other video")
    store.save_track(make_named_track("2026-09-25 Vasylia Stusa (1) (Car).mp4", [STUSA]))

    # WHEN planning renames
    plans = rename.plan_renames(library_dir, store, "Car", include_all=False)

    # THEN the suggestion avoids both names
    assert [plan.new_filename for plan in plans] == ["2026-09-25 Vasylia Stusa (2) (Car).mp4"]


def test_plan_renames_skips_outdated_street_list(library, add_named_trip, caplog):
    # GIVEN a trip whose samples changed after enrichment
    library_dir, store = library
    _, track = add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    track.clean_samples[0].status = cleaning.STATUS_SPOOFED
    store.save_track(track)

    # WHEN planning renames
    plans = rename.plan_renames(library_dir, store, "Car", include_all=False)

    # THEN the trip is skipped with a hint
    assert plans == []
    assert "run `dashcam enrich`" in caplog.text


def test_plan_renames_skips_trips_without_changes(library, add_named_trip):
    # GIVEN a trip that already has its suggested name
    library_dir, store = library
    add_named_trip("2026-09-25 Vasylia Stusa (Car).mp4", [STUSA])

    # WHEN planning renames of all trips
    plans = rename.plan_renames(library_dir, store, "Car", include_all=True)

    # THEN nothing is planned
    assert plans == []


def test_plan_renames_all_keeps_names_of_trips_without_overlay(library, make_track):
    # GIVEN a hand-named video without an overlay
    library_dir, store = library
    (library_dir / "2026-09-25 Lake House.mp4").write_bytes(b"video")
    track = make_track(video_filename="2026-09-25 Lake House.mp4", sample_count=0)
    track.extraction_status = metadata.EXTRACTION_NO_OVERLAY
    store.save_track(track)

    # WHEN planning renames of all trips
    plans = rename.plan_renames(library_dir, store, "Car", include_all=True)

    # THEN its name is kept
    assert plans == []


def test_plan_renames_names_trip_without_gps(library, make_track):
    # GIVEN a trip that was never enriched because it has no located samples
    library_dir, store = library
    (library_dir / "2026-09-25 Trip 11-17.mp4").write_bytes(b"video")
    track = make_track(video_filename="2026-09-25 Trip 11-17.mp4")
    for sample in track.clean_samples:
        sample.status = cleaning.STATUS_NO_FIX
    store.save_track(track)

    # WHEN planning renames
    plans = rename.plan_renames(library_dir, store, "Car", include_all=False)

    # THEN it gets the fallback name
    assert [plan.new_filename for plan in plans] == ["2026-09-25 Trip 11-17 (Car).mp4"]


@pytest.mark.parametrize("extension", metadata.VIDEO_EXTENSIONS)
def test_max_filename_length_fits_the_library_limit(extension):
    assert rename.MAX_FILENAME_LENGTH <= metadata.get_max_video_filename_bytes(extension)


@pytest.mark.parametrize(
    ("filename", "problem"),
    [
        ("2026-09-25 Vulytsia Stusa.mp4", None),
        ("2026-09-25 Вулиця.mp4", "ASCII"),
        ("2026-09-25 A/B.mp4", "do not use"),
        ("2026-09-25 " + "x" * 140 + ".mp4", "at most 140"),
        ("Stusa.mp4", "start with the date"),
        ("2026-09-25 Stusa.mov", "keep the .mp4"),
    ],
)
def test_validate_filename(filename, problem):
    result = rename.validate_filename(filename, ".mp4")

    if problem is None:
        assert result is None
    else:
        assert result is not None
        assert problem in result


def test_apply_rename_moves_video_track_and_preview(library, add_named_trip):
    # GIVEN a trip with a preview
    library_dir, store = library
    video_path, _ = add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    store.previews_dir.mkdir()
    store.preview_path(video_path.stem).write_bytes(b"preview")
    plan = rename.RenamePlan(video_path, "2026-09-25 Vasylia Stusa (Car).mp4")

    # WHEN renaming it
    rename.apply_rename(plan, store)

    # THEN the video, track, and preview all have the new name
    new_stem = "2026-09-25 Vasylia Stusa (Car)"
    assert sorted(path.name for path in library_dir.iterdir()) == [f"{new_stem}.mp4"]
    assert store.load_track(new_stem).video_filename == f"{new_stem}.mp4"
    assert not store.track_path(video_path.stem).exists()
    assert store.preview_path(new_stem).read_bytes() == b"preview"


def test_apply_rename_refuses_to_overwrite(library, add_named_trip):
    # GIVEN a video that already has the new name
    library_dir, store = library
    video_path, _ = add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    (library_dir / "2026-09-25 Stusa.mp4").write_bytes(b"other video")

    # WHEN renaming
    # THEN it fails and nothing changes
    with pytest.raises(FileExistsError):
        rename.apply_rename(rename.RenamePlan(video_path, "2026-09-25 Stusa.mp4"), store)
    assert (library_dir / "2026-09-25 Stusa.mp4").read_bytes() == b"other video"
    assert video_path.exists()


def test_apply_rename_restores_video_when_track_rename_fails(library, add_named_trip, monkeypatch):
    # GIVEN a track that cannot be renamed
    library_dir, store = library
    video_path, _ = add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])

    def failing_rename_trip(old_stem, new_video_filename):
        raise OSError("disk full")

    monkeypatch.setattr(store, "rename_trip", failing_rename_trip)

    # WHEN renaming
    with pytest.raises(OSError):
        rename.apply_rename(rename.RenamePlan(video_path, "2026-09-25 Stusa.mp4"), store)

    # THEN the video keeps its old name
    assert sorted(path.name for path in library_dir.iterdir()) == [video_path.name]


class ScriptedAnswers:
    """Answer prompts from a list and record them."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


@pytest.fixture
def two_plans(tmp_path):
    return [
        rename.RenamePlan(tmp_path / "2026-09-25 Trip 09-00.mp4", "2026-09-25 A (Car).mp4"),
        rename.RenamePlan(tmp_path / "2026-09-25 Trip 18-00.mp4", "2026-09-25 B (Car).mp4"),
    ]


def test_confirm_plans_with_yes(two_plans):
    assert rename.confirm_plans(two_plans, ScriptedAnswers("y")) == two_plans


def test_confirm_plans_with_no(two_plans):
    assert rename.confirm_plans(two_plans, ScriptedAnswers("n")) == []


def test_confirm_plans_repeats_unknown_answer(two_plans):
    # GIVEN an unknown answer followed by yes
    answers = ScriptedAnswers("maybe", "yes")

    # WHEN confirming
    confirmed_plans = rename.confirm_plans(two_plans, answers)

    # THEN the question is asked again
    assert confirmed_plans == two_plans
    assert len(answers.prompts) == 2


def test_confirm_plans_with_edit(two_plans, capsys):
    # GIVEN an invalid name and then a custom name for the first trip, and a skip for the second
    answers = ScriptedAnswers("e", "No Date", "2026-09-25 Morning Drive", "-")

    # WHEN confirming
    confirmed_plans = rename.confirm_plans(two_plans, answers)

    # THEN the invalid name is explained and only the custom name is applied
    assert "start with the date" in capsys.readouterr().out
    assert [(plan.video_path.name, plan.new_filename) for plan in confirmed_plans] == [
        ("2026-09-25 Trip 09-00.mp4", "2026-09-25 Morning Drive.mp4")
    ]


def test_confirm_plans_at_end_of_input(two_plans):
    # GIVEN no terminal to answer from
    def no_input(prompt):
        raise EOFError

    # WHEN confirming
    confirmed_plans = rename.confirm_plans(two_plans, no_input)

    # THEN nothing is renamed
    assert confirmed_plans == []


def test_confirm_plans_with_edit_accepting_suggestion(two_plans):
    answers = ScriptedAnswers("e", "", "")

    assert rename.confirm_plans(two_plans, answers) == two_plans


def run_rename_trips(library, config, interactive=False, assume_yes=False, ask=None):
    library_dir, store = library
    return rename.rename_trips(
        library_dir=library_dir,
        metadata_dir=store.root,
        config=config,
        include_all=False,
        interactive=interactive,
        assume_yes=assume_yes,
        ask=ask or ScriptedAnswers(),
    )


def test_rename_trips_without_confirmation_only_prints(library, add_named_trip, config, capsys):
    # GIVEN a trip to rename
    library_dir, _ = library
    video_path, _ = add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])

    # WHEN running without `--suggest` or `--yes`
    failed_count = run_rename_trips(library, config)

    # THEN the suggestion is printed and nothing is renamed
    assert failed_count == 0
    assert "2026-09-25 Vasylia Stusa.mp4" in capsys.readouterr().out
    assert video_path.exists()


def test_rename_trips_with_yes_renames_and_rebuilds_index(library, add_named_trip, config):
    # GIVEN a trip to rename
    library_dir, store = library
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])

    # WHEN running with `--yes`
    failed_count = run_rename_trips(library, config, assume_yes=True)

    # THEN the trip is renamed and the index lists the new name
    assert failed_count == 0
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 Vasylia Stusa.mp4"]
    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert [trip["id"] for trip in index["trips"]] == ["2026-09-25 Vasylia Stusa"]


def test_rename_trips_interactive_declined(library, add_named_trip, config):
    # GIVEN a trip to rename
    video_path, _ = add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])

    # WHEN declining the suggestion
    run_rename_trips(library, config, interactive=True, ask=ScriptedAnswers("n"))

    # THEN nothing is renamed
    assert video_path.exists()


def test_rename_trips_counts_failures(library, add_named_trip, config, monkeypatch):
    # GIVEN a rename that fails
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])

    def failing_apply_rename(plan, store):
        raise OSError("read-only file system")

    monkeypatch.setattr(rename, "apply_rename", failing_apply_rename)

    # WHEN renaming
    failed_count = run_rename_trips(library, config, assume_yes=True)

    # THEN the failure is counted
    assert failed_count == 1


def test_rename_trips_with_nothing_to_rename(library, config, caplog):
    assert run_rename_trips(library, config, assume_yes=True) == 0
    assert "No trips to rename" in caplog.text


def test_suggest_trip_filename(library, add_named_trip):
    # GIVEN a trip and another video that already has the plain suggested name
    library_dir, store = library
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    (library_dir / "2026-09-25 Vasylia Stusa (Car).mp4").write_bytes(b"other video")

    # WHEN suggesting a name for the trip
    filename = rename.suggest_trip_filename(library_dir, store, "2026-09-25 Trip 11-17", "Car")

    # THEN the suggestion avoids the taken name
    assert filename == "2026-09-25 Vasylia Stusa (1) (Car).mp4"


def test_suggest_trip_filename_ignores_own_name(library, add_named_trip):
    library_dir, store = library
    add_named_trip("2026-09-25 Vasylia Stusa (Car).mp4", [STUSA])

    filename = rename.suggest_trip_filename(
        library_dir, store, "2026-09-25 Vasylia Stusa (Car)", "Car"
    )

    assert filename == "2026-09-25 Vasylia Stusa (Car).mp4"


def test_suggest_trip_filename_with_outdated_street_list(library, add_named_trip):
    # GIVEN a trip whose samples changed after enrichment
    library_dir, store = library
    _, track = add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    track.clean_samples[0].status = cleaning.STATUS_SPOOFED
    store.save_track(track)

    # WHEN suggesting a name
    # THEN enriching is suggested instead
    with pytest.raises(rename.RenameError, match="dashcam enrich"):
        rename.suggest_trip_filename(library_dir, store, "2026-09-25 Trip 11-17", "Car")


def test_suggest_trip_filename_without_track(library):
    library_dir, store = library

    with pytest.raises(rename.RenameError, match="No track"):
        rename.suggest_trip_filename(library_dir, store, "2026-09-25 Missing", "Car")


@pytest.mark.parametrize("trip_id", ["../2026-09-25 Trip 11-17", "a/b", "a\\b", ".hidden", ""])
def test_suggest_trip_filename_with_invalid_trip_name(library, add_named_trip, trip_id):
    # GIVEN a trip, and a name that is not a single file name
    library_dir, store = library
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])

    # WHEN suggesting a filename for it
    # THEN it is refused
    with pytest.raises(rename.RenameError, match="Invalid trip name"):
        rename.suggest_trip_filename(library_dir, store, trip_id, "Car")


def test_suggest_trip_filename_without_video(library, add_named_trip):
    # GIVEN a trip whose video is not in the library directory
    library_dir, store = library
    video_path, _ = add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    video_path.unlink()

    # WHEN suggesting a name
    # THEN the missing video is reported
    with pytest.raises(rename.RenameError, match="not in the library directory"):
        rename.suggest_trip_filename(library_dir, store, "2026-09-25 Trip 11-17", "Car")


def test_rename_trip_to_chosen_name(library, add_named_trip):
    # GIVEN a trip
    library_dir, store = library
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])

    # WHEN renaming it to a name chosen by the user
    new_stem = rename.rename_trip(
        library_dir, store, "2026-09-25 Trip 11-17", "2026-09-25 To Work.mp4"
    )

    # THEN the video and track have the new name
    assert new_stem == "2026-09-25 To Work"
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 To Work.mp4"]
    assert store.load_track(new_stem).video_filename == "2026-09-25 To Work.mp4"


def test_rename_trip_changing_only_letter_case(library, add_named_trip):
    # GIVEN a trip (the test directory may be on a case-insensitive file system)
    library_dir, store = library
    add_named_trip("2026-09-25 to work.mp4", [STUSA])

    # WHEN changing only the letter case of its name
    rename.rename_trip(library_dir, store, "2026-09-25 to work", "2026-09-25 To Work.mp4")

    # THEN the video and track exist once, under the new name
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 To Work.mp4"]
    assert [path.name for path in store.list_track_paths()] == ["2026-09-25 To Work.json"]
    assert store.load_track("2026-09-25 To Work").video_filename == "2026-09-25 To Work.mp4"


def test_rename_trip_to_taken_name(library, add_named_trip):
    # GIVEN two trips
    library_dir, store = library
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    add_named_trip("2026-09-25 To Work.mp4", [STUSA])

    # WHEN renaming one to the name of the other, in another letter case
    # THEN it is refused
    with pytest.raises(rename.RenameError, match="already taken"):
        rename.rename_trip(library_dir, store, "2026-09-25 Trip 11-17", "2026-09-25 to work.mp4")


def test_rename_trip_to_invalid_name(library, add_named_trip):
    library_dir, store = library
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])

    with pytest.raises(rename.RenameError, match="Invalid name: use ASCII"):
        rename.rename_trip(library_dir, store, "2026-09-25 Trip 11-17", "2026-09-25 Стуса.mp4")


def test_rename_trip_to_same_name(library, add_named_trip):
    library_dir, store = library
    add_named_trip("2026-09-25 To Work.mp4", [STUSA])

    new_stem = rename.rename_trip(
        library_dir, store, "2026-09-25 To Work", "2026-09-25 To Work.mp4"
    )

    assert new_stem == "2026-09-25 To Work"
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 To Work.mp4"]


def test_print_plans_shows_old_and_new_names(two_plans, capsys):
    # GIVEN two rename plans

    # WHEN printing them
    rename.print_plans(two_plans)

    # THEN each old name is followed by an arrow and the new name
    assert capsys.readouterr().out.splitlines() == [
        "  1. 2026-09-25 Trip 09-00.mp4",
        "     → 2026-09-25 A (Car).mp4",
        "  2. 2026-09-25 Trip 18-00.mp4",
        "     → 2026-09-25 B (Car).mp4",
    ]


def test_find_taken_stems_in_library_uses_file_names_only(library, add_named_trip, monkeypatch):
    # GIVEN a trip, a video without a track, and an unreadable track
    library_dir, store = library
    add_named_trip("2026-09-25 Trip 11-17.mp4", [STUSA])
    (library_dir / "2026-09-26 New.MP4").write_bytes(b"video")
    store.track_path("2026-09-20 Broken").write_text("{", encoding="utf-8")
    monkeypatch.setattr(
        "dashcam.metadata.MetadataStore.iter_track_files",
        lambda self: pytest.fail("Tracks were read"),
    )

    # WHEN finding the taken names
    taken_stems = rename.find_taken_stems_in_library(library_dir, store)

    # THEN every video and track file counts, in lowercase
    assert taken_stems == {"2026-09-25 trip 11-17", "2026-09-26 new", "2026-09-20 broken"}
