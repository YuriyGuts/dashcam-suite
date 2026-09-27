"""
Turn raw overlay readings into a clean GPS track.

Cleaning runs on the raw readings stored in a track, so it can be repeated without decoding
the video again. The steps are:

1. Parse every reading. Blank GPS text means no fix. GPS text that cannot be trusted or parsed
   is unreadable. Fixes outside the allowed areas from the config are spoofed. The rest are
   candidate fixes. A camera clock date with the year last is read as day/month or month/day,
   whichever the video's dates show (see `infer_day_first`).
2. Split the candidate fixes into segments in which consecutive points are physically
   reachable from each other (implied speed at most `MAX_SPEED_KMH`).
3. Reject segments whose camera clock shows a date too far from the date in the filename, and
   segments whose displayed speed does not match the speed implied by their coordinates.
   Consecutive segments without a teleport between them are grouped into places, and the
   clock check is also applied to each place as a whole. A spoof often starts with the real
   date and only later sets the camera clock to its own, so the first part of it is caught
   this way.
4. Keep the heaviest time-ordered chain of the remaining segments in which every segment is
   reachable from the previous one. Everything else is spoofed. This catches long spoofs in
   which the fake points agree with each other.
5. Assign times. The camera clock is trusted on good fixes. Elsewhere, time is derived from the
   video offset, anchored to the nearest good fix. Forward jumps of the camera clock (up to
   `MAX_MERGE_GAP_HOURS`) mark gaps between merged segments and are added to derived times.
   Derived times that cross samples without a trustworthy clock are flagged as estimated.
6. Fill gaps of at most `MAX_INTERPOLATION_GAP_S` between good fixes by linear interpolation.

Manual overrides from the track are applied on top: bad ranges are excluded before step 2,
and good ranges are accepted after step 4.
"""

import dataclasses
import datetime
import re
import statistics
import zoneinfo

from dashcam import geo
from dashcam import overlay
from dashcam.config import Config

# Sample statuses.
STATUS_OK = "ok"
STATUS_INTERPOLATED = "interpolated"
STATUS_NO_FIX = "no_fix"
STATUS_SPOOFED = "spoofed"
STATUS_UNREADABLE = "unreadable"
ALL_STATUSES = frozenset(
    [STATUS_OK, STATUS_INTERPOLATED, STATUS_NO_FIX, STATUS_SPOOFED, STATUS_UNREADABLE]
)

# Statuses of samples with a usable position.
LOCATED_STATUSES = frozenset([STATUS_OK, STATUS_INTERPOLATED])

# Camera clock text: a date with the year first or last, and a 24-hour time.
CLOCK_TEXT_PATTERN = re.compile(
    r"^(?P<date_first>\d{1,4})[/.](?P<date_middle>\d{1,2})[/.](?P<date_last>\d{1,4}) "
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})$"
)
YEAR_DIGIT_COUNT = 4

# Highest month number. A larger value in a date with the year last must be the day.
MAX_MONTH = 12

# How much (in seconds) `camera time - video offset` may drift between neighboring samples
# before it counts as a clock jump. Sampling can shift a reading by up to one second.
CLOCK_JITTER_S = 1.5

# Speed consistency check: the displayed speed is compared with the speed implied by points
# about `SPEED_CHECK_SPAN_S` apart. The GPS position can lag the displayed speed by a second,
# so single-second comparisons are too noisy.
SPEED_CHECK_SPAN_S = 5
MIN_SPEED_CHECK_COUNT = 5
MAX_MEDIAN_SPEED_MISMATCH_KMH = 30

# The highest plausible speed.
MAX_SPEED_KMH = 250

# Jumps between consecutive segments at up to this implied speed are GPS noise within one place
# (a spoofed loop can briefly exceed `MAX_SPEED_KMH`). Faster jumps are teleports.
MAX_PLACE_JUMP_SPEED_KMH = 1000

# How far the camera clock date may be from the date in the video filename.
MAX_CLOCK_DATE_DIFF_DAYS = 1

# The longest gap between merged segments inside one trip video.
MAX_MERGE_GAP_HOURS = 12

# The longest gap in GPS data that is filled in by linear interpolation.
MAX_INTERPOLATION_GAP_S = 60


