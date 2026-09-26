import dataclasses
import datetime
import os
import shutil
from pathlib import Path

import pytest

from dashcam import encode

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
        "-hide_banner",
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
        output_dir=tmp_path,
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
):
    # GIVEN segments that form two trips
    make_raw_videos(
        "20260925111707_000001.MP4",
        "20260925111807_000002.MP4",
        "20260925180000_000003.MP4",
    )
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # WHEN encoding trips
    failed_count = encode.encode_trips(
        raw_video_dir=raw_video_dir,
        output_dir=output_dir,
        config=config,
        min_trip_gap_hours=3,
        job_count=2,
        dry_run=False,
        check_readability=False,
    )

    # THEN each trip gets its own video
    assert failed_count == 0
    assert sorted(path.name for path in output_dir.iterdir()) == [
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
):
    # GIVEN a trip for which ffmpeg fails
    make_raw_videos("20260925111707_000001.MP4")
    fake_ffmpeg.encode_return_code = 1

    # WHEN encoding trips
    failed_count = encode.encode_trips(
        raw_video_dir=raw_video_dir,
        output_dir=tmp_path,
        config=config,
        min_trip_gap_hours=3,
        job_count=2,
        dry_run=False,
        check_readability=False,
    )

    # THEN the failure is reported
    assert failed_count == 1


def test_encode_trips_dry_run_does_not_run_ffmpeg(
    config, raw_video_dir, make_raw_videos, tmp_path, fake_ffmpeg
):
    # GIVEN a raw segment
    make_raw_videos("20260925111707_000001.MP4")

    # WHEN encoding trips in dry run mode
    encode.encode_trips(
        raw_video_dir=raw_video_dir,
        output_dir=tmp_path,
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
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # WHEN encoding the range 2-3 without an output name
    encode.encode_range(
        raw_video_dir=raw_video_dir,
        output_dir=output_dir,
        config=config,
        start_index=2,
        end_index=3,
        output_name=None,
        dry_run=False,
        check_readability=False,
    )

    # THEN the output is named after the start time of segment 2
    assert [path.name for path in output_dir.iterdir()] == ["2026-09-25 Trip 11-01.mp4"]


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
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    # WHEN encoding it with an explicit output name
    encode.encode_range(
        raw_video_dir=raw_video_dir,
        output_dir=output_dir,
        config=config,
        start_index=1,
        end_index=1,
        output_name="Road Trip",
        dry_run=False,
        check_readability=False,
    )

    # THEN the output uses that name
    assert [path.name for path in output_dir.iterdir()] == ["Road Trip.mp4"]


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
