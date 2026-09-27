// The video panel: playback synchronized with the map, and moving the panel.

import {VIDEO_PANEL_POSITION_KEY} from "./constants.js";
import {dom} from "./elements.js";
import {el, readStoredJson, tripColor, videoSources, writeStoredJson} from "./helpers.js";
import {state} from "./state.js";
import {loadTrack, loadedTracks, positionAtVideoTime} from "./tracks.js";
import {map, playbackMarker} from "./map.js";
import {renderDetail} from "./detail.js";

export const video = {
  tripId: null,
  source: null,
  element: null,
  animationFrame: null,
};

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

// Escapes the characters that `--include` would read as a glob pattern.
function globEscape(text) {
  return text.replace(/[[*?]/g, "[$&]");
}

// Quotes a command-line argument. Double quotes work in every common shell, unless the text has
// characters that POSIX shells expand inside them. Such text gets POSIX single quotes.
function shellQuote(text) {
  if (!/["$`\\!]/.test(text)) {
    return `"${text}"`;
  }
  return `'${text.replaceAll("'", "'\\''")}'`;
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
        el("code", {}, `dashcam extract --previews --include ${shellQuote(globEscape(trip.video_filename))}`),
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
export function updatePlaybackMarker(seconds = null) {
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

export function seekVideo(seconds) {
  const element = video.element;
  if (!element) {
    return;
  }
  element.currentTime = seconds;
  updatePlaybackMarker(seconds);
}

export function setVideoSource(source, {startAt = 0, autoplay = false} = {}) {
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

export function openVideo(tripId, {startAt = 0, autoplay = false} = {}) {
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

export function closeVideo() {
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

export function bindVideoPanelDragging() {
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
