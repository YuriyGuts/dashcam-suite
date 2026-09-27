/*
 * Dashcam trip visualizer.
 *
 * Data comes from the `dashcam serve` API: `/api/trips` (the trip index), `/api/geometry`
 * (simplified routes for the coverage mode), and `/tracks/<id>.json` (full tracks, loaded when a
 * trip is drawn). Filters, the selection, and the map mode live in the URL hash.
 */
"use strict";

// Categorical trip colors, assigned in this order to selected trips. More trips reuse them.
const TRIP_COLORS = [
  "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300",
  "#4a3aa7", "#e34948", "#0e9aa7", "#a0522d", "#9b4dca", "#7a8b1f",
];

// Sequential speed scale (one hue, light to dark). Samples without a speed are gray.
const SPEED_BUCKETS = [
  {maxKmh: 20, color: "#86b6ef", label: "< 20"},
  {maxKmh: 40, color: "#5598e7", label: "20-40"},
  {maxKmh: 60, color: "#2a78d6", label: "40-60"},
  {maxKmh: 90, color: "#1c5cab", label: "60-90"},
  {maxKmh: 120, color: "#104281", label: "90-120"},
  {maxKmh: Infinity, color: "#0d366b", label: "120+"},
];
const UNKNOWN_SPEED_COLOR = "#898781";

// Line styles. Every route and coverage line sits on a white halo, so that it stands out from
// the muted basemap at any zoom.
const COVERAGE_COLOR = "#2a78d6";
const HALO_COLOR = "#ffffff";
const ROUTE_WEIGHT = 5;
const ROUTE_HALO_WEIGHT = 9;
const GAP_WEIGHT = 3;
const COVERAGE_WEIGHT = 4;
const COVERAGE_HALO_WEIGHT = 7;
const COVERAGE_OPACITY = 0.8;

// In coverage mode, at this zoom and below, each trip also gets a dot, so that short trips stay
// visible when the map shows a whole city or region.
const COVERAGE_DOTS_MAX_ZOOM = 12;

// Heatmap: one hue (orange, to stand apart from the blue coverage lines), light to dark. It
// shows how many trips passed through each cell of a ground grid, on a log scale.
const HEAT_GRADIENT = {0.15: "#f7c8b0", 0.45: "#eb6834", 0.75: "#b8461c", 1.0: "#6b240a"};
const HEAT_CELL_M = 10;
const HEAT_RADIUS_PX = 5;
const HEAT_BLUR_PX = 4;
const HEAT_MIN_OPACITY = 0.02;
// The heat of the least and the most visited cells, between 0 and 1.
const HEAT_MIN_LEVEL = 0.25;
// About how many point circles overlap on a line. The plugin adds up their opacities, so each
// point gets a lower opacity to reach the intended level.
const HEAT_OVERLAP = 4;
const METERS_PER_DEGREE_LAT = 111320;
// Width of the world at the equator in pixels at zoom 0, as Leaflet draws it.
const WORLD_WIDTH_PX = 256;
const EARTH_CIRCUMFERENCE_M = 40075016.686;

// Sample statuses with coordinates.
const LOCATED_STATUSES = new Set(["ok", "interpolated"]);

// Sample statuses as shown in the GPS coverage breakdown.
const STATUS_LABELS = {
  ok: "good",
  interpolated: "interpolated",
  no_fix: "no fix",
  spoofed: "spoofed",
  unreadable: "unreadable",
};

// The playback marker moves smoothly between samples at most this far apart (seconds).
const MAX_INTERPOLATION_STEP_S = 3;

// GPS coverage badge thresholds.
const GOOD_COVERAGE = 0.95;
const PARTIAL_COVERAGE = 0.7;

// A draw of at least this many trips shows its status on the map at once. Smaller draws show it
// only if they take longer than the delay (milliseconds).
const LARGE_DRAW_TRIP_COUNT = 30;
const MAP_STATUS_DELAY_MS = 200;

// The rename suggestion shows that it is loading only after this delay (milliseconds).
const SUGGESTION_LOADING_DELAY_MS = 300;

// Longest trip video filename, including the extension (see `dashcam rename`).
const MAX_FILENAME_LENGTH = 140;

// Where the position of the video panel is remembered between visits.
const VIDEO_PANEL_POSITION_KEY = "dashcam.videoPanelPosition";

const MILLISECONDS_PER_DAY = 86400000;

// Indexed by `Date.getUTCDay()`, which starts on Sunday.
const WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Separators in text.
const DOT = " \u00b7 ";
const DASH = "\u2013";
const ARROW = " \u2192 ";

// Stroke icons on a 24 x 24 grid, drawn with the current text color.
const ICON_PATHS = {
  arrowLeft: "M19 12H5M12 19l-7-7 7-7",
  play: "M7 4.5v15l12-7.5-12-7.5z",
  frame: "M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5",
  route: "M6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM18 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM8 17h8a3 3 0 0 0 0-6H8a3 3 0 0 1 0-6h8",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  gauge: "M4.5 17a8.5 8.5 0 1 1 15 0M12 13l4-4",
  peak: "M3 17l6-6 4 4 8-8M15 7h6v6",
  checkCircle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8.5 12l2.5 2.5 4.5-5",
  alert: "M12 4L2.5 20h19L12 4zM12 10v4M12 17h.01",
  xCircle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM15 9l-6 6M9 9l6 6",
  pencil: "M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3zM13.5 8.5l3 3",
  eyeOff: "M3 3l18 18M10.6 5.1A9.8 9.8 0 0 1 12 5c5 0 9 4.5 10 7a13 13 0 0 1-2.6 3.7M6.6 6.6C4.4 8 2.8 10.2 2 12c1 2.5 5 7 10 7 1.8 0 3.5-.6 4.9-1.5M9.9 9.9a3 3 0 0 0 4.2 4.2",
};

const state = {
  trips: [],
  tripsById: new Map(),
  libraryDir: "",
  // Whether the server allows renaming trips (on localhost, or with `--allow-rename`).
  canRename: false,
  // First and last trip dates, as ISO dates and as day numbers. Null without dated trips.
  dateBounds: null,
  // Empty `from` or `to` means the filter is open on that side.
  // `query` matches trip names, street names, and start and end localities.
  filters: {from: "", to: "", query: ""},
  selectedIds: [],
  focusedId: null,
  colorMode: "trip",
  mapMode: "routes",
  showHeat: false,
  // Trips hidden from the coverage map with the H shortcut, until the page is reloaded.
  hiddenIds: new Set(),
};

// Color slot of each selected trip. A trip keeps its color while it stays selected.
const colorSlots = new Map();

// Prepared tracks by trip ID: promises while loading, and the loaded values for synchronous use.
const trackPromises = new Map();
const loadedTracks = new Map();
let geometryPromise = null;

// Incremented on every redraw, so that a slow track load does not draw a stale selection.
let drawGeneration = 0;

// Set while a filter change waits for the next animation frame to be rendered.
let isFilterRenderScheduled = false;

// The trip name being edited: the trip, the text typed so far, the suggested name, and the
// state of the request. Null while no name is being edited.
let renameEdit = null;

const video = {
  tripId: null,
  source: null,
  element: null,
  animationFrame: null,
};

/* Helpers. */

function withoutEmpty(children) {
  return children.flat().filter((child) => child !== null && child !== undefined && child !== false);
}

function el(tag, attributes = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    if (name === "className") {
      element.className = value;
    } else if (name.startsWith("on")) {
      element.addEventListener(name.slice(2), value);
    } else {
      element.setAttribute(name, value === true ? "" : value);
    }
  }
  element.append(...withoutEmpty(children));
  return element;
}

