// The trip detail panel, with renaming the trip.

import {DASH, DOT, MAX_FILENAME_LENGTH, STATUS_LABELS, SUGGESTION_LOADING_DELAY_MS} from "./constants.js";
import {dom} from "./elements.js";
import {canPlayVideo, coverageBadge, el, fetchJson, formatClockTime, formatDate, formatDistance, formatDuration, formatSpeed, hasGps, icon, postJson, withoutEmpty} from "./helpers.js";
import {applyIndex, colorSlots, state, syncColorSlots, writeHash} from "./state.js";
import {loadTrack, moveCachedTracks} from "./tracks.js";
import {fitToTrips} from "./map.js";
import {formatLocalities, renderAll, renderDateTicks, renderStreets} from "./sidebar.js";
import {focusTrip} from "./actions.js";
import {closeVideo, openVideo, video} from "./video.js";

// The trip name being edited: the trip, the text typed so far, the suggested name, and the
// state of the request. Null while no name is being edited.
let renameEdit = null;

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
  try {
    await reloadTrips({oldId: trip.id, newId});
  } catch (error) {
    // The trip is renamed on disk, but the page still knows it by its old name.
    dom.summary.replaceChildren(
      el("p", {}, `The trip was renamed, but the trips cannot be reloaded: ${error.message}. Reload the page.`),
    );
    return;
  }
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
  moveCachedTracks(oldId, newId);
  syncColorSlots();
  renderDateTicks();
  writeHash();
  renderAll();
}

export function renderDetail() {
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
