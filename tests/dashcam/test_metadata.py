import datetime
import json
import logging
import math
from pathlib import Path

import pytest

from dashcam import cleaning
from dashcam import geo
from dashcam import metadata


@pytest.fixture
def store(tmp_path):
    return metadata.MetadataStore(tmp_path / ".metadata")


def clean_sample(t, lat, lon, status):
    return cleaning.CleanSample(t=t, time=None, lat=lat, lon=lon, kmh=None, status=status)


def located_sample(t, lat, lon):
    return clean_sample(t, lat, lon, cleaning.STATUS_OK)


def unlocated_sample(t, status=cleaning.STATUS_NO_FIX):
    return clean_sample(t, None, None, status)


def test_dump_track_round_trip(make_track):
    # GIVEN a track
    track = make_track()

    # WHEN serializing and parsing it
    loaded_track = metadata.load_track_text(metadata.dump_track(track))

    # THEN it is unchanged
    assert loaded_track == track


def test_dump_track_round_trip_with_street_data(make_track):
    # GIVEN an enriched track
    track = make_track()
    track.streets = [{"name": "Хрещатик", "distance_m": 900}]
    track.localities = {"start": {"name": "Київ", "place": "city"}, "end": None}
    track.enrichment = metadata.Enrichment(
        enricher_version=1,
        osm_timestamp="2026-09-25T20:24:36Z",
        samples_digest="0123456789abcdef",
        enriched_at="2026-09-26T20:00:00+03:00",
    )

    # WHEN serializing and parsing it
    loaded_track = metadata.load_track_text(metadata.dump_track(track))

    # THEN it is unchanged, with the names written as UTF-8 text
    assert loaded_track == track
    assert "Хрещатик" in metadata.dump_track(track)


def test_load_track_text_without_street_data(make_track):
    # GIVEN a track file written before `enrich` existed
    data = json.loads(metadata.dump_track(make_track()))
    del data["localities"]
    del data["enrichment"]

    # WHEN parsing it
    track = metadata.load_track_text(json.dumps(data))

    # THEN it has no street data
    assert track.localities == {}
    assert track.enrichment is None


def test_load_track_text_with_invalid_enrichment(make_track):
    # GIVEN a track whose enrichment lacks a field
    data = json.loads(metadata.dump_track(make_track()))
    data["enrichment"] = {"enricher_version": 1}

    # WHEN parsing it
    # THEN the error names the missing field
    with pytest.raises(metadata.TrackFormatError, match="osm_timestamp"):
        metadata.load_track_text(json.dumps(data))


def test_dump_track_writes_one_sample_per_line(make_track):
    # GIVEN a track with three samples
    track = make_track(sample_count=3)

    # WHEN serializing it
    text = metadata.dump_track(track)

    # THEN each sample is on its own line and the whole file is valid JSON
    sample_lines = [line for line in text.splitlines() if line.strip().startswith('{"t":')]
    assert len(sample_lines) == 3
    assert '"raw": "30 KM/H N49.800000 E24.000000 | 2026/09/25 11:17:00"' in sample_lines[0]
    assert json.loads(text)["overrides"]["bad_ranges_s"] == [[1.0, 2.0]]


def test_dump_track_without_samples(make_track):
    # GIVEN a track of a video without an overlay
    track = make_track(sample_count=0)

    # WHEN serializing and parsing it
    loaded_track = metadata.load_track_text(metadata.dump_track(track))

    # THEN it has no samples
    assert loaded_track.raw_samples == []


def test_dump_track_marks_estimated_times_only(make_track):
    # GIVEN a track with one estimated time
    track = make_track()
    track.clean_samples[1].time_estimated = True

    # WHEN serializing it
    sample_dicts = json.loads(metadata.dump_track(track))["samples"]

    # THEN only that sample has the flag
    assert ["time_estimated" in sample_dict for sample_dict in sample_dicts] == [False, True, False]


def test_load_track_text_with_invalid_json():
    # GIVEN a track file broken on its third line
    text = '{\n  "video_filename": "a.mp4",\n  oops\n}'

    # WHEN parsing it
    # THEN the error names the line
    with pytest.raises(metadata.TrackFormatError, match="line 3"):
        metadata.load_track_text(text)


