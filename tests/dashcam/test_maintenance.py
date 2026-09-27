import dataclasses
import json
import logging
import os
import time

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
def library_dir(tmp_path):
    library_dir = tmp_path / "videos"
    library_dir.mkdir()
    return library_dir


@pytest.fixture
def store(tmp_path):
    store = metadata.MetadataStore(tmp_path / ".metadata")
    store.ensure_dirs()
    return store


@pytest.fixture
def add_trip(library_dir, store, make_track):
    """Create a video file and a matching track."""

    def _add_trip(name, content=b"video"):
        video_path = library_dir / name
        video_path.write_bytes(content)
        stat = video_path.stat()
        track = make_track(video_filename=name)
        track.fingerprint = video.compute_fingerprint(video_path)
        track.video_size = stat.st_size
        track.video_mtime = stat.st_mtime
        store.save_track(track)
        return video_path, track

    return _add_trip


def run_doctor(library_dir, store, apply_fixes=False):
    return maintenance.run_doctor(library_dir, store.root, apply_fixes=apply_fixes)


def make_old(*paths):
    """Set the modification time of files to before `MIN_LEFTOVER_AGE_S`."""
    old_time = time.time() - maintenance.MIN_LEFTOVER_AGE_S - 60
    for path in paths:
        os.utime(path, (old_time, old_time))


def test_run_doctor_with_healthy_library(library_dir, store, add_trip, caplog):
    # GIVEN a video with a matching track and a current index
    add_trip("2026-09-25 Trip.mp4")
    store.rebuild_index()

    # WHEN running the doctor
    error_count = run_doctor(library_dir, store)

    # THEN no problems are found
    assert error_count == 0
    assert "No problems found" in caplog.text


def test_run_doctor_reports_unreadable_track(library_dir, store, caplog):
    # GIVEN a broken track file
    store.track_path("2026-09-25 Trip").write_text("{\n  oops\n}", encoding="utf-8")

    # WHEN running the doctor
    error_count = run_doctor(library_dir, store)

    # THEN the error is reported with its line
    assert error_count == 1
    assert "line 2" in caplog.text


def test_run_doctor_reports_track_in_other_encoding(library_dir, store, caplog):
    # GIVEN a track file that is not UTF-8 text
    store.track_path("2026-09-25 Trip").write_bytes('{"video_filename": "Вулиця"}'.encode("cp1251"))

    # WHEN running the doctor
    error_count = run_doctor(library_dir, store)

    # THEN the track is reported as unreadable
    assert error_count == 1
    assert "Not UTF-8" in caplog.text


def test_run_doctor_does_not_suggest_extracting_video_with_unreadable_track(
    library_dir, store, caplog
):
    # GIVEN a video whose track file is broken
    (library_dir / "2026-09-25 Trip.mp4").write_bytes(b"video")
    store.track_path("2026-09-25 Trip").write_text("{", encoding="utf-8")

    # WHEN running the doctor
    error_count = run_doctor(library_dir, store)

    # THEN only the broken track is reported
    assert error_count == 1
    assert "has no track" not in caplog.text


def test_run_doctor_fixes_track_filename(library_dir, store, add_trip):
    # GIVEN a track file renamed by hand so that it no longer matches its video
    add_trip("2026-09-25 Trip.mp4")
    store.track_path("2026-09-25 Trip").rename(store.track_path("wrong name"))

    # WHEN running the doctor with fixes
    error_count = run_doctor(library_dir, store, apply_fixes=True)

    # THEN the track file gets its proper name back
    assert error_count == 0
    assert store.track_path("2026-09-25 Trip").exists()
    assert not store.track_path("wrong name").exists()


def test_run_doctor_fixes_only_one_of_two_tracks_for_the_same_video(library_dir, store, add_trip):
    # GIVEN two copies of a track under wrong names, both belonging to the same video
    add_trip("2026-09-25 Trip.mp4")
    track_path = store.track_path("2026-09-25 Trip")
    copy_path = store.track_path("copy")
    copy_path.write_bytes(track_path.read_bytes())
    track_path.rename(store.track_path("wrong name"))

    # WHEN running the doctor with fixes
    error_count = run_doctor(library_dir, store, apply_fixes=True)

    # THEN one copy gets the proper name, and the other is kept and reported
    assert error_count == 1
    assert track_path.exists()
    assert len(store.list_track_paths()) == 2


