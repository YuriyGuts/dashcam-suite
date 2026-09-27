# `dashcam`: Dashcam Video Toolkit and Route Visualizer

One command-line tool that covers the whole dashcam workflow: merge raw SD card segments into trip videos, extract GPS tracks from the video overlay, match them to named roads, name the trips, and browse them on a map with synchronized video playback.

## Terms

- **Raw video directory**: the `DCIM` directory on the SD card, with the raw segments in its subdirectories (config: `raw_video_dir`, option: `--raw-video-dir`).
- **Library directory**: the directory with the trip videos and the metadata directory (config: `library_dir`, option: `--library-dir` / `-d`).
- **Metadata directory**: tracks, the trip index, previews, and OSM data; `.metadata` inside the library directory by default (config: `metadata_dir`, option: `--metadata-dir`).

## Background

- Camera: VIOFO A119 V3. Raw segments on the SD card are named `YYYYMMDDhhmmss_NNNNNN.MP4` (start time, 6-digit index), 60 s, H.264. Loop recordings are in `DCIM/Movie`. Clips locked by the G-sensor or the emergency button are moved, under the same name, to an `RO` folder (`DCIM/Movie/RO` or `DCIM/RO`, depending on the firmware). Parking mode clips are named `YYYYMMDDhhmmss_NNNNNNP.MP4`.
- Trip videos are raw segments merged and re-encoded to HEVC. The original segments are not kept, so everything is extracted from the burned-in overlay, not from embedded GPS.
- Overlay (bottom of frame, fixed monospace font, updates at 1 Hz):
  - Left: `46 KM/H N49.810205 E24.028992`. Blank when there is no GPS fix.
  - Middle: `VIOFO A119 V3` (optional; absent in some videos).
  - Right: `2026/09/23 18:42:06` (camera clock, local time, always present). The date format is a camera setting: some videos show `27/04/2024`.
- Resolutions vary (2560×1440, 2560×1600). Both put the text 20–44 px above the bottom edge on an 18 px character pitch. The overlay scales with the frame width, so videos scaled down after recording keep the layout in proportion.
- The camera occasionally renders garbage for a second (e.g. `169 KM/H E24.000110 10.248983`).
- GPS spoofing happens: coordinates jump to another continent, the displayed speed is ~200 km/h, and the camera clock jumps to a fake date and may run backwards. A spoof can last most of a trip.
- A trip video may contain gaps between merged segments (up to 12 h).
- Some older videos come from a camera without GPS and must be skipped.

## Platform and Project

- Supported: Linux, macOS, and Windows. Code stays portable (`pathlib`, commands as argument lists, no `shell=True`).
- Python project managed by `uv` with a local `.venv`. External requirement: `ffmpeg` and `ffprobe` (paths configurable).
- `src/` layout, pytest, ruff (isort with one import per line), type annotations, `ty` type checking.
- Per-machine TOML config overrides defaults: raw video, library, and metadata directories, `ffmpeg` and `ffprobe` executables, hwaccel (off by default), codec settings, job counts (separate for `encode` and `extract`), trip gap, car model, camera time zone, allowed GPS areas. `dashcam config` prints the config file path and the effective settings.
- Directories:
  - `library_dir` and `raw_video_dir` in the config expand `~` and must be absolute. A relative `metadata_dir` in the config is relative to the library directory.
  - Paths given as options are ordinary shell paths, relative to the current directory.
  - A command that needs the library directory fails when neither `--library-dir` nor `library_dir` in the config sets it; the error names both. `enrich`, `forget`, and `extract --reclean` only need the metadata directory.
- Cleaning thresholds are constants, so tracks do not depend on the machine that extracted them.
- Long-running commands (`encode`, `extract`) prevent OS sleep and run jobs in parallel.
- Filenames fit in 143 bytes, the eCryptfs limit of encrypted NAS shared folders:
  - Files are written under short random names (`.tmp-<hex>.partial`) in the target directory and renamed when complete.
  - A video name has at most 142 bytes in UTF-8, so that its track name (`.json`) fits. `encode range --output-name` rejects longer names, `extract` skips such videos and counts them as failed, and `doctor` reports them.

## Commands

### `dashcam encode trips|range`

