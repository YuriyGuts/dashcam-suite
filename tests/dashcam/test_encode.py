import dataclasses
import datetime
import logging
import os
import shutil
from pathlib import Path

import pytest

from dashcam import encode
from dashcam import video

SAMPLE_RAW_VIDEO_DIR = Path(__file__).parents[2] / "video" / "raw-sd" / "DCIM" / "Movie"


def make_segment(index, start_time, path=None):
    return encode.RawVideoSegment(
        index=index,
        path=path or Path(f"/sd/{start_time:%Y%m%d%H%M%S}_{index:06d}.MP4"),
        start_time=start_time,
    )


def test_parse_raw_video_segment_with_camera_filename():
    # GIVEN a filename written by the camera
    path = Path("/sd/20260925111707_000271.MP4")

    # WHEN parsing it
    segment = encode.parse_raw_video_segment(path)

    # THEN the index and start time come from the filename
    assert segment == encode.RawVideoSegment(
        index=271,
        path=path,
        start_time=datetime.datetime(2026, 9, 25, 11, 17, 7),
    )


def test_parse_raw_video_segment_with_index_above_9999():
    # GIVEN a camera filename whose index has more than 4 significant digits
    path = Path("/sd/20260925111707_010271.MP4")

    # WHEN parsing it
    segment = encode.parse_raw_video_segment(path)

    # THEN the full index is kept
    assert segment is not None
    assert segment.index == 10271


def test_parse_raw_video_segment_with_other_naming_scheme_uses_mtime(tmp_path):
    # GIVEN a file named by another camera, with a known modification time
    path = tmp_path / "MOVI0042.avi"
    path.write_bytes(b"")
    modification_time = datetime.datetime(2023, 1, 12, 15, 23, 45)
    os.utime(path, (modification_time.timestamp(), modification_time.timestamp()))

    # WHEN parsing it
    segment = encode.parse_raw_video_segment(path)

    # THEN the index comes from the trailing number and the start time from the mtime
    assert segment == encode.RawVideoSegment(index=42, path=path, start_time=modification_time)


def test_parse_raw_video_segment_without_index():
    # GIVEN a filename without a trailing number
    path = Path("/sd/clip.mp4")

    # WHEN parsing it
    segment = encode.parse_raw_video_segment(path)

    # THEN it is not recognized as a segment
    assert segment is None


def test_collect_raw_video_segments_sorts_by_start_time(raw_video_dir, make_raw_videos):
    # GIVEN segments whose index wrapped around after the camera counter reset
    make_raw_videos("20260925120000_000001.MP4", "20260925115900_999999.MP4")

    # WHEN collecting them
    segments = encode.collect_raw_video_segments(
        raw_video_dir, ffmpeg_executable="ffmpeg", check_readability=False
    )

    # THEN they are ordered by start time, not by index
    assert [segment.index for segment in segments] == [999999, 1]


def test_collect_raw_video_segments_skips_hidden_and_unsupported_files(
    raw_video_dir, make_raw_videos
):
    # GIVEN a video, a macOS metadata file, a non-video file, and a file without an index
    make_raw_videos(
        "20260925111707_000271.MP4",
        "._20260925111707_000271.MP4",
        "20260925111707_000271.txt",
        "clip.mp4",
    )

    # WHEN collecting segments
    segments = encode.collect_raw_video_segments(
        raw_video_dir, ffmpeg_executable="ffmpeg", check_readability=False
    )

    # THEN only the video is collected
    assert [segment.path.name for segment in segments] == ["20260925111707_000271.MP4"]


def test_collect_raw_video_segments_with_index_range(raw_video_dir, make_raw_videos):
    # GIVEN segments 1 to 4
    make_raw_videos(
        "20260925110000_000001.MP4",
        "20260925110100_000002.MP4",
        "20260925110200_000003.MP4",
        "20260925110300_000004.MP4",
    )

    # WHEN collecting segments 2 to 3
    segments = encode.collect_raw_video_segments(
        raw_video_dir,
        ffmpeg_executable="ffmpeg",
        start_index=2,
        end_index=3,
        check_readability=False,
    )

    # THEN only the segments in the range are collected, inclusive
    assert [segment.index for segment in segments] == [2, 3]


