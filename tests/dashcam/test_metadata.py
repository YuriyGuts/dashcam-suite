import datetime
import json

import pytest

from dashcam import cleaning
from dashcam import metadata


@pytest.fixture
def store(tmp_path):
    return metadata.MetadataStore(tmp_path / ".metadata")


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
    track.streets = [{"name": "Городоцька", "distance_m": 900}]
    track.localities = {"start": {"name": "Львів", "place": "city"}, "end": None}
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
    assert "Городоцька" in metadata.dump_track(track)


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


def test_parse_trip_name():
    # GIVEN a video filename with a date and a name

    # WHEN parsing it
    trip_name = metadata.parse_trip_name("2026-09-25 Horodotska, M06 (CX-5).mp4")

    # THEN the date and name are split
    assert trip_name == metadata.TripName(datetime.date(2026, 9, 25), "Horodotska, M06 (CX-5)")


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
    stats = metadata.compute_trip_stats(track, max_interpolation_gap_s=60)

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
    stats = metadata.compute_trip_stats(track, max_interpolation_gap_s=60)

    # THEN no distance is counted across the gap
    assert stats["distance_km"] == 0
    assert stats["coverage"] == pytest.approx(0.667, abs=0.001)


def test_compute_trip_stats_without_samples(make_track):
    # GIVEN a track without samples
    track = make_track(sample_count=0)

    # WHEN computing the stats
    stats = metadata.compute_trip_stats(track, max_interpolation_gap_s=60)

    # THEN the stats are empty
    assert stats["start_time"] is None
    assert stats["coverage"] == 0.0
    assert stats["bbox"] is None


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


def test_metadata_store_move_to_trash(make_track, store):
    # GIVEN a track with a preview
    track = make_track()
    store.save_track(track)
    store.previews_dir.mkdir(parents=True)
    store.preview_path(track.stem).write_bytes(b"preview")

    # WHEN moving it to the trash
    moved_paths = store.move_to_trash(track.stem)

    # THEN both files are in the trash
    assert len(moved_paths) == 2
    assert all(path.parent == store.trash_dir for path in moved_paths)
    assert not store.track_path(track.stem).exists()
    assert not store.preview_path(track.stem).exists()


def test_metadata_store_rename_trip(make_track, store):
    # GIVEN a track with a preview
    track = make_track()
    store.save_track(track)
    store.previews_dir.mkdir(parents=True)
    store.preview_path(track.stem).write_bytes(b"preview")

    # WHEN its video is renamed
    store.rename_trip(track.stem, "2026-09-25 Horodotska (CX-5).mp4")

    # THEN the track and preview follow the new name
    renamed_track = store.load_track("2026-09-25 Horodotska (CX-5)")
    assert renamed_track.video_filename == "2026-09-25 Horodotska (CX-5).mp4"
    assert not store.track_path(track.stem).exists()
    assert store.preview_path("2026-09-25 Horodotska (CX-5)").read_bytes() == b"preview"


def test_metadata_store_rebuild_index(make_track, store):
    # GIVEN a good track, a track without an overlay, and an unreadable track
    store.save_track(make_track())
    no_overlay_track = make_track("2023-01-01 Old Camera.mp4", sample_count=0)
    no_overlay_track.extraction_status = metadata.EXTRACTION_NO_OVERLAY
    store.save_track(no_overlay_track)
    store.track_path("broken").write_text("{", encoding="utf-8")

    # WHEN rebuilding the index
    index = store.rebuild_index(max_interpolation_gap_s=60)

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
    track.localities = {"start": {"name": "Львів", "place": "city"}, "end": None}
    store.save_track(track)

    # WHEN rebuilding the index
    index = store.rebuild_index(60)

    # THEN the trip has the locality names
    assert index["trips"][0]["start_locality"] == "Львів"
    assert index["trips"][0]["end_locality"] is None


def test_metadata_store_list_track_paths_ignores_temporary_files(make_track, store):
    # GIVEN a track and a leftover temporary file
    store.save_track(make_track())
    (store.tracks_dir / ".2026-09-25 Other.json.partial").write_text("{", encoding="utf-8")

    # WHEN listing tracks
    paths = store.list_track_paths()

    # THEN only the track is listed
    assert [path.name for path in paths] == ["2026-09-25 Trip 11-17.json"]
