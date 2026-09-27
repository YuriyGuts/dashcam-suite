import datetime
import json
import logging
import math

import pytest

from dashcam import cleaning
from dashcam import enrich
from dashcam import metadata
from dashcam import osm

KYIV_SUMMER = datetime.timezone(datetime.timedelta(hours=3))


@pytest.fixture
def make_driven_track(make_track, to_lat_lon):
    """Make a track from points in meters east and north of the synthetic map origin."""

    def _make_driven_track(points_m, statuses=None, video_filename="2026-09-25 Trip 11-17.mp4"):
        track = make_track(video_filename=video_filename, sample_count=0)
        start_time = datetime.datetime(2026, 9, 25, 11, 17, tzinfo=KYIV_SUMMER)
        for index, (east_m, north_m) in enumerate(points_m):
            status = statuses[index] if statuses else cleaning.STATUS_OK
            lat, lon = to_lat_lon(east_m, north_m)
            is_located = status in enrich.LOCATED_STATUSES
            track.raw_samples.append(
                cleaning.RawSample(
                    t=float(index), gps_text="", clock_text="", gps_score=1.0, clock_score=1.0
                )
            )
            track.clean_samples.append(
                cleaning.CleanSample(
                    t=float(index),
                    time=start_time + datetime.timedelta(seconds=index),
                    lat=round(lat, 6) if is_located else None,
                    lon=round(lon, 6) if is_located else None,
                    kmh=36,
                    status=status,
                )
            )
        track.duration_s = float(len(points_m))
        return track

    return _make_driven_track


def drive(start_m, end_m, step_m=10.0):
    """Return points every `step_m` meters along a straight line, including both ends."""
    east_m = end_m[0] - start_m[0]
    north_m = end_m[1] - start_m[1]
    step_count = max(1, round(math.hypot(east_m, north_m) / step_m))
    return [
        (start_m[0] + east_m * index / step_count, start_m[1] + north_m * index / step_count)
        for index in range(step_count + 1)
    ]


def street_names(track):
    return [street["name"] for street in track.streets]


def test_enrich_track_along_one_street(make_driven_track, road_database):
    # GIVEN a drive along `Main`
    track = make_driven_track(drive((50, 0), (450, 0)))

    # WHEN enriching it
    enrich.enrich_track(track, road_database)

    # THEN the street list has `Main` with its tags, distance, and video time range
    assert track.streets == [
        {
            "name": "Main",
            "name_en": "Main Street",
            "highway": "primary",
            "distance_m": pytest.approx(400, abs=2),
            "start_t": 0.0,
            "end_t": 40.0,
        }
    ]


def test_enrich_track_with_turn_at_intersection(make_driven_track, road_database):
    # GIVEN a drive east along `Main` and then north along `Cross`, past `Parallel`
    points_m = drive((100, 0), (500, 0)) + drive((500, 0), (500, 400))[1:]
    track = make_driven_track(points_m)

    # WHEN enriching it
    enrich.enrich_track(track, road_database)

    # THEN the streets follow the direction of travel, ignoring the crossed `Parallel`
    assert street_names(track) == ["Main", "Cross"]
    assert [street["distance_m"] for street in track.streets] == [
        pytest.approx(400, abs=15),
        pytest.approx(400, abs=15),
    ]


def test_match_run_ignores_gps_noise_toward_nearby_street(make_driven_track, road_database):
    # GIVEN a drive along `Main` where every fifth sample is closer to `Parallel`
    points_m = [
        (east_m, 45.0 if index % 5 == 2 else 0.0)
        for index, (east_m, _) in enumerate(drive((0, 0), (400, 0)))
    ]
    (run,) = enrich.split_into_runs(make_driven_track(points_m))

    # WHEN matching the samples
    matches = enrich.match_run(road_database, run)

    # THEN every sample stays on `Main`
    assert {match.street_key if match else None for match in matches} == {("Main", None)}


