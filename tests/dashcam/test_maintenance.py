import dataclasses
import json
import logging

import pytest

from dashcam import cleaning
from dashcam import enrich
from dashcam import maintenance
from dashcam import metadata
from dashcam import osm
from dashcam import video


@pytest.fixture(autouse=True)
def info_logs(caplog):
    caplog.set_level(logging.INFO)


@pytest.fixture
def video_dir(tmp_path):
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    return video_dir


@pytest.fixture
def store(tmp_path):
    store = metadata.MetadataStore(tmp_path / ".metadata")
    store.ensure_dirs()
    return store


@pytest.fixture
def add_trip(video_dir, store, make_track):
    """Create a video file and a matching track."""

    def _add_trip(name, content=b"video"):
        video_path = video_dir / name
        video_path.write_bytes(content)
        stat = video_path.stat()
        track = make_track(video_filename=name)
        track.fingerprint = video.compute_fingerprint(video_path)
        track.video_size = stat.st_size
        track.video_mtime = stat.st_mtime
        store.save_track(track)
        return video_path, track

    return _add_trip


def run_doctor(video_dir, store, apply_fixes=False):
    return maintenance.run_doctor(
        video_dir, store.root, max_interpolation_gap_s=60, apply_fixes=apply_fixes
    )


def test_run_doctor_with_healthy_library(video_dir, store, add_trip, caplog):
    # GIVEN a video with a matching track and a current index
    add_trip("2026-09-25 Trip.mp4")
    store.rebuild_index(60)

    # WHEN running the doctor
    error_count = run_doctor(video_dir, store)

    # THEN no problems are found
    assert error_count == 0
    assert "No problems found" in caplog.text


def test_run_doctor_reports_unreadable_track(video_dir, store, caplog):
    # GIVEN a broken track file
    store.track_path("2026-09-25 Trip").write_text("{\n  oops\n}", encoding="utf-8")

    # WHEN running the doctor
    error_count = run_doctor(video_dir, store)

    # THEN the error is reported with its line
    assert error_count == 1
    assert "line 2" in caplog.text


def test_run_doctor_fixes_track_filename(video_dir, store, add_trip):
    # GIVEN a track file renamed by hand so that it no longer matches its video
    add_trip("2026-09-25 Trip.mp4")
    store.track_path("2026-09-25 Trip").rename(store.track_path("wrong name"))

    # WHEN running the doctor with fixes
    error_count = run_doctor(video_dir, store, apply_fixes=True)

    # THEN the track file gets its proper name back
    assert error_count == 0
    assert store.track_path("2026-09-25 Trip").exists()
    assert not store.track_path("wrong name").exists()


def test_run_doctor_reconnects_renamed_video(video_dir, store, add_trip):
    # GIVEN a video renamed by hand
    video_path, _ = add_trip("2026-09-25 Trip.mp4")
    video_path.rename(video_dir / "2026-09-25 Horodotska (CX-5).mp4")

    # WHEN running the doctor with fixes
    run_doctor(video_dir, store, apply_fixes=True)

    # THEN the track follows the video and the index is rebuilt
    assert store.load_track("2026-09-25 Horodotska (CX-5)").video_filename == (
        "2026-09-25 Horodotska (CX-5).mp4"
    )
    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert [trip["id"] for trip in index["trips"]] == ["2026-09-25 Horodotska (CX-5)"]


def test_run_doctor_without_fix_changes_nothing(video_dir, store, add_trip, caplog):
    # GIVEN a video renamed by hand
    video_path, _ = add_trip("2026-09-25 Trip.mp4")
    video_path.rename(video_dir / "2026-09-25 Renamed.mp4")

    # WHEN running the doctor without fixes
    run_doctor(video_dir, store)

    # THEN the rename is reported as fixable but not applied
    assert store.track_path("2026-09-25 Trip").exists()
    assert "fixable: rename its track and preview" in caplog.text
    assert "doctor --fix" in caplog.text


