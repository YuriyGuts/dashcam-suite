import dataclasses
import datetime
from pathlib import Path

import pytest

from dashcam import cleaning
from dashcam import metadata

TRACK_FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "tracks"

TRIP_DATE = datetime.date(2026, 9, 25)
TRIP_START = datetime.datetime(2026, 9, 25, 10, 0, 0)

# Latitude change of roughly 30 m, i.e. ~108 km/h over one second.
LAT_STEP_30M = 0.00027


@pytest.fixture
def settings():
    return cleaning.CleaningSettings(
        trip_date=TRIP_DATE,
        timezone="Europe/Kyiv",
        max_speed_kmh=250,
        max_clock_date_diff_days=1,
        max_merge_gap_hours=12,
        max_interpolation_gap_s=60,
    )


def gps_text(lat, lon, kmh):
    lat_text = f"{'N' if lat >= 0 else 'S'}{abs(lat):.6f}"
    lon_text = f"{'E' if lon >= 0 else 'W'}{abs(lon):.6f}"
    return f"{kmh} KM/H {lat_text} {lon_text}"


def clock_text(clock):
    return clock.strftime(cleaning.CLOCK_FORMAT)


def make_sample(t, clock, lat=None, lon=None, kmh=0, left_score=0.95, right_score=0.95):
    left_text = gps_text(lat, lon, kmh) if lat is not None else ""
    return cleaning.RawSample(
        t=t,
        left_text=left_text,
        right_text=clock_text(clock),
        left_score=left_score if left_text else 0.1,
        right_score=right_score,
    )


def make_drive(start_t, start_clock, start_lat, count, lat_step=LAT_STEP_30M, kmh=108, lon=24.0):
    return [
        make_sample(
            t=start_t + index,
            clock=start_clock + datetime.timedelta(seconds=index),
            lat=start_lat + index * lat_step,
            lon=lon,
            kmh=kmh,
        )
        for index in range(count)
    ]


def make_no_fix(start_t, start_clock, count):
    return [
        make_sample(t=start_t + index, clock=start_clock + datetime.timedelta(seconds=index))
        for index in range(count)
    ]


def statuses_of(samples):
    return [sample.status for sample in samples]


def test_clean_track_with_steady_drive(settings):
    # GIVEN a steady drive with a good fix every second
    raw_samples = make_drive(0, TRIP_START, 49.8, count=20)

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN every sample is good and timed by the camera clock in the local time zone
    assert statuses_of(samples) == [cleaning.STATUS_OK] * 20
    assert samples[0].time == datetime.datetime(
        2026, 9, 25, 10, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3))
    )
    assert samples[5].lat == pytest.approx(49.8 + 5 * LAT_STEP_30M)
    assert samples[5].kmh == 108
    assert not any(sample.time_estimated for sample in samples)


def test_clean_track_uses_winter_time_offset(settings):
    # GIVEN a drive in January
    winter_start = datetime.datetime(2026, 1, 8, 12, 0, 0)
    winter_settings = dataclasses.replace(settings, trip_date=winter_start.date())
    raw_samples = make_drive(0, winter_start, 49.8, count=3)

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, winter_settings)

    # THEN times use the winter offset of the Kyiv time zone
    assert samples[0].time is not None
    assert samples[0].time.utcoffset() == datetime.timedelta(hours=2)


def test_clean_track_with_no_fix_at_start(settings):
    # GIVEN 10 seconds without a fix, then a drive
    raw_samples = make_no_fix(0, TRIP_START, 10) + make_drive(
        10, TRIP_START + datetime.timedelta(seconds=10), 49.8, count=10
    )

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN the start has no fix and gets exact times derived from the trusted camera clock
    assert statuses_of(samples[:10]) == [cleaning.STATUS_NO_FIX] * 10
    assert samples[0].lat is None
    assert samples[0].time is not None
    assert samples[0].time.replace(tzinfo=None) == TRIP_START
    assert not samples[0].time_estimated