- Collects videos from the raw video directory and all its subdirectories, skipping hidden files and directories. A filename found in several folders is taken from an `RO` folder (else the first in path order) with a warning. Parking mode clips are skipped with one summary line.
- Parses the start time and full index from `YYYYMMDDhhmmss_NNNNNN` filenames, falling back to file mtime. Segments are sorted by start time.
- `trips` groups segments into trips by time gap (default 3 h). `range START END` selects segments by index; an `--output-name` without a leading `YYYY-mm-dd` gets the date of the first segment.
- Options: `--raw-video-dir`, `--library-dir`, `--metadata-dir`, `--min-trip-gap-hours`, `--job-count` (trips only), `--dry-run`, `--skip-raw-video-validation`, `--output-name` (range only).
- The SD card is never modified. The user controls the scope.
- Encoded raw videos are logged by filename and size in `.metadata/encoded_segments.json` (logged by the parent process after each successful job). `trips` leaves logged videos out before grouping, so clips left on the card are not encoded again after their trip is renamed, and new clips recorded within the trip gap of an imported trip form a trip of their own. `range` encodes the selected videos regardless and logs them too.
- Output names are placeholders: `YYYY-mm-dd Trip HH-MM.mp4` (trip start time). An output whose name a library video already has, with any extension or letter case, is skipped with a warning, so videos are never overwritten and never share a track. Incomplete outputs never appear under the final name.

### `dashcam extract [-d DIR] [--metadata-dir PATH] [--include GLOB] [--exclude GLOB] [--only VIDEO] [--force] [--reclean] [--previews] [--job-count JC]`

- Glob patterns match filenames and may be repeated.
- Probe: 10 frames spread over each new video are checked for a readable date/time field; at least 3 must have it. Videos without it, or narrower than 1280 px, are recorded as `no_overlay` and skipped until `--force`. If no date/time field is found and ffmpeg returns no frame at some probe points, the video fails without a track, so the next run probes it again.
- OCR:
  - Frames are sampled at 2 fps from the bottom 64 px strip, decoded with `ffmpeg` (hardware acceleration from the config), and collapsed to one sample per camera clock tick. For other widths, a strip of proportional height is cut and scaled (Lanczos) to 2560 px wide.
  - Below 1280 px, glyphs are too small: characters are misread with scores above the reliability threshold. At 1280 px, a few readings per clip fall below it and are interpolated; at 1920 px, readings match the originals.
  - Every horizontal position of the strip is matched against glyph templates with normalized cross-correlation over the glyph fill and its 1 px dark outline only, so the background (night, snow, glare) does not matter. Each glyph may shift ±3 px vertically and ±2 px horizontally.
  - The text is read as tokens, each found anywhere along the line on its own 18 px grid, so the position, order, padding and spacing of the values do not matter: speed (`d{1,3} KM/H`), latitude and longitude (hemisphere letter before or after `d.dddd` with 4–8 decimals, the same style for both), date (year first or last, `/` or `.`, unpadded day and month allowed), and time (`d{1,2}:dd:dd`). Up to two spaces may separate the parts of a token, and they must be blank. Each cell only considers the characters its pattern allows there. The best-scoring match of each kind is extended to the longest match covering it, so a token is read in full; added cells must score 0.6 or more. A `.` is accepted only where no other glyph (except `:`) matches. Date and time cells are excluded from the GPS search.
  - The GPS text is stored as `46 KM/H N49.810205 E24.028992` (speed optional) and the clock text as `<date> <time>` with the date as displayed. No GPS tokens means no fix.
  - Dates with the year last are day/month or month/day. Cleaning decides per video: a number above 12 shows the order; if all dates are ambiguous, the order that matches the filename date wins; otherwise day first.
  - Only glyphs in the template atlas can be read, so formats that need other characters (`MPH`, `AM`/`PM`, degree signs) need new labeled strips.
  - Text whose worst cell scores below 0.6 is unreliable and becomes `unreadable` (genuine text scores above 0.8). It is kept as raw text but never used.
  - Glyph templates are averaged from hand-labeled strips in `tests/fixtures/overlay/` by `scripts/build_glyph_templates.py`.
