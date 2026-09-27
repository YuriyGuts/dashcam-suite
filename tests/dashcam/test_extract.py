import contextlib
import dataclasses
import json
import logging
import os
import shutil
from pathlib import Path

import pytest

from dashcam import cleaning
from dashcam import extract
from dashcam import metadata
from dashcam import overlay
from dashcam import video

OVERLAY_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "overlay"
# A 6-second clip of a real trip, with the picture above the overlay blacked out.
SAMPLE_VIDEO_PATH = Path(__file__).parents[1] / "fixtures" / "video" / "2026-09-23 Sample Trip.mp4"


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
def make_video_file(library_dir):
    def _make_video_file(name, content=b"video content"):
        path = library_dir / name
        path.write_bytes(content)
        return path

    return _make_video_file


@pytest.fixture
def save_track_for(store, make_track):
    """Save a track that matches an existing video file."""

    def _save_track_for(video_path, **changes):
        stat = video_path.stat()
        track = make_track(video_filename=video_path.name)
        track.fingerprint = video.compute_fingerprint(video_path)
        track.video_size = stat.st_size
        track.video_mtime = stat.st_mtime
        for name, value in changes.items():
            setattr(track, name, value)
        store.save_track(track)
        return track

    return _save_track_for


def make_reading(gps_text, clock_text, gps_score=0.95, clock_score=0.95):
    return overlay.OverlayReading(
        gps_text=gps_text,
        clock_text=clock_text,
        gps_min_score=gps_score,
        clock_min_score=clock_score,
    )


def test_filter_videos_with_include_and_exclude(tmp_path):
    # GIVEN trip videos from two years
    video_paths = [
        tmp_path / name
        for name in ("2026-09-25 Trip.mp4", "2026-09-26 Trip.MOV", "2019-01-01 Old Camera.mp4")
    ]

    # WHEN keeping the videos from 2026 only, without MOV files
    kept_paths = extract.filter_videos(video_paths, include=["2026-*"], exclude=["*.MOV"])

    # THEN only the matching trip video is kept
    assert [path.name for path in kept_paths] == ["2026-09-25 Trip.mp4"]


def test_filter_videos_without_patterns(tmp_path):
    # GIVEN two videos
    video_paths = [tmp_path / "a.mov", tmp_path / "b.mp4"]

    # WHEN filtering them without patterns
    kept_paths = extract.filter_videos(video_paths, include=[], exclude=[])

    # THEN all are kept
    assert kept_paths == video_paths


def test_collapse_readings_keeps_one_sample_per_clock_tick():
    # GIVEN two readings per second, where the GPS text updates in the second half of a tick
    readings = [
        (0.0, make_reading("10 KM/H N49.800000 E24.000000", "2026/09/25 10:00:00")),
        (0.5, make_reading("11 KM/H N49.800100 E24.000000", "2026/09/25 10:00:00")),
        (1.0, make_reading("12 KM/H N49.800200 E24.000000", "2026/09/25 10:00:01")),
    ]

    # WHEN collapsing them
    raw_samples = extract.collapse_readings(readings)

    # THEN each tick starts at its first frame and uses its last GPS reading
    assert [(sample.t, sample.gps_text) for sample in raw_samples] == [
        (0.0, "11 KM/H N49.800100 E24.000000"),
        (1.0, "12 KM/H N49.800200 E24.000000"),
    ]


def test_collapse_readings_prefers_reliable_gps_text():
    # GIVEN a tick whose last reading is unreliable
    readings = [
        (0.0, make_reading("10 KM/H N49.800000 E24.000000", "2026/09/25 10:00:00")),
        (0.5, make_reading("169 KM/H N24.000110 E0.248983", "2026/09/25 10:00:00", gps_score=0.2)),
    ]

    # WHEN collapsing them
    raw_samples = extract.collapse_readings(readings)

    # THEN the reliable reading is used
    assert raw_samples[0].gps_text == "10 KM/H N49.800000 E24.000000"
    assert raw_samples[0].gps_score == 0.95