def test_load_track_file_with_byte_order_mark(tmp_path, make_track):
    # GIVEN a track file saved with a UTF-8 byte order mark
    track = make_track(video_filename="2026-09-25 Вулиця.mp4")
    path = tmp_path / "track.json"
    path.write_bytes(b"\xef\xbb\xbf" + metadata.dump_track(track).encode("utf-8"))

    # WHEN loading it
    loaded_track = metadata.load_track_file(path)

    # THEN it loads like any other track
    assert loaded_track.video_filename == "2026-09-25 Вулиця.mp4"


def test_load_track_file_with_other_encoding(tmp_path, make_track):
    # GIVEN a track file saved in a legacy Cyrillic encoding
    track = make_track(video_filename="2026-09-25 Вулиця.mp4")
    path = tmp_path / "track.json"
    path.write_bytes(metadata.dump_track(track).encode("cp1251"))

    # WHEN loading it
    # THEN it is reported as an invalid track
    with pytest.raises(metadata.TrackFormatError, match="Not UTF-8"):
        metadata.load_track_file(path)


def test_load_track_text_with_missing_field(make_track):
    # GIVEN a track without a fingerprint
    data = json.loads(metadata.dump_track(make_track()))
    del data["fingerprint"]

    # WHEN parsing it
    # THEN the error names the field
    with pytest.raises(metadata.TrackFormatError, match="fingerprint"):
        metadata.load_track_text(json.dumps(data))


@pytest.mark.parametrize(
    "bad_ranges",
    [
        "10-20",
        [[10]],
        [[20, 10]],
        [["a", "b"]],
    ],
)
def test_load_track_text_with_invalid_override(make_track, bad_ranges):
    # GIVEN a hand-edited track with a malformed override
    data = json.loads(metadata.dump_track(make_track()))
    data["overrides"]["bad_ranges_s"] = bad_ranges

    # WHEN parsing it
    # THEN the error names the override
    with pytest.raises(metadata.TrackFormatError, match="bad_ranges_s"):
        metadata.load_track_text(json.dumps(data))


def test_load_track_text_with_invalid_sample(make_track):
    # GIVEN a track whose second sample has no raw text
    data = json.loads(metadata.dump_track(make_track()))
    del data["samples"][1]["raw"]

    # WHEN parsing it
    # THEN the error names the sample
    with pytest.raises(metadata.TrackFormatError, match="sample #2"):
        metadata.load_track_text(json.dumps(data))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": "ok", "lat": None}, "status 'ok' needs 'lat' and 'lon'"),
        ({"status": "interpolated", "lon": None}, "status 'interpolated' needs 'lat' and 'lon'"),
        ({"lat": "49.8"}, "must be numbers or null"),
        ({"lon": True}, "must be numbers or null"),
        ({"status": "good"}, "Unknown status 'good'"),
    ],
)
def test_load_track_text_with_hand_edited_sample(make_track, changes, message):
    # GIVEN a track whose second sample was edited by hand into an inconsistent state
    data = json.loads(metadata.dump_track(make_track()))
    data["samples"][1].update(changes)

    # WHEN parsing it
    # THEN the error names the sample and the problem, instead of a crash in a later command
    with pytest.raises(metadata.TrackFormatError, match=f"sample #2: .*{message}"):
        metadata.load_track_text(json.dumps(data))


def test_load_track_text_accepts_samples_without_position(make_track):
    # GIVEN a track with a sample without a fix
    data = json.loads(metadata.dump_track(make_track()))
    data["samples"][1].update({"status": "no_fix", "lat": None, "lon": None})

    # WHEN parsing it
    track = metadata.load_track_text(json.dumps(data))

    # THEN it loads
    assert track.clean_samples[1].status == "no_fix"


def test_parse_trip_name():
    # GIVEN a video filename with a date and a name

    # WHEN parsing it
    trip_name = metadata.parse_trip_name("2026-09-25 Khreshchatyk, M06 (Car).mp4")

    # THEN the date and name are split
    assert trip_name == metadata.TripName(datetime.date(2026, 9, 25), "Khreshchatyk, M06 (Car)")


def test_parse_trip_name_without_date():
    # GIVEN a video filename without a date

    # WHEN parsing it
    trip_name = metadata.parse_trip_name("Road Trip.mp4")

    # THEN the whole stem is the name
    assert trip_name == metadata.TripName(None, "Road Trip")