def test_collect_raw_video_segments_skips_unreadable_files(
    raw_video_dir, make_raw_videos, fake_ffmpeg
):
    # GIVEN two segments, one of which ffmpeg cannot read
    readable_path, unreadable_path = make_raw_videos(
        "20260925110000_000001.MP4", "20260925110100_000002.MP4"
    )
    fake_ffmpeg.unreadable_paths.add(str(unreadable_path))

    # WHEN collecting segments with the readability check
    segments = encode.collect_raw_video_segments(raw_video_dir, ffmpeg_executable="ffmpeg")

    # THEN the unreadable segment is skipped
    assert [segment.path for segment in segments] == [readable_path]


def test_collect_raw_video_segments_without_matches(raw_video_dir):
    # GIVEN an empty directory

    # WHEN collecting segments
    # THEN it fails
    with pytest.raises(RuntimeError, match="Could not find any files"):
        encode.collect_raw_video_segments(
            raw_video_dir, ffmpeg_executable="ffmpeg", check_readability=False
        )


def test_group_segments_into_trips_splits_on_large_gaps():
    # GIVEN two segments a minute apart, then one after a 4-hour gap
    first_start = datetime.datetime(2026, 9, 25, 11, 17, 7)
    segments = [
        make_segment(1, first_start),
        make_segment(2, first_start + datetime.timedelta(minutes=1)),
        make_segment(3, first_start + datetime.timedelta(hours=4)),
    ]

    # WHEN grouping them with a 3-hour trip gap
    trips = encode.group_segments_into_trips(segments, min_trip_gap_hours=3)

    # THEN there are two trips
    assert [[segment.index for segment in trip.raw_segments] for trip in trips] == [[1, 2], [3]]


def test_group_segments_into_trips_keeps_gaps_up_to_threshold():
    # GIVEN two segments exactly 3 hours apart
    first_start = datetime.datetime(2026, 9, 25, 11, 0, 0)
    segments = [
        make_segment(1, first_start),
        make_segment(2, first_start + datetime.timedelta(hours=3)),
    ]

    # WHEN grouping them with a 3-hour trip gap
    trips = encode.group_segments_into_trips(segments, min_trip_gap_hours=3)

    # THEN they stay in one trip
    assert len(trips) == 1


def test_trip_get_placeholder_name():
    # GIVEN a trip that starts at 11:17:07
    trip = encode.Trip(raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))])

    # WHEN getting its placeholder name
    name = trip.get_placeholder_name()

    # THEN it contains the start date, hour, and minute
    assert name == "2026-09-25 Trip 11-17"


def test_format_ffmpeg_concat_list_escapes_single_quotes(tmp_path):
    # GIVEN a segment whose path contains a single quote
    path = tmp_path / "Bob's card" / "20260925111707_000271.MP4"
    segment = make_segment(271, datetime.datetime(2026, 9, 25, 11, 17, 7), path=path)

    # WHEN formatting the concat list
    concat_list = encode.format_ffmpeg_concat_list([segment])

    # THEN the quote is escaped for the concat demuxer
    escaped_path = str(path.resolve()).replace("'", "'\\''")
    assert concat_list == f"file '{escaped_path}'\n"


def test_build_ffmpeg_command(config, tmp_path):
    # GIVEN a concat list and a partial output path
    concat_list_path = tmp_path / "list.txt"
    partial_output_path = tmp_path / "2026-09-25 Trip 11-17.mp4.partial"

    # WHEN building the ffmpeg command
    cmd = encode.build_ffmpeg_command(concat_list_path, partial_output_path, config)

    # THEN options from the config are split into separate arguments in the right order
    assert cmd == [
        "ffmpeg",
        "-nostdin",
        *["-v", "error", "-nostats", "-progress", "pipe:1"],
        *["-f", "concat", "-safe", "0"],
        *["-hwaccel", "videotoolbox"],
        *["-i", str(concat_list_path)],
        *["-c:v", "libx265", "-crf", "30", "-preset", "fast"],
        *["-c:a", "aac", "-b:a", "128k"],
        *["-f", "mp4", "-y", str(partial_output_path)],
    ]