def test_collapse_readings_with_repeated_clock_later():
    # GIVEN a camera clock that shows the same value again after other values (spoofed loop)
    readings = [
        (0.0, make_reading("", "2028/03/10 07:00:00")),
        (1.0, make_reading("", "2028/03/10 07:00:01")),
        (2.0, make_reading("", "2028/03/10 07:00:00")),
    ]

    # WHEN collapsing them
    raw_samples = extract.collapse_readings(readings)

    # THEN only consecutive readings are merged
    assert len(raw_samples) == 3


def test_collapse_readings_without_readings():
    # GIVEN no readings

    # WHEN collapsing them
    raw_samples = extract.collapse_readings([])

    # THEN there are no samples
    assert raw_samples == []


@pytest.fixture
def fake_strips(monkeypatch):
    """Serve fixture strips instead of decoding a video."""
    strips = {"frames": []}

    def fake_iter_overlay_strips(
        path, width, sample_fps, ffmpeg_executable, hwaccel_options, start_s=0.0, duration_s=None
    ):
        for offset_s, strip in strips["frames"]:
            if offset_s >= start_s and (duration_s is None or offset_s < start_s + duration_s):
                yield offset_s, strip

    monkeypatch.setattr("dashcam.extract.video.iter_overlay_strips", fake_iter_overlay_strips)
    return strips


def test_probe_overlay_with_camera_clock(fake_strips, config):
    # GIVEN a video whose frames show the camera clock
    strip = overlay.read_strip_image(OVERLAY_FIXTURE_DIR / "2026-09-23_60s.png")
    fake_strips["frames"] = [(offset_s / 2, strip) for offset_s in range(20)]
    video_info = video.VideoInfo(width=2560, height=1440, duration_s=10)

    # WHEN probing it
    has_overlay = extract.probe_overlay(Path("trip.mp4"), video_info, config)

    # THEN the overlay is found
    assert has_overlay


def test_probe_overlay_with_narrow_video(fake_strips, config):
    # GIVEN a video narrower than the overlay can be read at, whose frames show the camera clock
    strip = overlay.read_strip_image(OVERLAY_FIXTURE_DIR / "2026-09-23_60s.png")
    fake_strips["frames"] = [(offset_s / 2, strip) for offset_s in range(20)]
    video_info = video.VideoInfo(width=960, height=540, duration_s=10)

    # WHEN probing it
    has_overlay = extract.probe_overlay(Path("small.mp4"), video_info, config)

    # THEN it is treated as having no overlay
    assert not has_overlay


def test_probe_overlay_without_camera_clock(fake_strips, config):
    # GIVEN a video without any overlay
    strip = overlay.read_strip_image(OVERLAY_FIXTURE_DIR / "2026-09-23_60s.png") * 0 + 90
    fake_strips["frames"] = [(offset_s / 2, strip) for offset_s in range(20)]
    video_info = video.VideoInfo(width=2560, height=1440, duration_s=10)

    # WHEN probing it
    has_overlay = extract.probe_overlay(Path("old camera.mp4"), video_info, config)

    # THEN no overlay is found
    assert not has_overlay


def test_probe_overlay_without_camera_clock_and_with_missing_frames(fake_strips, config):
    # GIVEN a video without a readable clock whose second half cannot be decoded
    strip = overlay.read_strip_image(OVERLAY_FIXTURE_DIR / "2026-09-23_60s.png") * 0 + 90
    fake_strips["frames"] = [(offset_s / 2, strip) for offset_s in range(10)]
    video_info = video.VideoInfo(width=2560, height=1440, duration_s=10)

    # WHEN probing it
    # THEN it fails instead of reporting no overlay
    with pytest.raises(RuntimeError, match="no frame at 5 of 10 probe points"):
        extract.probe_overlay(Path("glitch.mp4"), video_info, config)


def test_probe_overlay_with_camera_clock_and_missing_frames(fake_strips, config):
    # GIVEN a video whose first half shows the camera clock and whose second half cannot be decoded
    strip = overlay.read_strip_image(OVERLAY_FIXTURE_DIR / "2026-09-23_60s.png")
    fake_strips["frames"] = [(offset_s / 2, strip) for offset_s in range(10)]
    video_info = video.VideoInfo(width=2560, height=1440, duration_s=10)

    # WHEN probing it
    has_overlay = extract.probe_overlay(Path("trip.mp4"), video_info, config)

    # THEN the overlay is found
    assert has_overlay


