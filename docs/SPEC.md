# `dashcam`: Dashcam Video Toolkit and Route Visualizer

One command-line tool that covers the whole dashcam workflow: merge raw SD card segments into trip videos, extract GPS tracks from the video overlay, match them to named roads, name the trips, and browse them on a map with synchronized video playback.

## Background

- Camera: VIOFO A119 V3. Raw segments on the SD card are named `YYYYMMDDhhmmss_NNNNNN.MP4` (start time, 6-digit index), 60 s, H.264.
- Trip videos are raw segments merged and re-encoded to HEVC. The original segments are not kept, so everything is extracted from the burned-in overlay, not from embedded GPS.
- Overlay (bottom of frame, fixed monospace font, updates at 1 Hz):
  - Left: `46 KM/H N49.810205 E24.028992`. Blank when there is no GPS fix.
  - Middle: `VIOFO A119 V3` (optional; absent in some videos).
  - Right: `2026/09/23 18:42:06` (camera clock, local time, always present).
- Resolutions vary (2560×1440, 2560×1600). Both share the same overlay layout: text 20–44 px above the bottom edge, 18 px character grid, left field at x≈18, right field at x≈2200. Videos of other widths are treated as having no overlay.
- The camera occasionally renders garbage for a second (e.g. `169 KM/H E24.000110 10.248983`).
- GPS spoofing happens: coordinates jump to another continent, the displayed speed is ~200 km/h, and the camera clock jumps to a fake date and may run backwards. A spoof can last most of a trip.
- A trip video may contain gaps between merged segments (up to 12 h).
- Some older videos come from a camera without GPS and must be skipped.

## Platform and Project

- Supported: macOS and Linux. Windows is not tested, but code stays portable (`pathlib`, commands as argument lists, no `shell=True`).
- Python project managed by `uv` with a local `.venv`. External requirement: `ffmpeg` in `PATH`.
- `src/` layout, pytest, ruff (isort with one import per line), type annotations, `ty` type checking.
- Code style follows the original `dashcam-encode` script.
- Per-machine TOML config overrides defaults: SD card path, hwaccel (`videotoolbox` on macOS, `vulkan` on Linux), codec settings, job count, car model, cleaning thresholds.
- Long-running commands (`encode`, `extract`) prevent OS sleep and run jobs in parallel.

## Commands

### `dashcam encode trips|range`

Port of `dashcam-encode`.

- Parses the start time and full index from `YYYYMMDDhhmmss_NNNNNN` filenames, falling back to file mtime. Segments are sorted by start time.
- `trips` groups segments into trips by time gap (default 3 h). `range START END` selects segments by index.
- Options: `--raw-video-dir`, `--output-dir`, `--min-trip-gap-hours`, `--job-count`, `--dry-run`, `--skip-raw-video-validation`, `--output-name` (range only).
- The SD card is never modified. The user controls the scope.
- Output names are placeholders: `YYYY-mm-dd Trip HH-MM.mp4` (trip start time). Existing outputs are skipped with a warning and never overwritten. Incomplete outputs never appear under the final name.

### `dashcam extract [DIR] [--metadata-dir PATH] [--include GLOB] [--exclude GLOB] [--only VIDEO] [--force] [--reclean] [--previews]`

- `DIR` defaults to the current directory. Glob patterns match filenames and may be repeated.
- Probe: 10 frames spread over each new video are checked for a readable date/time field; at least 3 must have it. Videos without it are recorded as `no_overlay` and skipped until `--force`.
- OCR:
  - Frames are sampled at 2 fps from the bottom 64 px strip, decoded with `ffmpeg` (hardware acceleration from the config), and collapsed to one sample per camera clock tick.
  - Each cell is matched against glyph templates with normalized cross-correlation over the glyph fill and its 1 px dark outline only, so the background (night, snow, glare) does not matter.
  - Fields are decoded with their known formats: `dddd/dd/dd dd:dd:dd` for the clock, and the 18 digit-count variants of `d KM/H hd.dddddd vd.dddddd` for GPS. A blank field means no fix.
  - Text whose worst cell scores below 0.6 is unreliable and becomes `unreadable` (genuine text scores above 0.8). It is kept as raw text but never used.
  - Glyph templates are averaged from hand-labeled strips in `tests/fixtures/overlay/` by `scripts/build_glyph_templates.py`.