def test_build_ffmpeg_command_without_hwaccel(config, tmp_path):
    # GIVEN a config without hardware acceleration
    config = dataclasses.replace(config, hwaccel_options="")

    # WHEN building the ffmpeg command
    cmd = encode.build_ffmpeg_command(tmp_path / "list.txt", tmp_path / "out.partial", config)

    # THEN no hwaccel arguments are included
    assert "-hwaccel" not in cmd


def test_run_encode_job_renames_partial_output_on_success(config, tmp_path, fake_ffmpeg):
    # GIVEN an encoding job
    output_path = tmp_path / "2026-09-25 Trip 11-17.mp4"
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=output_path,
        config=config,
    )

    # WHEN running it
    is_encoded = encode.run_encode_job(job_def)

    # THEN ffmpeg writes to the partial path, which is renamed to the final path
    assert is_encoded
    assert fake_ffmpeg.calls[-1][-1] == str(encode.get_partial_output_path(output_path))
    assert output_path.read_bytes() == b"partial video"
    assert not encode.get_partial_output_path(output_path).exists()


def test_run_encode_job_removes_partial_output_on_failure(config, tmp_path, fake_ffmpeg):
    # GIVEN an encoding job for which ffmpeg fails
    fake_ffmpeg.encode_return_code = 1
    output_path = tmp_path / "2026-09-25 Trip 11-17.mp4"
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=output_path,
        config=config,
    )

    # WHEN running it
    is_encoded = encode.run_encode_job(job_def)

    # THEN no output is left behind
    assert not is_encoded
    assert not output_path.exists()
    assert not encode.get_partial_output_path(output_path).exists()


def test_run_encode_job_logs_ffmpeg_errors(config, tmp_path, fake_ffmpeg, caplog):
    # GIVEN an encoding job for which ffmpeg fails with error output
    fake_ffmpeg.encode_return_code = 1
    fake_ffmpeg.encode_stderr = "Unknown encoder 'libx265'"
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=tmp_path / "2026-09-25 Trip 11-17.mp4",
        config=config,
    )

    # WHEN running it
    encode.run_encode_job(job_def)

    # THEN the error names the video and includes the ffmpeg output
    error_messages = [
        record.getMessage() for record in caplog.records if record.levelno == logging.ERROR
    ]
    assert error_messages == [
        "Encoding failed for '2026-09-25 Trip 11-17.mp4' (ffmpeg exit code 1)\n"
        "Unknown encoder 'libx265'"
    ]


def test_run_encode_job_logs_progress_every_interval(config, tmp_path, monkeypatch, caplog):
    # GIVEN an ffmpeg run that reports its progress every 4 seconds
    caplog.set_level(logging.INFO)
    wall_clock = {"now_s": 0.0}
    monkeypatch.setattr("dashcam.encode.time.monotonic", lambda: wall_clock["now_s"])

    def iter_progress(cmd):
        Path(cmd[-1]).write_bytes(b"partial video")
        for report_number in range(1, 7):
            wall_clock["now_s"] = report_number * 4.0
            yield video.FfmpegProgress(output_time_s=report_number * 6.0, speed=1.5)

    monkeypatch.setattr("dashcam.video.iter_ffmpeg_progress", iter_progress)
    monkeypatch.setattr("dashcam.video.probe_duration", lambda path: 60.0)
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=tmp_path / "trip.mp4",
        config=config,
    )

    # WHEN running it
    encode.run_encode_job(job_def)

    # THEN the progress is logged at most every `PROGRESS_INTERVAL_S` (at 12 s and 24 s)
    progress_messages = [
        record.getMessage()
        for record in caplog.records
        if getattr(record, "marker", None) == "progress"
    ]
    assert progress_messages == [
        "trip.mp4: 30% (0:18 of 1:00, 1.5x, <1 min left)",
        "trip.mp4: 60% (0:36 of 1:00, 1.5x, <1 min left)",
    ]


