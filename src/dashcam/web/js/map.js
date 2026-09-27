// The map: route and coverage drawing, the heatmap, and hover interactions.

import {COVERAGE_COLOR, COVERAGE_DOTS_MAX_ZOOM, COVERAGE_HALO_WEIGHT, COVERAGE_OPACITY, COVERAGE_WEIGHT, DOT, EARTH_CIRCUMFERENCE_M, GAP_WEIGHT, HALO_COLOR, HEAT_BLUR_PX, HEAT_CELL_M, HEAT_GRADIENT, HEAT_MIN_LEVEL, HEAT_MIN_OPACITY, HEAT_OVERLAP, HEAT_RADIUS_PX, LARGE_DRAW_TRIP_COUNT, MAP_STATUS_DELAY_MS, METERS_PER_DEGREE_LAT, ROUTE_HALO_WEIGHT, ROUTE_WEIGHT, SPEED_BUCKETS, UNKNOWN_SPEED_COLOR, WORLD_WIDTH_PX} from "./constants.js";
import {dom} from "./elements.js";
import {canPlayVideo, el, formatClockTime, formatCoordinates, formatDate, formatDuration, formatSpeed, hasGps, icon, nextPaint, speedColor, tripColor, withoutEmpty} from "./helpers.js";
import {drawnSelection, filteredTrips, state} from "./state.js";
import {loadGeometry, loadTrack, loadedTracks, nearestSampleIndex, sampleLatLng} from "./tracks.js";
import {renderSidebar} from "./sidebar.js";
import {focusTrip, setTripSelected} from "./actions.js";
import {openVideo, seekVideo, updatePlaybackMarker, video} from "./video.js";

// Incremented on every redraw, so that a slow track load does not draw a stale selection.
let drawGeneration = 0;

export const map = L.map("map", {preferCanvas: true, zoomSnap: 0.5});
const routeRenderer = L.canvas({tolerance: 8});
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

let routesLayer = L.layerGroup().addTo(map);
let coverageLayer = L.layerGroup();
let coverageDotsLayer = L.layerGroup();
let heatLayer = null;

const hoverTooltip = L.tooltip({direction: "top", offset: [0, -8], className: "map-tooltip"});
const hoverMarker = L.circleMarker([0, 0], {
  renderer: routeRenderer,
  radius: 5,
  color: "#101828",
  weight: 2,
  fillColor: "#ffffff",
  fillOpacity: 1,
  interactive: false,
});

// A DOM marker moves with a CSS transform, so following playback does not redraw the routes.
export const playbackMarker = L.marker([0, 0], {
  icon: L.divIcon({className: "playback-marker", iconSize: [18, 18]}),
  interactive: false,
  keyboard: false,
  zIndexOffset: 1000,
});

const speedLegend = L.control({position: "bottomleft"});
speedLegend.onAdd = () => {
  const rows = SPEED_BUCKETS.map((bucket) =>
    el(
      "div",
      {className: "map-legend-row"},
      el("span", {className: "map-legend-line", style: `background: ${bucket.color}`}),
      `${bucket.label} km/h`,
    ),
  );
  rows.push(
    el(
      "div",
      {className: "map-legend-row"},
      el("span", {className: "map-legend-line", style: `background: ${UNKNOWN_SPEED_COLOR}`}),
      "unknown",
    ),
  );
  return el("div", {className: "map-legend"}, el("div", {className: "map-legend-title"}, "Speed"), rows);
};

// View shown when there is nothing to fit to (Europe).
export const DEFAULT_VIEW = {center: [50, 15], zoom: 4};

function tripBounds(trips) {
  const boxes = trips.map((trip) => trip.bbox).filter(Boolean);
  if (!boxes.length) {
    return null;
  }
  return L.latLngBounds(
    [Math.min(...boxes.map((box) => box[0])), Math.min(...boxes.map((box) => box[1]))],
    [Math.max(...boxes.map((box) => box[2])), Math.max(...boxes.map((box) => box[3]))],
  );
}

export function fitToTrips(trips, {animate = true} = {}) {
  const bounds = tripBounds(trips);
  if (bounds) {
    map.fitBounds(bounds, {padding: [32, 32], maxZoom: 17, animate});
  }
  return bounds !== null;
}

// What is under the mouse: a trip, and on a full track also the nearest point, which the copy
// shortcut acts on. `latlng` is the mouse position, used while the point is not known.
let hovered = null;