def test_parse_trip_name_with_impossible_date():
    # GIVEN a video filename with an impossible date

    # WHEN parsing it
    trip_name = metadata.parse_trip_name("2026-13-45 Trip.mp4")

    # THEN it is treated as a name without a date
    assert trip_name.date is None


def test_compute_trip_stats(make_track):
    # GIVEN a track with three good fixes about 33 m apart, one second each
    track = make_track(sample_count=3)

    # WHEN computing the stats
    stats = metadata.compute_trip_stats(track)

    # THEN distance, duration, speed, and coverage are summarized
    assert stats["distance_km"] == pytest.approx(0.07, abs=0.005)
    assert stats["duration_s"] == 2
    assert stats["avg_kmh"] == pytest.approx(120, rel=0.05)
    assert stats["max_kmh"] == 30
    assert stats["coverage"] == 1.0
    assert stats["start_time"] == "2026-09-25T11:17:00+03:00"
    assert stats["status_counts"] == {"ok": 3}


def test_compute_trip_stats_skips_gaps(make_track):
    # GIVEN a track whose middle sample has no fix
    track = make_track(sample_count=3)
    track.clean_samples[1] = cleaning.CleanSample(
        t=1.0,
        time=track.clean_samples[1].time,
        lat=None,
        lon=None,
        kmh=None,
        status=cleaning.STATUS_NO_FIX,
    )

    # WHEN computing the stats
    stats = metadata.compute_trip_stats(track)

    # THEN no distance is counted across the gap
    assert stats["distance_km"] == 0
    assert stats["coverage"] == pytest.approx(0.667, abs=0.001)


def test_compute_trip_stats_without_samples(make_track):
    # GIVEN a track without samples
    track = make_track(sample_count=0)

    # WHEN computing the stats
    stats = metadata.compute_trip_stats(track)

    # THEN the stats are empty
    assert stats["start_time"] is None
    assert stats["coverage"] == 0.0
    assert stats["bbox"] is None


def test_simplify_route_splits_runs_at_unlocated_samples():
    # GIVEN two located stretches separated by a sample without a fix
    samples = [
        located_sample(0, 49.80, 24.0),
        located_sample(1, 49.81, 24.0),
        unlocated_sample(2),
        located_sample(3, 49.82, 24.0),
        located_sample(4, 49.83, 24.0),
    ]

    # WHEN simplifying the route
    runs = metadata.simplify_route(samples)

    # THEN each stretch is a separate run
    assert runs == [[[49.80, 24.0], [49.81, 24.0]], [[49.82, 24.0], [49.83, 24.0]]]


def test_simplify_route_drops_close_points_but_keeps_run_end():
    # GIVEN points 1 m apart (a car creeping forward)
    samples = [located_sample(index, 49.8 + index * 0.00001, 24.0) for index in range(5)]

    # WHEN simplifying the route
    runs = metadata.simplify_route(samples)

    # THEN only the first and the last points are kept
    assert runs == [[[49.8, 24.0], [49.80004, 24.0]]]


def test_simplify_route_drops_single_point_runs():
    # GIVEN a lone located sample between samples without a fix
    samples = [
        unlocated_sample(0),
        located_sample(1, 49.8, 24.0),
        unlocated_sample(2, cleaning.STATUS_SPOOFED),
    ]

    # WHEN simplifying the route
    runs = metadata.simplify_route(samples)

    # THEN nothing is left to draw
    assert runs == []


def test_simplify_route_ignores_located_status_without_coordinates():
    # GIVEN an `ok` sample whose coordinates are missing
    samples = [
        located_sample(0, 49.80, 24.0),
        clean_sample(1, None, None, cleaning.STATUS_OK),
        located_sample(2, 49.81, 24.0),
    ]

    # WHEN simplifying the route
    runs = metadata.simplify_route(samples)

    # THEN the sample splits the track like a missing fix
    assert runs == []


def test_simplify_route_keeps_interpolated_samples():
    # GIVEN an interpolated sample at a turn between good fixes
    samples = [
        located_sample(0, 49.80, 24.0),
        clean_sample(1, 49.81, 24.01, cleaning.STATUS_INTERPOLATED),
        located_sample(2, 49.82, 24.0),
    ]

    # WHEN simplifying the route
    runs = metadata.simplify_route(samples)

    # THEN it is part of the run
    assert runs == [[[49.80, 24.0], [49.81, 24.01], [49.82, 24.0]]]