function icon(name, className = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", ICON_PATHS[name]);
  svg.append(path);
  return svg;
}

// Trip dates are calendar dates: formatted from the text, with no time zone involved.
function formatDate(isoDate) {
  if (!isoDate) {
    return "No date";
  }
  const [year, month, day] = isoDate.split("-").map(Number);
  const weekday = WEEKDAY_NAMES[new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
  return `${weekday}, ${MONTH_NAMES[month - 1]} ${day}, ${year}`;
}

const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

// How long (milliseconds) a date field stays marked after a rejected entry.
const INVALID_DATE_FLASH_MS = 1500;

// Accepts only real calendar dates written as `yyyy-mm-dd`.
function isValidIsoDate(text) {
  if (!ISO_DATE_PATTERN.test(text)) {
    return false;
  }
  return dayToIsoDate(isoDateToDay(text)) === text;
}

function formatIsoDate(date) {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

// First day of a date preset, counted back from today in the browser's time zone. A month or a
// year back from a day that does not exist in the target month (e.g. March 31) is the last day
// of that month.
function presetStartDate(preset) {
  const today = new Date();
  if (preset === "week") {
    return formatIsoDate(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 7));
  }
  const yearsBack = preset === "year" ? 1 : 0;
  const monthsBack = preset === "month" ? 1 : 0;
  const year = today.getFullYear() - yearsBack;
  const month = today.getMonth() - monthsBack;
  const lastDayOfMonth = new Date(year, month + 1, 0).getDate();
  return formatIsoDate(new Date(year, month, Math.min(today.getDate(), lastDayOfMonth)));
}

function isoDateToDay(isoDate) {
  const [year, month, day] = isoDate.split("-").map(Number);
  return Date.UTC(year, month - 1, day) / MILLISECONDS_PER_DAY;
}

function dayToIsoDate(dayNumber) {
  return new Date(dayNumber * MILLISECONDS_PER_DAY).toISOString().slice(0, 10);
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) {
    return "-";
  }
  const totalSeconds = Math.round(seconds);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const secondsPart = String(totalSeconds % 60).padStart(2, "0");
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${secondsPart}`;
  }
  return `${minutes}:${secondsPart}`;
}

// Times are shown as the camera clock showed them, whatever the time zone of the browser.
function formatClockTime(isoTime, withSeconds = false) {
  if (!isoTime) {
    return "?";
  }
  return isoTime.slice(11, withSeconds ? 19 : 16);
}

function formatDistance(km) {
  if (km === null || km === undefined) {
    return "-";
  }
  return km >= 100 ? `${Math.round(km)} km` : `${km.toFixed(1)} km`;
}

function formatSpeed(kmh) {
  return kmh === null || kmh === undefined ? "-" : `${Math.round(kmh)} km/h`;
}

function formatCoordinates(lat, lon) {
  return `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

function hasGps(trip) {
  return trip.extraction_status === "ok";
}

function isLocated(sample) {
  return LOCATED_STATUSES.has(sample.status) && sample.lat !== null && sample.lon !== null;
}

function tripColor(tripId) {
  const slot = colorSlots.get(tripId) ?? 0;
  return TRIP_COLORS[slot % TRIP_COLORS.length];
}

function speedColor(kmh) {
  if (kmh === null || kmh === undefined) {
    return UNKNOWN_SPEED_COLOR;
  }
  return SPEED_BUCKETS.find((bucket) => kmh < bucket.maxKmh).color;
}

function coverageBadge(trip) {
  const coverage = trip.coverage ?? 0;
  const [level, iconName] =
    coverage >= GOOD_COVERAGE
      ? ["good", "checkCircle"]
      : coverage >= PARTIAL_COVERAGE
        ? ["partial", "alert"]
        : ["poor", "xCircle"];
  return el(
    "span",
    {className: `badge badge-${level}`, title: "Share of samples with a GPS position"},
    icon(iconName),
    `${Math.round(coverage * 100)}% GPS`,
  );
}

function videoSources(trip) {
  const sources = {};
  if (trip.preview_url) sources.preview = trip.preview_url;
  if (trip.video_url) sources.original = trip.video_url;
  return sources;
}

function canPlayVideo(trip) {
  return Object.keys(videoSources(trip)).length > 0;
}

// Fetches JSON. Errors carry the `error` message of the response if the server sent one.
async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.error ?? `${url}: HTTP ${response.status}`);
  }
  return response.json();
}

function postJson(url, data) {
  return fetchJson(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data),
  });
}

function readStoredJson(key) {
  try {
    return JSON.parse(localStorage.getItem(key));
  } catch {
    return null;
  }
}

function writeStoredJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage may be unavailable (private windows, blocked site data). The value is a nicety.
  }
}

// Resolves once the browser had a chance to paint, e.g. a status shown before a long draw.
function nextPaint() {
  return new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
}

/* URL hash state. */

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  state.filters.from = params.get("from") || "";
  state.filters.to = params.get("to") || "";
  state.filters.query = params.get("q") || "";
  state.selectedIds = [...new Set(params.getAll("sel"))].filter((id) => state.tripsById.has(id));
  const focusedId = params.get("trip");
  state.focusedId = state.tripsById.has(focusedId) ? focusedId : null;
  state.colorMode = params.get("color") === "speed" ? "speed" : "trip";
  state.mapMode = params.get("mode") === "coverage" ? "coverage" : "routes";
  state.showHeat = params.get("heat") === "1";
}

function writeHash({pushHistory = false} = {}) {
  const params = new URLSearchParams();
  const {from, to, query} = state.filters;
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  if (query) params.set("q", query);
  for (const id of state.selectedIds) params.append("sel", id);
  if (state.focusedId) params.set("trip", state.focusedId);
  if (state.colorMode !== "trip") params.set("color", state.colorMode);
  if (state.mapMode !== "routes") params.set("mode", state.mapMode);
  if (state.showHeat) params.set("heat", "1");

  const hashText = params.toString();
  const url = hashText ? `#${hashText}` : location.pathname + location.search;
  if (pushHistory) {
    history.pushState(null, "", url);
  } else {
    history.replaceState(null, "", url);
  }
}

function syncColorSlots() {
  const selected = new Set(state.selectedIds);
  for (const id of [...colorSlots.keys()]) {
    if (!selected.has(id)) {
      colorSlots.delete(id);
    }
  }
  const usedSlots = new Set(colorSlots.values());
  let nextSlot = 0;
  for (const id of state.selectedIds) {
    if (colorSlots.has(id)) {
      continue;
    }
    while (usedSlots.has(nextSlot)) {
      nextSlot++;
    }
    colorSlots.set(id, nextSlot);
    usedSlots.add(nextSlot);
  }
}

/* Filtering and aggregates. */

function computeDateBounds(trips) {
  const dates = trips.map((trip) => trip.date).filter(Boolean).sort();
  if (!dates.length) {
    return null;
  }
  const first = dates[0];
  const last = dates.at(-1);
  return {first, last, firstDay: isoDateToDay(first), lastDay: isoDateToDay(last)};
}

// A query holds one or more comma-separated terms, e.g. "Stryiska, Zelena". A trip matches if each
// term is part of its name, one of its street names, or its start or end locality.
function queryTerms(query) {
  return query
    .split(",")
    .map((term) => term.trim().toLowerCase())
    .filter(Boolean);
}

function matchesQueryTerm(trip, term) {
  const texts = [trip.name, ...trip.streets, trip.start_locality, trip.end_locality];
  return texts.some((text) => text && text.toLowerCase().includes(term));
}