def test_read_all_frames_logs_progress_at_fixed_interval(fake_strips, config, monkeypatch, caplog):
    # GIVEN a 10-minute video whose frames take 1 second each to read
    caplog.set_level(logging.INFO)
    strip = overlay.read_strip_image(OVERLAY_FIXTURE_DIR / "2026-09-23_60s.png")
    fake_strips["frames"] = [(offset_s * 60.0, strip) for offset_s in range(10)]
    wall_clock = {"now_s": 0.0}
    monkeypatch.setattr("dashcam.terminal.time.monotonic", lambda: wall_clock["now_s"])

    def slow_read_overlay(strip):
        wall_clock["now_s"] += 1
        return overlay.OverlayReading("", "2026/09/23 18:43:01", -1.0, 0.99)

    monkeypatch.setattr("dashcam.extract.overlay.read_overlay", slow_read_overlay)
    monkeypatch.setattr("dashcam.extract.PROGRESS_INTERVAL_S", 4)
    video_info = video.VideoInfo(width=2560, height=1440, duration_s=600)

    # WHEN reading all frames
    readings = extract.read_all_frames(Path("trip.mp4"), video_info, config)

    # THEN every frame is read, and the progress is logged every 4 seconds
    assert len(readings) == 10
    progress_messages = [
        record.getMessage()
        for record in caplog.records
        if getattr(record, "marker", None) == "progress"
    ]
    assert progress_messages == [
        "trip.mp4: 30% (3:00 of 10:00, 45.0x, <1 min left)",
        "trip.mp4: 70% (7:00 of 10:00, 52.5x, <1 min left)",
    ]


def test_plan_extraction_with_new_video(store, make_video_file):
    # GIVEN a video without a track
    video_path = make_video_file("2026-09-25 Trip.mp4")

    # WHEN planning
    planned = extract.plan_extraction([video_path], store, force=False)

    # THEN it is extracted
    assert [item.video_path for item in planned] == [video_path]


def test_plan_extraction_with_up_to_date_track(store, make_video_file, save_track_for):
    # GIVEN a video with a matching track
    video_path = make_video_file("2026-09-25 Trip.mp4")
    save_track_for(video_path)

    # WHEN planning
    planned = extract.plan_extraction([video_path], store, force=False)

    # THEN nothing is extracted
    assert planned == []


def test_plan_extraction_with_touched_video(store, make_video_file, save_track_for):
    # GIVEN a video whose modification time changed but whose content did not
    video_path = make_video_file("2026-09-25 Trip.mp4")
    save_track_for(video_path)
    os.utime(video_path, (1_800_000_000, 1_800_000_000))

    # WHEN planning
    planned = extract.plan_extraction([video_path], store, force=False)

    # THEN nothing is extracted and the track remembers the new modification time
    assert planned == []
    assert store.load_track(video_path.stem).video_mtime == 1_800_000_000


def test_plan_extraction_with_changed_content(store, make_video_file, save_track_for):
    # GIVEN a video whose content changed since extraction
    video_path = make_video_file("2026-09-25 Trip.mp4")
    save_track_for(video_path)
    video_path.write_bytes(b"different video content")

    # WHEN planning
    planned = extract.plan_extraction([video_path], store, force=False)

    # THEN it is extracted again, without the old overrides, and the old track is in the trash
    assert [item.video_path for item in planned] == [video_path]
    assert planned[0].previous_track is None
    assert not store.track_path(video_path.stem).exists()
    assert len(list(store.trash_dir.iterdir())) == 1


def test_plan_extraction_with_renamed_video(store, make_video_file, save_track_for):
    # GIVEN a track whose video was renamed
    old_path = make_video_file("2026-09-25 Trip 11-17.mp4")
    save_track_for(old_path)
    new_path = old_path.rename(old_path.with_name("2026-09-25 Khreshchatyk (Car).mp4"))

    # WHEN planning
    planned = extract.plan_extraction([new_path], store, force=False)

    # THEN nothing is extracted and the track follows the new name
    assert planned == []
    assert store.load_track(new_path.stem).video_filename == new_path.name
    assert not store.track_path(old_path.stem).exists()