def test_simplify_route_drops_points_on_a_straight_line():
    # GIVEN points every ~11 m along a straight road, with a turn at the end
    samples = [located_sample(index, 49.8 + index * 0.0001, 24.0) for index in range(10)]
    samples.append(located_sample(10, 49.8009, 24.001))

    # WHEN simplifying the route
    runs = metadata.simplify_route(samples)

    # THEN only the ends of the straight stretch and the turn are kept
    assert runs == [[[49.8, 24.0], [49.8009, 24.0], [49.8009, 24.001]]]


@pytest.mark.parametrize(
    ("offset_m", "is_kept"),
    [(3.0, False), (8.0, True)],
)
def test_simplify_polyline_keeps_points_beyond_tolerance(offset_m, is_kept):
    # GIVEN a straight 200 m road with its middle point off the line by `offset_m` to the east
    offset_lon = offset_m / (geo.METERS_PER_DEGREE * math.cos(math.radians(49.8)))
    points = [[49.8, 24.0], [49.8009, 24.0 + offset_lon], [49.8018, 24.0]]

    # WHEN simplifying it with a 5 m tolerance
    simplified = metadata.simplify_polyline(points, 5.0)

    # THEN the middle point is kept only if it is farther than the tolerance
    assert (points[1] in simplified) == is_kept


def test_metadata_store_save_and_load_track(make_track, store):
    # GIVEN a track
    track = make_track()

    # WHEN saving and loading it
    path = store.save_track(track)
    loaded_track = store.load_track(track.stem)

    # THEN it is stored under the video stem, without leftover temporary files
    assert path.name == "2026-09-25 Trip 11-17.json"
    assert loaded_track == track
    assert [path.name for path in store.tracks_dir.iterdir()] == [path.name]


def test_metadata_store_iter_track_files(make_track, store, caplog):
    # GIVEN a readable and a broken track
    caplog.set_level(logging.INFO)
    store.save_track(make_track())
    (store.tracks_dir / "2026-09-26 Broken.json").write_text("{", encoding="utf-8")

    # WHEN iterating over the track files
    track_files = list(store.iter_track_files())

    # THEN both are listed in name order, the broken one with its error
    assert [track_file.path.name for track_file in track_files] == [
        "2026-09-25 Trip 11-17.json",
        "2026-09-26 Broken.json",
    ]
    assert track_files[0].track == make_track()
    assert track_files[0].error is None
    assert track_files[1].track is None
    assert track_files[1].error
    # THEN the loading is announced before it starts
    assert caplog.records[0].getMessage() == f"Loading 2 tracks from '{store.tracks_dir}'"


def test_metadata_store_move_to_trash(make_track, store):
    # GIVEN a track with a preview
    track = make_track()
    store.save_track(track)
    store.previews_dir.mkdir(parents=True)
    store.preview_path(track.stem).write_bytes(b"preview")

    # WHEN moving it to the trash
    moved_paths = store.move_to_trash(track.stem)

    # THEN both files are in one timestamped trash folder, under their own names
    assert sorted(path.name for path in moved_paths) == [
        "2026-09-25 Trip 11-17.json",
        "2026-09-25 Trip 11-17.mp4",
    ]
    assert len({path.parent for path in moved_paths}) == 1
    assert moved_paths[0].parent.parent == store.trash_dir
    assert not store.track_path(track.stem).exists()
    assert not store.preview_path(track.stem).exists()


def test_metadata_store_get_free_trash_dir_skips_taken_names(store):
    # GIVEN a trash folder that has a track with the same name
    taken_dir = store.trash_dir / "20260925-111707"
    taken_dir.mkdir(parents=True)
    (taken_dir / "2026-09-25 Trip.json").write_text("{}", encoding="utf-8")

    # WHEN choosing a folder for a track and preview of that name in the same second
    trash_dir = store.get_free_trash_dir(
        "20260925-111707", ["2026-09-25 Trip.json", "2026-09-25 Trip.mp4"]
    )

    # THEN a numbered folder is chosen
    assert trash_dir == store.trash_dir / "20260925-111707-1"