- Cleaning (runs on stored raw values; `--reclean` re-runs it without OCR):
  - A sample is suspect if its date is more than 1 day from the filename date, the clock does not advance ~1 s per video second, the implied speed from neighbouring good points exceeds a threshold (default 250 km/h), or the displayed speed disagrees with the implied speed.
  - The track is split into internally consistent segments, and only segments that chain together plausibly are kept. This catches long spoofs where fake points agree with each other.
  - A forward clock jump up to 12 h between good fixes with plausible positions is a merge gap, not a spoof.
  - Time: the overlay clock is trusted only on good fixes. Elsewhere, time is derived from the video offset anchored to the nearest good sample. Camera-clock jumps (up to 12 h) during no-fix stretches mark merge gaps. Times derived across an uncertain boundary are flagged `time_estimated`.
  - Gaps up to 60 s are interpolated linearly, including short spoofed or unreadable stretches. Longer gaps stay gaps.
  - Manual overrides from the track are applied last (see Track Format).
  - Sample statuses: `ok`, `interpolated`, `no_fix`, `spoofed`, `unreadable`.
  - Times are stored as ISO-8601 with offset in `Europe/Kyiv`.
- Incremental and resumable: only videos without a track, or whose content changed, are processed. Each track is written when its video is done.
- `--only` re-extracts the named videos and keeps their manual overrides.
- `--metadata-dir` defaults to `.metadata` in the current directory (config: `metadata_dir`).
- `--previews`: generate 480p H.264 previews in `.metadata/previews/` for browsers that cannot play HEVC.

### `dashcam enrich [--update-osm]`

- `--update-osm` downloads the Geofabrik Ukraine extract once, keeps only named roads and localities in `.metadata/osm/`, and deletes the raw download.
- Map matching: nearest named road consistent with the direction of travel, using a spatial index.
- Produces an ordered street list per trip (with distance per street) and the start/end localities, stored in the track. Incremental.

### `dashcam rename --suggest [--all]`

- Pattern: `YYYY-mm-dd <streets> (<car model>).mp4`. Car model defaults to `CX-5` (config override).
- ASCII only. Whole filename (including extension) at most 140 characters.
- Streets:
  - In travel order. Stretches under ~300 m and consecutive repeats are dropped.
  - If the name is too long, keep the first, the last, and the longest streets in between, in travel order.
  - Name source: OSM `name:en`, else KMU 2010 transliteration of `name`.
  - All street-type words are dropped (e.g. `vulytsia`, `prospekt`, `Street`, `Avenue`).
  - Highways use their ref (`M06`).
- Collisions: the first trip keeps the plain name; later ones get `(1)`, `(2)`, … before the car model.
- Fallback for trips without usable GPS: `YYYY-mm-dd Trip HH-MM (<car model>).mp4`.
- Targets trips still named `Trip …` unless `--all` is given.
- Shows an old → new table and asks for confirmation: yes, no, or edit individually.
- Renames the video, its track, and its preview together.

### `dashcam import`

Runs `encode` → `extract` → `enrich` → `rename --suggest`. Accepts the `encode` options plus `--out DIR` and `--no-rename`.

### `dashcam status [DIR]` and `dashcam forget VIDEO|STEM`

- `status` lists trips, unprocessed videos, `no_overlay` videos, and unreachable tracks.
- `forget` moves a track (and its preview) to `.metadata/trash/`.

### `dashcam doctor [DIR] [--fix]`

| Check | `--fix` |
|---|---|
| Track JSON parses, schema valid, overrides well-formed | Report line and field |
| Track filename differs from its `video_filename` field | Rename the track file |
| Two tracks share a fingerprint | Report both |
| Video in `DIR` without a track | Suggest `extract` |
| Track whose video is not in `DIR` | Reconnect renames via fingerprint; report the rest as unreachable |
| Same stem, fingerprint mismatch | Suggest `extract --only` |
| `index.json` stale or missing | Rebuild |
| Track from an older extractor or cleaning version | Suggest `--reclean` |
| Street list older than the track or the OSM data | Suggest `enrich` |
| Orphaned previews or temp files | Delete (listed) |