@dataclasses.dataclass(frozen=True)
class RawSample:
    """One overlay reading per camera clock tick."""

    t: float
    gps_text: str
    clock_text: str
    gps_score: float
    clock_score: float

    @property
    def is_gps_text_reliable(self) -> bool:
        return bool(self.gps_text) and self.gps_score >= overlay.MIN_RELIABLE_CELL_SCORE

    @property
    def is_clock_text_reliable(self) -> bool:
        return bool(self.clock_text) and self.clock_score >= overlay.MIN_RELIABLE_CELL_SCORE


@dataclasses.dataclass(frozen=True)
class Overrides:
    """Manual corrections: video offset ranges (in seconds) to treat as spoofed or as good."""

    bad_ranges_s: list[tuple[float, float]] = dataclasses.field(default_factory=list)
    good_ranges_s: list[tuple[float, float]] = dataclasses.field(default_factory=list)

    def is_bad(self, t: float) -> bool:
        return any(start <= t <= end for start, end in self.bad_ranges_s)

    def is_good(self, t: float) -> bool:
        return any(start <= t <= end for start, end in self.good_ranges_s)


@dataclasses.dataclass(frozen=True)
class CleaningSettings:
    """Context for cleaning one track."""

    trip_date: datetime.date | None
    timezone: str

    # Boxes of `(min_lat, min_lon, max_lat, max_lon)`. Empty means anywhere.
    allowed_areas: tuple[tuple[float, float, float, float], ...] = ()

    @classmethod
    def from_config(cls, config: Config, trip_date: datetime.date | None) -> "CleaningSettings":
        allowed_areas = tuple(
            (min_lat, min_lon, max_lat, max_lon)
            for min_lat, min_lon, max_lat, max_lon in config.allowed_areas
        )
        return cls(trip_date=trip_date, timezone=config.timezone, allowed_areas=allowed_areas)

    def is_allowed_position(self, lat: float, lon: float) -> bool:
        if not self.allowed_areas:
            return True
        return any(
            min_lat <= lat <= max_lat and min_lon <= lon <= max_lon
            for min_lat, min_lon, max_lat, max_lon in self.allowed_areas
        )


@dataclasses.dataclass
class CleanSample:
    """A cleaned sample. Coordinates are set for `ok` and `interpolated` samples only."""

    t: float
    time: datetime.datetime | None
    lat: float | None
    lon: float | None
    kmh: int | None
    status: str
    time_estimated: bool = False


@dataclasses.dataclass(frozen=True)
class ParsedSample:
    """A raw sample with its values parsed."""

    t: float
    gps: overlay.GpsReading | None
    clock: datetime.datetime | None
    is_clock_trusted: bool
    initial_status: str


def split_year_last_date(clock_text: str) -> tuple[int, int] | None:
    """Return the first two date numbers if the clock text has the year last, or None."""
    match = CLOCK_TEXT_PATTERN.match(clock_text)
    if match is None or len(match["date_last"]) != YEAR_DIGIT_COUNT:
        return None
    return int(match["date_first"]), int(match["date_middle"])


def parse_clock_text(clock_text: str, is_day_first: bool = True) -> datetime.datetime | None:
    """
    Parse the camera clock text, or return None if it is not a valid date and time.

    A date with the year first is year/month/day. A date with the year last is day/month/year
    if `is_day_first`, and month/day/year otherwise.
    """
    match = CLOCK_TEXT_PATTERN.match(clock_text)
    if match is None:
        return None
    if len(match["date_first"]) == YEAR_DIGIT_COUNT and len(match["date_last"]) <= 2:
        year, month, day = match["date_first"], match["date_middle"], match["date_last"]
    elif len(match["date_last"]) == YEAR_DIGIT_COUNT and len(match["date_first"]) <= 2:
        year = match["date_last"]
        if is_day_first:
            day, month = match["date_first"], match["date_middle"]
        else:
            month, day = match["date_first"], match["date_middle"]
    else:
        return None

    try:
        return datetime.datetime(
            int(year),
            int(month),
            int(day),
            int(match["hour"]),
            int(match["minute"]),
            int(match["second"]),
        )
    except ValueError:
        return None