function filteredTrips() {
  const {from, to} = state.filters;
  const terms = queryTerms(state.filters.query);
  return state.trips.filter((trip) => {
    if ((from || to) && !trip.date) return false;
    if (from && trip.date < from) return false;
    if (to && trip.date > to) return false;
    return terms.every((term) => matchesQueryTerm(trip, term));
  });
}

// Selected trips that the filters let through, which are the ones drawn on the route map. The
// focused trip is drawn even if the filters hide it.
function drawnSelection(visibleTrips = filteredTrips()) {
  const visibleIds = new Set(visibleTrips.map((trip) => trip.id));
  return state.selectedIds.filter((id) => visibleIds.has(id) || id === state.focusedId);
}

function aggregateStats(trips) {
  let distanceKm = 0;
  let durationS = 0;
  let movingHours = 0;
  let maxKmh = null;
  for (const trip of trips) {
    distanceKm += trip.distance_km ?? 0;
    durationS += trip.duration_s ?? 0;
    if (trip.avg_kmh) {
      movingHours += (trip.distance_km ?? 0) / trip.avg_kmh;
    }
    if (trip.max_kmh !== null && trip.max_kmh !== undefined) {
      maxKmh = Math.max(maxKmh ?? 0, trip.max_kmh);
    }
  }
  return {
    count: trips.length,
    distanceKm,
    durationS,
    avgKmh: movingHours > 0 ? distanceKm / movingHours : null,
    maxKmh,
  };
}

/* Tracks. */

function prepareTrack(trip, data) {
  const samples = data.samples || [];
  const runs = [];
  let run = [];
  samples.forEach((sample, index) => {
    if (isLocated(sample)) {
      run.push(index);
    } else if (run.length) {
      runs.push(run);
      run = [];
    }
  });
  if (run.length) {
    runs.push(run);
  }
  const gaps = [];
  for (let runIndex = 1; runIndex < runs.length; runIndex++) {
    gaps.push([runs[runIndex - 1].at(-1), runs[runIndex][0]]);
  }
  return {trip, samples, runs, gaps, locatedIndexes: runs.flat(), streets: data.streets || []};
}

function loadTrack(tripId) {
  if (!trackPromises.has(tripId)) {
    const trip = state.tripsById.get(tripId);
    const promise = fetchJson(trip.track_url).then((data) => {
      const track = prepareTrack(trip, data);
      loadedTracks.set(tripId, track);
      return track;
    });
    promise.catch(() => trackPromises.delete(tripId));
    trackPromises.set(tripId, promise);
  }
  return trackPromises.get(tripId);
}

function loadGeometry() {
  if (!geometryPromise) {
    geometryPromise = fetchJson("/api/geometry").then((data) => data.trips);
    geometryPromise.catch(() => {
      geometryPromise = null;
    });
  }
  return geometryPromise;
}

function sampleLatLng(sample) {
  return [sample.lat, sample.lon];
}

function nearestSampleIndex(track, sampleIndexes, latlng) {
  const lonScale = Math.cos((latlng.lat * Math.PI) / 180);
  let bestIndex = sampleIndexes[0];
  let bestDistance = Infinity;
  for (const index of sampleIndexes) {
    const sample = track.samples[index];
    const dLat = sample.lat - latlng.lat;
    const dLon = (sample.lon - latlng.lng) * lonScale;
    const distance = dLat * dLat + dLon * dLon;
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = index;
    }
  }
  return bestIndex;
}

// Position at a video offset: the last sample at or before it, moved towards the next sample.
function positionAtVideoTime(track, seconds) {
  const samples = track.samples;
  if (!samples.length) {
    return null;
  }
  let low = 0;
  let high = samples.length - 1;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (samples[middle].t <= seconds) {
      low = middle;
    } else {
      high = middle - 1;
    }
  }
  const sample = samples[low];
  if (!isLocated(sample)) {
    return null;
  }
  const nextSample = samples[low + 1];
  const canInterpolate =
    nextSample &&
    isLocated(nextSample) &&
    seconds > sample.t &&
    nextSample.t - sample.t <= MAX_INTERPOLATION_STEP_S;
  if (!canInterpolate) {
    return sampleLatLng(sample);
  }
  const fraction = (seconds - sample.t) / (nextSample.t - sample.t);
  return [
    sample.lat + (nextSample.lat - sample.lat) * fraction,
    sample.lon + (nextSample.lon - sample.lon) * fraction,
  ];
}

/* Map. */

