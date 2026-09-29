// Small helpers: building DOM elements, formatting, trip colors, and fetching JSON.

import {GOOD_COVERAGE, ICON_PATHS, LOCATED_STATUSES, MILLISECONDS_PER_DAY, MONTH_NAMES, PARTIAL_COVERAGE, SPEED_BUCKETS, TRIP_COLORS, UNKNOWN_SPEED_COLOR, WEEKDAY_NAMES} from "./constants.js";
import {colorSlots} from "./state.js";

export function withoutEmpty(children) {
  return children.flat().filter((child) => child !== null && child !== undefined && child !== false);
}

export function el(tag, attributes = {}, ...children) {
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

// Rendering replaces the controls of the trip list and the detail panel. A focused control
// marked with `data-focus-key` passes the keyboard focus to the new control with the same key,
// or else to the one named by `data-focus-fallback` (e.g. from a trip in the list to the back
// button of its detail).
export function keepingFocus(render) {
  const focused = document.activeElement?.closest("[data-focus-key]");
  render();
  if (!focused || focused.isConnected) {
    return;
  }
  for (const key of withoutEmpty([focused.dataset.focusKey, focused.dataset.focusFallback])) {
    const target = document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
    if (target && !target.disabled && !target.closest("[hidden]")) {
      target.focus();
      return;
    }
  }
}

export function icon(name, className = "icon") {
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
export function formatDate(isoDate) {
  if (!isoDate) {
    return "No date";
  }
  const [year, month, day] = isoDate.split("-").map(Number);
  const weekday = WEEKDAY_NAMES[new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
  return `${weekday}, ${MONTH_NAMES[month - 1]} ${day}, ${year}`;
}

const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

// How long (milliseconds) a date field stays marked after a rejected entry.
export const INVALID_DATE_FLASH_MS = 1500;

// Accepts only real calendar dates written as `yyyy-mm-dd`.
export function isValidIsoDate(text) {
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
export function presetStartDate(preset) {
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

export function isoDateToDay(isoDate) {
  const [year, month, day] = isoDate.split("-").map(Number);
  return Date.UTC(year, month - 1, day) / MILLISECONDS_PER_DAY;
}

export function dayToIsoDate(dayNumber) {
  return new Date(dayNumber * MILLISECONDS_PER_DAY).toISOString().slice(0, 10);
}

export function formatDuration(seconds) {
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
export function formatClockTime(isoTime, withSeconds = false) {
  if (!isoTime) {
    return "?";
  }
  return isoTime.slice(11, withSeconds ? 19 : 16);
}

export function formatDistance(km) {
  if (km === null || km === undefined) {
    return "-";
  }
  return km >= 100 ? `${Math.round(km)} km` : `${km.toFixed(1)} km`;
}

export function formatSpeed(kmh) {
  return kmh === null || kmh === undefined ? "-" : `${Math.round(kmh)} km/h`;
}

export function formatCoordinates(lat, lon) {
  return `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

export function hasGps(trip) {
  return trip.extraction_status === "ok";
}

export function isTypingTarget(target) {
  return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

export function isLocated(sample) {
  return LOCATED_STATUSES.has(sample.status) && sample.lat !== null && sample.lon !== null;
}

export function tripColor(tripId) {
  const slot = colorSlots.get(tripId) ?? 0;
  return TRIP_COLORS[slot % TRIP_COLORS.length];
}

export function speedColor(kmh) {
  if (kmh === null || kmh === undefined) {
    return UNKNOWN_SPEED_COLOR;
  }
  return SPEED_BUCKETS.find((bucket) => kmh < bucket.maxKmh).color;
}

export function coverageBadge(trip) {
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

export function videoSources(trip) {
  const sources = {};
  if (trip.preview_url) sources.preview = trip.preview_url;
  if (trip.video_url) sources.original = trip.video_url;
  return sources;
}

export function canPlayVideo(trip) {
  return Object.keys(videoSources(trip)).length > 0;
}

// Fetches JSON. Errors carry the `error` message of the response if the server sent one.
export async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.error ?? `${url}: HTTP ${response.status}`);
  }
  return response.json();
}

export function postJson(url, data) {
  return fetchJson(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data),
  });
}

export function readStoredJson(key) {
  try {
    return JSON.parse(localStorage.getItem(key));
  } catch {
    return null;
  }
}

export function writeStoredJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Storage may be unavailable (private windows, blocked site data). The value is a nicety.
  }
}

// Resolves once the browser had a chance to paint, e.g. a status shown before a long draw.
export function nextPaint() {
  return new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
}
