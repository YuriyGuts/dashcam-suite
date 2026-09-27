// The sidebar: filters, the trip list, and the selection summary.

import {ARROW, DASH, DOT} from "./constants.js";
import {dom} from "./elements.js";
import {coverageBadge, el, formatClockTime, formatDate, formatDistance, formatDuration, formatSpeed, hasGps, icon, isoDateToDay, presetStartDate, tripColor, withoutEmpty} from "./helpers.js";
import {aggregateStats, drawnSelection, filteredTrips, state} from "./state.js";
import {map, renderMap, showHiddenTrips} from "./map.js";
import {renderDetail} from "./detail.js";
import {focusTrip, setTripSelected} from "./actions.js";

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

export function renderDateTicks() {
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

export function renderStreets(container, track) {
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

// Start and end localities, e.g. "Kyiv -> Brovary", or one name for a trip within a locality.
export function formatLocalities(trip) {
  const {start_locality: start, end_locality: end} = trip;
  if (!start && !end) return null;
  if (start === end) return start;
  return `${start ?? "?"}${ARROW}${end ?? "?"}`;
}

export function renderSidebar() {
  const visibleTrips = filteredTrips();
  renderLibrarySummary();
  renderControls();
  renderSummary(visibleTrips);
  renderTripList(visibleTrips);
  renderDetail();
}

export function renderAll() {
  renderSidebar();
  renderMap();
}