const map = L.map("map", {preferCanvas: true, zoomSnap: 0.5});
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
const playbackMarker = L.marker([0, 0], {
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
const DEFAULT_VIEW = {center: [50, 15], zoom: 4};

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

function fitToTrips(trips, {animate = true} = {}) {
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
function onHoverShortcut(event) {
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

function showHiddenTrips(tripIds) {
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
  const simplifiedTrips = simplifiedIds.filter((id) => geometry[id]).map((id) => state.tripsById.get(id));
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

function coverageTrips() {
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
  const maxCount = Math.max(HEAT_MIN_SCALE_COUNT, ...cells.values());
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
  const tripsWithRoutes = trips.filter((trip) => geometry[trip.id]);
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

function renderMap() {
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

/* Sidebar. */

const dom = {
  librarySummary: document.getElementById("library-summary"),
  filterFrom: document.getElementById("filter-from"),
  filterTo: document.getElementById("filter-to"),
  filterQuery: document.getElementById("filter-query"),
  streetNames: document.getElementById("street-names"),
  dateFilter: document.getElementById("date-filter"),
  dateSlider: document.getElementById("date-slider"),
  dateSliderFrom: document.getElementById("date-slider-from"),
  dateSliderTo: document.getElementById("date-slider-to"),
  datePresets: document.getElementById("date-presets"),
  colorControls: document.getElementById("color-controls"),
  heatControl: document.getElementById("heat-control"),
  heatToggle: document.getElementById("heat-toggle"),
  summary: document.getElementById("summary"),
  tripBrowser: document.getElementById("trip-browser"),
  tripList: document.getElementById("trip-list"),
  noGpsTrips: document.getElementById("no-gps-trips"),
  tripDetail: document.getElementById("trip-detail"),
  mapArea: document.getElementById("map-area"),
  mapStatus: document.getElementById("map-status"),
  mapStatusText: document.getElementById("map-status-text"),
  videoPanel: document.getElementById("video-panel"),
  videoHeader: document.querySelector("#video-panel .video-header"),
  videoTitle: document.getElementById("video-title"),
  videoSource: document.getElementById("video-source"),
  videoContainer: document.getElementById("video-container"),
  videoMessage: document.getElementById("video-message"),
};

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

function hideMapStatus() {
  clearTimeout(mapStatusTimer);
  dom.mapStatus.hidden = true;
}

function renderLibrarySummary() {
  const tripCountText = `${state.trips.length} ${state.trips.length === 1 ? "trip" : "trips"}`;
  const bounds = state.dateBounds;
  let yearText = "";
  if (bounds) {
    const [firstYear, lastYear] = [bounds.first.slice(0, 4), bounds.last.slice(0, 4)];
    yearText = firstYear === lastYear ? firstYear : `${firstYear}${DASH}${lastYear}`;
  }
  dom.librarySummary.textContent = [tripCountText, yearText].filter(Boolean).join(DOT);
}

function renderDateTicks() {
  const bounds = state.dateBounds;
  const ticks = dom.dateSlider.querySelector(".range-ticks");
  const daySpan = bounds ? bounds.lastDay - bounds.firstDay : 0;
  if (!daySpan) {
    ticks.replaceChildren();
    return;
  }
  const tripDays = new Set(state.trips.filter((trip) => trip.date).map((trip) => isoDateToDay(trip.date)));
  ticks.replaceChildren(
    ...[...tripDays].map((day) =>
      el("span", {className: "range-tick", style: `left: ${((day - bounds.firstDay) / daySpan) * 100}%`}),
    ),
  );
}

function renderDateFilter() {
  const bounds = state.dateBounds;
  dom.dateFilter.hidden = bounds === null;
  if (!bounds) {
    return;
  }
  const daySpan = bounds.lastDay - bounds.firstDay;
  dom.dateSlider.hidden = daySpan === 0;
  const clampDay = (day) => Math.min(Math.max(day, 0), daySpan);
  const fromDay = state.filters.from ? clampDay(isoDateToDay(state.filters.from) - bounds.firstDay) : 0;
  const toDay = state.filters.to ? clampDay(isoDateToDay(state.filters.to) - bounds.firstDay) : daySpan;
  for (const slider of [dom.dateSliderFrom, dom.dateSliderTo]) {
    slider.min = "0";
    slider.max = String(daySpan);
    slider.step = "1";
  }
  dom.dateSliderFrom.value = String(fromDay);
  dom.dateSliderTo.value = String(toDay);
  // When both thumbs meet near the end, the start thumb must stay on top to be draggable back.
  dom.dateSliderFrom.style.zIndex = fromDay > daySpan / 2 ? "2" : "1";
  const fill = dom.dateSlider.querySelector(".range-fill");
  if (daySpan) {
    fill.style.left = `${(fromDay / daySpan) * 100}%`;
    fill.style.right = `${100 - (toDay / daySpan) * 100}%`;
  }

  const fromText = state.filters.from || bounds.first;
  const toText = state.filters.to || bounds.last;
  if (document.activeElement !== dom.filterFrom) dom.filterFrom.value = fromText;
  if (document.activeElement !== dom.filterTo) dom.filterTo.value = toText;

  for (const button of dom.datePresets.querySelectorAll("[data-preset]")) {
    const preset = button.dataset.preset;
    const isActive =
      preset === "all"
        ? !state.filters.from && !state.filters.to
        : state.filters.from === presetStartDate(preset) && !state.filters.to;
    button.setAttribute("aria-pressed", String(isActive));
  }
}

function renderControls() {
  if (dom.filterQuery.value !== state.filters.query) dom.filterQuery.value = state.filters.query;
  renderDateFilter();

  for (const button of document.querySelectorAll("[data-mode]")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === state.mapMode));
  }
  for (const button of document.querySelectorAll("[data-color]")) {
    button.setAttribute("aria-pressed", String(button.dataset.color === state.colorMode));
  }
  dom.colorControls.hidden = state.mapMode !== "routes";
  dom.heatControl.hidden = state.mapMode !== "coverage";
  dom.heatToggle.checked = state.showHeat;
}

function renderSummary(visibleTrips) {
  const visibleWithGps = visibleTrips.filter(hasGps);
  const drawnIds = drawnSelection(visibleTrips);
  const useSelection = state.mapMode === "routes" && drawnIds.length > 0;
  const hiddenIds =
    state.mapMode === "coverage"
      ? visibleWithGps.filter((trip) => state.hiddenIds.has(trip.id)).map((trip) => trip.id)
      : [];
  const trips = useSelection
    ? drawnIds.map((id) => state.tripsById.get(id))
    : visibleWithGps.filter((trip) => !hiddenIds.includes(trip.id));
  const stats = aggregateStats(trips);
  const filteredOutCount = state.mapMode === "routes" ? state.selectedIds.length - drawnIds.length : 0;
  const titleText = useSelection
    ? `${stats.count} of ${visibleWithGps.length} trips selected`
    : `${stats.count} ${stats.count === 1 ? "trip" : "trips"} shown`;
  let titleNote = null;
  if (hiddenIds.length) {
    titleNote = el(
      "button",
      {type: "button", className: "chip chip-small", title: "Show the hidden trips again", onclick: () => showHiddenTrips(hiddenIds)},
      `${hiddenIds.length} hidden${DOT}Show all`,
    );
  } else if (filteredOutCount > 0) {
    titleNote = el(
      "span",
      {className: "summary-note", title: "Selected trips that the filters leave out are not drawn"},
      `${filteredOutCount} more hidden by filters`,
    );
  }
  const title = el("div", {className: "summary-title"}, el("span", {}, titleText), titleNote);

  const statTiles = [
    ["Distance", formatDistance(stats.distanceKm)],
    ["Time", formatDuration(stats.durationS)],
    ["Average", formatSpeed(stats.avgKmh)],
    ["Top speed", formatSpeed(stats.maxKmh)],
  ].map(([label, value]) =>
    el(
      "div",
      {className: "stat"},
      el("span", {className: "stat-label"}, label),
      el("span", {className: "stat-value", title: value}, value),
    ),
  );
  const children = [
    title,
    el("div", {className: "stat-grid"}, statTiles),
  ];
  if (!state.trips.length) {
    children.push(
      el("p", {className: "hint"}, "No trips yet. Run ", el("code", {}, "dashcam extract"), "."),
    );
  } else if (!visibleTrips.length) {
    children.push(el("p", {className: "hint"}, "No trips match the filters."));
  } else if (state.mapMode === "routes" && !drawnIds.length) {
    children.push(el("p", {className: "hint"}, "Select trips to draw them, or switch the map to Coverage."));
  }
  dom.summary.replaceChildren(...children);
}

function renderTripRow(trip) {
  const isSelected = state.selectedIds.includes(trip.id);
  const checkbox = el("input", {
    type: "checkbox",
    checked: isSelected,
    "aria-label": `Draw ${trip.name}`,
    onchange: (event) => setTripSelected(trip.id, event.target.checked),
  });
  // A checked box takes the color of the trip on the map, so the list doubles as the legend.
  if (isSelected && state.colorMode === "trip" && state.mapMode === "routes") {
    checkbox.style.setProperty("--check-color", tripColor(trip.id));
  }
  const meta = [
    formatDate(trip.date),
    trip.start_time ? formatClockTime(trip.start_time) : null,
    formatDistance(trip.distance_km),
    formatDuration(trip.duration_s),
  ].filter(Boolean);
  const isHidden = state.mapMode === "coverage" && state.hiddenIds.has(trip.id);
  const body = el(
    "button",
    {type: "button", className: "trip-body", onclick: () => focusTrip(trip.id)},
    el(
      "span",
      {className: "trip-title-row"},
      el("span", {className: "trip-name", title: trip.name}, trip.name),
      coverageBadge(trip),
    ),
    el("span", {className: "trip-meta"}, meta.join(DOT)),
  );
  const showButton = isHidden
    ? el(
        "button",
        {type: "button", className: "icon-button trip-show", "aria-label": `Show ${trip.name} on the map`, title: "Hidden from the map. Click to show", onclick: () => showHiddenTrips([trip.id])},
        icon("eyeOff", "icon icon-small"),
      )
    : null;
  const classNames = ["trip", trip.id === state.focusedId ? "focused" : null, isHidden ? "is-hidden" : null];
  return el(
    "li",
    {className: withoutEmpty(classNames).join(" "), "data-id": trip.id},
    checkbox,
    body,
    showButton,
  );
}

function renderTripList(visibleTrips) {
  const tripsWithGps = visibleTrips.filter(hasGps);
  const tripsWithoutGps = visibleTrips.filter((trip) => !hasGps(trip));
  dom.tripList.replaceChildren(...tripsWithGps.map(renderTripRow));

  dom.noGpsTrips.hidden = tripsWithoutGps.length === 0;
  dom.noGpsTrips.querySelector("summary").textContent = `Without GPS (${tripsWithoutGps.length})`;
  dom.noGpsTrips.querySelector("ul").replaceChildren(
    ...tripsWithoutGps.map((trip) => el("li", {}, trip.video_filename)),
  );

  const allStreetNames = [...new Set(state.trips.flatMap((trip) => trip.streets))].sort();
  dom.streetNames.replaceChildren(...allStreetNames.map((name) => el("option", {value: name})));
}

function renderStreets(container, track) {
  if (!track.streets.length) {
    container.replaceChildren();
    return;
  }
  const items = track.streets.map((street) => {
    const name = typeof street === "string" ? street : street.name;
    const distanceM = typeof street === "object" ? street.distance_m : null;
    return el(
      "li",
      {},
      name ?? "?",
      distanceM ? el("span", {className: "street-distance"}, ` ${formatDistance(distanceM / 1000)}`) : null,
    );
  });
  container.replaceChildren(
    el("div", {className: "detail-section"}, el("h3", {}, "Streets"), el("ol", {className: "street-list"}, items)),
  );
}

// Start and end localities, e.g. "Lviv -> Stryi", or one name for a trip within a locality.
function formatLocalities(trip) {
  const {start_locality: start, end_locality: end} = trip;
  if (!start && !end) return null;
  if (start === end) return start;
  return `${start ?? "?"}${ARROW}${end ?? "?"}`;
}

/* Renaming a trip. */

function fileExtension(filename) {
  const dotIndex = filename.lastIndexOf(".");
  return dotIndex > 0 ? filename.slice(dotIndex) : "";
}

function canRenameTrip(trip) {
  return state.canRename && Boolean(trip.video_url) && Boolean(trip.date);
}

// The filename a trip gets for the typed name: the date and the extension stay as they are.
function renamedFilename(trip, name) {
  return `${trip.date} ${name.trim()}${fileExtension(trip.video_filename)}`;
}

// The part of a suggested filename between the date and the extension.
function nameFromFilename(trip, filename) {
  return filename.slice(trip.date.length + 1, filename.length - fileExtension(filename).length);
}

function renderTripName(trip) {
  if (!canRenameTrip(trip)) {
    return el("h2", {}, trip.name);
  }
  return el(
    "div",
    {className: "trip-name-row"},
    el("h2", {}, trip.name),
    el(
      "button",
      {type: "button", className: "icon-button", "aria-label": "Rename trip", title: "Rename trip", onclick: () => startRename(trip.id)},
      icon("pencil", "icon icon-small"),
    ),
  );
}

// The rename field grows with the name, so that a long name stays readable while it is edited.
function fitRenameInput() {
  const input = renameEdit?.input;
  if (input?.isConnected) {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }
}

function renderRenameForm(trip) {
  const input = el("textarea", {
    className: "rename-input",
    rows: 1,
    spellcheck: false,
    autocomplete: "off",
    "aria-label": "Trip name",
    "aria-invalid": renameEdit.error ? "true" : false,
    disabled: renameEdit.isSaving,
  });
  input.value = renameEdit.draft;
  input.addEventListener("input", () => {
    // A filename has no line breaks, e.g. from a pasted text.
    if (/[\r\n]/.test(input.value)) {
      input.value = input.value.replace(/[\r\n]+/g, " ");
    }
    renameEdit.draft = input.value;
    renameEdit.error = null;
    input.removeAttribute("aria-invalid");
    fitRenameInput();
    updateRenameHints(trip);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveRename();
    } else if (event.key === "Escape") {
      event.stopPropagation();
      cancelRename();
    }
  });
  renameEdit.input = input;
  renameEdit.filenameNote = el("p", {className: "rename-filename"});
  renameEdit.hint = el("div", {className: "rename-hint"});

  const form = el(
    "form",
    {className: "rename-form", onsubmit: (event) => {
      event.preventDefault();
      saveRename();
    }},
    input,
    renameEdit.filenameNote,
    renameEdit.hint,
    el(
      "div",
      {className: "rename-actions"},
      el("button", {type: "submit", className: "button button-primary", disabled: renameEdit.isSaving}, renameEdit.isSaving ? "Saving..." : "Save"),
      el("button", {type: "button", className: "button", disabled: renameEdit.isSaving, onclick: cancelRename}, "Cancel"),
    ),
  );
  updateRenameHints(trip);
  return form;
}

// Updates the length and the error or suggestion under the field without rebuilding it.
function updateRenameHints(trip) {
  const filename = renamedFilename(trip, renameEdit.draft);
  const lengthNote = el("span", {className: "rename-length"}, `${filename.length}/${MAX_FILENAME_LENGTH}`);
  lengthNote.classList.toggle("is-over", filename.length > MAX_FILENAME_LENGTH);
  renameEdit.filenameNote.replaceChildren(el("span", {className: "rename-filename-text"}, filename), DOT, lengthNote);

  let hint;
  if (renameEdit.error) {
    hint = el("p", {className: "rename-error", role: "alert"}, renameEdit.error);
  } else if (renameEdit.suggestionError) {
    hint = el("p", {}, renameEdit.suggestionError);
  } else if (renameEdit.suggestion === null) {
    hint = renameEdit.isSuggestionSlow
      ? el("p", {}, el("span", {className: "spinner", "aria-hidden": "true"}), "Loading the suggested name...")
      : null;
  } else if (renameEdit.suggestion === renameEdit.draft.trim()) {
    hint = el("p", {}, "This is the suggested name.");
  } else {
    hint = el(
      "p",
      {},
      "Suggested: ",
      el(
        "button",
        {type: "button", className: "link-button rename-suggestion", title: "Use the suggested name", onclick: useSuggestedName},
        renameEdit.suggestion,
      ),
    );
  }
  renameEdit.hint.replaceChildren(...withoutEmpty([hint]));
}

function startRename(tripId) {
  const trip = state.tripsById.get(tripId);
  renameEdit = {
    tripId,
    draft: trip.name,
    suggestion: null,
    suggestionError: null,
    isSuggestionSlow: false,
    error: null,
    isSaving: false,
  };
  renderDetail();
  renameEdit.input.focus();
  renameEdit.input.select();
  const edit = renameEdit;
  const slowTimer = setTimeout(() => {
    edit.isSuggestionSlow = true;
    if (renameEdit === edit) updateRenameHints(trip);
  }, SUGGESTION_LOADING_DELAY_MS);
  fetchJson(`/api/suggestion?id=${encodeURIComponent(tripId)}`)
    .then((data) => {
      edit.suggestion = nameFromFilename(trip, data.filename);
    })
    .catch((error) => {
      edit.suggestionError = `No suggestion: ${error.message}`;
    })
    .finally(() => {
      clearTimeout(slowTimer);
      if (renameEdit === edit) updateRenameHints(trip);
    });
}

function useSuggestedName() {
  const trip = state.tripsById.get(renameEdit.tripId);
  renameEdit.draft = renameEdit.suggestion;
  renameEdit.error = null;
  renameEdit.input.value = renameEdit.draft;
  renameEdit.input.removeAttribute("aria-invalid");
  renameEdit.input.focus();
  fitRenameInput();
  updateRenameHints(trip);
}

function cancelRename() {
  renameEdit = null;
  renderDetail();
}

async function saveRename() {
  if (!renameEdit || renameEdit.isSaving) {
    return;
  }
  const edit = renameEdit;
  const trip = state.tripsById.get(edit.tripId);
  if (!edit.draft.trim()) {
    edit.error = "Enter a name.";
    renderDetail();
    return;
  }
  const filename = renamedFilename(trip, edit.draft);
  if (filename === trip.video_filename) {
    cancelRename();
    return;
  }

  // The video is renamed on disk, so its player is closed and reopened under the new name.
  const playback = video.tripId === trip.id && video.element
    ? {startAt: video.element.currentTime, autoplay: !video.element.paused}
    : null;
  edit.isSaving = true;
  closeVideo();
  renderDetail();
  let newId;
  try {
    newId = (await postJson("/api/rename", {id: trip.id, filename})).id;
  } catch (error) {
    edit.isSaving = false;
    edit.error = error.message;
    if (playback) openVideo(trip.id, playback);
    renderDetail();
    edit.input.focus();
    return;
  }
  renameEdit = null;
  await reloadTrips({oldId: trip.id, newId});
  if (playback) openVideo(newId, playback);
}

// Loads the trip index again after a rename, moving everything keyed by the old trip ID.
async function reloadTrips({oldId, newId}) {
  const index = await fetchJson("/api/trips");
  applyIndex(index);
  const toNewId = (id) => (id === oldId ? newId : id);
  state.selectedIds = state.selectedIds.map(toNewId).filter((id) => state.tripsById.has(id));
  const focusedId = state.focusedId && toNewId(state.focusedId);
  state.focusedId = state.tripsById.has(focusedId) ? focusedId : null;
  if (colorSlots.has(oldId)) {
    colorSlots.set(newId, colorSlots.get(oldId));
    colorSlots.delete(oldId);
  }
  for (const tracks of [trackPromises, loadedTracks]) {
    if (tracks.has(oldId)) {
      tracks.set(newId, tracks.get(oldId));
      tracks.delete(oldId);
    }
  }
  for (const [id, track] of loadedTracks) {
    track.trip = state.tripsById.get(id) ?? track.trip;
  }
  geometryPromise = null;
  syncColorSlots();
  renderDateTicks();
  writeHash();
  renderAll();
}

function renderDetail() {
  const trip = state.focusedId ? state.tripsById.get(state.focusedId) : null;
  dom.tripBrowser.hidden = trip !== null;
  dom.tripDetail.hidden = trip === null;
  if (!trip) {
    dom.tripDetail.replaceChildren();
    return;
  }

  const timeRange = trip.start_time
    ? `${formatClockTime(trip.start_time, true)}${DASH}${formatClockTime(trip.end_time, true)}`
    : "Time unknown";
  const statusText = Object.entries(trip.status_counts || {})
    .map(([status, count]) => `${count} ${STATUS_LABELS[status] ?? status}`)
    .join(", ");
  const facts = [
    ["route", formatDistance(trip.distance_km), "Distance"],
    ["clock", formatDuration(trip.duration_s), "Duration"],
    ["gauge", formatSpeed(trip.avg_kmh), "Average"],
    ["peak", formatSpeed(trip.max_kmh), "Top speed"],
  ].map(([iconName, value, label]) =>
    el(
      "div",
      {className: "fact"},
      icon(iconName),
      el("span", {className: "fact-value"}, value),
      el("span", {className: "fact-label"}, label),
    ),
  );

  const canPlay = canPlayVideo(trip);
  const isPlaying = video.tripId === trip.id;
  const playButton = el(
    "button",
    {
      type: "button",
      className: "button button-primary",
      disabled: !canPlay,
      onclick: () => (isPlaying ? closeVideo() : openVideo(trip.id)),
    },
    isPlaying ? null : icon("play", "icon icon-small"),
    isPlaying ? "Close video" : "Play video",
  );
  const zoomButton = el(
    "button",
    {type: "button", className: "button", disabled: !trip.bbox, onclick: () => fitToTrips([trip])},
    icon("frame", "icon icon-small"),
    "Zoom to trip",
  );
  const missingVideoNote = canPlay
    ? null
    : el(
        "p",
        {className: "detail-note"},
        `${trip.video_filename} is not in `,
        el("code", {}, state.libraryDir || "the library directory"),
        ".",
      );
  const localitiesText = formatLocalities(trip);
  const streetsContainer = el("div", {});
  const focusedRenameInput = renameEdit?.input === document.activeElement ? renameEdit.input : null;
  dom.tripDetail.replaceChildren(
    el(
      "div",
      {className: "detail-header"},
      el(
        "button",
        {type: "button", className: "round-button", "aria-label": "All trips", title: "All trips", onclick: () => focusTrip(null)},
        icon("arrowLeft", "icon icon-small"),
      ),
      el(
        "div",
        {className: "detail-title"},
        renameEdit?.tripId === trip.id ? renderRenameForm(trip) : renderTripName(trip),
        el("p", {className: "detail-subtitle"}, [formatDate(trip.date), timeRange].join(DOT)),
        localitiesText ? el("p", {className: "detail-subtitle"}, localitiesText) : null,
      ),
    ),
    el("div", {className: "detail-actions"}, withoutEmpty([playButton, zoomButton, missingVideoNote])),
    el("div", {className: "detail-facts"}, facts),
    el(
      "div",
      {className: "detail-section"},
      el("h3", {}, "GPS coverage"),
      el("div", {className: "coverage-row"}, coverageBadge(trip), statusText),
    ),
    streetsContainer,
  );
  fitRenameInput();
  // Rendering replaces the rename field; keep the focus and the cursor in the new one.
  if (focusedRenameInput && renameEdit?.input) {
    renameEdit.input.focus();
    renameEdit.input.setSelectionRange(focusedRenameInput.selectionStart, focusedRenameInput.selectionEnd);
  }

  if (hasGps(trip)) {
    loadTrack(trip.id)
      .then((track) => {
        if (state.focusedId === trip.id) renderStreets(streetsContainer, track);
      })
      .catch((error) => console.error(error));
  }
}

function renderSidebar() {
  const visibleTrips = filteredTrips();
  renderLibrarySummary();
  renderControls();
  renderSummary(visibleTrips);
  renderTripList(visibleTrips);
  renderDetail();
}

function renderAll() {
  renderSidebar();
  renderMap();
}

/* Actions. */

function setTripSelected(tripId, isSelected) {
  const wasEmpty = state.selectedIds.length === 0;
  if (isSelected && !state.selectedIds.includes(tripId)) {
    state.selectedIds.push(tripId);
  } else if (!isSelected) {
    state.selectedIds = state.selectedIds.filter((id) => id !== tripId);
  }
  syncColorSlots();
  writeHash();
  renderAll();
  if (wasEmpty && isSelected) {
    fitToTrips([state.tripsById.get(tripId)]);
  }
}

function focusTrip(tripId, {fit = true} = {}) {
  if (video.tripId && video.tripId !== tripId) {
    closeVideo();
  }
  state.focusedId = tripId;
  // On the route map, the focused trip is drawn as part of the selection. The coverage map
  // draws it on its own, so the selection stays as it is.
  if (tripId && state.mapMode === "routes" && !state.selectedIds.includes(tripId)) {
    state.selectedIds.push(tripId);
    syncColorSlots();
  }
  writeHash({pushHistory: true});
  renderAll();
  if (tripId && fit) {
    fitToTrips([state.tripsById.get(tripId)]);
  }
}

// Filter inputs fire many events while typing or dragging, so rendering waits for a frame.
function scheduleFilterRender() {
  writeHash();
  if (isFilterRenderScheduled) {
    return;
  }
  isFilterRenderScheduled = true;
  requestAnimationFrame(() => {
    isFilterRenderScheduled = false;
    renderSidebar();
    renderMap();
  });
}

function updateQueryFilter() {
  state.filters.query = dom.filterQuery.value;
  scheduleFilterRender();
}

// A date at or beyond the data's bounds leaves that side of the filter open.
function setDateFilter(fromDate, toDate) {
  const bounds = state.dateBounds;
  state.filters.from = fromDate && fromDate > bounds.first ? fromDate : "";
  state.filters.to = toDate && toDate < bounds.last ? toDate : "";
  if (state.filters.from && state.filters.to && state.filters.from > state.filters.to) {
    [state.filters.from, state.filters.to] = [state.filters.to, state.filters.from];
  }
  scheduleFilterRender();
}

function onDateSliderInput(event) {
  const bounds = state.dateBounds;
  let fromDay = Number(dom.dateSliderFrom.value);
  let toDay = Number(dom.dateSliderTo.value);
  // The dragged thumb stops at the other one instead of crossing it.
  if (fromDay > toDay) {
    if (event.target === dom.dateSliderFrom) {
      fromDay = toDay;
      dom.dateSliderFrom.value = String(fromDay);
    } else {
      toDay = fromDay;
      dom.dateSliderTo.value = String(toDay);
    }
  }
  setDateFilter(dayToIsoDate(bounds.firstDay + fromDay), dayToIsoDate(bounds.firstDay + toDay));
}

// A preset starts at a fixed day, even before the first trip, so that it stays recognizable.
function applyDatePreset(preset) {
  state.filters.from = preset === "all" ? "" : presetStartDate(preset);
  state.filters.to = "";
  for (const input of [dom.filterFrom, dom.filterTo]) {
    input.removeAttribute("aria-invalid");
  }
  scheduleFilterRender();
}

// The filter changes as soon as a field holds a complete valid date, or is emptied.
function onDateFieldInput() {
  const fieldDate = (input, currentDate) => {
    const text = input.value.trim();
    return text === "" || isValidIsoDate(text) ? text : currentDate;
  };
  const fromDate = fieldDate(dom.filterFrom, state.filters.from);
  const toDate = fieldDate(dom.filterTo, state.filters.to);
  if (fromDate !== state.filters.from || toDate !== state.filters.to) {
    setDateFilter(fromDate, toDate);
  }
}

// When a field is left with an incomplete or impossible date, it is reset and briefly flagged.
function onDateFieldChange(event) {
  const input = event.target;
  const text = input.value.trim();
  if (text === "" || isValidIsoDate(text)) {
    input.removeAttribute("aria-invalid");
    return;
  }
  input.setAttribute("aria-invalid", "true");
  setTimeout(() => input.removeAttribute("aria-invalid"), INVALID_DATE_FLASH_MS);
  const bounds = state.dateBounds;
  input.value = input === dom.filterFrom ? state.filters.from || bounds.first : state.filters.to || bounds.last;
}

/* Video panel. */

function setVideoMessage(...content) {
  dom.videoMessage.hidden = content.length === 0;
  dom.videoMessage.replaceChildren(...content);
}

function describeMediaError(element) {
  const error = element.error;
  if (!error) {
    return null;
  }
  const codeNames = {1: "aborted", 2: "network error", 3: "decoding failed", 4: "format not supported"};
  const codeText = codeNames[error.code] ?? `error ${error.code}`;
  return error.message ? `${codeText}: ${error.message}` : codeText;
}

function reportPlaybackError() {
  const trip = state.tripsById.get(video.tripId);
  const sources = videoSources(trip);
  const otherSource = video.source === "preview" ? "original" : "preview";
  const reason = "This browser cannot play this video.";
  const lines = [];
  if (sources[otherSource]) {
    lines.push(el("p", {}, `${reason} Try the ${otherSource} video.`));
  } else if (video.source === "original") {
    lines.push(
      el(
        "p",
        {},
        `${reason} Make an H.264 preview that every browser can play, then reload the page: `,
        el("code", {}, `dashcam extract --previews --include "${trip.video_filename}"`),
      ),
    );
  } else {
    lines.push(el("p", {}, reason));
  }
  const errorText = video.element ? describeMediaError(video.element) : null;
  if (errorText) {
    lines.push(el("p", {className: "video-error-detail"}, `Browser: ${errorText}`));
  }
  setVideoMessage(...lines);
}

// Moves the marker on every animation frame while the video plays.
function startPlaybackSync() {
  stopPlaybackSync();
  const onAnimationFrame = () => {
    updatePlaybackMarker();
    video.animationFrame = requestAnimationFrame(onAnimationFrame);
  };
  video.animationFrame = requestAnimationFrame(onAnimationFrame);
}

function stopPlaybackSync() {
  if (video.animationFrame !== null) {
    cancelAnimationFrame(video.animationFrame);
    video.animationFrame = null;
  }
}

// `seconds` overrides the video clock, e.g. right after a seek, before the video catches up.
function updatePlaybackMarker(seconds = null) {
  const track = video.tripId ? loadedTracks.get(video.tripId) : null;
  const isTrackDrawn = state.mapMode === "routes" || video.tripId === state.focusedId;
  if (!track || !video.element || !isTrackDrawn) {
    playbackMarker.remove();
    return;
  }
  const position = positionAtVideoTime(track, seconds ?? video.element.currentTime);
  if (!position) {
    playbackMarker.remove();
    return;
  }
  playbackMarker.setLatLng(position);
  if (!map.hasLayer(playbackMarker)) {
    playbackMarker.addTo(map);
  }
  let markerColor = "#101828";
  if (state.mapMode === "routes" && state.colorMode === "trip") {
    markerColor = tripColor(video.tripId);
  }
  playbackMarker.getElement()?.style.setProperty("--marker-color", markerColor);
}

function seekVideo(seconds) {
  const element = video.element;
  if (!element) {
    return;
  }
  element.currentTime = seconds;
  updatePlaybackMarker(seconds);
}

function setVideoSource(source, {startAt = 0, autoplay = false} = {}) {
  const trip = state.tripsById.get(video.tripId);
  const sources = videoSources(trip);
  if (video.element) {
    startAt = video.element.currentTime;
    autoplay = !video.element.paused;
    destroyVideoElement();
  }
  video.source = source;
  setVideoMessage();

  const element = el("video", {controls: true, playsinline: true, preload: "metadata"});
  element.addEventListener("loadedmetadata", () => {
    if (startAt > 0) element.currentTime = startAt;
    if (autoplay) element.play().catch(() => {});
  });
  element.addEventListener("play", startPlaybackSync);
  element.addEventListener("pause", () => {
    stopPlaybackSync();
    updatePlaybackMarker();
  });
  element.addEventListener("seeked", () => updatePlaybackMarker());
  element.addEventListener("error", reportPlaybackError);
  element.src = sources[source];
  video.element = element;
  dom.videoContainer.replaceChildren(element);

  for (const button of dom.videoSource.querySelectorAll("[data-source]")) {
    button.hidden = !sources[button.dataset.source];
    button.setAttribute("aria-pressed", String(button.dataset.source === source));
  }
  dom.videoSource.hidden = Object.keys(sources).length < 2;
}

function openVideo(tripId, {startAt = 0, autoplay = false} = {}) {
  const trip = state.tripsById.get(tripId);
  const sources = videoSources(trip);
  if (!sources.preview && !sources.original) {
    return;
  }
  closeVideo();
  video.tripId = tripId;
  dom.videoTitle.textContent = trip.name;
  dom.videoTitle.title = trip.name;
  dom.videoPanel.hidden = false;
  restoreVideoPanelPosition();
  setVideoSource(sources.preview ? "preview" : "original", {startAt, autoplay});
  loadTrack(tripId)
    .then(() => updatePlaybackMarker(startAt || null))
    .catch(() => {});
  renderDetail();
}

function destroyVideoElement() {
  stopPlaybackSync();
  if (video.element) {
    video.element.pause();
    // Removing the source stops the browser from downloading more of the video.
    video.element.removeAttribute("src");
    video.element.load();
    video.element.remove();
    video.element = null;
  }
}

function closeVideo() {
  if (!video.tripId) {
    return;
  }
  destroyVideoElement();
  video.tripId = null;
  video.source = null;
  dom.videoPanel.hidden = true;
  setVideoMessage();
  playbackMarker.remove();
  renderDetail();
}

/* Moving the video panel. */

// Keeps the panel inside the map area, whatever its size.
function placeVideoPanel(left, top) {
  const areaRect = dom.mapArea.getBoundingClientRect();
  const panelRect = dom.videoPanel.getBoundingClientRect();
  const maxLeft = Math.max(areaRect.width - panelRect.width, 0);
  const maxTop = Math.max(areaRect.height - panelRect.height, 0);
  const clampedLeft = Math.min(Math.max(left, 0), maxLeft);
  const clampedTop = Math.min(Math.max(top, 0), maxTop);
  Object.assign(dom.videoPanel.style, {
    left: `${clampedLeft}px`,
    top: `${clampedTop}px`,
    right: "auto",
    bottom: "auto",
  });
  return {left: clampedLeft, top: clampedTop};
}

function restoreVideoPanelPosition() {
  const position = readStoredJson(VIDEO_PANEL_POSITION_KEY);
  if (position && Number.isFinite(position.left) && Number.isFinite(position.top)) {
    placeVideoPanel(position.left, position.top);
  }
}

function bindVideoPanelDragging() {
  let drag = null;
  dom.videoHeader.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest("button")) {
      return;
    }
    const areaRect = dom.mapArea.getBoundingClientRect();
    const panelRect = dom.videoPanel.getBoundingClientRect();
    drag = {
      pointerId: event.pointerId,
      offsetX: event.clientX - panelRect.left,
      offsetY: event.clientY - panelRect.top,
      areaLeft: areaRect.left,
      areaTop: areaRect.top,
    };
    dom.videoHeader.setPointerCapture(event.pointerId);
    dom.videoPanel.classList.add("dragging");
    event.preventDefault();
  });
  dom.videoHeader.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) {
      return;
    }
    placeVideoPanel(
      event.clientX - drag.areaLeft - drag.offsetX,
      event.clientY - drag.areaTop - drag.offsetY,
    );
  });
  const endDrag = (event) => {
    if (!drag || event.pointerId !== drag.pointerId) {
      return;
    }
    drag = null;
    dom.videoPanel.classList.remove("dragging");
    const areaRect = dom.mapArea.getBoundingClientRect();
    const panelRect = dom.videoPanel.getBoundingClientRect();
    writeStoredJson(VIDEO_PANEL_POSITION_KEY, {
      left: panelRect.left - areaRect.left,
      top: panelRect.top - areaRect.top,
    });
  };
  dom.videoHeader.addEventListener("pointerup", endDrag);
  dom.videoHeader.addEventListener("pointercancel", endDrag);
  dom.videoHeader.addEventListener("lostpointercapture", endDrag);
  window.addEventListener("resize", () => {
    if (!dom.videoPanel.hidden && dom.videoPanel.style.left) {
      const areaRect = dom.mapArea.getBoundingClientRect();
      const panelRect = dom.videoPanel.getBoundingClientRect();
      placeVideoPanel(panelRect.left - areaRect.left, panelRect.top - areaRect.top);
    }
  });
}