def test_metadata_store_rename_trip(make_track, store):
    # GIVEN a track with a preview
    track = make_track()
    store.save_track(track)
    store.previews_dir.mkdir(parents=True)
    store.preview_path(track.stem).write_bytes(b"preview")

    # WHEN its video is renamed
    store.rename_trip(track.stem, "2026-09-25 Khreshchatyk (Car).mp4")

    # THEN the track and preview follow the new name
    renamed_track = store.load_track("2026-09-25 Khreshchatyk (Car)")
    assert renamed_track.video_filename == "2026-09-25 Khreshchatyk (Car).mp4"
    assert not store.track_path(track.stem).exists()
    assert store.preview_path("2026-09-25 Khreshchatyk (Car)").read_bytes() == b"preview"


def test_metadata_store_rename_trip_changing_only_letter_case(make_track, store):
    # GIVEN a track with a preview (the test directory may be on a case-insensitive file system)
    store.save_track(make_track(video_filename="2026-09-25 to work.mp4"))
    store.previews_dir.mkdir()
    store.preview_path("2026-09-25 to work").write_bytes(b"preview")

    # WHEN renaming it to the same name in another letter case
    store.rename_trip("2026-09-25 to work", "2026-09-25 To Work.mp4")

    # THEN the track and preview exist once, under the new name
    assert [path.name for path in store.list_track_paths()] == ["2026-09-25 To Work.json"]
    assert store.load_track("2026-09-25 To Work").video_filename == "2026-09-25 To Work.mp4"
    assert [path.name for path in store.previews_dir.iterdir()] == ["2026-09-25 To Work.mp4"]


def test_metadata_store_rename_trip_undoes_steps_when_preview_rename_fails(
    make_track, store, monkeypatch
):
    # GIVEN a track with a preview, and a preview that cannot be renamed
    track = make_track()
    store.save_track(track)
    store.previews_dir.mkdir(parents=True)
    store.preview_path(track.stem).write_bytes(b"preview")
    original_rename = metadata.rename_without_overwrite

    def rename_failing_for_previews(source_path, target_path):
        if source_path.parent == store.previews_dir:
            raise PermissionError("preview is locked")
        original_rename(source_path, target_path)

    monkeypatch.setattr(metadata, "rename_without_overwrite", rename_failing_for_previews)

    # WHEN its video is renamed
    with pytest.raises(PermissionError):
        store.rename_trip(track.stem, "2026-09-25 Khreshchatyk.mp4")

    # THEN the track is back under its old name, still naming the old video
    assert [path.name for path in store.list_track_paths()] == [f"{track.stem}.json"]
    assert store.load_track(track.stem).video_filename == track.video_filename
    assert store.preview_path(track.stem).read_bytes() == b"preview"


def test_metadata_store_rename_trip_refuses_to_replace_another_track(make_track, store):
    # GIVEN two tracks
    track = make_track()
    store.save_track(track)
    other_track = make_track(video_filename="2026-09-25 Khreshchatyk.mp4")
    store.save_track(other_track)

    # WHEN renaming the first to the name of the second
    with pytest.raises(FileExistsError):
        store.rename_trip(track.stem, "2026-09-25 Khreshchatyk.mp4")

    # THEN both tracks are unchanged
    assert store.load_track(track.stem).video_filename == track.video_filename
    assert store.load_track(other_track.stem).video_filename == other_track.video_filename


def test_metadata_store_rebuild_index(make_track, store):
    # GIVEN a good track, a track without an overlay, and an unreadable track
    store.save_track(make_track())
    no_overlay_track = make_track("2023-01-01 Old Camera.mp4", sample_count=0)
    no_overlay_track.extraction_status = metadata.EXTRACTION_NO_OVERLAY
    store.save_track(no_overlay_track)
    store.track_path("broken").write_text("{", encoding="utf-8")

    # WHEN rebuilding the index
    index = store.rebuild_index()

    # THEN readable tracks are listed, with stats for extracted trips only
    trips_by_id = {trip["id"]: trip for trip in index["trips"]}
    assert set(trips_by_id) == {"2026-09-25 Trip 11-17", "2023-01-01 Old Camera"}
    assert trips_by_id["2026-09-25 Trip 11-17"]["name"] == "Trip 11-17"
    assert trips_by_id["2026-09-25 Trip 11-17"]["date"] == "2026-09-25"
    assert "distance_km" in trips_by_id["2026-09-25 Trip 11-17"]
    assert "distance_km" not in trips_by_id["2023-01-01 Old Camera"]
    assert json.loads(store.index_path.read_text(encoding="utf-8")) == index


