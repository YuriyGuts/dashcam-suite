// Actions that change the selection, the focus, and the filters.

import {dom} from "./elements.js";
import {INVALID_DATE_FLASH_MS, dayToIsoDate, isValidIsoDate, presetStartDate} from "./helpers.js";
import {state, syncColorSlots, writeHash} from "./state.js";
import {fitToTrips, renderMap} from "./map.js";
import {renderAll, renderSidebar} from "./sidebar.js";
import {closeVideo, video} from "./video.js";

// Set while a filter change waits for the next animation frame to be rendered.
let isFilterRenderScheduled = false;

export function setTripSelected(tripId, isSelected) {
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

export function focusTrip(tripId, {fit = true} = {}) {
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

export function updateQueryFilter() {
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

export function onDateSliderInput(event) {
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
export function applyDatePreset(preset) {
  state.filters.from = preset === "all" ? "" : presetStartDate(preset);
  state.filters.to = "";
  for (const input of [dom.filterFrom, dom.filterTo]) {
    input.removeAttribute("aria-invalid");
  }
  scheduleFilterRender();
}

// The filter changes as soon as a field holds a complete valid date, or is emptied.
export function onDateFieldInput() {
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
export function onDateFieldChange(event) {
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