- Cleaning (runs on stored raw values; `--reclean` re-runs it without OCR):
  - A sample is suspect if its date is more than 1 day from the filename date, the clock does not advance ~1 s per video second, the implied speed from neighboring good points exceeds a threshold (default 250 km/h), or the displayed speed disagrees with the implied speed.
  - Fixes outside every box in `allowed_areas` (`[min_lat, min_lon, max_lat, max_lon]`) are spoofed. An empty list allows anywhere.
  - The track is split into internally consistent segments, and only segments that chain together plausibly are kept. This catches long spoofs where fake points agree with each other.
  - Consecutive segments joined by jumps of at most 1000 km/h are one place. If most readable clocks in a place show an implausible date, all its segments are rejected. This catches spoofs that show the real date before switching the camera clock to their own.
  - A forward clock jump up to 12 h between good fixes with plausible positions is a merge gap, not a spoof.
  - Time: the overlay clock is trusted only on good fixes. Elsewhere, time is derived from the video offset anchored to the previous or next good sample, preferring the one reached without crossing untrusted clocks, then the nearer one. Camera-clock jumps (up to 12 h) during no-fix stretches mark merge gaps. Times derived across an uncertain boundary are flagged `time_estimated`.
  - Gaps up to 60 s are interpolated linearly, including short spoofed or unreadable stretches. Longer gaps stay gaps.
  - Manual overrides from the track are applied last (see Track Format).
  - Sample statuses: `ok`, `interpolated`, `no_fix`, `spoofed`, `unreadable`.
  - Times are stored as ISO-8601 with the offset of the configured `timezone`.
  - The camera clock is read as local time. In the hour repeated when daylight saving time ends, a reading counts as the second occurrence once the clock has stepped back within the video. A recording that starts in that hour after the change is read as the first occurrence.
- Incremental and resumable: only videos without a track, or whose content changed, are processed. Each track is written when its video is done.
- Videos whose names differ only in the extension or letter case would share one track, so they are skipped and counted as failed until all but one are renamed.
- `--only` re-extracts the named videos and keeps their manual overrides.
- `--previews`: generate 480p H.264 previews in `.metadata/previews/` for browsers that cannot play HEVC.

### `dashcam enrich [-d DIR] [--metadata-dir PATH] [--update-osm] [--osm-file PBF] [--force]`

- `--update-osm` downloads the OSM extract from `osm_extract_url` (e.g. from Geofabrik), keeps only named drivable roads and localities (city, town, village, hamlet) in `.metadata/osm/roads.sqlite`, and deletes the raw download. The download and the database build go to the user cache directory on the local disk, since the metadata directory may be on a slow network drive. The finished database is then copied into place. `--osm-file` builds the same data from a local `.osm.pbf` file and keeps the file.
- Storage: SQLite with R*Tree indexes. OSM ways are cut into chunks of up to 8 nodes so that index boxes stay small. Tags kept: `name`, `name:en`, `ref`, `highway`; localities also keep `place` and `population`.
- Map matching: a hidden Markov model over the `ok` and `interpolated` samples. Candidates are streets (same `name` and `ref`) within 50 m. The cost grows with the distance to the road and the angle to the direction of travel; switching streets has a fixed cost, so GPS noise does not flicker between streets. Runs are split at gaps and position jumps.
- Street list: stretches in travel order, with off-road and sub-50 m stretches dropped and consecutive repeats merged.
- Localities: the place with the smallest distance relative to its radius (by place type, growing with population); none if the point is outside all radii.
- Incremental: a track is re-enriched when it has no street list, its located samples changed, the OSM data changed (its timestamp or its source: the extract URL or file name), or the enricher version increased. `--force` re-enriches all tracks. `extract --reclean` and `--only` keep the street list; `enrich` and `doctor` detect whether it is outdated.

### `dashcam rename [-d DIR] [--metadata-dir PATH] [--suggest] [--all] [--yes]`

- Pattern: `YYYY-mm-dd <streets> (<car model>).mp4`, streets separated by `, `. The car model comes from the config and is left out when empty (the default). The extension of the video is kept.
- ASCII only. Whole filename (including extension) at most 140 characters.
- Streets:
  - In travel order. Stretches under ~300 m and consecutive repeats are dropped.
  - If the name is too long, keep the first, the last, and the longest streets in between, in travel order.
  - Name source: OSM `name:en`, else KMU 2010 transliteration of `name`.
  - All street-type words are dropped (e.g. `vulytsia`, `prospekt`, `Street`, `Avenue`).
  - Motorways, trunks, and roads without a name use their ref, transliterated and without spaces or hyphens (`М-06` → `M06`). City streets that carry a ref keep their name.
  - Characters not allowed in filenames (`/ \ : * ? " < > |`) are removed.