def test_clean_track_interpolates_short_gap(settings):
    # GIVEN a 10-second loss of fix in the middle of a drive
    drive = make_drive(0, TRIP_START, 49.8, count=30)
    raw_samples = [
        make_sample(t=sample.t, clock=TRIP_START + datetime.timedelta(seconds=sample.t))
        if 10 <= sample.t < 20
        else sample
        for sample in drive
    ]

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN the gap is filled along the line between the neighbouring fixes
    assert statuses_of(samples[10:20]) == [cleaning.STATUS_INTERPOLATED] * 10
    assert samples[15].lat == pytest.approx(49.8 + 15 * LAT_STEP_30M)
    assert samples[15].kmh is None


def test_clean_track_keeps_long_gap(settings):
    # GIVEN a 2-minute loss of fix while the car stands still
    raw_samples = (
        make_drive(0, TRIP_START, 49.8, count=5, lat_step=0, kmh=0)
        + make_no_fix(5, TRIP_START + datetime.timedelta(seconds=5), 120)
        + make_drive(
            125, TRIP_START + datetime.timedelta(seconds=125), 49.8, count=5, lat_step=0, kmh=0
        )
    )

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN the gap is not filled
    assert statuses_of(samples[5:125]) == [cleaning.STATUS_NO_FIX] * 120


def test_clean_track_rejects_spoofed_stretch_with_fake_date(settings):
    # GIVEN a drive interrupted by 90 seconds of spoofed coordinates with a fake clock
    fake_clock = datetime.datetime(2028, 3, 10, 7, 0, 0)
    raw_samples = (
        make_drive(0, TRIP_START, 49.8, count=20)
        + make_drive(20, fake_clock, -12.03, count=90, lon=-77.04, kmh=200)
        + make_drive(
            110, TRIP_START + datetime.timedelta(seconds=110), 49.8 + 110 * LAT_STEP_30M, count=20
        )
    )

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN the spoofed stretch is rejected and gets estimated times from the video offset
    assert statuses_of(samples[20:110]) == [cleaning.STATUS_SPOOFED] * 90
    assert samples[30].lat is None
    assert samples[30].time is not None
    assert samples[30].time.replace(tzinfo=None) == TRIP_START + datetime.timedelta(seconds=30)
    assert samples[30].time_estimated
    assert statuses_of(samples[110:]) == [cleaning.STATUS_OK] * 20


def test_clean_track_interpolates_short_spoofed_stretch(settings):
    # GIVEN a drive interrupted by 10 seconds of spoofed coordinates with a fake clock
    fake_clock = datetime.datetime(2028, 3, 10, 7, 0, 0)
    raw_samples = (
        make_drive(0, TRIP_START, 49.8, count=20)
        + make_drive(20, fake_clock, -12.03, count=10, lon=-77.04, kmh=200)
        + make_drive(
            30, TRIP_START + datetime.timedelta(seconds=30), 49.8 + 30 * LAT_STEP_30M, count=20
        )
    )

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN the spoofed values are replaced by interpolation between the neighbouring good fixes
    assert statuses_of(samples[20:30]) == [cleaning.STATUS_INTERPOLATED] * 10
    assert samples[25].lat == pytest.approx(49.8 + 25 * LAT_STEP_30M)
    assert samples[25].lon == pytest.approx(24.0)


def test_clean_track_rejects_unreachable_stretch_with_plausible_date(settings):
    # GIVEN a drive interrupted by a short jump to a far-away place with a correct-looking clock
    raw_samples = (
        make_drive(0, TRIP_START, 49.8, count=20)
        + make_drive(20, TRIP_START + datetime.timedelta(seconds=20), 50.5, count=5)
        + make_drive(
            25, TRIP_START + datetime.timedelta(seconds=25), 49.8 + 25 * LAT_STEP_30M, count=20
        )
    )

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN the jump is rejected because it cannot be chained with the rest of the drive
    assert statuses_of(samples[20:25]) == [cleaning.STATUS_INTERPOLATED] * 5
    assert samples[22].lat == pytest.approx(49.8 + 22 * LAT_STEP_30M)
    assert statuses_of(samples[:20]) == [cleaning.STATUS_OK] * 20
    assert statuses_of(samples[25:]) == [cleaning.STATUS_OK] * 20


def test_clean_track_rejects_segment_with_speed_mismatch(settings):
    # GIVEN fixes that claim 200 km/h while barely moving
    raw_samples = make_drive(0, TRIP_START, 49.8, count=20, lat_step=0.00001, kmh=200)

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN the segment is rejected
    assert set(statuses_of(samples)) == {cleaning.STATUS_SPOOFED}