def infer_day_first(clock_texts: list[str], trip_date: datetime.date | None) -> bool:
    """
    Decide whether the dates with the year last show the day or the month first.

    A number above 12 can only be the day, so the position where such numbers appear decides.
    If every date is ambiguous (e.g. `05/04/2024`), the order whose dates fall on the trip date
    more often wins. Day first is the default.
    """
    year_last_dates = [
        date_numbers
        for clock_text in clock_texts
        if (date_numbers := split_year_last_date(clock_text)) is not None
    ]
    day_first_votes = sum(first > MAX_MONTH for first, _ in year_last_dates)
    month_first_votes = sum(middle > MAX_MONTH for _, middle in year_last_dates)
    if day_first_votes or month_first_votes:
        return day_first_votes >= month_first_votes
    if trip_date is None:
        return True

    def count_trip_date_matches(is_day_first: bool) -> int:
        clocks = [parse_clock_text(clock_text, is_day_first) for clock_text in clock_texts]
        return sum(clock is not None and clock.date() == trip_date for clock in clocks)

    return count_trip_date_matches(True) >= count_trip_date_matches(False)


def parse_samples(
    raw_samples: list[RawSample],
    settings: CleaningSettings,
    overrides: Overrides,
) -> list[ParsedSample]:
    """Parse the raw readings and assign initial statuses."""
    reliable_clock_texts = [
        raw_sample.clock_text for raw_sample in raw_samples if raw_sample.is_clock_text_reliable
    ]
    is_day_first = infer_day_first(reliable_clock_texts, settings.trip_date)

    parsed_samples = []
    for raw_sample in raw_samples:
        gps = None
        if raw_sample.is_gps_text_reliable:
            gps = overlay.parse_gps_text(raw_sample.gps_text)

        clock = None
        if raw_sample.is_clock_text_reliable:
            clock = parse_clock_text(raw_sample.clock_text, is_day_first)

        if not raw_sample.gps_text:
            initial_status = STATUS_NO_FIX
        elif gps is None:
            initial_status = STATUS_UNREADABLE
        elif overrides.is_bad(raw_sample.t) or not settings.is_allowed_position(gps.lat, gps.lon):
            initial_status = STATUS_SPOOFED
        else:
            initial_status = STATUS_OK

        parsed_samples.append(
            ParsedSample(
                t=raw_sample.t,
                gps=gps,
                clock=clock,
                is_clock_trusted=is_clock_plausible(clock, settings),
                initial_status=initial_status,
            )
        )
    return parsed_samples


def is_clock_plausible(clock: datetime.datetime | None, settings: CleaningSettings) -> bool:
    """Check whether a camera clock value is close enough to the trip date."""
    if clock is None:
        return False
    if settings.trip_date is None:
        return True
    date_diff_days = abs((clock.date() - settings.trip_date).days)
    return date_diff_days <= MAX_CLOCK_DATE_DIFF_DAYS


def split_into_segments(parsed_samples: list[ParsedSample]) -> list[list[int]]:
    """
    Split the candidate fixes into segments of physically reachable consecutive points.

    Returns
    -------
    list[list[int]]
        Sample indexes of each segment, in time order.
    """
    segments: list[list[int]] = []
    previous_index = None
    for index, sample in enumerate(parsed_samples):
        if sample.initial_status != STATUS_OK or sample.gps is None:
            continue
        if previous_index is None:
            segments.append([index])
            previous_index = index
            continue

        previous = parsed_samples[previous_index]
        assert previous.gps is not None
        distance_m = geo.haversine_m(
            previous.gps.lat, previous.gps.lon, sample.gps.lat, sample.gps.lon
        )
        # GPS updates once per second, so allow at least one second between readings.
        duration_s = max(1.0, sample.t - previous.t)
        if geo.implied_speed_kmh(distance_m, duration_s) > MAX_SPEED_KMH:
            segments.append([index])
        else:
            segments[-1].append(index)
        previous_index = index
    return segments


