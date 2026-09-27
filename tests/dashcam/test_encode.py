import dataclasses
import datetime
import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dashcam import encode
from dashcam import metadata
from dashcam import video


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


def test_collect_raw_video_segments_includes_locked_folders(raw_video_dir, make_raw_videos):
    # GIVEN loop clips with a gap where one clip was locked into `Movie/RO`, and one in `DCIM/RO`
    make_raw_videos("20260925110000_000001.MP4", "20260925110200_000003.MP4")
    make_raw_videos("20260925110100_000002.MP4", folder="Movie/RO")
    make_raw_videos("20260925110300_000004.MP4", folder="RO")

    # WHEN collecting segments
    segments = encode.collect_raw_video_segments(
        raw_video_dir, ffmpeg_executable="ffmpeg", check_readability=False
    )

    # THEN the locked clips fill their places in the sequence
    assert [segment.index for segment in segments] == [1, 2, 3, 4]


def test_collect_raw_video_segments_includes_files_in_the_directory_itself(
    raw_video_dir, make_raw_videos
):
    # GIVEN a clip directly in the raw video directory
    make_raw_videos("20260925110000_000001.MP4", folder="")

    # WHEN collecting segments
    segments = encode.collect_raw_video_segments(
        raw_video_dir, ffmpeg_executable="ffmpeg", check_readability=False
    )

    # THEN it is collected
    assert [segment.index for segment in segments] == [1]


def test_collect_raw_video_segments_skips_hidden_folders(raw_video_dir, make_raw_videos):
    # GIVEN a clip, and a deleted one in the macOS trash folder of the card
    make_raw_videos("20260925110000_000001.MP4")
    make_raw_videos("20260925110100_000002.MP4", folder=".Trashes/501")

    # WHEN collecting segments
    segments = encode.collect_raw_video_segments(
        raw_video_dir, ffmpeg_executable="ffmpeg", check_readability=False
    )

    # THEN the clip in the hidden folder is skipped
    assert [segment.index for segment in segments] == [1]


def test_collect_raw_video_segments_skips_parking_clips(raw_video_dir, make_raw_videos, caplog):
    # GIVEN a loop clip and two parking mode clips
    caplog.set_level(logging.INFO)
    make_raw_videos(
        "20260925110000_000001.MP4", "20260925230000_000002P.MP4", "20260925230100_000003p.mp4"
    )

    # WHEN collecting segments
    segments = encode.collect_raw_video_segments(
        raw_video_dir, ffmpeg_executable="ffmpeg", check_readability=False
    )

    # THEN only the loop clip is collected, with one summary line and no warnings
    assert [segment.index for segment in segments] == [1]
    assert "Skipping 2 parking mode files" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_find_raw_video_paths_prefers_the_locked_copy(raw_video_dir, make_raw_videos, caplog):
    # GIVEN the same clip in the loop folder and in its locked folder
    make_raw_videos("20260925110000_000001.MP4")
    (locked_path,) = make_raw_videos("20260925110000_000001.MP4", folder="Movie/RO")

    # WHEN finding the raw videos
    paths = encode.find_raw_video_paths(raw_video_dir)

    # THEN the locked copy is used, with a warning
    assert paths == [locked_path]
    assert "using the one in 'Movie/RO'" in caplog.text


def test_find_raw_video_paths_with_copies_outside_locked_folders(raw_video_dir, make_raw_videos):
    # GIVEN the same clip in two folders, neither of them locked
    (first_path,) = make_raw_videos("20260925110000_000001.MP4", folder="Backup")
    make_raw_videos("20260925110000_000001.MP4")

    # WHEN finding the raw videos
    paths = encode.find_raw_video_paths(raw_video_dir)

    # THEN the copy in the first folder in path order is used
    assert paths == [first_path]


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