def test_find_candidates_prefers_street_along_heading(road_database, to_lat_lon):
    # GIVEN a sample at the intersection of `Main` (east-west) and `Cross` (north-south)
    sample = enrich.LocatedSample(0.0, *to_lat_lon(505, 5))

    # WHEN finding candidates while driving north and while driving east
    costs_north = {
        candidate.street_key[0]: candidate.cost
        for candidate in enrich.find_candidates(road_database, sample, heading=(0.0, 1.0))
    }
    costs_east = {
        candidate.street_key[0]: candidate.cost
        for candidate in enrich.find_candidates(road_database, sample, heading=(1.0, 0.0))
    }

    # THEN the street along the direction of travel is cheaper
    assert costs_north["Cross"] < costs_north["Main"]
    assert costs_east["Main"] < costs_east["Cross"]


def test_enrich_track_names_ref_only_road_by_ref(make_driven_track, road_database):
    # GIVEN a drive along the highway that has a ref but no name
    track = make_driven_track(drive((100, -1000), (900, -1000)))

    # WHEN enriching it
    enrich.enrich_track(track, road_database)

    # THEN the street is named by its ref
    assert track.streets[0]["name"] == "M06"
    assert track.streets[0]["ref"] == "M06"
    assert track.streets[0]["highway"] == "trunk"


def test_enrich_track_ignores_footways_and_unnamed_roads(make_driven_track, road_database):
    # GIVEN drives along a named footway and along an unnamed road
    points_m = drive((100, -300), (900, -300)) + drive((900, -600), (100, -600))
    track = make_driven_track(points_m)

    # WHEN enriching it
    enrich.enrich_track(track, road_database)

    # THEN no street is matched
    assert track.streets == []


def test_enrich_track_merges_street_across_gps_gap(make_driven_track, road_database):
    # GIVEN a drive along `Main` with a no-fix gap in the middle
    points_m = drive((0, 0), (900, 0))
    statuses = [
        cleaning.STATUS_NO_FIX if 40 <= index < 50 else cleaning.STATUS_OK
        for index in range(len(points_m))
    ]
    track = make_driven_track(points_m, statuses=statuses)

    # WHEN enriching it
    enrich.enrich_track(track, road_database)

    # THEN `Main` appears once, without the distance of the gap
    assert street_names(track) == ["Main"]
    assert track.streets[0]["distance_m"] == pytest.approx(790, abs=2)


def test_enrich_track_without_located_samples(make_driven_track, road_database):
    # GIVEN a track without a GPS fix
    points_m = drive((0, 0), (100, 0))
    track = make_driven_track(points_m, statuses=[cleaning.STATUS_NO_FIX] * len(points_m))

    # WHEN enriching it
    enrich.enrich_track(track, road_database)

    # THEN there are no streets or localities, but the track is marked as enriched
    assert track.streets == []
    assert track.localities == {"start": None, "end": None}
    assert track.enrichment is not None


def test_enrich_track_finds_start_and_end_localities(make_driven_track, road_database):
    # GIVEN a drive from the city into open country near a village
    points_m = drive((0, 0), (0, -11500), step_m=30)
    track = make_driven_track(points_m)

    # WHEN enriching it
    enrich.enrich_track(track, road_database)

    # THEN the trip starts in the city and ends in the village
    assert track.localities == {
        "start": {"name": "Львів", "name_en": "Lviv", "place": "city"},
        "end": {"name": "Сокільники", "place": "village"},
    }


def test_find_locality_outside_all_localities(road_database, to_lat_lon):
    # GIVEN a point far from the city and the village
    lat, lon = to_lat_lon(0, -30000)

    # WHEN looking up its locality
    locality = enrich.find_locality(road_database, lat, lon)

    # THEN there is none
    assert locality is None