def test_run_doctor_reports_changed_video(video_dir, store, add_trip, caplog):
    # GIVEN a video whose content changed after extraction
    video_path, _ = add_trip("2026-09-25 Trip.mp4")
    video_path.write_bytes(b"new content")

    # WHEN running the doctor
    run_doctor(video_dir, store)

    # THEN re-extraction is suggested
    assert "changed since extraction" in caplog.text


def test_run_doctor_reports_video_without_track_and_unreachable_track(
    video_dir, store, add_trip, caplog
):
    # GIVEN a new video, and a track whose video was deleted
    (video_dir / "2026-09-26 New.mp4").write_bytes(b"new")
    old_path, _ = add_trip("2026-09-25 Trip.mp4")
    old_path.unlink()

    # WHEN running the doctor
    run_doctor(video_dir, store)

    # THEN both are reported
    assert "has no track" in caplog.text
    assert "unreachable" in caplog.text


def test_run_doctor_reports_duplicate_fingerprints(video_dir, store, add_trip, caplog):
    # GIVEN two tracks of the same video content
    add_trip("2026-09-25 Trip.mp4", content=b"same")
    add_trip("2026-09-25 Copy.mp4", content=b"same")

    # WHEN running the doctor
    run_doctor(video_dir, store)

    # THEN the duplicate is reported
    assert "share the same video content" in caplog.text


def test_run_doctor_warns_about_track_without_accepted_fix(video_dir, store, add_trip, caplog):
    # GIVEN a track with GPS text in which every fix was rejected
    _, track = add_trip("2024-02-11 Night.mp4")
    for sample in track.clean_samples:
        sample.status = cleaning.STATUS_SPOOFED
    store.save_track(track)

    # WHEN running the doctor
    run_doctor(video_dir, store)

    # THEN the filename date is mentioned as a possible cause
    assert "check the filename" in caplog.text


def test_run_doctor_reports_outdated_track(video_dir, store, add_trip, caplog):
    # GIVEN a track made by an older cleaning version
    _, track = add_trip("2026-09-25 Trip.mp4")
    track.cleaning_version = 0
    store.save_track(track)

    # WHEN running the doctor
    run_doctor(video_dir, store)

    # THEN recleaning is suggested
    assert "--reclean" in caplog.text


def test_run_doctor_deletes_leftover_files(video_dir, store, add_trip):
    # GIVEN a preview without a track and a temporary file from an interrupted run
    add_trip("2026-09-25 Trip.mp4")
    store.previews_dir.mkdir()
    orphan_preview_path = store.preview_path("2026-01-01 Deleted")
    orphan_preview_path.write_bytes(b"preview")
    partial_path = store.tracks_dir / ".2026-09-25 Other.json.partial"
    partial_path.write_text("{", encoding="utf-8")

    # WHEN running the doctor with fixes
    run_doctor(video_dir, store, apply_fixes=True)

    # THEN both are deleted
    assert not orphan_preview_path.exists()
    assert not partial_path.exists()


def test_run_doctor_deletes_leftover_osm_download(video_dir, store, add_trip):
    # GIVEN a partial OSM download from an interrupted `enrich --update-osm`
    add_trip("2026-09-25 Trip.mp4")
    osm_dir = store.root / osm.OSM_DIR_NAME
    osm_dir.mkdir()
    download_path = osm_dir / ".download.partial.osm.pbf"
    download_path.write_bytes(b"pbf")

    # WHEN running the doctor with fixes
    run_doctor(video_dir, store, apply_fixes=True)

    # THEN the download is deleted
    assert not download_path.exists()


def test_run_doctor_reports_missing_street_list(
    video_dir, store, add_trip, osm_metadata_dir, caplog
):
    # GIVEN OSM data and a track that was never enriched
    add_trip("2026-09-25 Trip.mp4")

    # WHEN running the doctor
    run_doctor(video_dir, store)

    # THEN enriching is suggested
    assert "has no street list (run `dashcam enrich`)" in caplog.text