def test_run_encode_job_without_durations_still_encodes(
    config, tmp_path, fake_ffmpeg, monkeypatch, caplog
):
    # GIVEN a job whose raw video durations cannot be read
    def failing_probe_duration(path):
        raise RuntimeError(f"Cannot read the duration of '{path}'")

    monkeypatch.setattr("dashcam.video.probe_duration", failing_probe_duration)
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=tmp_path / "trip.mp4",
        config=config,
    )

    # WHEN running it
    is_encoded = encode.run_encode_job(job_def)

    # THEN the video is still encoded, with a warning about the progress
    assert is_encoded
    assert "Cannot compute the encoding progress" in caplog.text


def test_format_encode_progress_with_total_and_speed():
    # GIVEN 1:23 of a 6-minute video encoded at 1.2x
    progress = video.FfmpegProgress(output_time_s=83.0, speed=1.2)

    # WHEN describing the progress
    text = encode.format_encode_progress("trip.mp4", progress, total_duration_s=360.0)

    # THEN it has the percentage, times, speed, and the time left in minutes
    assert text == "trip.mp4: 23% (1:23 of 6:00, 1.2x, ~4 min left)"


def test_format_encode_progress_before_speed_is_known():
    # GIVEN the first report, without a speed
    progress = video.FfmpegProgress(output_time_s=0.0, speed=None)

    # WHEN describing the progress
    text = encode.format_encode_progress("trip.mp4", progress, total_duration_s=360.0)

    # THEN the speed and the time left are left out
    assert text == "trip.mp4: 0% (0:00 of 6:00)"


def test_format_encode_progress_without_total_duration():
    # GIVEN a job whose total duration is unknown
    progress = video.FfmpegProgress(output_time_s=83.0, speed=1.2)

    # WHEN describing the progress
    text = encode.format_encode_progress("trip.mp4", progress, total_duration_s=None)

    # THEN the encoded time and speed are shown
    assert text == "trip.mp4: 1:23 encoded, 1.2x"


def test_format_video_time_from_one_hour():
    assert encode.format_video_time(3723.9) == "1:02:03"


def test_format_video_time_under_one_hour():
    assert encode.format_video_time(83.0) == "1:23"


def test_run_encode_job_deletes_concat_list(config, tmp_path, fake_ffmpeg):
    # GIVEN an encoding job
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=tmp_path / "out.mp4",
        config=config,
    )

    # WHEN running it
    encode.run_encode_job(job_def)

    # THEN the temporary concat list passed to ffmpeg is deleted
    cmd = fake_ffmpeg.calls[-1]
    concat_list_path = Path(cmd[cmd.index("-i") + 1])
    assert not concat_list_path.exists()


def test_plan_encode_jobs_skips_existing_outputs(config, tmp_path):
    # GIVEN two trips, one of which has already been encoded
    segment = make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))
    (tmp_path / "2026-09-25 Trip 11-17.mp4").write_bytes(b"existing")

    # WHEN planning jobs
    job_defs = encode.plan_encode_jobs(
        [("2026-09-25 Trip 11-17", [segment]), ("2026-09-25 Trip 15-00", [segment])],
        library_dir=tmp_path,
        config=config,
    )

    # THEN only the missing output is planned
    assert [job_def.output_path.name for job_def in job_defs] == ["2026-09-25 Trip 15-00.mp4"]


def test_encode_trips_encodes_each_trip(
    config,
    raw_video_dir,
    make_raw_videos,
    tmp_path,
    fake_ffmpeg,
    serial_pool,
    no_os_sleep_prevention,
    caplog,
):
    # GIVEN segments that form two trips
    caplog.set_level(logging.INFO)
    make_raw_videos(
        "20260925111707_000001.MP4",
        "20260925111807_000002.MP4",
        "20260925180000_000003.MP4",
    )
    library_dir = tmp_path / "out"
    library_dir.mkdir()

    # WHEN encoding trips
    failed_count = encode.encode_trips(
        raw_video_dir=raw_video_dir,
        library_dir=library_dir,
        config=config,
        min_trip_gap_hours=3,
        job_count=2,
        dry_run=False,
        check_readability=False,
    )

    # THEN each trip gets its own video, and the progress counts finished videos
    assert failed_count == 0
    assert "Progress: 2/2 videos" in caplog.text
    assert sorted(path.name for path in library_dir.iterdir()) == [
        "2026-09-25 Trip 11-17.mp4",
        "2026-09-25 Trip 18-00.mp4",
    ]