- Collisions: the first trip (by date and start time) keeps the plain name; later ones get `(1)`, `(2)`, … before the car model. Names of existing videos and tracks (including unreachable ones) count as taken.
- Fallback for trips without usable GPS: `YYYY-mm-dd Trip HH-MM (<car model>).mp4`. Without any known time, the current name is kept as it is.
- Targets trips still named `Trip HH-MM` unless `--all` is given. Trips whose street list is missing or older than their samples are skipped with a hint to run `enrich`.
- Without options, prints the old → new table only. `--suggest` asks for confirmation: yes, no, or edit individually (Enter accepts, `-` skips, or type a name; typed names are validated). The end of input counts as no. `--yes` applies without asking.
- Renames the video, its track, and its preview together.

### `dashcam import`

Runs `encode trips` → `extract` → `enrich` → `rename --suggest`. Accepts the `encode trips` options plus `--metadata-dir` and `--no-rename`, with `--encode-job-count` and `--extract-job-count` in place of `--job-count`.

- `--dry-run` only prints the encoding plan.
- Without OSM data, `enrich` and `rename` are skipped with a hint to run `enrich --update-osm`; the large download is never started implicitly.

### `dashcam status [-d DIR]` and `dashcam forget VIDEO|STEM`

- `status` lists trips, unprocessed videos, `no_overlay` videos, and unreachable tracks.
- `forget` moves a track (and its preview) to `.metadata/trash/`.

### `dashcam doctor [-d DIR] [--fix]`

| Check | `--fix` |
|---|---|
| Track JSON parses, schema valid, overrides well-formed | Report line and field |
| Track filename differs from its `video_filename` field | Rename the track file |
| Two tracks share a fingerprint | Report both |
| Videos whose names differ only in the extension or letter case | Report |
| Video in the library without a track | Suggest `extract` |
| Track whose video is not in the library | Reconnect renames via fingerprint; report the rest as unreachable |
| Same stem, fingerprint mismatch | Suggest `extract --only` |
| `index.json` stale or missing | Rebuild (with `geometry.json`) |
| Track from an older extractor or cleaning version | Suggest `--reclean` |
| Street list older than the track or the OSM data | Suggest `enrich` |
| Video name too long for its track name to fit | Report |
| Orphaned previews or temp files (also in the library) | Delete (listed); temp files modified in the last hour may belong to a running job and are only reported |

`--fix` never deletes or moves a track anywhere but to its proper name. `extract` and `serve` run the cheap checks at startup and warn.

### `dashcam serve [-d DIR] [--host ADDRESS] [--port PORT] [--allow-rename]`

Local HTTP server for the static web app, the metadata directory, and the videos in the library directory.

Every request must address the server by a loopback host name. When it listens on another address, IP addresses, `*.local` names and the machine's host name are accepted too. DNS rebinding always uses the attacker's host name, so it is refused. Requests whose `Sec-Fetch-Site` is neither `same-origin` nor `none` are refused as well, so pages of other sites cannot probe for trip videos with `<video>` elements. The absolute library path is only sent to the web app when the server listens on loopback.

- Plain ES modules in `web/js/`, served as they are (no build step). Leaflet (vendored) with OpenStreetMap tiles.
- Sidebar: date range, one search box matching trip names, street names, and start/end localities (comma-separated terms must all match), trip list with stats (date, start/end time, distance, duration, average/max speed, GPS coverage badge), aggregate stats for the selection. Selected trips that the filters leave out stay selected but are not drawn; "Select all" selects exactly the listed trips. Filter state lives in the URL hash.
- Map:
  - Selected trips drawn together, one color per trip (12 colors, reused beyond that), or colored by speed. Large selections are drawn from the simplified routes; a trip's full track loads on hover or click.
  - Long gaps drawn as faint dashed lines, excluded from stats. Spoofed points are never drawn.
  - Coverage mode: all filtered routes as thin translucent lines. Clicking one opens the trip and draws its full track on top, without leaving coverage mode. Optional heatmap layer: how many trips passed through each 10 m ground cell, on a log scale.
  - Hover shows the trip name, date, time, video offset, speed, and coordinates. C copies them; H hides the trip (unchecks it, or hides it from the coverage map until shown again).
  - A status on the map shows while trips load or many routes are drawn.
- Trip detail: stats, street list, and a closable video panel that loads nothing until opened. A marker follows playback without moving the map; clicking the route seeks the video. Disabled when the video is unreachable. Uses the preview if one exists.
- Renaming: the trip name in the trip detail can be edited in place (date and extension fixed), prefilled with the current name, with the `rename` suggestion one click away. Names are validated like `rename`; errors appear under the field. A playing video is reopened at the same position under the new name. Only available when the server listens on a loopback address, or with `--allow-rename` on any address. Only same-origin JSON requests are accepted.

