/*
 * Dashcam trip visualizer.
 *
 * Data comes from the `dashcam serve` API: `/api/trips` (the trip index), `/api/geometry`
 * (simplified routes for the coverage mode), and `/tracks/<id>.json` (full tracks, loaded when a
 * trip is drawn). Filters, the selection, and the map mode live in the URL hash.
 *
 * This entry module wires the controls to the other modules and starts the app.
 */

import {dom} from "./elements.js";
import {el, fetchJson, hasGps} from "./helpers.js";
import {applyIndex, drawnSelection, filteredTrips, readHash, state, syncColorSlots, writeHash} from "./state.js";
import {DEFAULT_VIEW, coverageTrips, fitToTrips, hideMapStatus, map, onHoverShortcut, renderMap} from "./map.js";
import {renderAll, renderDateTicks} from "./sidebar.js";
import {applyDatePreset, onDateFieldChange, onDateFieldInput, onDateSliderInput, updateQueryFilter} from "./actions.js";
import {bindVideoPanelDragging, closeVideo, setVideoSource, video} from "./video.js";

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