def test_get_locality_radius_grows_with_population():
    # GIVEN a small city and a large one
    small_city = osm.Locality("A", None, "city", 10_000, 0.0, 0.0)
    large_city = osm.Locality("B", None, "city", 1_000_000, 0.0, 0.0)

    # WHEN computing their radii
    small_radius_m = enrich.get_locality_radius_m(small_city)
    large_radius_m = enrich.get_locality_radius_m(large_city)

    # THEN the small city gets the default radius and the large one a larger one
    assert small_radius_m == enrich.LOCALITY_RADII_M["city"]
    assert large_radius_m == pytest.approx(10_000)


def test_merge_stretches_drops_short_and_off_road_stretches():
    # GIVEN stretches with a short detour and an off-road part between two parts of a street
    stretches = [
        enrich.Stretch(("A", None), 0.0, 50.0, 500.0, {1: 500.0}),
        enrich.Stretch(None, 51.0, 60.0, 100.0),
        enrich.Stretch(("B", None), 61.0, 63.0, 30.0, {2: 30.0}),
        enrich.Stretch(("A", None), 64.0, 100.0, 400.0, {3: 400.0}),
    ]

    # WHEN merging them
    merged = enrich.merge_stretches(stretches)

    # THEN one stretch of `A` remains, covering both parts
    assert len(merged) == 1
    assert merged[0].street_key == ("A", None)
    assert (merged[0].start_t, merged[0].end_t) == (0.0, 100.0)
    assert merged[0].distance_m == 900.0
    assert merged[0].distance_by_road_id == {1: 500.0, 3: 400.0}


def test_estimate_heading_when_stationary():
    # GIVEN a car standing still with a little GPS jitter
    run = [enrich.LocatedSample(float(index), 49.8 + index % 2 * 1e-6, 24.0) for index in range(12)]

    # WHEN estimating the heading
    heading = enrich.estimate_heading(run, 6)

    # THEN it is unknown
    assert heading is None


def test_estimate_heading_when_moving_north(to_lat_lon):
    # GIVEN a car driving north
    run = [enrich.LocatedSample(float(index), *to_lat_lon(0, index * 10.0)) for index in range(5)]

    # WHEN estimating the heading
    heading = enrich.estimate_heading(run, 2)

    # THEN it points north
    assert heading == pytest.approx((0.0, 1.0), abs=1e-3)


def test_measure_segment_beyond_its_end():
    # GIVEN a segment from 10 m to 20 m east of the sample
    start = (10.0, 0.0)
    end = (20.0, 0.0)

    # WHEN measuring it
    distance_m, direction = enrich.measure_segment(start, end)

    # THEN the distance is to the nearer end, and the direction points east
    assert distance_m == pytest.approx(10.0)
    assert direction == pytest.approx((1.0, 0.0))


def test_measure_segment_with_zero_length():
    distance_m, direction = enrich.measure_segment((3.0, 4.0), (3.0, 4.0))

    assert distance_m == pytest.approx(5.0)
    assert direction is None


def test_split_into_runs_breaks_at_position_jumps(make_driven_track):
    # GIVEN a track whose position jumps by 1 km between two samples
    track = make_driven_track(drive((0, 0), (100, 0)) + drive((1100, 0), (1200, 0)))

    # WHEN splitting it into runs
    runs = enrich.split_into_runs(track)

    # THEN the jump separates two runs
    assert [len(run) for run in runs] == [11, 11]


def test_is_enrichment_current_without_enrichment(make_driven_track):
    track = make_driven_track(drive((0, 0), (100, 0)))

    assert not enrich.is_enrichment_current(track, osm_timestamp=None)


def test_is_enrichment_current_after_enrichment(make_driven_track, road_database):
    # GIVEN an enriched track
    track = make_driven_track(drive((0, 0), (100, 0)))
    enrich.enrich_track(track, road_database)

    # WHEN checking it against the same OSM data, and without OSM data
    is_current = enrich.is_enrichment_current(track, road_database.osm_timestamp)
    is_current_without_osm = enrich.is_enrichment_current(track, osm_timestamp=None)

    # THEN it is current
    assert is_current
    assert is_current_without_osm