def test_encode_trips_counts_failures(
    config,
    raw_video_dir,
    make_raw_videos,
    tmp_path,
    fake_ffmpeg,
    serial_pool,
    no_os_sleep_prevention,
    caplog,
):
    # GIVEN a trip for which ffmpeg fails
    make_raw_videos("20260925111707_000001.MP4")
    fake_ffmpeg.encode_return_code = 1

    # WHEN encoding trips
    failed_count = encode.encode_trips(
        raw_video_dir=raw_video_dir,
        library_dir=tmp_path,
        config=config,
        min_trip_gap_hours=3,
        job_count=2,
        dry_run=False,
        check_readability=False,
    )

    # THEN the failure is counted and summarized as a warning
    assert failed_count == 1
    assert [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ] == ["Encoded 0 videos, 1 failed"]


def test_encode_trips_dry_run_does_not_run_ffmpeg(
    config, raw_video_dir, make_raw_videos, tmp_path, fake_ffmpeg
):
    # GIVEN a raw segment
    make_raw_videos("20260925111707_000001.MP4")

    # WHEN encoding trips in dry run mode
    encode.encode_trips(
        raw_video_dir=raw_video_dir,
        library_dir=tmp_path,
        config=config,
        min_trip_gap_hours=3,
        job_count=2,
        dry_run=True,
        check_readability=True,
    )

    # THEN ffmpeg is never called, not even for the readability check
    assert fake_ffmpeg.calls == []


def test_encode_range_uses_first_segment_start_time_as_default_name(
    config,
    raw_video_dir,
    make_raw_videos,
    tmp_path,
    fake_ffmpeg,
    serial_pool,
    no_os_sleep_prevention,
):
    # GIVEN segments 1 to 3
    make_raw_videos(
        "20260925110000_000001.MP4",
        "20260925110100_000002.MP4",
        "20260925110200_000003.MP4",
    )
    library_dir = tmp_path / "out"
    library_dir.mkdir()

    # WHEN encoding the range 2-3 without an output name
    encode.encode_range(
        raw_video_dir=raw_video_dir,
        library_dir=library_dir,
        config=config,
        start_index=2,
        end_index=3,
        output_name=None,
        dry_run=False,
        check_readability=False,
    )

    # THEN the output is named after the start time of segment 2
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 Trip 11-01.mp4"]


def test_encode_range_with_output_name(
    config,
    raw_video_dir,
    make_raw_videos,
    tmp_path,
    fake_ffmpeg,
    serial_pool,
    no_os_sleep_prevention,
):
    # GIVEN a raw segment
    make_raw_videos("20260925110000_000001.MP4")
    library_dir = tmp_path / "out"
    library_dir.mkdir()

    # WHEN encoding it with an explicit output name
    encode.encode_range(
        raw_video_dir=raw_video_dir,
        library_dir=library_dir,
        config=config,
        start_index=1,
        end_index=1,
        output_name="Road Trip",
        dry_run=False,
        check_readability=False,
    )

    # THEN the output uses that name
    assert [path.name for path in library_dir.iterdir()] == ["Road Trip.mp4"]


@pytest.mark.skipif(not SAMPLE_RAW_VIDEO_DIR.is_dir(), reason="Sample SD card videos not present")
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_collect_raw_video_segments_with_sample_sd_card():
    # GIVEN the sample SD card directory with six consecutive 1-minute segments

    # WHEN collecting and grouping its segments with real readability checks
    segments = encode.collect_raw_video_segments(SAMPLE_RAW_VIDEO_DIR, ffmpeg_executable="ffmpeg")
    trips = encode.group_segments_into_trips(segments, min_trip_gap_hours=3)

    # THEN they form a single readable trip
    assert [segment.index for segment in segments] == [271, 272, 273, 274, 275, 276]
    assert [trip.get_placeholder_name() for trip in trips] == ["2026-09-25 Trip 11-17"]