// How long (milliseconds) the tooltip confirms a copied point.
const COPIED_NOTICE_MS = 1500;

function sampleTimeText(sample) {
  if (!sample.time) {
    return "Time unknown";
  }
  return `${formatClockTime(sample.time, true)}${sample.time_estimated ? " (estimated)" : ""}`;
}

function sampleDate(track, sample) {
  return sample.time ? sample.time.slice(0, 10) : track.trip.date;
}

// Plain text, one labeled field per line, for pasting into notes or messages.
function pointDetailsText(track, sample) {
  const timeText = sample.time ? sampleTimeText(sample) : "unknown";
  const speedText = sample.kmh === null || sample.kmh === undefined ? "unknown" : formatSpeed(sample.kmh);
  return [
    `Trip name: ${track.trip.name}`,
    `Date: ${formatDate(sampleDate(track, sample))}`,
    `Time: ${timeText}`,
    `Video position: ${formatDuration(sample.t)}`,
    `Speed: ${speedText}`,
    `Coordinates: ${formatCoordinates(sample.lat, sample.lon)}`,
  ].join("\n");
}

function hoverActionsText() {
  const {trip, sample} = hovered;
  if (state.mapMode === "coverage" && trip.id !== state.focusedId) {
    return ["Click to open", "H to hide"].join(DOT);
  }
  return withoutEmpty([
    canPlayVideo(trip) ? "Click to play from here" : null,
    sample ? "C to copy" : null,
    "H to hide",
  ]).join(DOT);
}

function renderHoverTooltip() {
  const {trip, track, sample, copiedAt} = hovered;
  const lines = [el("strong", {}, trip.name)];
  if (sample) {
    const speedText = sample.kmh === null || sample.kmh === undefined ? "Speed unknown" : formatSpeed(sample.kmh);
    lines.push(
      el("span", {}, [formatDate(sampleDate(track, sample)), sampleTimeText(sample)].join(DOT)),
      el("span", {}, [`Video ${formatDuration(sample.t)}`, speedText].join(DOT)),
      el("span", {}, formatCoordinates(sample.lat, sample.lon)),
    );
  } else {
    const startTime = trip.start_time ? formatClockTime(trip.start_time) : null;
    lines.push(el("span", {}, withoutEmpty([formatDate(trip.date), startTime]).join(DOT)));
  }
  const isCopiedNoticeShown = copiedAt !== null && performance.now() - copiedAt < COPIED_NOTICE_MS;
  lines.push(el("span", {className: "tooltip-hint"}, isCopiedNoticeShown ? "Copied to clipboard" : hoverActionsText()));
  hoverTooltip.setContent(el("div", {}, lines));
}

// Shows the tooltip of a trip under the mouse. On a full track, it snaps to the nearest point.
function showHover(trip, latlng, track = null, sampleIndexes = null) {
  const sample = track ? track.samples[nearestSampleIndex(track, sampleIndexes, latlng)] : null;
  const isSamePoint = hovered && hovered.trip === trip && hovered.sample === sample;
  hovered = {trip, track, sample, latlng, copiedAt: isSamePoint ? hovered.copiedAt : null};
  if (sample) {
    hoverMarker.setLatLng(sampleLatLng(sample)).addTo(map);
  } else {
    hoverMarker.remove();
  }
  hoverTooltip.setLatLng(sample ? sampleLatLng(sample) : latlng);
  renderHoverTooltip();
  if (!map.hasLayer(hoverTooltip)) {
    hoverTooltip.addTo(map);
  }
}

// Simplified routes have no times or speeds, so the full track is loaded on the first hover.
function showSimplifiedHover(trip, latlng) {
  const track = loadedTracks.get(trip.id);
  if (track) {
    showHover(trip, latlng, track, track.locatedIndexes);
    return;
  }
  showHover(trip, latlng);
  loadTrack(trip.id)
    .then((loadedTrack) => {
      if (hovered?.trip === trip && !hovered.track) {
        showHover(trip, hovered.latlng, loadedTrack, loadedTrack.locatedIndexes);
      }
    })
    .catch((error) => console.error(error));
}

function hideHover() {
  hovered = null;
  hoverMarker.remove();
  hoverTooltip.remove();
}

// The clipboard API needs a secure context, which a page served to other devices on the network
// (`--host 0.0.0.0`) is not; the older copy command still works there.
async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const textArea = el("textarea", {style: "position: fixed; opacity: 0"}, text);
    document.body.append(textArea);
    textArea.select();
    const isCopied = document.execCommand("copy");
    textArea.remove();
    return isCopied;
  }
}