def test_is_enrichment_current_with_changed_samples(make_driven_track, road_database):
    # GIVEN an enriched track whose positions changed afterwards (e.g. after `--reclean`)
    track = make_driven_track(drive((0, 0), (100, 0)))
    enrich.enrich_track(track, road_database)
    track.clean_samples[3].status = cleaning.STATUS_SPOOFED

    # WHEN checking it
    is_current = enrich.is_enrichment_current(track, road_database.osm_timestamp)

    # THEN it is outdated
    assert not is_current


def test_is_enrichment_current_with_newer_osm_data(make_driven_track, road_database):
    # GIVEN an enriched track
    track = make_driven_track(drive((0, 0), (100, 0)))
    enrich.enrich_track(track, road_database)

    # WHEN checking it against other OSM data
    is_current = enrich.is_enrichment_current(track, "2026-10-01T00:00:00Z")

    # THEN it is outdated
    assert not is_current


def test_is_enrichment_current_with_older_enricher_version(
    make_driven_track, road_database, monkeypatch
):
    # GIVEN a track enriched by an older version
    track = make_driven_track(drive((0, 0), (100, 0)))
    enrich.enrich_track(track, road_database)
    monkeypatch.setattr(enrich, "ENRICHER_VERSION", enrich.ENRICHER_VERSION + 1)

    # WHEN checking it
    is_current = enrich.is_enrichment_current(track, road_database.osm_timestamp)

    # THEN it is outdated
    assert not is_current


def test_compute_samples_digest_survives_save_and_load(make_driven_track, road_database, tmp_path):
    # GIVEN an enriched track
    track = make_driven_track(drive((0, 0), (100, 0)))
    enrich.enrich_track(track, road_database)

    # WHEN saving and loading it
    reloaded_track = metadata.load_track_text(metadata.dump_track(track))

    # THEN its street list is still current
    assert enrich.is_enrichment_current(reloaded_track, road_database.osm_timestamp)


@pytest.fixture
def enrich_store(osm_metadata_dir):
    store = metadata.MetadataStore(osm_metadata_dir)
    store.ensure_dirs()
    return store


def run_enrich_tracks(store, config, force=False):
    return enrich.enrich_tracks(
        metadata_dir=store.root, config=config, update_osm=False, osm_file=None, force=force
    )


def test_enrich_tracks_enriches_and_rebuilds_index(enrich_store, config, make_driven_track):
    # GIVEN a track that was never enriched
    enrich_store.save_track(make_driven_track(drive((50, 0), (450, 0))))

    # WHEN enriching the tracks
    failed_count = run_enrich_tracks(enrich_store, config)

    # THEN the track has streets, and the index has the street names and localities
    assert failed_count == 0
    assert street_names(enrich_store.load_track("2026-09-25 Trip 11-17")) == ["Main"]
    index = json.loads(enrich_store.index_path.read_text(encoding="utf-8"))
    assert index["trips"][0]["streets"] == ["Main"]
    assert index["trips"][0]["start_locality"] == "Львів"
    assert index["trips"][0]["end_locality"] == "Львів"


def test_enrich_tracks_skips_current_tracks(enrich_store, config, make_driven_track, caplog):
    # GIVEN an already enriched track
    enrich_store.save_track(make_driven_track(drive((50, 0), (450, 0))))
    run_enrich_tracks(enrich_store, config)
    caplog.clear()
    caplog.set_level(logging.INFO)

    # WHEN enriching again
    run_enrich_tracks(enrich_store, config)

    # THEN nothing is enriched
    assert "Enriched:" not in caplog.text
    assert "All street lists are up to date" in caplog.text