def group_into_places(
    segments: list[list[int]], parsed_samples: list[ParsedSample]
) -> list[list[list[int]]]:
    """
    Group consecutive segments without a teleport between them.

    Returns
    -------
    list[list[list[int]]]
        The segments of each place, in time order.
    """
    places: list[list[list[int]]] = []
    for segment in segments:
        if places:
            last_sample = parsed_samples[places[-1][-1][-1]]
            first_sample = parsed_samples[segment[0]]
            assert last_sample.gps is not None and first_sample.gps is not None
            distance_m = geo.haversine_m(
                last_sample.gps.lat, last_sample.gps.lon, first_sample.gps.lat, first_sample.gps.lon
            )
            duration_s = max(1.0, first_sample.t - last_sample.t)
            if geo.implied_speed_kmh(distance_m, duration_s) <= MAX_PLACE_JUMP_SPEED_KMH:
                places[-1].append(segment)
                continue
        places.append([segment])
    return places


def has_implausible_clock(indexes: list[int], parsed_samples: list[ParsedSample]) -> bool:
    """Check whether most readable clocks among the samples show an implausible date."""
    readable_clock_samples = [
        parsed_samples[index] for index in indexes if parsed_samples[index].clock is not None
    ]
    if not readable_clock_samples:
        return False
    implausible_count = sum(not sample.is_clock_trusted for sample in readable_clock_samples)
    return implausible_count * 2 > len(readable_clock_samples)


def has_speed_mismatch(segment: list[int], parsed_samples: list[ParsedSample]) -> bool:
    """Check whether the displayed speed disagrees with the speed implied by the coordinates."""
    mismatches = []
    end_position = 0
    for start_position, start_index in enumerate(segment):
        start = parsed_samples[start_index]
        end_position = max(end_position, start_position + 1)
        while (
            end_position < len(segment)
            and parsed_samples[segment[end_position]].t - start.t < SPEED_CHECK_SPAN_S
        ):
            end_position += 1
        if end_position >= len(segment):
            break

        span_gps = [
            parsed_samples[index].gps for index in segment[start_position : end_position + 1]
        ]
        start_gps = span_gps[0]
        end_gps = span_gps[-1]
        assert start_gps is not None and end_gps is not None
        distance_m = geo.haversine_m(start_gps.lat, start_gps.lon, end_gps.lat, end_gps.lon)
        duration_s = parsed_samples[segment[end_position]].t - start.t
        displayed_speeds = [
            gps.speed_kmh for gps in span_gps if gps is not None and gps.speed_kmh is not None
        ]
        if not displayed_speeds:
            continue
        implied_kmh = geo.implied_speed_kmh(distance_m, duration_s)
        mismatches.append(abs(implied_kmh - statistics.mean(displayed_speeds)))

    if len(mismatches) < MIN_SPEED_CHECK_COUNT:
        return False
    return statistics.median(mismatches) > MAX_MEDIAN_SPEED_MISMATCH_KMH


def is_reachable(from_sample: ParsedSample, to_sample: ParsedSample) -> bool:
    """
    Check whether the car could get from one fix to a later one.

    If both clocks are trusted and show a forward jump of up to `MAX_MERGE_GAP_HOURS`, the
    time between the fixes includes that gap (e.g. the car was parked between two merged
    segments). Otherwise, the time between the fixes is taken from the video offsets.
    """
    assert from_sample.gps is not None and to_sample.gps is not None
    duration_s = max(1.0, to_sample.t - from_sample.t)
    if (
        from_sample.is_clock_trusted
        and to_sample.is_clock_trusted
        and from_sample.clock is not None
        and to_sample.clock is not None
    ):
        clock_duration_s = (to_sample.clock - from_sample.clock).total_seconds()
        max_merge_gap_s = MAX_MERGE_GAP_HOURS * 3600
        if duration_s - CLOCK_JITTER_S <= clock_duration_s <= duration_s + max_merge_gap_s:
            duration_s = max(duration_s, clock_duration_s)

    distance_m = geo.haversine_m(
        from_sample.gps.lat, from_sample.gps.lon, to_sample.gps.lat, to_sample.gps.lon
    )
    return geo.implied_speed_kmh(distance_m, duration_s) <= MAX_SPEED_KMH