def test_run_doctor_reconnects_renamed_video(library_dir, store, add_trip):
    # GIVEN a video renamed by hand
    video_path, _ = add_trip("2026-09-25 Trip.mp4")
    video_path.rename(library_dir / "2026-09-25 Khreshchatyk (Car).mp4")

    # WHEN running the doctor with fixes
    run_doctor(library_dir, store, apply_fixes=True)

    # THEN the track follows the video and the index is rebuilt
    assert store.load_track("2026-09-25 Khreshchatyk (Car)").video_filename == (
        "2026-09-25 Khreshchatyk (Car).mp4"
    )
    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert [trip["id"] for trip in index["trips"]] == ["2026-09-25 Khreshchatyk (Car)"]


def test_run_doctor_reconnects_renamed_video_of_misnamed_track(library_dir, store, add_trip):
    # GIVEN a renamed video whose track file was also renamed by hand
    video_path, _ = add_trip("2026-09-25 Trip.mp4")
    store.track_path("2026-09-25 Trip").rename(store.track_path("wrong name"))
    video_path.rename(library_dir / "2026-09-25 Khreshchatyk.mp4")

    # WHEN running the doctor with fixes
    error_count = run_doctor(library_dir, store, apply_fixes=True)

    # THEN both fixes apply in turn, and the track ends up with the video
    assert error_count == 0
    assert [path.stem for path in store.list_track_paths()] == ["2026-09-25 Khreshchatyk"]
    assert store.load_track("2026-09-25 Khreshchatyk").video_filename == (
        "2026-09-25 Khreshchatyk.mp4"
    )


def test_run_doctor_reports_a_failing_fix_and_continues(
    library_dir, store, add_trip, monkeypatch, caplog
):
    # GIVEN a renamed video whose track cannot be renamed, and a leftover file
    video_path, _ = add_trip("2026-09-25 Trip.mp4")
    video_path.rename(library_dir / "2026-09-25 Khreshchatyk.mp4")
    leftover_path = (
        library_dir / f"{metadata.PARTIAL_FILE_PREFIX}1234{metadata.PARTIAL_FILE_SUFFIX}"
    )
    leftover_path.write_bytes(b"partial")
    make_old(leftover_path)

    def locked_rename_trip(self, old_stem, new_video_filename):
        raise PermissionError("track is locked")

    monkeypatch.setattr(metadata.MetadataStore, "rename_trip", locked_rename_trip)

    # WHEN running the doctor with fixes
    run_doctor(library_dir, store, apply_fixes=True)

    # THEN the failure is reported, and the other fixes are still applied
    assert "Cannot fix: Video '2026-09-25 Trip' was renamed" in caplog.text
    assert "track is locked" in caplog.text
    assert not leftover_path.exists()


def test_run_doctor_without_fix_changes_nothing(library_dir, store, add_trip, caplog):
    # GIVEN a video renamed by hand
    video_path, _ = add_trip("2026-09-25 Trip.mp4")
    video_path.rename(library_dir / "2026-09-25 Renamed.mp4")

    # WHEN running the doctor without fixes
    run_doctor(library_dir, store)

    # THEN the rename is reported as fixable but not applied
    assert store.track_path("2026-09-25 Trip").exists()
    assert "fixable: rename its track and preview" in caplog.text
    assert "doctor --fix" in caplog.text


def test_run_doctor_reports_changed_video(library_dir, store, add_trip, caplog):
    # GIVEN a video whose content changed after extraction
    video_path, _ = add_trip("2026-09-25 Trip.mp4")
    video_path.write_bytes(b"new content")

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN re-extraction is suggested
    assert "changed since extraction" in caplog.text


def test_run_doctor_reports_video_without_track_and_unreachable_track(
    library_dir, store, add_trip, caplog
):
    # GIVEN a new video, and a track whose video was deleted
    (library_dir / "2026-09-26 New.mp4").write_bytes(b"new")
    old_path, _ = add_trip("2026-09-25 Trip.mp4")
    old_path.unlink()

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN both are reported
    assert "has no track" in caplog.text
    assert "unreachable" in caplog.text


def test_run_doctor_reports_duplicate_fingerprints(library_dir, store, add_trip, caplog):
    # GIVEN two tracks of the same video content
    add_trip("2026-09-25 Trip.mp4", content=b"same")
    add_trip("2026-09-25 Copy.mp4", content=b"same")

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN the duplicate is reported
    assert "share the same video content" in caplog.text