def test_collect_raw_video_segments_without_directory(tmp_path):
    # GIVEN a raw video directory that does not exist, e.g. an unmounted SD card
    missing_dir = tmp_path / "DCIM"

    # WHEN collecting segments
    # THEN it fails with the path
    with pytest.raises(RuntimeError, match="Cannot find the raw video directory"):
        encode.collect_raw_video_segments(
            missing_dir, ffmpeg_executable="ffmpeg", check_readability=False
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
    # GIVEN a config with hardware acceleration, a concat list, and a partial output path
    config = dataclasses.replace(config, hwaccel_options="-hwaccel videotoolbox")
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
    # GIVEN the default config, without hardware acceleration

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

    # THEN ffmpeg writes to a partial path, which is renamed to the final path
    assert is_encoded
    partial_path = Path(fake_ffmpeg.calls[-1][-1])
    assert partial_path.parent == tmp_path
    assert partial_path.name.endswith(metadata.PARTIAL_FILE_SUFFIX)
    assert output_path.read_bytes() == b"partial video"
    assert list(tmp_path.iterdir()) == [output_path]


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
    assert list(tmp_path.iterdir()) == []


def test_run_encode_job_keeps_an_output_that_appeared_meanwhile(
    config, tmp_path, fake_ffmpeg, caplog
):
    # GIVEN an encoding job whose output is created by someone else while it runs
    output_path = tmp_path / "2026-09-25 Trip 11-17.mp4"
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=output_path,
        config=config,
    )
    output_path.write_bytes(b"other video")

    # WHEN running it
    is_encoded = encode.run_encode_job(job_def)

    # THEN the other video is kept, the job fails, and no partial output is left behind
    assert not is_encoded
    assert output_path.read_bytes() == b"other video"
    assert list(tmp_path.iterdir()) == [output_path]
    assert "Cannot save '2026-09-25 Trip 11-17.mp4'" in caplog.text


def test_run_encode_job_reports_unexpected_error(
    config, tmp_path, fake_ffmpeg, monkeypatch, caplog
):
    # GIVEN an encoding job that fails with an unexpected error after writing a partial output
    output_path = tmp_path / "2026-09-25 Trip 11-17.mp4"
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=output_path,
        config=config,
    )

    def failing_ffmpeg(cmd, output_name, total_duration_s):
        Path(cmd[-1]).write_bytes(b"partial video")
        raise ValueError("unexpected")

    monkeypatch.setattr(encode, "run_ffmpeg_with_progress", failing_ffmpeg)

    # WHEN running it
    is_encoded = encode.run_encode_job(job_def)

    # THEN the job fails without raising, with the traceback logged and no files left behind
    assert not is_encoded
    assert "Encoding failed for '2026-09-25 Trip 11-17.mp4' with an unexpected error" in caplog.text
    assert "ValueError: unexpected" in caplog.text
    assert list(tmp_path.iterdir()) == []


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
    monkeypatch.setattr("dashcam.terminal.time.monotonic", lambda: wall_clock["now_s"])

    def iter_progress(cmd):
        Path(cmd[-1]).write_bytes(b"partial video")
        for report_number in range(1, 7):
            wall_clock["now_s"] = report_number * 4.0
            yield video.FfmpegProgress(output_time_s=report_number * 6.0, speed=1.5)

    monkeypatch.setattr("dashcam.video.iter_ffmpeg_progress", iter_progress)
    monkeypatch.setattr("dashcam.video.probe_duration", lambda path, ffprobe_executable: 60.0)
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


def test_run_encode_job_probes_durations_with_configured_ffprobe(
    config, tmp_path, fake_ffmpeg, monkeypatch
):
    # GIVEN a config with a custom ffprobe
    probed_executables = []

    def fake_probe_duration(path, ffprobe_executable):
        probed_executables.append(ffprobe_executable)
        return 60.0

    monkeypatch.setattr("dashcam.video.probe_duration", fake_probe_duration)
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=tmp_path / "trip.mp4",
        config=dataclasses.replace(config, ffprobe_executable="/opt/ffmpeg/bin/ffprobe"),
    )

    # WHEN running the job
    encode.run_encode_job(job_def)

    # THEN the custom ffprobe reads the durations
    assert probed_executables == ["/opt/ffmpeg/bin/ffprobe"]