def select_segment_chain(
    segments: list[list[int]], parsed_samples: list[ParsedSample]
) -> list[list[int]]:
    """
    Find the time-ordered chain of mutually reachable segments with the most points.

    Returns
    -------
    list[list[int]]
        The segments in the chain.
    """
    if not segments:
        return []

    best_weights = [0] * len(segments)
    best_predecessors: list[int | None] = [None] * len(segments)
    for index, segment in enumerate(segments):
        best_weights[index] = len(segment)
        first_sample = parsed_samples[segment[0]]
        for previous_index in range(index):
            last_sample = parsed_samples[segments[previous_index][-1]]
            weight = best_weights[previous_index] + len(segment)
            if weight > best_weights[index] and is_reachable(last_sample, first_sample):
                best_weights[index] = weight
                best_predecessors[index] = previous_index

    chain = []
    current_index: int | None = max(range(len(segments)), key=lambda index: best_weights[index])
    while current_index is not None:
        chain.append(segments[current_index])
        current_index = best_predecessors[current_index]
    return list(reversed(chain))


def detect_good_fixes(parsed_samples: list[ParsedSample], overrides: Overrides) -> list[str]:
    """
    Decide which candidate fixes are genuine.

    Returns
    -------
    list[str]
        The status of every sample.
    """
    statuses = [sample.initial_status for sample in parsed_samples]
    for index, status in enumerate(statuses):
        if status == STATUS_OK:
            statuses[index] = STATUS_SPOOFED

    segments = []
    for place in group_into_places(split_into_segments(parsed_samples), parsed_samples):
        place_indexes = [index for segment in place for index in segment]
        if has_implausible_clock(place_indexes, parsed_samples):
            continue
        segments.extend(
            segment
            for segment in place
            if not has_implausible_clock(segment, parsed_samples)
            and not has_speed_mismatch(segment, parsed_samples)
        )
    for segment in select_segment_chain(segments, parsed_samples):
        for index in segment:
            statuses[index] = STATUS_OK

    for index, sample in enumerate(parsed_samples):
        if sample.gps is not None and overrides.is_good(sample.t):
            statuses[index] = STATUS_OK

    return statuses


def compute_clock_jumps(parsed_samples: list[ParsedSample]) -> list[float]:
    """
    Compute the merge gap (in seconds) between each sample and the previous one.

    A gap is a forward jump of `camera time - video offset` between two samples with trusted
    clocks. Jumps within `CLOCK_JITTER_S` are sampling noise and count as zero.
    """
    max_merge_gap_s = MAX_MERGE_GAP_HOURS * 3600
    jumps = [0.0] * len(parsed_samples)
    for index in range(1, len(parsed_samples)):
        previous = parsed_samples[index - 1]
        current = parsed_samples[index]
        if not (previous.is_clock_trusted and current.is_clock_trusted):
            continue
        assert previous.clock is not None and current.clock is not None
        jump_s = (current.clock - previous.clock).total_seconds() - (current.t - previous.t)
        if CLOCK_JITTER_S < jump_s <= max_merge_gap_s:
            jumps[index] = jump_s
    return jumps