/* Wiring. */

function bindControls() {
  dom.filterQuery.addEventListener("input", updateQueryFilter);
  for (const input of [dom.filterFrom, dom.filterTo]) {
    input.addEventListener("input", onDateFieldInput);
    input.addEventListener("change", onDateFieldChange);
  }
  dom.dateSliderFrom.addEventListener("input", onDateSliderInput);
  dom.dateSliderTo.addEventListener("input", onDateSliderInput);
  for (const button of dom.datePresets.querySelectorAll("[data-preset]")) {
    button.addEventListener("click", () => applyDatePreset(button.dataset.preset));
  }
  for (const button of document.querySelectorAll("[data-mode]")) {
    button.addEventListener("click", () => {
      state.mapMode = button.dataset.mode;
      // A trip opened on the coverage map is drawn on the route map too.
      if (state.mapMode === "routes" && state.focusedId && !state.selectedIds.includes(state.focusedId)) {
        state.selectedIds.push(state.focusedId);
        syncColorSlots();
      }
      writeHash();
      renderAll();
    });
  }
  for (const button of document.querySelectorAll("[data-color]")) {
    button.addEventListener("click", () => {
      state.colorMode = button.dataset.color;
      writeHash();
      renderAll();
    });
  }
  dom.heatToggle.addEventListener("change", () => {
    state.showHeat = dom.heatToggle.checked;
    writeHash();
    renderMap();
  });
  // Selects exactly the trips in the list, so that the map draws what the list shows.
  document.getElementById("select-all").addEventListener("click", () => {
    state.selectedIds = filteredTrips().filter(hasGps).map((trip) => trip.id);
    state.mapMode = "routes";
    syncColorSlots();
    writeHash();
    renderAll();
    fitToTrips(state.selectedIds.map((id) => state.tripsById.get(id)));
  });
  document.getElementById("select-none").addEventListener("click", () => {
    state.selectedIds = [];
    syncColorSlots();
    writeHash();
    renderAll();
  });
  document.getElementById("zoom-selection").addEventListener("click", () => {
    const drawnTrips = drawnSelection().map((id) => state.tripsById.get(id));
    fitToTrips(state.mapMode === "routes" && drawnTrips.length ? drawnTrips : coverageTrips());
  });
  for (const button of dom.videoSource.querySelectorAll("[data-source]")) {
    button.addEventListener("click", () => {
      if (button.dataset.source !== video.source) setVideoSource(button.dataset.source);
    });
  }
  document.getElementById("video-close").addEventListener("click", closeVideo);
  bindVideoPanelDragging();
  document.addEventListener("keydown", onHoverShortcut);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && video.tripId) closeVideo();
  });
  window.addEventListener("popstate", () => {
    const previousFocusedId = state.focusedId;
    readHash();
    syncColorSlots();
    if (video.tripId && video.tripId !== state.focusedId) closeVideo();
    renderAll();
    if (state.focusedId && state.focusedId !== previousFocusedId) {
      fitToTrips([state.tripsById.get(state.focusedId)]);
    }
  });
}