def test_plan_extraction_with_duplicate_video(store, make_video_file, save_track_for):
    # GIVEN a video with a track and a copy of it under another name
    original_path = make_video_file("2026-09-25 Trip.mp4")
    save_track_for(original_path)
    copy_path = make_video_file("2026-09-25 Trip copy.mp4")

    # WHEN planning
    planned = extract.plan_extraction([original_path, copy_path], store, force=False)

    # THEN the copy is skipped and the original keeps its track
    assert planned == []
    assert store.track_path(original_path.stem).exists()
    assert not store.track_path(copy_path.stem).exists()


def test_plan_extraction_of_copy_alone_leaves_the_original_track(
    store, make_video_file, save_track_for
):
    # GIVEN a video with a track and a copy of it under another name
    original_path = make_video_file("2026-09-25 Trip.mp4")
    save_track_for(original_path)
    copy_path = make_video_file("2026-09-25 Trip copy.mp4")

    # WHEN planning only the copy, with the original still in the library
    planned = extract.plan_extraction(
        [copy_path], store, force=True, library_stems={original_path.stem, copy_path.stem}
    )

    # THEN the copy is skipped as a duplicate and the original keeps its track
    assert planned == []
    assert store.track_path(original_path.stem).exists()
    assert not store.track_path(copy_path.stem).exists()


def test_extract_videos_with_only_a_copy_leaves_the_original_track(
    config,
    library_dir,
    store,
    make_video_file,
    save_track_for,
    fake_extract_video,
    serial_extract_pool,
):
    # GIVEN a video with a track and a copy of it under another name
    original_path = make_video_file("2026-09-25 Trip.mp4")
    save_track_for(original_path)
    make_video_file("2026-09-25 Trip copy.mp4")

    # WHEN extracting only the copy
    extract.extract_videos(
        library_dir,
        store.root,
        config,
        include=[],
        exclude=[],
        only=["2026-09-25 Trip copy.mp4"],
        force=False,
        make_previews=False,
        job_count=2,
    )

    # THEN nothing is extracted and the original keeps its track
    assert fake_extract_video == []
    assert [path.stem for path in store.list_track_paths()] == [original_path.stem]


def test_plan_extraction_with_force_keeps_overrides(store, make_video_file, save_track_for):
    # GIVEN an up-to-date track with manual overrides
    video_path = make_video_file("2026-09-25 Trip.mp4")
    overrides = cleaning.Overrides(bad_ranges_s=[(10.0, 20.0)])
    save_track_for(video_path, overrides=overrides)

    # WHEN planning a forced re-extraction
    planned = extract.plan_extraction([video_path], store, force=True)

    # THEN it is extracted again with the same overrides
    assert [item.video_path for item in planned] == [video_path]
    assert planned[0].previous_track is not None
    assert planned[0].previous_track.overrides == overrides


def test_make_extract_job_keeps_previous_track_data(tmp_path, config, make_track):
    # GIVEN a planned re-extraction of a track with overrides and a street list
    previous_track = make_track()
    previous_track.streets = [{"name": "Khreshchatyk", "distance_m": 900}]
    previous_track.localities = {"start": {"name": "Kyiv", "place": "city"}, "end": None}
    previous_track.enrichment = metadata.Enrichment(
        enricher_version=1,
        osm_timestamp="2026-09-25T20:24:36Z",
        samples_digest="abc",
        enriched_at="2026-09-26T20:00:00+03:00",
    )
    planned = extract.PlannedExtraction(tmp_path / "trip.mp4", "100:abc", previous_track)

    # WHEN defining the job
    job_def = extract.make_extract_job(planned, tmp_path, config, make_preview=False)

    # THEN the job carries the overrides and the street data
    assert job_def.overrides == previous_track.overrides
    assert job_def.streets == previous_track.streets
    assert job_def.localities == previous_track.localities
    assert job_def.enrichment == previous_track.enrichment


def test_make_extract_job_for_new_video(tmp_path, config):
    # GIVEN a planned extraction without a previous track
    planned = extract.PlannedExtraction(tmp_path / "trip.mp4", "100:abc")

    # WHEN defining the job
    job_def = extract.make_extract_job(planned, tmp_path, config, make_preview=True)

    # THEN the job starts with no overrides and no street data
    assert job_def.overrides == cleaning.Overrides()
    assert job_def.streets == []
    assert job_def.enrichment is None
    assert job_def.make_preview