def assign_times(
    parsed_samples: list[ParsedSample],
    statuses: list[str],
    settings: CleaningSettings,
) -> list[tuple[datetime.datetime | None, bool]]:
    """
    Assign a time to every sample.

    Returns
    -------
    list[tuple[datetime.datetime | None, bool]]
        The local time of every sample and whether it is estimated.
    """
    timezone = zoneinfo.ZoneInfo(settings.timezone)
    jumps = compute_clock_jumps(parsed_samples)

    # Cumulative merge gaps and count of samples without a trusted clock, so that the values
    # between any two samples can be looked up in constant time.
    cumulative_jumps = [0.0]
    cumulative_untrusted = [0]
    for index, sample in enumerate(parsed_samples):
        cumulative_jumps.append(cumulative_jumps[-1] + jumps[index])
        cumulative_untrusted.append(cumulative_untrusted[-1] + (not sample.is_clock_trusted))

    anchor_indexes = [
        index
        for index, sample in enumerate(parsed_samples)
        if statuses[index] == STATUS_OK and sample.is_clock_trusted
    ]

    def derive_time(anchor_index: int, index: int) -> tuple[datetime.datetime, bool]:
        anchor = parsed_samples[anchor_index]
        assert anchor.clock is not None
        # Merge gaps on the steps between the anchor and the sample, and the number of samples
        # on the way (excluding the anchor) whose missing or untrusted clock could hide a gap.
        if index > anchor_index:
            gap_s = cumulative_jumps[index + 1] - cumulative_jumps[anchor_index + 1]
            untrusted_count = (
                cumulative_untrusted[index + 1] - cumulative_untrusted[anchor_index + 1]
            )
        else:
            gap_s = -(cumulative_jumps[anchor_index + 1] - cumulative_jumps[index + 1])
            untrusted_count = cumulative_untrusted[anchor_index] - cumulative_untrusted[index]
        # The camera clock has one-second resolution, so derived times are rounded to it.
        offset_s = round(parsed_samples[index].t - anchor.t + gap_s)
        derived_time = anchor.clock + datetime.timedelta(seconds=offset_s)
        return derived_time, untrusted_count > 0

    times: list[tuple[datetime.datetime | None, bool]] = []
    anchor_position = 0
    for index, sample in enumerate(parsed_samples):
        if statuses[index] == STATUS_OK and sample.is_clock_trusted:
            assert sample.clock is not None
            times.append((sample.clock.replace(tzinfo=timezone), False))
            continue

        if not anchor_indexes:
            # No good fix at all: fall back to the camera clock, which may be off.
            if sample.is_clock_trusted and sample.clock is not None:
                times.append((sample.clock.replace(tzinfo=timezone), True))
            else:
                times.append((None, True))
            continue

        while anchor_position < len(anchor_indexes) and anchor_indexes[anchor_position] < index:
            anchor_position += 1
        candidates = []
        if anchor_position > 0:
            candidates.append(anchor_indexes[anchor_position - 1])
        if anchor_position < len(anchor_indexes):
            candidates.append(anchor_indexes[anchor_position])

        derived = [
            (is_estimated, abs(parsed_samples[anchor].t - sample.t), derived_time)
            for anchor in candidates
            for derived_time, is_estimated in [derive_time(anchor, index)]
        ]
        # Prefer a derivation that crosses only trusted clocks, then the nearest anchor.
        is_estimated, _, derived_time = min(derived, key=lambda item: (item[0], item[1]))
        times.append((derived_time.replace(tzinfo=timezone), is_estimated))

    return times


def interpolate_short_gaps(samples: list[CleanSample]) -> None:
    """Fill runs of samples without a good fix between two good fixes, if the gap is short."""
    good_indexes = [index for index, sample in enumerate(samples) if sample.status == STATUS_OK]
    for previous_index, next_index in zip(good_indexes, good_indexes[1:], strict=False):
        if next_index - previous_index < 2:
            continue
        previous = samples[previous_index]
        following = samples[next_index]
        assert previous.lat is not None and previous.lon is not None
        assert following.lat is not None and following.lon is not None

        if previous.time is not None and following.time is not None:
            gap_s = (following.time - previous.time).total_seconds()
        else:
            gap_s = following.t - previous.t
        if gap_s > MAX_INTERPOLATION_GAP_S or gap_s <= 0:
            continue

        for index in range(previous_index + 1, next_index):
            sample = samples[index]
            if sample.time is not None and previous.time is not None:
                elapsed_s = (sample.time - previous.time).total_seconds()
            else:
                elapsed_s = sample.t - previous.t
            fraction = min(1.0, max(0.0, elapsed_s / gap_s))
            sample.lat = previous.lat + (following.lat - previous.lat) * fraction
            sample.lon = previous.lon + (following.lon - previous.lon) * fraction
            sample.status = STATUS_INTERPOLATED


def clean_track(
    raw_samples: list[RawSample],
    settings: CleaningSettings,
    overrides: Overrides | None = None,
) -> list[CleanSample]:
    """Clean the raw readings of one video. See the module docstring for the steps."""
    if overrides is None:
        overrides = Overrides()

    parsed_samples = parse_samples(raw_samples, settings, overrides)
    statuses = detect_good_fixes(parsed_samples, overrides)
    times = assign_times(parsed_samples, statuses, settings)

    samples = []
    for parsed_sample, status, (time, is_time_estimated) in zip(
        parsed_samples, statuses, times, strict=True
    ):
        good_gps = parsed_sample.gps if status == STATUS_OK else None
        samples.append(
            CleanSample(
                t=parsed_sample.t,
                time=time,
                lat=good_gps.lat if good_gps else None,
                lon=good_gps.lon if good_gps else None,
                kmh=good_gps.speed_kmh if good_gps else None,
                status=status,
                time_estimated=is_time_estimated,
            )
        )

    interpolate_short_gaps(samples)
    return samples