def test_clean_track_keeps_segment_with_matching_speed(settings):
    # GIVEN fixes whose displayed speed matches their movement
    raw_samples = make_drive(0, TRIP_START, 49.8, count=20, kmh=108)

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN the segment is kept
    assert set(statuses_of(samples)) == {cleaning.STATUS_OK}


def test_clean_track_accepts_merge_gap_between_fixes(settings):
    # GIVEN two drives merged into one video, with a 2-hour parking gap between them
    later_clock = TRIP_START + datetime.timedelta(hours=2, seconds=20)
    end_lat = 49.8 + 19 * LAT_STEP_30M
    raw_samples = make_drive(0, TRIP_START, 49.8, count=20) + make_drive(
        20, later_clock, end_lat + 0.001, count=20
    )

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN both drives are kept and timed by the clock, including the gap
    assert set(statuses_of(samples)) == {cleaning.STATUS_OK}
    assert samples[20].time is not None
    assert samples[20].time.replace(tzinfo=None) == later_clock


def test_clean_track_adds_merge_gap_during_no_fix(settings):
    # GIVEN a no-fix stretch in which the camera clock jumps forward by one hour, then a drive
    raw_samples = (
        make_no_fix(0, TRIP_START, 10)
        + make_no_fix(10, TRIP_START + datetime.timedelta(hours=1, seconds=10), 10)
        + make_drive(20, TRIP_START + datetime.timedelta(hours=1, seconds=20), 49.8, count=10)
    )

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN times before the jump are derived across the gap, not just from video offsets
    assert samples[0].time is not None
    assert samples[0].time.replace(tzinfo=None) == TRIP_START
    assert not samples[0].time_estimated


def test_clean_track_flags_unreliable_text_as_unreadable(settings):
    # GIVEN a drive in which one reading has low OCR scores
    raw_samples = make_drive(0, TRIP_START, 49.8, count=10)
    bad_sample = raw_samples[5]
    raw_samples[5] = cleaning.RawSample(
        t=bad_sample.t,
        left_text=bad_sample.left_text,
        right_text=bad_sample.right_text,
        left_score=0.3,
        right_score=bad_sample.right_score,
    )

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN the reading is not used, and the short gap is interpolated
    assert samples[5].status == cleaning.STATUS_INTERPOLATED
    assert samples[5].lat == pytest.approx(49.8 + 5 * LAT_STEP_30M)


def test_clean_track_marks_unparseable_text_as_unreadable(settings):
    # GIVEN a reading with reliable but malformed GPS text at the start
    raw_samples = make_drive(0, TRIP_START, 49.8, count=5)
    raw_samples[0] = cleaning.RawSample(
        t=0,
        left_text="1 KM/H",
        right_text=raw_samples[0].right_text,
        left_score=0.9,
        right_score=0.9,
    )

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN it is unreadable (and not interpolated, since it has no earlier fix)
    assert samples[0].status == cleaning.STATUS_UNREADABLE


def test_clean_track_with_bad_range_override(settings):
    # GIVEN a normal drive and a manual override marking seconds 5-9 as bad
    raw_samples = make_drive(0, TRIP_START, 49.8, count=20)
    overrides = cleaning.Overrides(bad_ranges_s=[(5, 9)])

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings, overrides)

    # THEN the range is not used as fixes (and is interpolated, since the gap is short)
    assert statuses_of(samples[5:10]) == [cleaning.STATUS_INTERPOLATED] * 5


def test_clean_track_with_good_range_override(settings):
    # GIVEN a stretch rejected for its implausible clock date, marked good by hand
    fake_clock = datetime.datetime(2028, 3, 10, 7, 0, 0)
    raw_samples = make_drive(0, fake_clock, 49.8, count=10)
    overrides = cleaning.Overrides(good_ranges_s=[(0, 9)])

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings, overrides)

    # THEN the fixes are accepted
    assert statuses_of(samples) == [cleaning.STATUS_OK] * 10


def test_clean_track_without_good_fix_uses_camera_clock(settings):
    # GIVEN a video without any fix
    raw_samples = make_no_fix(0, TRIP_START, 5)

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN times come from the camera clock and are marked as estimated
    assert samples[0].time is not None
    assert samples[0].time.replace(tzinfo=None) == TRIP_START
    assert samples[0].time_estimated