def test_run_encode_job_without_durations_still_encodes(
    config, tmp_path, fake_ffmpeg, monkeypatch, caplog
):
    # GIVEN a job whose raw video durations cannot be read
    def failing_probe_duration(path, ffprobe_executable):
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


def test_plan_encode_jobs_rejects_duplicate_output_names(config, tmp_path):
    # GIVEN two trips that start in the same minute (with a very small trip gap)
    segment = make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))

    # WHEN planning jobs
    # THEN planning fails instead of letting one output replace the other
    with pytest.raises(RuntimeError, match="Two trips would be named '2026-09-25 Trip 11-17'"):
        encode.plan_encode_jobs(
            [("2026-09-25 Trip 11-17", [segment]), ("2026-09-25 Trip 11-17", [segment])],
            library_dir=tmp_path,
            config=config,
        )


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
        metadata_dir=tmp_path / "metadata",
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
        metadata_dir=tmp_path / "metadata",
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


def run_encode_trips(config, raw_video_dir, library_dir, metadata_dir):
    return encode.encode_trips(
        raw_video_dir=raw_video_dir,
        library_dir=library_dir,
        metadata_dir=metadata_dir,
        config=config,
        min_trip_gap_hours=3,
        job_count=2,
        dry_run=False,
        check_readability=False,
    )


def test_encode_trips_skips_encoded_segments_after_rename(
    config,
    raw_video_dir,
    make_raw_videos,
    tmp_path,
    fake_ffmpeg,
    serial_pool,
    no_os_sleep_prevention,
    caplog,
):
    # GIVEN a trip that has been encoded and renamed, with its raw videos still on the SD card
    caplog.set_level(logging.INFO)
    make_raw_videos("20260925111707_000001.MP4", "20260925111807_000002.MP4")
    library_dir = tmp_path / "out"
    library_dir.mkdir()
    metadata_dir = tmp_path / "metadata"
    run_encode_trips(config, raw_video_dir, library_dir, metadata_dir)
    (library_dir / "2026-09-25 Trip 11-17.mp4").rename(library_dir / "2026-09-25 Home.mp4")

    # WHEN encoding trips again
    failed_count = run_encode_trips(config, raw_video_dir, library_dir, metadata_dir)

    # THEN nothing is encoded again
    assert failed_count == 0
    assert "Skipping 2 files that have already been encoded" in caplog.text
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 Home.mp4"]


def test_encode_trips_encodes_new_segments_of_an_encoded_trip(
    config,
    raw_video_dir,
    make_raw_videos,
    tmp_path,
    fake_ffmpeg,
    serial_pool,
    no_os_sleep_prevention,
):
    # GIVEN an encoded trip, and a raw video recorded later within the trip gap
    make_raw_videos("20260925111707_000001.MP4")
    library_dir = tmp_path / "out"
    library_dir.mkdir()
    metadata_dir = tmp_path / "metadata"
    run_encode_trips(config, raw_video_dir, library_dir, metadata_dir)
    make_raw_videos("20260925115000_000002.MP4")

    # WHEN encoding trips again
    run_encode_trips(config, raw_video_dir, library_dir, metadata_dir)

    # THEN only the new raw video is encoded, as a trip of its own
    assert sorted(path.name for path in library_dir.iterdir()) == [
        "2026-09-25 Trip 11-17.mp4",
        "2026-09-25 Trip 11-50.mp4",
    ]


def test_encode_trips_does_not_log_failed_trips(
    config,
    raw_video_dir,
    make_raw_videos,
    tmp_path,
    fake_ffmpeg,
    serial_pool,
    no_os_sleep_prevention,
):
    # GIVEN a trip whose encoding failed
    make_raw_videos("20260925111707_000001.MP4")
    library_dir = tmp_path / "out"
    library_dir.mkdir()
    metadata_dir = tmp_path / "metadata"
    fake_ffmpeg.encode_return_code = 1
    run_encode_trips(config, raw_video_dir, library_dir, metadata_dir)

    # WHEN encoding trips again after the problem is gone
    fake_ffmpeg.encode_return_code = 0
    failed_count = run_encode_trips(config, raw_video_dir, library_dir, metadata_dir)

    # THEN the trip is encoded
    assert failed_count == 0
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 Trip 11-17.mp4"]