def test_plan_extraction_skips_unreadable_tracks(store, make_video_file):
    # GIVEN a video whose track file is broken
    video_path = make_video_file("2026-09-25 Trip.mp4")
    store.track_path(video_path.stem).write_text("{", encoding="utf-8")

    # WHEN planning
    planned = extract.plan_extraction([video_path], store, force=False)

    # THEN the video is extracted again
    assert [item.video_path for item in planned] == [video_path]


@pytest.fixture
def fake_extract_video(monkeypatch, make_track):
    """Replace video decoding with a synthetic track."""
    extracted_names = []

    def _fake_extract_video(job_def):
        extracted_names.append(job_def.video_path.name)
        track = make_track(video_filename=job_def.video_path.name, fingerprint=job_def.fingerprint)
        track.overrides = job_def.overrides
        return track

    monkeypatch.setattr("dashcam.extract.extract_video", _fake_extract_video)
    return extracted_names


@pytest.fixture
def serial_extract_pool(monkeypatch, serial_pool):
    monkeypatch.setattr("dashcam.extract.prevent_os_sleep", contextlib.nullcontext)


def test_extract_videos_extracts_new_videos_and_builds_index(
    config, library_dir, store, make_video_file, fake_extract_video, serial_extract_pool
):
    # GIVEN two new videos
    make_video_file("2026-09-25 Trip A.mp4", b"a")
    make_video_file("2026-09-25 Trip B.mp4", b"b")

    # WHEN extracting
    failed_count = extract.extract_videos(
        library_dir,
        store.root,
        config,
        include=[],
        exclude=[],
        only=[],
        force=False,
        make_previews=False,
        job_count=2,
    )

    # THEN both are extracted and listed in the index
    assert failed_count == 0
    assert sorted(fake_extract_video) == ["2026-09-25 Trip A.mp4", "2026-09-25 Trip B.mp4"]
    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert sorted(trip["id"] for trip in index["trips"]) == [
        "2026-09-25 Trip A",
        "2026-09-25 Trip B",
    ]


def test_extract_videos_with_only_forces_one_video(
    config,
    library_dir,
    store,
    make_video_file,
    save_track_for,
    fake_extract_video,
    serial_extract_pool,
):
    # GIVEN two videos with up-to-date tracks
    first_path = make_video_file("2026-09-25 Trip A.mp4", b"a")
    second_path = make_video_file("2026-09-25 Trip B.mp4", b"b")
    save_track_for(first_path)
    save_track_for(second_path)

    # WHEN re-extracting only one of them
    extract.extract_videos(
        library_dir,
        store.root,
        config,
        include=[],
        exclude=[],
        only=["2026-09-25 Trip B.mp4", "missing.mp4"],
        force=False,
        make_previews=False,
        job_count=2,
    )

    # THEN only that video is extracted
    assert fake_extract_video == ["2026-09-25 Trip B.mp4"]


def test_extract_videos_reports_failures(
    monkeypatch, config, library_dir, store, make_video_file, serial_extract_pool
):
    # GIVEN a video that cannot be decoded
    make_video_file("2026-09-25 Broken.mp4")

    def failing_extract_video(job_def):
        raise RuntimeError("ffmpeg failed")

    monkeypatch.setattr("dashcam.extract.extract_video", failing_extract_video)

    # WHEN extracting
    failed_count = extract.extract_videos(
        library_dir,
        store.root,
        config,
        include=[],
        exclude=[],
        only=[],
        force=False,
        make_previews=False,
        job_count=2,
    )

    # THEN the failure is counted and no track is written
    assert failed_count == 1
    assert store.list_track_paths() == []


def test_extract_videos_skips_too_long_names(
    config, library_dir, store, make_video_file, fake_extract_video, serial_extract_pool, caplog
):
    # GIVEN a video with a regular name and one whose track name would be too long
    make_video_file("2026-09-25 Trip.mp4", b"a")
    long_name = "2026-09-25 " + "x" * 128 + ".mp4"
    make_video_file(long_name, b"b")

    # WHEN extracting
    failed_count = extract.extract_videos(
        library_dir,
        store.root,
        config,
        include=[],
        exclude=[],
        only=[],
        force=False,
        make_previews=False,
        job_count=2,
    )

    # THEN only the regular video is extracted, and the long name is reported as a failure
    assert failed_count == 1
    assert fake_extract_video == ["2026-09-25 Trip.mp4"]
    assert f"Skipping '{long_name}': the name is too long" in caplog.text


