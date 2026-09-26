import logging

import pytest

from dashcam import importer


@pytest.fixture
def step_calls(monkeypatch):
    """Record the steps of an import instead of running them, each failing once."""
    calls = []

    def recorder(name):
        def record(**kwargs):
            calls.append((name, kwargs))
            return 1

        return record

    monkeypatch.setattr("dashcam.importer.encode.encode_trips", recorder("encode"))
    monkeypatch.setattr("dashcam.importer.extract.extract_videos", recorder("extract"))
    monkeypatch.setattr("dashcam.importer.enrich.enrich_tracks", recorder("enrich"))
    monkeypatch.setattr("dashcam.importer.rename.rename_trips", recorder("rename"))
    return calls


def run_import(tmp_path, config, metadata_dir, dry_run=False, suggest_names=True):
    return importer.import_trips(
        raw_video_dir=tmp_path / "sd",
        library_dir=tmp_path / "videos",
        metadata_dir=metadata_dir,
        config=config,
        min_trip_gap_hours=3,
        job_count=2,
        dry_run=dry_run,
        check_readability=True,
        suggest_names=suggest_names,
    )


def test_import_trips_runs_all_steps(tmp_path, config, osm_metadata_dir, step_calls):
    # GIVEN a library with OSM data

    # WHEN importing
    failed_count = run_import(tmp_path, config, osm_metadata_dir)

    # THEN all steps run on the same directories, and their failures add up
    assert [name for name, _ in step_calls] == ["encode", "extract", "enrich", "rename"]
    kwargs_by_step = dict(step_calls)
    assert kwargs_by_step["encode"]["library_dir"] == tmp_path / "videos"
    assert kwargs_by_step["extract"]["library_dir"] == tmp_path / "videos"
    assert kwargs_by_step["extract"]["metadata_dir"] == osm_metadata_dir
    assert kwargs_by_step["enrich"]["update_osm"] is False
    assert kwargs_by_step["rename"]["interactive"] is True
    assert kwargs_by_step["rename"]["include_all"] is False
    assert failed_count == 4


def test_import_trips_dry_run_only_plans_encoding(tmp_path, config, osm_metadata_dir, step_calls):
    # WHEN importing with a dry run
    run_import(tmp_path, config, osm_metadata_dir, dry_run=True)

    # THEN only the encoding plan runs
    assert [name for name, _ in step_calls] == ["encode"]
    assert step_calls[0][1]["dry_run"] is True


def test_import_trips_without_osm_data(tmp_path, config, step_calls, caplog):
    # GIVEN a library without OSM data
    caplog.set_level(logging.WARNING)

    # WHEN importing
    run_import(tmp_path, config, tmp_path / ".metadata")

    # THEN street matching and renaming are skipped with a hint
    assert [name for name, _ in step_calls] == ["encode", "extract"]
    assert "dashcam enrich --update-osm" in caplog.text


def test_import_trips_without_rename(tmp_path, config, osm_metadata_dir, step_calls):
    # WHEN importing without renaming
    run_import(tmp_path, config, osm_metadata_dir, suggest_names=False)

    # THEN no names are suggested
    assert [name for name, _ in step_calls] == ["encode", "extract", "enrich"]