## Metadata Layout

```
.metadata/
  index.json                    Derived from tracks; never edit.
  geometry.json                 Simplified routes, written with the index; never edit.
  encoded_segments.json         Raw videos already encoded: filename → size.
  tracks/<video stem>.json      One per trip; hand-editable.
  previews/<video stem>.mp4     Optional.
  osm/                          Filtered road and locality data.
  trash/<timestamp>/            Replaced or forgotten tracks and previews, under their own names.
```

### Identity and Renames

- The video filename is the only source of truth for the trip name and date.
- Each track stores a content fingerprint: `<size>:<sha256 of first 1 MB + last 1 MB>`.
- A video with no track of the same stem is fingerprinted and matched against existing tracks. A match is a rename: the track and preview are renamed, no OCR. No match means a new video.
- If the matched track's video still exists under its old name, the new file is a duplicate copy: it is skipped with a warning, and `doctor` reports it.
- Same stem but different fingerprint means the content changed: the old track goes to trash and the video is re-extracted.
- Tracks outlive their videos. A track whose video is missing is `unreachable`.
- A track file that cannot be read, or is named after another video than its `video_filename`, is left alone: commands skip it with a warning, the index leaves it out, and `doctor` reports it.

### Track Format

One JSON file. Header pretty-printed, one sample per line.

```json
{
  "video_filename": "2026-09-25 Trip 11-17.mp4",
  "fingerprint": "252181145:9f2c41...",
  "extractor_version": 1,
  "overrides": {"bad_ranges_s": [], "good_ranges_s": []},
  "streets": [
    {"name": "вулиця Хрещатик", "name_en": "Khreshchatyk Street", "highway": "secondary", "distance_m": 401, "start_t": 117.0, "end_t": 173.0}
  ],
  "localities": {"start": {"name": "Київ", "name_en": "Kyiv", "place": "city"}, "end": null},
  "enrichment": {"enricher_version": 1, "osm_timestamp": "2026-09-25T20:24:36Z", "osm_source": "https://download.geofabrik.de/europe/ukraine-latest.osm.pbf", "samples_digest": "3f0c...", "enriched_at": "2026-09-26T20:40:11+03:00"},
  "samples": [
    {"t": 0.0, "time": "2026-09-25T10:22:28+03:00", "lat": 50.447312, "lon": 30.52259, "kmh": 28, "status": "ok", "raw": "28 KM/H N50.447312 E30.522590 | 2026/09/25 10:22:28", "scores": [0.97, 0.99]}
  ]
}
```

- `overrides.bad_ranges_s` / `overrides.good_ranges_s`: lists of `[start, end]` video-second ranges forced to spoofed or trusted. User-editable; survive `--reclean`.
- `scores` are the lowest OCR cell scores of the GPS and clock fields; they decide which raw text is reliable.
- `time_estimated: true` appears on samples whose time was derived across samples without a trustworthy clock.
- `streets[].name` is the OSM `name`, or the `ref` for roads without a name. `ref` appears when the road has one. The other street fields come from the road driven the longest in the stretch.
- Everything else is derived. `--reclean` regenerates `time`, `lat`, `lon`, and `status` from `raw` and `scores`.

## Validation

- OCR: `scripts/evaluate_overlay_ocr.py` reads every frame of the sample videos at 2 fps (~4,300 frames: day, night, rain, snow, glare, no fix, spoofed) and flags readings that break physical consistency: clock not advancing with the video, isolated coordinate jumps, GPS text flickering, and displayed speed disagreeing with implied speed. Flagged frames are saved for review by eye. Tesseract proved too noisy on this font to serve as a reference. Twelve hand-labeled strips (two with day-first dates) are kept as a regression fixture, and are also read back from synthetic videos scaled to 1920 and 1280 px. An end-to-end extraction test runs on a 6-second clip of a sample trip, with the picture above the overlay blacked out.
- Cleaning: synthetic-track tests for every rule; golden tests on the stored raw readings of the bad-GPS, night (merge gap), snow (camera glitches), and spoofed-only trips.
- Encode: grouping, parsing, and naming tests, plus a readability check of ffmpeg-generated segments.
- Enrich: tests against a synthetic OSM map (intersections, GPS noise, ref-only highways, footways, localities); street lists of the sample trips checked against the map by eye.
- Visualizer: tests for every server route; the web app is checked by hand in the browser.