@pytest.mark.parametrize(
    "logged_sizes, expected_contains",
    [
        ({"20260925111707_000001.MP4": 5}, True),
        ({"20260925111707_000001.MP4": 7}, False),
        ({"20260925111707_000002.MP4": 5}, False),
    ],
)
def test_encoded_segment_log_matches_filename_and_size(
    raw_video_dir, tmp_path, logged_sizes, expected_contains
):
    # GIVEN a 5-byte raw video and a log of encoded raw videos
    path = raw_video_dir / "20260925111707_000001.MP4"
    path.write_bytes(b"12345")
    segment = make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7), path=path)
    encoded_log = encode.EncodedSegmentLog(path=tmp_path / "log.json", sizes=logged_sizes)

    # WHEN checking whether the raw video has been encoded
    contains = encoded_log.contains(segment)

    # THEN it only matches a logged video with the same filename and size
    assert contains == expected_contains


def test_encoded_segment_log_with_invalid_file(tmp_path):
    # GIVEN a metadata directory with a broken log
    (tmp_path / "encoded_segments.json").write_text("{not json", encoding="utf-8")

    # WHEN loading the log
    # THEN it fails with a readable error
    with pytest.raises(RuntimeError, match="Cannot read the encoded video log"):
        encode.EncodedSegmentLog.load(tmp_path)


def test_encode_range_encodes_and_logs_encoded_segments(
    config,
    raw_video_dir,
    make_raw_videos,
    tmp_path,
    fake_ffmpeg,
    serial_pool,
    no_os_sleep_prevention,
):
    # GIVEN a raw video that has already been encoded
    make_raw_videos("20260925110000_000001.MP4")
    library_dir = tmp_path / "out"
    library_dir.mkdir()
    metadata_dir = tmp_path / "metadata"
    run_encode_trips(config, raw_video_dir, library_dir, metadata_dir)

    # WHEN encoding it again as a range with another name
    encode.encode_range(
        raw_video_dir=raw_video_dir,
        library_dir=library_dir,
        metadata_dir=metadata_dir,
        config=config,
        start_index=1,
        end_index=1,
        output_name="Road Trip",
        dry_run=False,
        check_readability=False,
    )

    # THEN the range is encoded, and the raw video stays logged
    assert sorted(path.name for path in library_dir.iterdir()) == [
        "2026-09-25 Road Trip.mp4",
        "2026-09-25 Trip 11-00.mp4",
    ]
    encoded_log = encode.EncodedSegmentLog.load(metadata_dir)
    assert encoded_log.sizes == {"20260925110000_000001.MP4": 0}


def test_encode_trips_dry_run_does_not_run_ffmpeg(
    config, raw_video_dir, make_raw_videos, tmp_path, fake_ffmpeg
):
    # GIVEN a raw segment
    make_raw_videos("20260925111707_000001.MP4")

    # WHEN encoding trips in dry run mode
    encode.encode_trips(
        raw_video_dir=raw_video_dir,
        library_dir=tmp_path,
        metadata_dir=tmp_path / "metadata",
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
        metadata_dir=tmp_path / "metadata",
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
        metadata_dir=tmp_path / "metadata",
        config=config,
        start_index=1,
        end_index=1,
        output_name="Road Trip",
        dry_run=False,
        check_readability=False,
    )

    # THEN the output uses that name, after the date of the first segment
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-25 Road Trip.mp4"]