def test_run_doctor_reports_copy_of_video_with_track(library_dir, store, add_trip, caplog):
    # GIVEN a video with a track, and a copy of the video under another name
    video_path, _ = add_trip("2026-09-25 Trip.mp4")
    (library_dir / "2026-09-25 Trip copy.mp4").write_bytes(video_path.read_bytes())

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN the copy is reported as such, not as a video to extract
    assert "'2026-09-25 Trip copy.mp4' has the same content as '2026-09-25 Trip'" in caplog.text
    assert "has no track" not in caplog.text


def test_run_doctor_reports_videos_sharing_a_track(library_dir, store, add_trip, caplog):
    # GIVEN a video with a track, and another video whose name differs only in the extension
    add_trip("2026-09-25 Trip.mp4")
    (library_dir / "2026-09-25 Trip.mov").write_bytes(b"other")

    # WHEN running the doctor
    error_count = run_doctor(library_dir, store)

    # THEN the conflict is an error
    assert error_count == 1
    assert "'2026-09-25 Trip.mov', '2026-09-25 Trip.mp4' would share one track" in caplog.text


def test_run_doctor_reports_time_without_offset(library_dir, store, add_trip, caplog):
    # GIVEN a track with a hand-edited time without a UTC offset
    add_trip("2026-09-25 Trip.mp4")
    track_path = store.track_path("2026-09-25 Trip")
    text = track_path.read_text(encoding="utf-8")
    track_path.write_text(text.replace("11:17:01+03:00", "11:17:01", 1), encoding="utf-8")

    # WHEN running the doctor
    error_count = run_doctor(library_dir, store)

    # THEN the track is reported as unreadable
    assert error_count == 1
    assert "'time' needs a UTC offset" in caplog.text


def test_run_doctor_accepts_rebuilt_index_with_misnamed_track(library_dir, store, add_trip, caplog):
    # GIVEN a track and a copy of it under another name, and a rebuilt index
    add_trip("2026-09-25 Trip.mp4")
    track_path = store.track_path("2026-09-25 Trip")
    store.track_path("copy").write_bytes(track_path.read_bytes())
    store.rebuild_index()

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN the copy is reported, but the index is current
    assert "Track 'copy.json' belongs to '2026-09-25 Trip.mp4'" in caplog.text
    assert "trip index is missing or out of date" not in caplog.text


def test_run_doctor_warns_about_track_without_accepted_fix(library_dir, store, add_trip, caplog):
    # GIVEN a track with GPS text in which every fix was rejected
    _, track = add_trip("2024-02-11 Night.mp4")
    for sample in track.clean_samples:
        sample.status = cleaning.STATUS_SPOOFED
    store.save_track(track)

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN the filename date is mentioned as a possible cause
    assert "check the filename" in caplog.text


def test_run_doctor_reports_outdated_track(library_dir, store, add_trip, caplog):
    # GIVEN a track made by an older cleaning version
    _, track = add_trip("2026-09-25 Trip.mp4")
    track.cleaning_version = 0
    store.save_track(track)

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN recleaning is suggested
    assert "--reclean" in caplog.text


def test_run_doctor_ignores_cleaning_version_of_track_without_overlay(
    library_dir, store, add_trip, caplog
):
    # GIVEN a track of a video without an overlay, from an older cleaning version
    _, track = add_trip("2019-05-01 Trip.mp4")
    track.extraction_status = metadata.EXTRACTION_NO_OVERLAY
    track.raw_samples = []
    track.clean_samples = []
    track.cleaning_version = 0
    store.save_track(track)

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN nothing is suggested, since there are no readings to clean
    assert "older" not in caplog.text


def test_run_doctor_reports_track_from_older_extractor(library_dir, store, add_trip, caplog):
    # GIVEN a track made by an older extractor
    _, track = add_trip("2026-09-25 Trip.mp4")
    track.extractor_version = 0
    store.save_track(track)

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN re-extracting the video is suggested
    assert "`dashcam extract --only '2026-09-25 Trip.mp4'`" in caplog.text
    assert "--reclean" not in caplog.text


def test_run_doctor_keeps_recent_temporary_files(library_dir, store, add_trip, caplog):
    # GIVEN temporary files that a running job may still be writing
    add_trip("2026-09-25 Trip.mp4")
    partial_track_path = store.tracks_dir / ".tmp-0123abcd.partial"
    partial_track_path.write_text("{", encoding="utf-8")
    partial_video_path = library_dir / ".tmp-4567cdef.partial"
    partial_video_path.write_bytes(b"video")

    # WHEN running the doctor with fixes
    run_doctor(library_dir, store, apply_fixes=True)

    # THEN they are reported and kept
    assert partial_track_path.exists()
    assert partial_video_path.exists()
    assert f"Recent temporary file, possibly of a running job: {partial_video_path}" in caplog.text