function isTypingTarget(target) {
  return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

async function copyHoveredPoint() {
  const point = hovered;
  if (!(await copyToClipboard(pointDetailsText(point.track, point.sample)))) {
    return;
  }
  point.copiedAt = performance.now();
  if (hovered === point) {
    renderHoverTooltip();
    setTimeout(() => {
      if (hovered === point) renderHoverTooltip();
    }, COPIED_NOTICE_MS);
  }
}

// Shortcuts for the trip under the mouse. H hides it. C copies the point; Cmd+C and Ctrl+C do
// too, unless text on the page is selected.
export function onHoverShortcut(event) {
  if (!hovered || event.altKey || event.shiftKey || isTypingTarget(event.target)) {
    return;
  }
  const key = event.key.toLowerCase();
  const hasModifier = event.metaKey || event.ctrlKey;
  if (key === "h" && !hasModifier) {
    event.preventDefault();
    hideTrip(hovered.trip.id);
  } else if (key === "c" && hovered.sample) {
    if (hasModifier && !window.getSelection().isCollapsed) {
      return;
    }
    event.preventDefault();
    copyHoveredPoint();
  }
}

// On the route map, hiding a trip clears its checkbox. On the coverage map, the trip stays
// hidden until it is shown again from the list, or the page is reloaded.
function hideTrip(tripId) {
  hideHover();
  if (state.mapMode === "coverage") {
    state.hiddenIds.add(tripId);
    renderSidebar();
    renderMap();
  } else {
    setTripSelected(tripId, false);
  }
}

export function showHiddenTrips(tripIds) {
  for (const tripId of tripIds) {
    state.hiddenIds.delete(tripId);
  }
  renderSidebar();
  renderMap();
}

function onRouteClick(track, sampleIndexes, latlng) {
  const index = nearestSampleIndex(track, sampleIndexes, latlng);
  const seconds = track.samples[index].t;
  const tripId = track.trip.id;
  if (state.focusedId !== tripId) {
    focusTrip(tripId, {fit: false});
  }
  if (video.tripId === tripId && video.element) {
    seekVideo(seconds);
  } else if (canPlayVideo(track.trip)) {
    openVideo(tripId, {startAt: seconds, autoplay: true});
  }
}

async function onSimplifiedRouteClick(trip, latlng) {
  try {
    const track = await loadTrack(trip.id);
    onRouteClick(track, track.locatedIndexes, latlng);
  } catch (error) {
    console.error(error);
  }
}

function bindRouteEvents(line, track, sampleIndexes) {
  line.on("mousemove", (event) => showHover(track.trip, event.latlng, track, sampleIndexes));
  line.on("mouseout", hideHover);
  line.on("click", (event) => onRouteClick(track, sampleIndexes, event.latlng));
}

function addRouteLine(layer, track, sampleIndexes, color) {
  const latlngs = sampleIndexes.map((index) => sampleLatLng(track.samples[index]));
  const line = L.polyline(latlngs, {renderer: routeRenderer, color, weight: ROUTE_WEIGHT, opacity: 1});
  bindRouteEvents(line, track, sampleIndexes);
  line.addTo(layer);
}

function addHalo(layer, latlngs, weight, opacity) {
  L.polyline(latlngs, {renderer: routeRenderer, color: HALO_COLOR, weight, opacity, interactive: false}).addTo(layer);
}

function drawTrack(layer, track, {color, isColoredBySpeed}) {
  const latlngsOf = (indexes) => indexes.map((index) => sampleLatLng(track.samples[index]));

  for (const [fromIndex, toIndex] of track.gaps) {
    L.polyline(latlngsOf([fromIndex, toIndex]), {
      renderer: routeRenderer,
      color: isColoredBySpeed ? UNKNOWN_SPEED_COLOR : color,
      weight: GAP_WEIGHT,
      opacity: 0.6,
      dashArray: "2 8",
      interactive: false,
    }).addTo(layer);
  }

  for (const run of track.runs) {
    if (run.length < 2) {
      continue;
    }
    addHalo(layer, latlngsOf(run), ROUTE_HALO_WEIGHT, 0.8);

    if (!isColoredBySpeed) {
      addRouteLine(layer, track, run, color);
      continue;
    }

    // Speed colors: one polyline per stretch of consecutive samples in the same speed bucket.
    let stretch = [run[0]];
    let stretchColor = null;
    for (let position = 1; position < run.length; position++) {
      const previous = track.samples[run[position - 1]];
      const current = track.samples[run[position]];
      const segmentColor = speedColor(previous.kmh ?? current.kmh);
      if (stretchColor !== null && segmentColor !== stretchColor) {
        addRouteLine(layer, track, stretch, stretchColor);
        stretch = [run[position - 1]];
      }
      stretchColor = segmentColor;
      stretch.push(run[position]);
    }
    addRouteLine(layer, track, stretch, stretchColor);
  }
}

// Shows what the map is busy with, at once for large draws and after a delay for small ones.
// Resolves when the status is painted, so that the draw that follows does not hide it.
async function showDrawStatus(tripCount) {
  const text = `Drawing ${tripCount} ${tripCount === 1 ? "route" : "routes"}...`;
  if (tripCount >= LARGE_DRAW_TRIP_COUNT) {
    showMapStatus(text);
    await nextPaint();
  } else {
    showMapStatus(text, {delayMs: MAP_STATUS_DELAY_MS});
  }
}

async function drawRoutes(generation) {
  const tripIds = drawnSelection().filter((id) => hasGps(state.tripsById.get(id)));
  if (!tripIds.length) {
    hideMapStatus();
  } else {
    await showDrawStatus(tripIds.length);
  }
  // Many trips are drawn from the simplified routes, except the focused and the playing trip.
  // Speed colors need the full tracks.
  const isColoredBySpeed = state.colorMode === "speed";
  const isSimplified = !isColoredBySpeed && tripIds.length >= LARGE_DRAW_TRIP_COUNT;
  const needsFullTrack = (id) => !isSimplified || id === state.focusedId || id === video.tripId;
  const fullTrackIds = tripIds.filter(needsFullTrack);
  const simplifiedIds = tripIds.filter((id) => !needsFullTrack(id));
  // The focused trip is drawn last, on top of the others.
  fullTrackIds.sort((a, b) => (a === state.focusedId) - (b === state.focusedId));
  const [geometry, results] = await Promise.all([
    simplifiedIds.length ? loadGeometry() : {},
    Promise.allSettled(fullTrackIds.map(loadTrack)),
  ]);
  if (generation !== drawGeneration) {
    return;
  }

  const layer = L.layerGroup();
  const simplifiedTrips = simplifiedIds.filter((id) => geometry[id]?.length).map((id) => state.tripsById.get(id));
  // All halos go first, so that no halo covers another trip's line.
  for (const trip of simplifiedTrips) {
    addHalo(layer, geometry[trip.id], ROUTE_HALO_WEIGHT, 0.8);
  }
  for (const trip of simplifiedTrips) {
    const line = L.polyline(geometry[trip.id], {
      renderer: routeRenderer,
      color: tripColor(trip.id),
      weight: ROUTE_WEIGHT,
      opacity: 1,
    });
    line.on("mousemove", (event) => showSimplifiedHover(trip, event.latlng));
    line.on("mouseout", hideHover);
    line.on("click", (event) => onSimplifiedRouteClick(trip, event.latlng));
    line.addTo(layer);
  }
  results.forEach((result, index) => {
    if (result.status === "fulfilled") {
      drawTrack(layer, result.value, {color: tripColor(fullTrackIds[index]), isColoredBySpeed});
    } else {
      console.error(`Cannot load the track of '${fullTrackIds[index]}':`, result.reason);
    }
  });
  routesLayer.remove();
  routesLayer = layer.addTo(map);
  updatePlaybackMarker();
  hideMapStatus();
}

export function coverageTrips() {
  return filteredTrips().filter((trip) => hasGps(trip) && !state.hiddenIds.has(trip.id));
}

/* Heatmap. */

// Ground cells are keyed by row and column, offset to be positive, in one number.
const HEAT_COLUMN_SPAN = 2 ** 23;
const HEAT_ROW_OFFSET = 2 ** 21;
const HEAT_COLUMN_OFFSET = 2 ** 22;

// Below this many trips on the busiest cell, the colors are scaled as if it had this many, so
// that a single trip is not drawn in the darkest color.
const HEAT_MIN_SCALE_COUNT = 10;

// The heatmap of the drawn coverage, kept to recompute its points on zoom.
let heatCells = null;

function heatCellKey(row, column) {
  return (row + HEAT_ROW_OFFSET) * HEAT_COLUMN_SPAN + (column + HEAT_COLUMN_OFFSET);
}

// Counts the trips through each cell of a ground grid. Each trip counts once per cell, however
// long it stayed there or however many points its route has there.
function countTripsPerCell(routes) {
  const cells = new Map();
  const firstPoint = routes[0]?.[0]?.[0];
  const referenceLat = firstPoint ? firstPoint[0] : 0;
  const cellLat = HEAT_CELL_M / METERS_PER_DEGREE_LAT;
  const cellLon = cellLat / Math.cos((referenceLat * Math.PI) / 180);
  for (const runs of routes) {
    const tripCells = new Set();
    for (const run of runs) {
      for (let index = 1; index < run.length; index++) {
        const [lat1, lon1] = run[index - 1];
        const [lat2, lon2] = run[index];
        // Steps of half a cell, so that no cell along the segment is skipped.
        const cellsAlong = Math.max(Math.abs(lat2 - lat1) / cellLat, Math.abs(lon2 - lon1) / cellLon);
        const stepCount = Math.max(1, Math.ceil(cellsAlong * 2));
        for (let step = 0; step <= stepCount; step++) {
          const fraction = step / stepCount;
          const row = Math.floor((lat1 + (lat2 - lat1) * fraction) / cellLat);
          const column = Math.floor((lon1 + (lon2 - lon1) * fraction) / cellLon);
          tripCells.add(heatCellKey(row, column));
        }
      }
    }
    for (const key of tripCells) {
      cells.set(key, (cells.get(key) ?? 0) + 1);
    }
  }
  // A loop, because spreading millions of cells into `Math.max` exceeds the argument limit.
  let maxCount = HEAT_MIN_SCALE_COUNT;
  for (const count of cells.values()) {
    maxCount = Math.max(maxCount, count);
  }
  return {cells, cellLat, cellLon, referenceLat, maxCount};
}

// Heatmap points for a zoom level. The heatmap plugin adds up the points that fall within half
// a radius on screen, so cells that small are merged first, keeping the highest count. This
// keeps the colors the same at every zoom.
function heatPoints(heat, zoom) {
  const {cells, cellLat, cellLon, referenceLat, maxCount} = heat;
  const metersPerPixel =
    (EARTH_CIRCUMFERENCE_M * Math.cos((referenceLat * Math.PI) / 180)) / (WORLD_WIDTH_PX * 2 ** zoom);
  const mergeFactor = Math.max(1, Math.floor(((HEAT_RADIUS_PX / 2) * metersPerPixel) / HEAT_CELL_M));
  const merged = new Map();
  for (const [key, count] of cells) {
    const row = Math.floor(key / HEAT_COLUMN_SPAN) - HEAT_ROW_OFFSET;
    const column = (key % HEAT_COLUMN_SPAN) - HEAT_COLUMN_OFFSET;
    const mergedRow = Math.floor(row / mergeFactor);
    const mergedColumn = Math.floor(column / mergeFactor);
    const mergedKey = heatCellKey(mergedRow, mergedColumn);
    if ((merged.get(mergedKey)?.count ?? 0) < count) {
      merged.set(mergedKey, {row: mergedRow, column: mergedColumn, count});
    }
  }
  const scale = Math.log(maxCount);
  return [...merged.values()].map(({row, column, count}) => {
    const level = HEAT_MIN_LEVEL + (1 - HEAT_MIN_LEVEL) * (Math.log(count) / scale);
    return [
      (row + 0.5) * mergeFactor * cellLat,
      (column + 0.5) * mergeFactor * cellLon,
      1 - (1 - level) ** (1 / HEAT_OVERLAP),
    ];
  });
}

function updateHeatPoints() {
  if (heatLayer && heatCells) {
    heatLayer.setLatLngs(heatPoints(heatCells, map.getZoom()));
  }
}

map.on("zoomend", updateHeatPoints);

/* Coverage. */

// The focused trip on the coverage map, drawn in full on top of the coverage lines.
const FOCUSED_COVERAGE_COLOR = "#101828";

async function drawCoverage(generation) {
  const trips = coverageTrips();
  await showDrawStatus(trips.length);
  if (generation !== drawGeneration) {
    return;
  }
  const focusedTrip = state.focusedId ? state.tripsById.get(state.focusedId) : null;
  const focusedTrackPromise = focusedTrip && hasGps(focusedTrip) ? loadTrack(focusedTrip.id) : null;
  const geometry = await loadGeometry();
  const focusedTrack = focusedTrackPromise ? await focusedTrackPromise.catch(() => null) : null;
  if (generation !== drawGeneration) {
    return;
  }
  const layer = L.layerGroup();
  const tripsWithRoutes = trips.filter((trip) => geometry[trip.id]?.length);
  // All halos go first, so that no halo covers another trip's line.
  for (const trip of tripsWithRoutes) {
    addHalo(layer, geometry[trip.id], COVERAGE_HALO_WEIGHT, 0.9);
  }
  const dotsLayer = L.layerGroup();
  for (const trip of tripsWithRoutes) {
    const runs = geometry[trip.id];
    const line = L.polyline(runs, {
      renderer: routeRenderer,
      color: COVERAGE_COLOR,
      weight: COVERAGE_WEIGHT,
      opacity: COVERAGE_OPACITY,
    });
    line.on("mousemove", (event) => showHover(trip, event.latlng));
    line.on("mouseout", hideHover);
    line.on("click", () => focusTrip(trip.id));
    line.addTo(layer);

    const longestRun = runs.reduce((longest, run) => (run.length > longest.length ? run : longest));
    const dot = L.circleMarker(longestRun[Math.floor(longestRun.length / 2)], {
      renderer: routeRenderer,
      radius: 5,
      color: HALO_COLOR,
      weight: 2,
      fillColor: COVERAGE_COLOR,
      fillOpacity: 1,
    });
    dot.on("mouseover", () => showHover(trip, dot.getLatLng()));
    dot.on("mouseout", hideHover);
    dot.on("click", () => focusTrip(trip.id));
    dot.addTo(dotsLayer);
  }
  if (focusedTrack) {
    drawTrack(layer, focusedTrack, {color: FOCUSED_COVERAGE_COLOR, isColoredBySpeed: false});
  }
  coverageLayer.remove();
  coverageLayer = layer.addTo(map);
  coverageDotsLayer.remove();
  coverageDotsLayer = dotsLayer;
  updateCoverageDots();
  if (heatLayer) {
    heatLayer.remove();
    heatLayer = null;
  }
  heatCells = null;
  if (state.showHeat && tripsWithRoutes.length) {
    heatCells = countTripsPerCell(tripsWithRoutes.map((trip) => geometry[trip.id]));
    heatLayer = L.heatLayer(heatPoints(heatCells, map.getZoom()), {
      radius: HEAT_RADIUS_PX,
      blur: HEAT_BLUR_PX,
      max: 1,
      // Intensities do not grow with the zoom: the points are merged per zoom instead.
      maxZoom: 0,
      minOpacity: HEAT_MIN_OPACITY,
      gradient: HEAT_GRADIENT,
    }).addTo(map);
  }
  updatePlaybackMarker();
  hideMapStatus();
}

function updateCoverageDots() {
  const shouldShowDots = state.mapMode === "coverage" && map.getZoom() <= COVERAGE_DOTS_MAX_ZOOM;
  if (shouldShowDots && !map.hasLayer(coverageDotsLayer)) {
    coverageDotsLayer.addTo(map);
  } else if (!shouldShowDots) {
    coverageDotsLayer.remove();
  }
}

map.on("zoomend", updateCoverageDots);

export function renderMap() {
  const generation = ++drawGeneration;
  hideHover();
  if (state.mapMode === "coverage") {
    routesLayer.remove();
    speedLegend.remove();
    drawCoverage(generation).catch((error) => {
      console.error("Cannot load the coverage:", error);
      hideMapStatus();
    });
    return;
  }
  coverageLayer.remove();
  coverageDotsLayer.remove();
  if (heatLayer) {
    heatLayer.remove();
    heatLayer = null;
  }
  heatCells = null;
  if (state.colorMode === "speed") {
    speedLegend.addTo(map);
  } else {
    speedLegend.remove();
  }
  drawRoutes(generation).catch((error) => {
    console.error("Cannot draw the routes:", error);
    hideMapStatus();
  });
}

let mapStatusTimer = null;

function showMapStatus(text, {delayMs = 0} = {}) {
  clearTimeout(mapStatusTimer);
  const show = () => {
    dom.mapStatusText.textContent = text;
    dom.mapStatus.hidden = false;
  };
  if (delayMs) {
    mapStatusTimer = setTimeout(show, delayMs);
  } else {
    show();
  }
}

export function hideMapStatus() {
  clearTimeout(mapStatusTimer);
  dom.mapStatus.hidden = true;
}