def test_encode_range_with_dated_output_name(
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

    # WHEN encoding it with an output name that starts with a date
    encode.encode_range(
        raw_video_dir=raw_video_dir,
        library_dir=library_dir,
        metadata_dir=tmp_path / "metadata",
        config=config,
        start_index=1,
        end_index=1,
        output_name="2026-09-24 Road Trip",
        dry_run=False,
        check_readability=False,
    )

    # THEN the name is used as given
    assert [path.name for path in library_dir.iterdir()] == ["2026-09-24 Road Trip.mp4"]


def test_encode_range_rejects_too_long_output_name(
    config, raw_video_dir, make_raw_videos, tmp_path, fake_ffmpeg
):
    # GIVEN a raw segment, and an output name whose track name would not fit on an encrypted
    # NAS folder once the date is added
    make_raw_videos("20260925110000_000001.MP4")
    output_name = "x" * 128

    # WHEN encoding with it
    with pytest.raises(RuntimeError, match="is too long: use at most 142 characters"):
        encode.encode_range(
            raw_video_dir=raw_video_dir,
            library_dir=tmp_path / "out",
            metadata_dir=tmp_path / "metadata",
            config=config,
            start_index=1,
            end_index=1,
            output_name=output_name,
            dry_run=False,
            check_readability=False,
        )

    # THEN nothing is encoded
    assert fake_ffmpeg.calls == []


@pytest.mark.parametrize("output_name", ["../Road Trip", "Trips/Road Trip", ".Road Trip", "A: B"])
def test_encode_range_rejects_output_name_outside_a_plain_filename(
    config, raw_video_dir, tmp_path, fake_ffmpeg, output_name
):
    # GIVEN an output name that leaves the library, is hidden, or has a forbidden character

    # WHEN encoding with it
    with pytest.raises(RuntimeError, match="must not start with '.' or contain"):
        encode.encode_range(
            raw_video_dir=raw_video_dir,
            library_dir=tmp_path / "out",
            metadata_dir=tmp_path / "metadata",
            config=config,
            start_index=1,
            end_index=1,
            output_name=output_name,
            dry_run=False,
            check_readability=False,
        )

    # THEN nothing is encoded
    assert fake_ffmpeg.calls == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_collect_raw_video_segments_with_real_ffmpeg(raw_video_dir):
    # GIVEN three consecutive 1-second H.264 segments and a corrupt one
    for raw_video_name in [
        "20260925111707_000271.MP4",
        "20260925111708_000272.MP4",
        "20260925111709_000273.MP4",
    ]:
        cmd = [
            "ffmpeg",
            *["-v", "error"],
            *["-f", "lavfi", "-i", "testsrc=s=320x180:r=10:d=1"],
            *["-c:v", "libx264", "-pix_fmt", "yuv420p"],
            str(raw_video_dir / raw_video_name),
        ]
        subprocess.run(cmd, check=True)
    (raw_video_dir / "20260925111710_000274.MP4").write_bytes(b"not a video")

    # WHEN collecting and grouping the segments with real readability checks
    segments = encode.collect_raw_video_segments(raw_video_dir, ffmpeg_executable="ffmpeg")
    trips = encode.group_segments_into_trips(segments, min_trip_gap_hours=3)

    # THEN the readable segments form a single trip
    assert [segment.index for segment in segments] == [271, 272, 273]
    assert [trip.get_placeholder_name() for trip in trips] == ["2026-09-25 Trip 11-17"]


def test_run_encode_job_removes_partial_output_when_interrupted(config, tmp_path, monkeypatch):
    # GIVEN an encoding job that is interrupted while ffmpeg writes the partial output
    output_path = tmp_path / "2026-09-25 Trip 11-17.mp4"
    job_def = encode.EncodeJobDefinition(
        raw_segments=[make_segment(1, datetime.datetime(2026, 9, 25, 11, 17, 7))],
        output_path=output_path,
        config=config,
    )

    def interrupted_ffmpeg(cmd, output_name, total_duration_s):
        Path(cmd[-1]).write_bytes(b"partial video")
        raise SystemExit(130)

    monkeypatch.setattr(encode, "get_total_duration", lambda segments, ffprobe: None)
    monkeypatch.setattr(encode, "run_ffmpeg_with_progress", interrupted_ffmpeg)

    # WHEN running it
    with pytest.raises(SystemExit):
        encode.run_encode_job(job_def)

    # THEN no partial output is left behind
    assert list(tmp_path.iterdir()) == []