def test_metadata_store_rebuild_index_with_localities(make_track, store):
    # GIVEN a track with a start locality and an unknown end locality
    track = make_track()
    track.localities = {"start": {"name": "Київ", "place": "city"}, "end": None}
    store.save_track(track)

    # WHEN rebuilding the index
    index = store.rebuild_index()

    # THEN the trip has the locality names
    assert index["trips"][0]["start_locality"] == "Київ"
    assert index["trips"][0]["end_locality"] is None


def test_get_street_names_lists_each_name_once(make_track):
    # GIVEN streets as dictionaries and strings, with a repeat and entries without a name
    track = make_track()
    track.streets = [
        {"name": "Khreshchatyk", "distance_m": 900},
        "Lesi Ukrainky",
        {"name": "Khreshchatyk"},
        {"distance_m": 10},
        "",
    ]

    # WHEN listing the street names
    street_names = metadata.get_street_names(track)

    # THEN each name appears once, in travel order
    assert street_names == ["Khreshchatyk", "Lesi Ukrainky"]


def test_metadata_store_rebuild_index_writes_streets_and_geometry(make_track, store):
    # GIVEN a trip with streets and a video without an overlay
    track = make_track()
    track.streets = [{"name": "Lesi Ukrainky", "distance_m": 700}, {"name": "Hrushevskoho"}]
    store.save_track(track)
    no_overlay_track = make_track("2023-01-01 Old Camera.mp4", sample_count=0)
    no_overlay_track.extraction_status = metadata.EXTRACTION_NO_OVERLAY
    store.save_track(no_overlay_track)

    # WHEN rebuilding the index
    index = store.rebuild_index()

    # THEN the index has the street names, and the geometry file has the route of the GPS trip
    trips_by_id = {trip["id"]: trip for trip in index["trips"]}
    assert index["format_version"] == metadata.INDEX_FORMAT_VERSION
    assert trips_by_id["2026-09-25 Trip 11-17"]["streets"] == ["Lesi Ukrainky", "Hrushevskoho"]
    assert trips_by_id["2023-01-01 Old Camera"]["streets"] == []
    geometry = store.load_geometry()
    assert geometry["format_version"] == metadata.INDEX_FORMAT_VERSION
    assert geometry["trips"] == {"2026-09-25 Trip 11-17": [[[49.8, 24.0], [49.8006, 24.0]]]}


def test_metadata_store_update_index_after_rename_matches_rebuild(make_track, store):
    # GIVEN an index of three trips
    for video_filename in ["2026-09-24 A.mp4", "2026-09-25 B.mp4", "2026-09-26 C.mp4"]:
        store.save_track(make_track(video_filename))
    store.rebuild_index()

    # WHEN renaming a trip and updating the index for it
    store.rename_trip("2026-09-25 B", "2026-09-25 A b.mp4")
    index = store.update_index_after_rename("2026-09-25 B", "2026-09-25 A b")

    # THEN the index and the geometry are the same as after a full rebuild
    updated_geometry = store.load_geometry()
    rebuilt_index, rebuilt_geometry = store.build_index_and_geometry()
    assert index["trips"] == rebuilt_index["trips"]
    assert store.load_index()["trips"] == rebuilt_index["trips"]
    assert updated_geometry == rebuilt_geometry


def test_metadata_store_list_track_paths_sorts_by_case_sensitive_name(make_track, store):
    # GIVEN tracks whose names differ in letter case
    for video_filename in ["2026-09-25 b.mp4", "2026-09-25 C.mp4", "2026-09-25 A.mp4"]:
        store.save_track(make_track(video_filename))

    # WHEN listing them
    track_paths = store.list_track_paths()

    # THEN they are in the same order as the incremental index update uses, on every platform
    assert [path.name for path in track_paths] == [
        "2026-09-25 A.json",
        "2026-09-25 C.json",
        "2026-09-25 b.json",
    ]


def test_metadata_store_list_track_paths_ignores_temporary_files(make_track, store):
    # GIVEN a track and a leftover temporary file
    store.save_track(make_track())
    (store.tracks_dir / ".tmp-0123abcd.partial").write_text("{", encoding="utf-8")

    # WHEN listing tracks
    paths = store.list_track_paths()

    # THEN only the track is listed
    assert [path.name for path in paths] == ["2026-09-25 Trip 11-17.json"]