def test_run_doctor_reports_street_list_from_older_osm_data(
    video_dir, store, add_trip, osm_metadata_dir, road_database, caplog
):
    # GIVEN a track enriched against OSM data other than the current one
    _, track = add_trip("2026-09-25 Trip.mp4")
    enrich.enrich_track(track, road_database)
    track.enrichment = dataclasses.replace(track.enrichment, osm_timestamp="2025-01-01T00:00:00Z")
    store.save_track(track)

    # WHEN running the doctor
    run_doctor(video_dir, store)

    # THEN enriching is suggested
    assert "has an outdated street list (run `dashcam enrich`)" in caplog.text


def test_run_doctor_accepts_current_street_list(
    video_dir, store, add_trip, osm_metadata_dir, road_database, caplog
):
    # GIVEN a track enriched against the current OSM data
    _, track = add_trip("2026-09-25 Trip.mp4")
    enrich.enrich_track(track, road_database)
    store.save_track(track)
    store.rebuild_index(60)

    # WHEN running the doctor
    error_count = run_doctor(video_dir, store)

    # THEN no problems are found
    assert error_count == 0
    assert "No problems found" in caplog.text


def test_run_doctor_rebuilds_stale_index(video_dir, store, add_trip):
    # GIVEN an index that does not list an existing track
    add_trip("2026-09-25 Trip.mp4")
    store.index_path.write_text('{"trips": []}', encoding="utf-8")

    # WHEN running the doctor with fixes
    run_doctor(video_dir, store, apply_fixes=True)

    # THEN the index lists the track
    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert [trip["id"] for trip in index["trips"]] == ["2026-09-25 Trip"]


def test_forget_trips(store, add_trip):
    # GIVEN a trip
    add_trip("2026-09-25 Trip.mp4")

    # WHEN forgetting it by video filename, along with an unknown name
    missing_count = maintenance.forget_trips(
        ["2026-09-25 Trip.mp4", "unknown"], store.root, max_interpolation_gap_s=60
    )

    # THEN its track is in the trash and the unknown name is reported
    assert missing_count == 1
    assert not store.track_path("2026-09-25 Trip").exists()
    assert len(list(store.trash_dir.iterdir())) == 1


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("2026-09-25 Trip.mp4", "2026-09-25 Trip"),
        ("2026-09-25 Trip.json", "2026-09-25 Trip"),
        ("2026-09-25 Trip", "2026-09-25 Trip"),
        ("/videos/2026-09-25 Trip.MOV", "2026-09-25 Trip"),
        ("2026-09-25 Trip (CX-5)", "2026-09-25 Trip (CX-5)"),
    ],
)
def test_resolve_stem(name, expected):
    # GIVEN a name given on the command line

    # WHEN resolving it
    stem = maintenance.resolve_stem(name)

    # THEN the video stem is returned
    assert stem == expected


def test_print_status(video_dir, store, add_trip, make_track, capsys):
    # GIVEN a trip, an unprocessed video, a video without an overlay, and an unreachable track
    add_trip("2026-09-25 Trip.mp4")
    (video_dir / "2026-09-26 New.mp4").write_bytes(b"new")
    _, no_overlay_track = add_trip("2019-01-01 Old Camera.mp4")
    no_overlay_track.extraction_status = metadata.EXTRACTION_NO_OVERLAY
    store.save_track(no_overlay_track)
    store.save_track(make_track(video_filename="2026-01-01 Deleted.mp4"))

    # WHEN printing the status
    maintenance.print_status(video_dir, store.root, max_interpolation_gap_s=60)

    # THEN every group is listed
    output = capsys.readouterr().out
    assert "Trips (2):" in output
    assert "Videos not extracted yet" in output and "2026-09-26 New.mp4" in output
    assert "Videos without an overlay" in output and "2019-01-01 Old Camera.mp4" in output
    assert "not in" in output and "2026-01-01 Deleted.mp4" in output


def test_format_duration():
    # GIVEN a duration of 1 hour, 2 minutes, and 3 seconds

    # WHEN formatting it
    text = maintenance.format_duration(3723)

    # THEN it is shown as H:MM:SS
    assert text == "1:02:03"