def test_run_doctor_deletes_leftover_files(library_dir, store, add_trip):
    # GIVEN a preview without a track, and temporary files from interrupted runs
    add_trip("2026-09-25 Trip.mp4")
    store.previews_dir.mkdir()
    orphan_preview_path = store.preview_path("2026-01-01 Deleted")
    orphan_preview_path.write_bytes(b"preview")
    partial_track_path = store.tracks_dir / ".tmp-0123abcd.partial"
    partial_track_path.write_text("{", encoding="utf-8")
    partial_video_path = library_dir / ".tmp-4567cdef.partial"
    partial_video_path.write_bytes(b"video")
    make_old(partial_track_path, partial_video_path)

    # WHEN running the doctor with fixes
    run_doctor(library_dir, store, apply_fixes=True)

    # THEN they are deleted, and the video is kept
    assert not orphan_preview_path.exists()
    assert not partial_track_path.exists()
    assert not partial_video_path.exists()
    assert (library_dir / "2026-09-25 Trip.mp4").exists()


def test_run_doctor_deletes_leftover_osm_download(library_dir, store, add_trip):
    # GIVEN a partial OSM download from an interrupted `enrich --update-osm`
    add_trip("2026-09-25 Trip.mp4")
    osm_dir = store.root / osm.OSM_DIR_NAME
    osm_dir.mkdir()
    download_path = osm_dir / ".download.partial.osm.pbf"
    download_path.write_bytes(b"pbf")
    make_old(download_path)

    # WHEN running the doctor with fixes
    run_doctor(library_dir, store, apply_fixes=True)

    # THEN the download is deleted
    assert not download_path.exists()


def test_run_doctor_reports_too_long_video_name(library_dir, store, add_trip, caplog):
    # GIVEN a video whose track name would not fit on an encrypted NAS folder
    add_trip("2026-09-25 Trip.mp4")
    store.rebuild_index()
    long_name = "2026-09-25 " + "x" * 128 + ".mp4"
    (library_dir / long_name).write_bytes(b"long")

    # WHEN running the doctor
    error_count = run_doctor(library_dir, store)

    # THEN it is reported as an error, without suggesting to extract it
    assert error_count == 1
    assert f"Video '{long_name}' has a name that is too long" in caplog.text
    assert "run `dashcam extract`" not in caplog.text


def test_run_doctor_reports_missing_street_list(
    library_dir, store, add_trip, osm_metadata_dir, caplog
):
    # GIVEN OSM data and a track that was never enriched
    add_trip("2026-09-25 Trip.mp4")

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN enriching is suggested
    assert "has no street list (run `dashcam enrich`)" in caplog.text


def test_run_doctor_reports_street_list_from_older_osm_data(
    library_dir, store, add_trip, osm_metadata_dir, road_database, caplog
):
    # GIVEN a track enriched against OSM data other than the current one
    _, track = add_trip("2026-09-25 Trip.mp4")
    enrich.enrich_track(track, road_database)
    track.enrichment = dataclasses.replace(track.enrichment, osm_timestamp="2025-01-01T00:00:00Z")
    store.save_track(track)

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN enriching is suggested
    assert "has an outdated street list (run `dashcam enrich`)" in caplog.text


def test_run_doctor_reports_street_list_from_another_osm_region(
    library_dir, store, add_trip, osm_metadata_dir, road_database, caplog
):
    # GIVEN a track enriched against another region's extract from the same day
    _, track = add_trip("2026-09-25 Trip.mp4")
    enrich.enrich_track(track, road_database)
    track.enrichment = dataclasses.replace(track.enrichment, osm_source="poland-latest.osm.pbf")
    store.save_track(track)

    # WHEN running the doctor
    run_doctor(library_dir, store)

    # THEN enriching is suggested
    assert "has an outdated street list (run `dashcam enrich`)" in caplog.text