def test_extract_videos_makes_missing_previews(
    monkeypatch, config, library_dir, store, make_video_file, save_track_for, serial_extract_pool
):
    # GIVEN a video with a track but without a preview
    video_path = make_video_file("2026-09-25 Trip.mp4")
    save_track_for(video_path)
    previews = []
    monkeypatch.setattr(
        "dashcam.extract.make_preview",
        lambda video_path, preview_path, config, duration_s: previews.append(
            (video_path, preview_path)
        ),
    )

    # WHEN extracting with previews
    extract.extract_videos(
        library_dir,
        store.root,
        config,
        include=[],
        exclude=[],
        only=[],
        force=False,
        make_previews=True,
        job_count=2,
    )

    # THEN only the preview is made
    assert previews == [(video_path, store.preview_path(video_path.stem))]


def test_extract_videos_with_previews_skips_videos_without_overlay(
    monkeypatch,
    config,
    library_dir,
    store,
    make_video_file,
    save_track_for,
    serial_extract_pool,
    caplog,
):
    # GIVEN an up-to-date track of a video without an overlay, and no preview
    video_path = make_video_file("2023-01-01 Old Camera.mp4")
    save_track_for(video_path, extraction_status=metadata.EXTRACTION_NO_OVERLAY)
    previews = []
    monkeypatch.setattr(
        "dashcam.extract.make_preview",
        lambda video_path, preview_path, config, duration_s: previews.append(video_path),
    )
    caplog.set_level(logging.INFO)

    # WHEN extracting with previews
    extract.extract_videos(
        library_dir,
        store.root,
        config,
        include=[],
        exclude=[],
        only=[],
        force=False,
        make_previews=True,
        job_count=2,
    )

    # THEN no job is started, and the library is reported as up to date
    assert previews == []
    assert "All tracks are up to date" in caplog.text


def test_make_preview_replaces_an_older_preview(fake_ffmpeg, config, tmp_path):
    # GIVEN an older preview
    preview_path = tmp_path / "previews" / "2026-09-25 Trip.mp4"
    preview_path.parent.mkdir()
    preview_path.write_bytes(b"old preview")

    # WHEN making the preview again
    extract.make_preview(tmp_path / "trip.mp4", preview_path, config, duration_s=60.0)

    # THEN the new preview replaces it
    assert list(preview_path.parent.iterdir()) == [preview_path]
    assert preview_path.read_bytes() == b"partial video"


def test_make_preview_renames_partial_output(fake_ffmpeg, config, tmp_path):
    # GIVEN an ffmpeg run that writes the partial output
    preview_path = tmp_path / "previews" / "2026-09-25 Trip.mp4"

    # WHEN making a preview
    extract.make_preview(tmp_path / "trip.mp4", preview_path, config, duration_s=60.0)

    # THEN the result is renamed to the final path
    assert list(preview_path.parent.iterdir()) == [preview_path]
    assert preview_path.read_bytes() == b"partial video"
    assert "scale=-2:480" in fake_ffmpeg.calls[0]


def test_make_preview_removes_partial_output_on_failure(fake_ffmpeg, config, tmp_path):
    # GIVEN an ffmpeg run that fails after writing some output
    fake_ffmpeg.encode_return_code = 1
    fake_ffmpeg.encode_stderr = "Invalid data found"
    preview_path = tmp_path / "previews" / "2026-09-25 Trip.mp4"

    # WHEN making a preview
    with pytest.raises(RuntimeError, match="Invalid data found"):
        extract.make_preview(tmp_path / "trip.mp4", preview_path, config, duration_s=60.0)

    # THEN nothing is left behind, and the error names the ffmpeg error
    assert list(preview_path.parent.iterdir()) == []


