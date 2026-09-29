// Loading and caching full tracks and simplified routes, and positions along a track.

import {MAX_INTERPOLATION_STEP_S, MAX_ROUTE_STEP_S} from "./constants.js";
import {fetchJson, isLocated} from "./helpers.js";
import {state} from "./state.js";

// Prepared tracks by trip ID: promises while loading, and the loaded values for synchronous use.
const trackPromises = new Map();
export const loadedTracks = new Map();
let geometryPromise = null;

// Whether consecutive samples are further apart in time than a route may join. A sample without
// a time does not break the route.
function isGapBetween(previous, current) {
  if (!previous?.time || !current?.time) {
    return false;
  }
  const stepS = (Date.parse(current.time) - Date.parse(previous.time)) / 1000;
  return !(stepS >= 0 && stepS <= MAX_ROUTE_STEP_S);
}

function prepareTrack(trip, data) {
  const samples = data.samples || [];
  const runs = [];
  let run = [];
  samples.forEach((sample, index) => {
    if (run.length && (!isLocated(sample) || isGapBetween(samples[index - 1], sample))) {
      runs.push(run);
      run = [];
    }
    if (isLocated(sample)) {
      run.push(index);
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

export function loadTrack(tripId) {
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

export function loadGeometry() {
  if (!geometryPromise) {
    geometryPromise = fetchJson("/api/geometry").then((data) => data.trips);
    geometryPromise.catch(() => {
      geometryPromise = null;
    });
  }
  return geometryPromise;
}

// After a rename: moves the cached track and route to the new trip ID, and points the track at
// the renamed trip.
export function moveCachedTracks(oldId, newId) {
  for (const tracks of [trackPromises, loadedTracks]) {
    if (tracks.has(oldId)) {
      tracks.set(newId, tracks.get(oldId));
      tracks.delete(oldId);
    }
  }
  const track = loadedTracks.get(newId);
  if (track) {
    track.trip = state.tripsById.get(newId);
  }
  geometryPromise
    ?.then((routes) => {
      if (oldId in routes) {
        routes[newId] = routes[oldId];
        delete routes[oldId];
      }
    })
    .catch(() => {});
}

export function sampleLatLng(sample) {
  return [sample.lat, sample.lon];
}

export function nearestSampleIndex(track, sampleIndexes, latlng) {
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
export function positionAtVideoTime(track, seconds) {
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
    nextSample.t - sample.t <= MAX_INTERPOLATION_STEP_S &&
    !isGapBetween(sample, nextSample);
  if (!canInterpolate) {
    return sampleLatLng(sample);
  }
  const fraction = (seconds - sample.t) / (nextSample.t - sample.t);
  return [
    sample.lat + (nextSample.lat - sample.lat) * fraction,
    sample.lon + (nextSample.lon - sample.lon) * fraction,
  ];
}