def test_enrich_tracks_with_force(enrich_store, config, make_driven_track, caplog):
    # GIVEN an already enriched track
    enrich_store.save_track(make_driven_track(drive((50, 0), (450, 0))))
    run_enrich_tracks(enrich_store, config)
    caplog.clear()
    caplog.set_level(logging.INFO)

    # WHEN enriching again with force
    run_enrich_tracks(enrich_store, config, force=True)

    # THEN the track is enriched again
    assert "Enriched: 2026-09-25 Trip 11-17.mp4" in caplog.text


def test_enrich_tracks_skips_videos_without_overlay(enrich_store, config, make_driven_track):
    # GIVEN a track of a video without an overlay
    track = make_driven_track([])
    track.extraction_status = metadata.EXTRACTION_NO_OVERLAY
    enrich_store.save_track(track)

    # WHEN enriching the tracks
    run_enrich_tracks(enrich_store, config)

    # THEN the track is left alone
    assert enrich_store.load_track(track.stem).enrichment is None


def test_enrich_tracks_skips_misnamed_track_files(enrich_store, config, make_driven_track, caplog):
    # GIVEN a track file whose name differs from the video it declares, next to that video's track
    other_track = make_driven_track(drive((50, 0), (450, 0)))
    enrich_store.save_track(other_track)
    misnamed_track = make_driven_track(drive((50, 0), (900, 0)))
    enrich_store.write_track_file(misnamed_track, enrich_store.track_path("2026-09-24 Old Name"))
    caplog.set_level(logging.INFO)

    # WHEN enriching the tracks
    run_enrich_tracks(enrich_store, config)

    # THEN the misnamed file is skipped with a hint, and does not overwrite the other track
    assert "Skipped '2026-09-24 Old Name.json'" in caplog.text
    assert "dashcam doctor --fix" in caplog.text
    assert enrich_store.load_track(other_track.stem).raw_samples == other_track.raw_samples
    assert enrich_store.load_track("2026-09-24 Old Name").enrichment is None


def test_enrich_tracks_reports_unreadable_tracks(enrich_store, config, make_driven_track):
    # GIVEN a broken track next to a good one
    enrich_store.track_path("2026-09-24 Broken").write_text("{", encoding="utf-8")
    enrich_store.save_track(make_driven_track(drive((50, 0), (450, 0))))

    # WHEN enriching the tracks
    failed_count = run_enrich_tracks(enrich_store, config)

    # THEN the broken track counts as failed and the good one is enriched
    assert failed_count == 1
    assert enrich_store.load_track("2026-09-25 Trip 11-17").enrichment is not None


def test_enrich_tracks_without_osm_data(tmp_path, config):
    with pytest.raises(RuntimeError, match="--update-osm"):
        enrich.enrich_tracks(
            metadata_dir=tmp_path, config=config, update_osm=False, osm_file=None, force=False
        )


def test_enrich_tracks_with_osm_file_builds_data_first(tmp_path, config, osm_pbf_path):
    # GIVEN a metadata directory without OSM data
    metadata_dir = tmp_path / "library"

    # WHEN enriching with a local OSM file
    failed_count = enrich.enrich_tracks(
        metadata_dir=metadata_dir,
        config=config,
        update_osm=False,
        osm_file=osm_pbf_path,
        force=False,
    )

    # THEN the OSM data is built
    assert failed_count == 0
    assert osm.get_database_path(metadata_dir).is_file()


def test_enrich_tracks_with_update_osm_downloads_data(tmp_path, config, monkeypatch):
    # GIVEN a download that fails
    requested_urls = []

    def failing_update_osm_data(metadata_dir, extract_url, pbf_path=None):
        requested_urls.append(extract_url)
        raise OSError("network is down")

    monkeypatch.setattr(osm, "update_osm_data", failing_update_osm_data)

    # WHEN enriching with `--update-osm`
    with pytest.raises(OSError):
        enrich.enrich_tracks(
            metadata_dir=tmp_path, config=config, update_osm=True, osm_file=None, force=False
        )

    # THEN the configured extract was requested
    assert requested_urls == [config.osm_extract_url]