function applyIndex(index) {
  state.trips = index.trips.slice().sort((a, b) =>
    (b.start_time ?? b.date ?? "").localeCompare(a.start_time ?? a.date ?? ""),
  );
  state.tripsById = new Map(state.trips.map((trip) => [trip.id, trip]));
  state.libraryDir = index.library_dir ?? "";
  state.canRename = Boolean(index.can_rename);
  state.dateBounds = computeDateBounds(state.trips);
}

async function start() {
  bindControls();
  let index;
  try {
    index = await fetchJson("/api/trips");
  } catch (error) {
    map.setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
    hideMapStatus();
    dom.tripList.replaceChildren();
    dom.summary.replaceChildren(el("p", {}, `Cannot load the trips: ${error.message}`));
    return;
  }
  document.body.classList.remove("is-loading");
  hideMapStatus();
  applyIndex(index);
  readHash();
  syncColorSlots();
  renderDateTicks();
  // The layout may have settled after the map measured its container.
  map.invalidateSize();
  let initialTrips = filteredTrips();
  if (state.focusedId) {
    initialTrips = [state.tripsById.get(state.focusedId)];
  } else if (drawnSelection().length && state.mapMode === "routes") {
    initialTrips = drawnSelection().map((id) => state.tripsById.get(id));
  }
  if (!fitToTrips(initialTrips, {animate: false})) {
    map.setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
  }
  renderAll();
}

start();
