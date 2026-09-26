/*
 * Dashcam trip visualizer.
 *
 * Data comes from the `dashcam serve` API: `/api/trips` (the trip index), `/api/geometry`
 * (simplified routes for the coverage mode), and `/tracks/<id>.json` (full tracks, loaded when a
 * trip is drawn). Filters, the selection, and the map mode live in the URL hash.
 */
"use strict";

// Categorical trip colors, assigned in this order to selected trips. Trips selected beyond
// these slots share the neutral color.
const TRIP_COLORS = [
  "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948",
];
const OTHER_TRIP_COLOR = "#898781";

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

// Heatmap: one hue (orange, to stand apart from the blue coverage lines), light to dark.
const HEAT_GRADIENT = {0.2: "#f7c8b0", 0.5: "#eb6834", 0.8: "#b8461c", 1.0: "#7a2c0f"};

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

// Where the position of the video panel is remembered between visits.
const VIDEO_PANEL_POSITION_KEY = "dashcam.videoPanelPosition";

const MILLISECONDS_PER_DAY = 86400000;

// Indexed by `Date.getUTCDay()`, which starts on Sunday.
const WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Separators in text.
const DOT = " \u00b7 ";
const DASH = "\u2013";

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
};

const state = {
  trips: [],
  tripsById: new Map(),
  videoDir: "",
  // First and last trip dates, as ISO dates and as day numbers. Null without dated trips.
  dateBounds: null,
  // Empty `from` or `to` means the filter is open on that side.
  filters: {from: "", to: "", name: "", street: ""},
  selectedIds: [],
  focusedId: null,
  colorMode: "trip",
  mapMode: "routes",
  showHeat: false,
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
  const slot = colorSlots.get(tripId);
  return slot === undefined || slot >= TRIP_COLORS.length ? OTHER_TRIP_COLOR : TRIP_COLORS[slot];
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

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${url}: HTTP ${response.status}`);
  }
  return response.json();
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

/* URL hash state. */

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  state.filters.from = params.get("from") || "";
  state.filters.to = params.get("to") || "";
  state.filters.name = params.get("q") || "";
  state.filters.street = params.get("street") || "";
  state.selectedIds = [...new Set(params.getAll("sel"))].filter((id) => state.tripsById.has(id));
  const focusedId = params.get("trip");
  state.focusedId = state.tripsById.has(focusedId) ? focusedId : null;
  state.colorMode = params.get("color") === "speed" ? "speed" : "trip";
  state.mapMode = params.get("mode") === "coverage" ? "coverage" : "routes";
  state.showHeat = params.get("heat") === "1";
}

function writeHash({pushHistory = false} = {}) {
  const params = new URLSearchParams();
  const {from, to, name, street} = state.filters;
  if (from) params.set("from", from);
  if (to) params.set("to", to);
  if (name) params.set("q", name);
  if (street) params.set("street", street);
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

function filteredTrips() {
  const {from, to} = state.filters;
  const name = state.filters.name.trim().toLowerCase();
  const street = state.filters.street.trim().toLowerCase();
  return state.trips.filter((trip) => {
    if ((from || to) && !trip.date) return false;
    if (from && trip.date < from) return false;
    if (to && trip.date > to) return false;
    if (name && !trip.id.toLowerCase().includes(name)) return false;
    if (street && !trip.streets.some((streetName) => streetName.toLowerCase().includes(street))) {
      return false;
    }
    return true;
  });
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
  return {trip, samples, runs, gaps, streets: data.streets || []};
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

// The route point under the mouse, which the copy shortcut acts on.
let hoveredPoint = null;

// How long (milliseconds) the tooltip confirms a copied point.
const COPIED_NOTICE_MS = 1500;

// Plain text, one labeled field per line, for pasting into notes or messages.
function pointDetailsText(track, sample) {
  const dateText = sample.time ? formatDate(sample.time.slice(0, 10)) : formatDate(track.trip.date);
  const timeText = sample.time
    ? `${formatClockTime(sample.time, true)}${sample.time_estimated ? " (estimated)" : ""}`
    : "unknown";
  const speedText = sample.kmh === null || sample.kmh === undefined ? "unknown" : formatSpeed(sample.kmh);
  return [
    `Trip name: ${track.trip.name}`,
    `Date: ${dateText}`,
    `Time: ${timeText}`,
    `Video position: ${formatDuration(sample.t)}`,
    `Speed: ${speedText}`,
    `Coordinates: ${formatCoordinates(sample.lat, sample.lon)}`,
  ].join("\n");
}

function renderHoverTooltip() {
  const {track, sample, copiedAt} = hoveredPoint;
  const timeText = sample.time
    ? `${formatClockTime(sample.time, true)}${sample.time_estimated ? " (estimated)" : ""}`
    : "Time unknown";
  const isCopiedNoticeShown = copiedAt !== null && performance.now() - copiedAt < COPIED_NOTICE_MS;
  const actions = canPlayVideo(track.trip) ? "Click to play from here, C to copy" : "C to copy";
  hoverTooltip.setContent(
    el(
      "div",
      {},
      el("strong", {}, track.trip.name),
      el("span", {}, [timeText, `video ${formatDuration(sample.t)}`, formatSpeed(sample.kmh)].join(DOT)),
      el("span", {}, formatCoordinates(sample.lat, sample.lon)),
      el("span", {className: "tooltip-hint"}, isCopiedNoticeShown ? "Copied to clipboard" : actions),
    ),
  );
}

function showHover(track, sampleIndexes, latlng) {
  const index = nearestSampleIndex(track, sampleIndexes, latlng);
  const sample = track.samples[index];
  const isSamePoint = hoveredPoint && hoveredPoint.track === track && hoveredPoint.sample === sample;
  hoveredPoint = {track, sample, copiedAt: isSamePoint ? hoveredPoint.copiedAt : null};
  const position = sampleLatLng(sample);
  hoverMarker.setLatLng(position).addTo(map);
  hoverTooltip.setLatLng(position);
  renderHoverTooltip();
  if (!map.hasLayer(hoverTooltip)) {
    hoverTooltip.addTo(map);
  }
}

function hideHover() {
  hoveredPoint = null;
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

// C copies the hovered point. Cmd+C and Ctrl+C do too, unless text on the page is selected.
async function onCopyShortcut(event) {
  if (!hoveredPoint || event.key.toLowerCase() !== "c" || event.altKey || event.shiftKey) {
    return;
  }
  if (isTypingTarget(event.target)) {
    return;
  }
  const hasModifier = event.metaKey || event.ctrlKey;
  if (hasModifier && !window.getSelection().isCollapsed) {
    return;
  }
  event.preventDefault();
  const point = hoveredPoint;
  if (!(await copyToClipboard(pointDetailsText(point.track, point.sample)))) {
    return;
  }
  point.copiedAt = performance.now();
  if (hoveredPoint === point) {
    renderHoverTooltip();
    setTimeout(() => {
      if (hoveredPoint === point) renderHoverTooltip();
    }, COPIED_NOTICE_MS);
  }
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

function bindRouteEvents(line, track, sampleIndexes) {
  line.on("mousemove", (event) => showHover(track, sampleIndexes, event.latlng));
  line.on("mouseout", hideHover);
  line.on("click", (event) => onRouteClick(track, sampleIndexes, event.latlng));
}

function addRouteLine(layer, track, sampleIndexes, color) {
  const latlngs = sampleIndexes.map((index) => sampleLatLng(track.samples[index]));
  const line = L.polyline(latlngs, {renderer: routeRenderer, color, weight: ROUTE_WEIGHT, opacity: 1});
  bindRouteEvents(line, track, sampleIndexes);
  line.addTo(layer);
}

function drawTrack(layer, track) {
  const color = tripColor(track.trip.id);
  const latlngsOf = (indexes) => indexes.map((index) => sampleLatLng(track.samples[index]));

  for (const [fromIndex, toIndex] of track.gaps) {
    L.polyline(latlngsOf([fromIndex, toIndex]), {
      renderer: routeRenderer,
      color: state.colorMode === "speed" ? UNKNOWN_SPEED_COLOR : color,
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
    L.polyline(latlngsOf(run), {
      renderer: routeRenderer,
      color: HALO_COLOR,
      weight: ROUTE_HALO_WEIGHT,
      opacity: 0.8,
      interactive: false,
    }).addTo(layer);

    if (state.colorMode === "trip") {
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

async function drawRoutes(generation) {
  const tripIds = state.selectedIds.filter((id) => hasGps(state.tripsById.get(id)));
  // The focused trip is drawn last, on top of the others.
  tripIds.sort((a, b) => (a === state.focusedId) - (b === state.focusedId));
  const results = await Promise.allSettled(tripIds.map(loadTrack));
  if (generation !== drawGeneration) {
    return;
  }
  const layer = L.layerGroup();
  results.forEach((result, index) => {
    if (result.status === "fulfilled") {
      drawTrack(layer, result.value);
    } else {
      console.error(`Cannot load the track of '${tripIds[index]}':`, result.reason);
    }
  });
  routesLayer.remove();
  routesLayer = layer.addTo(map);
  updatePlaybackMarker();
}

async function drawCoverage(generation) {
  const geometry = await loadGeometry();
  if (generation !== drawGeneration) {
    return;
  }
  const layer = L.layerGroup();
  const heatPoints = [];
  const tripsWithRoutes = filteredTrips().filter((trip) => geometry[trip.id]);
  // All halos go first, so that no halo covers another trip's line.
  for (const trip of tripsWithRoutes) {
    L.polyline(geometry[trip.id], {
      renderer: routeRenderer,
      color: HALO_COLOR,
      weight: COVERAGE_HALO_WEIGHT,
      opacity: 0.9,
      interactive: false,
    }).addTo(layer);
  }
  const dotsLayer = L.layerGroup();
  for (const trip of tripsWithRoutes) {
    const runs = geometry[trip.id];
    const tooltipContent = () =>
      el(
        "div",
        {},
        el("strong", {}, trip.name),
        el("span", {}, formatDate(trip.date)),
        el("span", {className: "tooltip-hint"}, "Click to open"),
      );
    const line = L.polyline(runs, {
      renderer: routeRenderer,
      color: COVERAGE_COLOR,
      weight: COVERAGE_WEIGHT,
      opacity: COVERAGE_OPACITY,
    });
    line.bindTooltip(tooltipContent(), {sticky: true, className: "map-tooltip"});
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
    dot.bindTooltip(tooltipContent(), {direction: "top", offset: [0, -6], className: "map-tooltip"});
    dot.on("click", () => focusTrip(trip.id));
    dot.addTo(dotsLayer);
    if (state.showHeat) {
      for (const run of runs) {
        heatPoints.push(...run);
      }
    }
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
  if (state.showHeat && heatPoints.length) {
    heatLayer = L.heatLayer(heatPoints, {
      radius: 14,
      blur: 16,
      max: 0.6,
      minOpacity: 0.45,
      gradient: HEAT_GRADIENT,
    }).addTo(map);
  }
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
    playbackMarker.remove();
    speedLegend.remove();
    drawCoverage(generation).catch((error) => console.error("Cannot load the coverage:", error));
    return;
  }
  coverageLayer.remove();
  coverageDotsLayer.remove();
  if (heatLayer) {
    heatLayer.remove();
    heatLayer = null;
  }
  if (state.colorMode === "speed") {
    speedLegend.addTo(map);
  } else {
    speedLegend.remove();
  }
  drawRoutes(generation);
}

/* Sidebar. */

const dom = {
  librarySummary: document.getElementById("library-summary"),
  filterFrom: document.getElementById("filter-from"),
  filterTo: document.getElementById("filter-to"),
  filterName: document.getElementById("filter-name"),
  filterStreet: document.getElementById("filter-street"),
  streetFilter: document.getElementById("street-filter"),
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
  videoPanel: document.getElementById("video-panel"),
  videoHeader: document.querySelector("#video-panel .video-header"),
  videoTitle: document.getElementById("video-title"),
  videoSource: document.getElementById("video-source"),
  videoContainer: document.getElementById("video-container"),
  videoMessage: document.getElementById("video-message"),
};

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
  if (dom.filterName.value !== state.filters.name) dom.filterName.value = state.filters.name;
  if (dom.filterStreet.value !== state.filters.street) dom.filterStreet.value = state.filters.street;
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
  const selectedTrips = state.selectedIds.map((id) => state.tripsById.get(id));
  const useSelection = state.mapMode === "routes" && selectedTrips.length > 0;
  const trips = useSelection ? selectedTrips : visibleWithGps;
  const stats = aggregateStats(trips);
  const title = useSelection
    ? `${stats.count} of ${visibleWithGps.length} trips selected`
    : `${stats.count} ${stats.count === 1 ? "trip" : "trips"} shown`;

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
    el("div", {className: "summary-title"}, title),
    el("div", {className: "stat-grid"}, statTiles),
  ];
  if (!state.trips.length) {
    children.push(
      el("p", {className: "hint"}, "No trips yet. Run ", el("code", {}, "dashcam extract"), " in the video directory."),
    );
  } else if (!visibleTrips.length) {
    children.push(el("p", {className: "hint"}, "No trips match the filters."));
  } else if (state.mapMode === "routes" && !selectedTrips.length) {
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
  return el(
    "li",
    {className: `trip${trip.id === state.focusedId ? " focused" : ""}`, "data-id": trip.id},
    checkbox,
    body,
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
  dom.streetFilter.hidden = allStreetNames.length === 0;
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
        el("code", {}, state.videoDir || "the video directory"),
        ".",
      );
  const streetsContainer = el("div", {});
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
        el("h2", {}, trip.name),
        el("p", {className: "detail-subtitle"}, [formatDate(trip.date), timeRange].join(DOT)),
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
  if (tripId) {
    state.mapMode = "routes";
    if (!state.selectedIds.includes(tripId)) {
      state.selectedIds.push(tripId);
      syncColorSlots();
    }
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
    if (state.mapMode === "coverage") {
      renderMap();
    }
  });
}

function updateTextFilters() {
  state.filters.name = dom.filterName.value;
  state.filters.street = dom.filterStreet.value;
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
  if (!track || !video.element || state.mapMode !== "routes") {
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
  const markerColor = state.colorMode === "trip" ? tripColor(video.tripId) : "#101828";
  playbackMarker.getElement()?.style.setProperty("--marker-color", markerColor);
  if (!video.element.paused && !map.getBounds().pad(-0.1).contains(position)) {
    map.panTo(position);
  }
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
  dom.filterName.addEventListener("input", updateTextFilters);
  dom.filterStreet.addEventListener("input", updateTextFilters);
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
  document.getElementById("select-all").addEventListener("click", () => {
    const visibleIds = filteredTrips().filter(hasGps).map((trip) => trip.id);
    state.selectedIds = [...new Set([...state.selectedIds, ...visibleIds])];
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
    const selectedTrips = state.selectedIds.map((id) => state.tripsById.get(id));
    fitToTrips(selectedTrips.length ? selectedTrips : filteredTrips());
  });
  for (const button of dom.videoSource.querySelectorAll("[data-source]")) {
    button.addEventListener("click", () => {
      if (button.dataset.source !== video.source) setVideoSource(button.dataset.source);
    });
  }
  document.getElementById("video-close").addEventListener("click", closeVideo);
  bindVideoPanelDragging();
  document.addEventListener("keydown", onCopyShortcut);
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

async function start() {
  bindControls();
  let index;
  try {
    index = await fetchJson("/api/trips");
  } catch (error) {
    map.setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
    dom.summary.replaceChildren(el("p", {}, `Cannot load the trips: ${error.message}`));
    return;
  }
  state.trips = index.trips.slice().sort((a, b) =>
    (b.start_time ?? b.date ?? "").localeCompare(a.start_time ?? a.date ?? ""),
  );
  state.tripsById = new Map(state.trips.map((trip) => [trip.id, trip]));
  state.videoDir = index.video_dir ?? "";
  state.dateBounds = computeDateBounds(state.trips);
  readHash();
  syncColorSlots();
  renderDateTicks();
  // The layout may have settled after the map measured its container.
  map.invalidateSize();
  let initialTrips = filteredTrips();
  if (state.focusedId) {
    initialTrips = [state.tripsById.get(state.focusedId)];
  } else if (state.selectedIds.length && state.mapMode === "routes") {
    initialTrips = state.selectedIds.map((id) => state.tripsById.get(id));
  }
  if (!fitToTrips(initialTrips, {animate: false})) {
    map.setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
  }
  renderAll();
}

start();