def test_clean_track_without_good_fix_or_plausible_clock(settings):
    # GIVEN a video without any fix and with a clock far from the trip date
    raw_samples = make_no_fix(0, datetime.datetime(2020, 1, 1), 5)

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, settings)

    # THEN no time is assigned
    assert [sample.time for sample in samples] == [None] * 5


def test_clean_track_without_trip_date_skips_date_check(settings):
    # GIVEN a drive with a clock far in the future and no date in the filename
    undated_settings = dataclasses.replace(settings, trip_date=None)
    raw_samples = make_drive(0, datetime.datetime(2028, 3, 10, 7, 0, 0), 49.8, count=10)

    # WHEN cleaning it
    samples = cleaning.clean_track(raw_samples, undated_settings)

    # THEN the fixes are accepted
    assert statuses_of(samples) == [cleaning.STATUS_OK] * 10


def test_clean_track_with_empty_input(settings):
    # GIVEN no samples

    # WHEN cleaning
    samples = cleaning.clean_track([], settings)

    # THEN the result is empty
    assert samples == []


def test_parse_clock_text_with_invalid_date():
    # GIVEN a clock text with an impossible date
    right_text = "2026/13/45 10:00:00"

    # WHEN parsing it
    clock = cleaning.parse_clock_text(right_text)

    # THEN it is rejected
    assert clock is None


def load_fixture_track(name):
    return metadata.load_track_text(
        (TRACK_FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8")
    )


def reclean_fixture_track(name, settings):
    track = load_fixture_track(name)
    trip_date = metadata.parse_trip_name(track.video_filename).date
    fixture_settings = dataclasses.replace(settings, trip_date=trip_date)
    return cleaning.clean_track(track.raw_samples, fixture_settings, track.overrides)


def test_clean_track_golden_bad_gps_trip(settings):
    # GIVEN the raw readings of the sample trip with ~20 minutes of spoofed GPS

    # WHEN cleaning them
    samples = reclean_fixture_track("2026-08-03 Trip with Bad GPS", settings)

    # THEN the Lima coordinates are all rejected, and the real fixes at the end are kept
    good_samples = [sample for sample in samples if sample.status == cleaning.STATUS_OK]
    spoofed_samples = [sample for sample in samples if sample.status == cleaning.STATUS_SPOOFED]
    assert len(spoofed_samples) > 1000
    assert len(good_samples) > 180
    assert all(sample.lat is not None and sample.lat > 49 for sample in good_samples)
    assert min(sample.t for sample in good_samples) > 1200

    # AND the start without a fix gets estimated times derived from the end of the trip
    assert samples[0].status == cleaning.STATUS_NO_FIX
    assert samples[0].time_estimated
    assert samples[-1].time is not None
    assert samples[-1].time.isoformat() == "2026-08-03T13:47:17+03:00"


def test_clean_track_golden_night_trip_with_merge_gap(settings):
    # GIVEN the raw readings of the night trip, which contains a ~4-minute merge gap

    # WHEN cleaning them
    samples = reclean_fixture_track("2024-02-11 Night", settings)

    # THEN the gap is kept as a gap, and the clock jump is reflected in the times
    gap_index = next(
        index for index, sample in enumerate(samples) if sample.status == cleaning.STATUS_NO_FIX
    )
    before = samples[gap_index - 1]
    after = samples[gap_index + 1]
    assert before.time is not None and after.time is not None
    assert (after.time - before.time).total_seconds() > 200
    assert sum(sample.status == cleaning.STATUS_OK for sample in samples) == len(samples) - 1


def test_clean_track_golden_snow_trip_with_camera_glitches(settings):
    # GIVEN the raw readings of the snow trip, in which the camera showed garbage twice

    # WHEN cleaning them
    samples = reclean_fixture_track("2026-01-08 Heavy Snow", settings)

    # THEN the garbage seconds are interpolated and nothing is rejected
    statuses = statuses_of(samples)
    assert statuses.count(cleaning.STATUS_INTERPOLATED) == 2
    assert cleaning.STATUS_SPOOFED not in statuses