`--fix` never deletes a track; it moves it to `.metadata/trash/`. `extract` and `serve` run the cheap checks at startup and warn.

### `dashcam serve [DIR]`

Local HTTP server for the static web app, `.metadata/`, and the videos in `DIR`.

- Leaflet (vendored) with OpenStreetMap tiles. Main browser: Firefox; secondary: Brave.
- Sidebar: date range, trip name search, street filter, trip list with stats (date, start/end time, distance, duration, average/max speed, GPS coverage badge), aggregate stats for the selection. Filter state lives in the URL hash.
- Map:
  - Selected trips drawn together, one colour per trip, or coloured by speed.
  - Long gaps drawn as faint dashed lines, excluded from stats. Spoofed points are never drawn.
  - Coverage mode: all filtered routes as thin translucent lines. Optional heatmap layer.
  - Hover shows the time and video offset.
- Trip detail: stats, street list, and a closable video panel that loads nothing until opened. A marker follows playback; clicking the route seeks the video. Disabled when the video is unreachable. Uses the preview if one exists.

## Metadata Layout

```
.metadata/
  index.json                    Derived from tracks; never edit.
  tracks/<video stem>.json      One per trip; hand-editable.
  previews/<video stem>.mp4     Optional.
  osm/                          Filtered road and locality data.
  trash/                        Replaced or forgotten tracks.
```

### Identity and Renames

- The video filename is the only source of truth for the trip name and date.
- Each track stores a content fingerprint: `<size>:<sha256 of first 1 MB + last 1 MB>`.
- A video with no track of the same stem is fingerprinted and matched against existing tracks. A match is a rename: the track and preview are renamed, no OCR. No match means a new video.
- If the matched track's video still exists under its old name, the new file is a duplicate copy: it is skipped with a warning, and `doctor` reports it.
- Same stem but different fingerprint means the content changed: the old track goes to trash and the video is re-extracted.
- Tracks outlive their videos. A track whose video is missing is `unreachable`.

### Track Format

One JSON file. Header pretty-printed, one sample per line.

```json
{
  "video_filename": "2026-09-25 Trip 11-17.mp4",
  "fingerprint": "252181145:9f2c41...",
  "extractor_version": 1,
  "overrides": {"bad_ranges_s": [], "good_ranges_s": []},
  "streets": [],
  "samples": [
    {"t": 0.0, "time": "2026-09-25T10:22:28+03:00", "lat": 49.828035, "lon": 24.00494, "kmh": 28, "status": "ok", "raw": "28 KM/H N49.828035 E24.004940 | 2026/09/25 10:22:28", "scores": [0.97, 0.99]}
  ]
}
```

- `overrides.bad_ranges_s` / `overrides.good_ranges_s`: lists of `[start, end]` video-second ranges forced to spoofed or trusted. User-editable; survive `--reclean`.
- `scores` are the lowest OCR cell scores of the GPS and clock fields; they decide which raw text is reliable.
- `time_estimated: true` appears on samples whose time was derived across samples without a trustworthy clock.
- Everything else is derived. `--reclean` regenerates `time`, `lat`, `lon`, and `status` from `raw` and `scores`.

## Validation

- OCR: `scripts/evaluate_overlay_ocr.py` reads every frame of the sample videos at 2 fps (~4,300 frames: day, night, rain, snow, glare, no fix, spoofed) and flags readings that break physical consistency: clock not advancing with the video, isolated coordinate jumps, GPS text flickering, and displayed speed disagreeing with implied speed. Flagged frames are saved for review by eye. Tesseract proved too noisy on this font to serve as a reference. Ten hand-labeled strips are kept as a regression fixture.
- Cleaning: synthetic-track tests for every rule; golden tests on the stored raw readings of the bad-GPS, night (merge gap), and snow (camera glitches) trips.
- Encode: grouping, parsing, and naming tests against `video/raw-sd`.
- Visualizer: browser smoke test.

## Build Order

1. Project skeleton and `encode` port.
2. `extract` (with `status`, `forget`, `doctor`).
3. `serve`.
4. `enrich`.
5. `rename`.
6. `import`.