def test_get_partial_path_does_not_depend_on_the_name(tmp_path):
    # GIVEN a file with a long name
    path = tmp_path / ("2026-09-25 " + "x" * 127 + ".json")

    # WHEN getting two partial paths for it
    first_partial_path = metadata.get_partial_path(path)
    second_partial_path = metadata.get_partial_path(path)

    # THEN they are short, hidden, distinct, and in the same directory
    assert first_partial_path.parent == tmp_path
    assert first_partial_path.name.startswith(".")
    assert first_partial_path.name.endswith(metadata.PARTIAL_FILE_SUFFIX)
    assert len(first_partial_path.name) < 30
    assert first_partial_path != second_partial_path


@pytest.mark.parametrize(
    ("video_filename", "problem"),
    [
        ("2026-09-25 " + "x" * 127 + ".mp4", None),
        ("2026-09-25 " + "x" * 128 + ".mp4", "at most 142 characters (143 now)"),
        ("2026-09-25 " + "в" * 63 + ".mp4", None),
        ("2026-09-25 " + "в" * 64 + ".mp4", "at most 142 bytes in UTF-8 (143 now)"),
    ],
)
def test_get_filename_length_problem(video_filename, problem):
    assert metadata.get_filename_length_problem(video_filename) == (
        None if problem is None else f"use {problem}"
    )


def test_longest_allowed_video_filename_fits_everywhere(make_track, store):
    # GIVEN a track of a video with the longest allowed name, with a preview
    video_filename = "2026-09-25 " + "x" * 127 + ".mp4"
    assert metadata.get_filename_length_problem(video_filename) is None
    track = make_track(video_filename=video_filename)
    store.save_track(track)
    store.previews_dir.mkdir(parents=True)
    store.preview_path(track.stem).write_bytes(b"preview")

    # WHEN moving it to the trash
    moved_paths = store.move_to_trash(track.stem)

    # THEN no filename, in the tracks or in the trash, exceeds the limit
    names = [store.track_path(track.stem).name, store.preview_path(track.stem).name]
    names += [path.name for path in moved_paths] + [moved_paths[0].parent.name]
    assert max(len(name.encode("utf-8")) for name in names) <= metadata.MAX_FILENAME_BYTES


def test_find_videos_lists_trip_videos_by_name(tmp_path):
    # GIVEN trip videos, a hidden file, a non-video file, and a directory
    for name in ("b.mp4", "A.MOV", "c.avi", "._b.mp4", "notes.txt"):
        (tmp_path / name).write_bytes(b"video")
    (tmp_path / "d.mp4").mkdir()

    # WHEN finding videos
    video_paths = metadata.find_videos(tmp_path)

    # THEN only the videos are found, sorted by their exact names
    assert [path.name for path in video_paths] == ["A.MOV", "b.mp4", "c.avi"]


def test_find_videos_announces_the_scan(tmp_path, monkeypatch, caplog):
    # GIVEN a library directory that is slow to list
    caplog.set_level(logging.INFO)
    messages_before_listing = []
    original_iterdir = Path.iterdir

    def recording_iterdir(path):
        messages_before_listing.extend(record.getMessage() for record in caplog.records)
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", recording_iterdir)

    # WHEN finding videos
    metadata.find_videos(tmp_path)

    # THEN the scan is logged before the directory is listed
    assert messages_before_listing == [f"Scanning '{tmp_path}' for videos"]


def test_write_text_atomically_replaces_the_file(tmp_path):
    # GIVEN an existing file
    path = tmp_path / "index.json"
    path.write_text("old", encoding="utf-8")

    # WHEN writing it atomically
    metadata.write_text_atomically(path, "new")

    # THEN it has the new text, and no temporary file is left
    assert path.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.iterdir()) == [path]


def test_write_text_atomically_keeps_the_old_file_on_failure(tmp_path, monkeypatch):
    # GIVEN an existing file, and a disk that fails to flush
    path = tmp_path / "index.json"
    path.write_text("old", encoding="utf-8")

    def failing_fsync(file_descriptor):
        raise OSError("disk full")

    monkeypatch.setattr(metadata.os, "fsync", failing_fsync)

    # WHEN writing it atomically
    with pytest.raises(OSError, match="disk full"):
        metadata.write_text_atomically(path, "new")

    # THEN the old text is kept, and no temporary file is left
    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.iterdir()) == [path]
