// Application state, the URL hash that stores it, filtering, and aggregates.

import {isoDateToDay} from "./helpers.js";

export const state = {
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

// Color slot of each selected trip and of the focused trip. A trip keeps its color while it
// stays selected or focused.
export const colorSlots = new Map();

export function readHash() {
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

export function writeHash({pushHistory = false} = {}) {
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

export function syncColorSlots() {
  const coloredIds = withFocused(state.selectedIds);
  const colored = new Set(coloredIds);
  for (const id of [...colorSlots.keys()]) {
    if (!colored.has(id)) {
      colorSlots.delete(id);
    }
  }
  const usedSlots = new Set(colorSlots.values());
  let nextSlot = 0;
  for (const id of coloredIds) {
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

// A query holds one or more comma-separated terms, e.g. "Khreshchatyk, Lesi Ukrainky". A trip matches if each
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

export function filteredTrips() {
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
export function drawnSelection(visibleTrips = filteredTrips()) {
  const visibleIds = new Set(visibleTrips.map((trip) => trip.id));
  return state.selectedIds.filter((id) => visibleIds.has(id) || id === state.focusedId);
}

// Trips drawn on the route map: the drawn selection and the focused trip, which is drawn without
// being selected.
export function drawnTripIds() {
  return withFocused(drawnSelection());
}

function withFocused(tripIds) {
  const {focusedId} = state;
  return focusedId && !tripIds.includes(focusedId) ? [...tripIds, focusedId] : tripIds;
}

export function aggregateStats(trips) {
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

export function applyIndex(index) {
  setTrips(index.trips);
  state.libraryDir = index.library_dir ?? "";
  state.canRename = Boolean(index.can_rename);
}

// Puts the index entry of a renamed trip in place of its entry under the old ID.
export function replaceTrip(oldId, trip) {
  setTrips([...state.trips.filter((other) => other.id !== oldId), trip]);
}

function setTrips(trips) {
  state.trips = trips.slice().sort((a, b) =>
    (b.start_time ?? b.date ?? "").localeCompare(a.start_time ?? a.date ?? ""),
  );
  state.tripsById = new Map(state.trips.map((trip) => [trip.id, trip]));
  state.dateBounds = computeDateBounds(state.trips);
}