def test_make_preview_logs_progress(fake_ffmpeg, config, tmp_path, monkeypatch, caplog):
    # GIVEN an ffmpeg run that reports its progress every 20 seconds
    caplog.set_level(logging.INFO)
    wall_clock = {"now_s": 0.0}
    monkeypatch.setattr("dashcam.terminal.time.monotonic", lambda: wall_clock["now_s"])

    def reports():
        for report_number in range(1, 4):
            wall_clock["now_s"] = report_number * 20.0
            yield video.FfmpegProgress(output_time_s=report_number * 600.0, speed=30.0)

    fake_ffmpeg.progress_reports = reports()

    # WHEN making a preview of a 1-hour video
    extract.make_preview(tmp_path / "trip.mp4", tmp_path / "preview.mp4", config, 3600.0)

    # THEN the progress is logged at most every `PROGRESS_INTERVAL_S` (at 40 s)
    progress_messages = [
        record.getMessage()
        for record in caplog.records
        if getattr(record, "marker", None) == "progress"
    ]
    assert progress_messages == ["trip.mp4 (preview): 33% (20:00 of 1:00:00, 30.0x, ~1 min left)"]


def test_reclean_tracks_applies_overrides(config, store, make_track):
    # GIVEN a track with a bad range added by hand after extraction
    track = make_track(sample_count=3)
    track.overrides = cleaning.Overrides(bad_ranges_s=[(0.0, 2.0)])
    store.save_track(track)

    # WHEN recleaning
    failed_count = extract.reclean_tracks(store.root, config)

    # THEN the overridden samples are no longer good fixes
    recleaned_track = store.load_track(track.stem)
    assert failed_count == 0
    assert {sample.status for sample in recleaned_track.clean_samples} == {cleaning.STATUS_SPOOFED}


def test_reclean_tracks_keeps_street_data(config, store, make_track):
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
    store.save_track(track)

    # WHEN recleaning
    extract.reclean_tracks(store.root, config)

    # THEN the street data is kept for `enrich` to check
    recleaned_track = store.load_track(track.stem)
    assert recleaned_track.streets == track.streets
    assert recleaned_track.localities == track.localities
    assert recleaned_track.enrichment == track.enrichment


def test_reclean_tracks_reports_unreadable_tracks(config, store):
    # GIVEN a broken track file
    store.track_path("broken").write_text("{", encoding="utf-8")

    # WHEN recleaning
    failed_count = extract.reclean_tracks(store.root, config)

    # THEN the failure is counted
    assert failed_count == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_extract_video_with_sample_video(config, tmp_path):
    # GIVEN the sample trip clip
    job_def = extract.ExtractJobDefinition(
        video_path=SAMPLE_VIDEO_PATH,
        fingerprint="sample",
        metadata_dir=tmp_path,
        config=dataclasses.replace(config, hwaccel_options=""),
        make_preview=False,
    )

    # WHEN extracting it
    track = extract.extract_video(job_def)

    # THEN every second has a good fix, in the right place and time
    assert track.extraction_status == metadata.EXTRACTION_OK
    assert [sample.status for sample in track.clean_samples] == [cleaning.STATUS_OK] * 6
    assert [sample.time.isoformat() for sample in track.clean_samples if sample.time] == [
        f"2026-09-23T18:42:{second}+03:00" for second in range(32, 38)
    ]
    first_sample = track.clean_samples[0]
    assert (first_sample.lat, first_sample.lon) == (49.811027, 24.02489)
    last_sample = track.clean_samples[-1]
    assert (last_sample.lat, last_sample.lon) == (49.811157, 24.024162)


def test_extract_video_probes_with_configured_ffprobe(monkeypatch, config, tmp_path):
    # GIVEN a config with a custom ffprobe
    probed_executables = []

    def fake_probe_video(path, ffprobe_executable):
        probed_executables.append(ffprobe_executable)
        raise RuntimeError("stop after probing")

    monkeypatch.setattr("dashcam.extract.video.probe_video", fake_probe_video)
    job_def = extract.ExtractJobDefinition(
        video_path=tmp_path / "2026-09-25 Trip.mp4",
        fingerprint="100:abc",
        metadata_dir=tmp_path / ".metadata",
        config=dataclasses.replace(config, ffprobe_executable="/opt/ffmpeg/bin/ffprobe"),
        make_preview=False,
    )

    # WHEN extracting the video
    with pytest.raises(RuntimeError, match="stop after probing"):
        extract.extract_video(job_def)

    # THEN the custom ffprobe reads the video
    assert probed_executables == ["/opt/ffmpeg/bin/ffprobe"]