def test_run_doctor_accepts_current_street_list(
    library_dir, store, add_trip, osm_metadata_dir, road_database, caplog
):
    # GIVEN a track enriched against the current OSM data
    _, track = add_trip("2026-09-25 Trip.mp4")
    enrich.enrich_track(track, road_database)
    store.save_track(track)
    store.rebuild_index()

    # WHEN running the doctor
    error_count = run_doctor(library_dir, store)

    # THEN no problems are found
    assert error_count == 0
    assert "No problems found" in caplog.text


def test_run_doctor_rebuilds_stale_index(library_dir, store, add_trip):
    # GIVEN an index that does not list an existing track
    add_trip("2026-09-25 Trip.mp4")
    store.index_path.write_text('{"trips": []}', encoding="utf-8")

    # WHEN running the doctor with fixes
    run_doctor(library_dir, store, apply_fixes=True)

    # THEN the index lists the track
    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert [trip["id"] for trip in index["trips"]] == ["2026-09-25 Trip"]


def test_forget_trips(store, add_trip):
    # GIVEN a trip
    add_trip("2026-09-25 Trip.mp4")

    # WHEN forgetting it by video filename, along with an unknown name
    missing_count = maintenance.forget_trips(["2026-09-25 Trip.mp4", "unknown"], store.root)

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
        ("2026-09-25 Trip (Car)", "2026-09-25 Trip (Car)"),
    ],
)
def test_resolve_stem(name, expected):
    # GIVEN a name given on the command line

    # WHEN resolving it
    stem = maintenance.resolve_stem(name)

    # THEN the video stem is returned
    assert stem == expected


def test_print_status(library_dir, store, add_trip, make_track, capsys):
    # GIVEN a trip, an unprocessed video, a video without an overlay, and an unreachable track
    add_trip("2026-09-25 Trip.mp4")
    (library_dir / "2026-09-26 New.mp4").write_bytes(b"new")
    _, no_overlay_track = add_trip("2019-01-01 Old Camera.mp4")
    no_overlay_track.extraction_status = metadata.EXTRACTION_NO_OVERLAY
    store.save_track(no_overlay_track)
    store.save_track(make_track(video_filename="2026-01-01 Deleted.mp4"))

    # WHEN printing the status
    maintenance.print_status(library_dir, store.root)

    # THEN every group is listed
    output = capsys.readouterr().out
    assert "Trips (2)" in output
    assert "Videos not extracted yet" in output and "2026-09-26 New.mp4" in output
    assert "Videos without an overlay" in output and "2019-01-01 Old Camera.mp4" in output
    assert "not in" in output and "2026-01-01 Deleted.mp4" in output


def test_print_status_lists_video_with_unreadable_track_once(library_dir, store, capsys):
    # GIVEN a video whose track file is broken
    (library_dir / "2026-09-25 Trip.mp4").write_bytes(b"video")
    store.track_path("2026-09-25 Trip").write_text("{", encoding="utf-8")

    # WHEN printing the status
    maintenance.print_status(library_dir, store.root)

    # THEN the track is listed as unreadable, and the video is not listed as not extracted
    output = capsys.readouterr().out
    assert "Unreadable tracks" in output
    assert "Videos not extracted yet" not in output


def test_format_duration():
    # GIVEN a duration of 1 hour, 2 minutes, and 3 seconds

    # WHEN formatting it
    text = maintenance.format_duration(3723)

    # THEN it is shown as H:MM:SS
    assert text == "1:02:03"


@pytest.mark.parametrize(
    ("coverage_percent", "expected_style"),
    [
        (100, "green"),
        (90, "green"),
        (89.5, "yellow"),
        (50, "yellow"),
        (49.9, "red"),
        (0, "red"),
    ],
)
def test_get_coverage_style(coverage_percent, expected_style):
    assert maintenance.get_coverage_style(coverage_percent) == expected_style


def test_run_doctor_reads_each_track_once_without_fixes(library_dir, store, add_trip, monkeypatch):
    # GIVEN a healthy library
    add_trip("2026-09-25 Trip.mp4")
    store.rebuild_index()
    loaded_paths = []
    original_iter_track_files = metadata.MetadataStore.iter_track_files

    def recording_iter_track_files(self):
        for track_file in original_iter_track_files(self):
            loaded_paths.append(track_file.path)
            yield track_file

    monkeypatch.setattr(metadata.MetadataStore, "iter_track_files", recording_iter_track_files)

    # WHEN running the doctor
    error_count = run_doctor(library_dir, store)

    # THEN the track is read only once, and the index is found current
    assert error_count == 0
    assert loaded_paths == [store.track_path("2026-09-25 Trip")]
