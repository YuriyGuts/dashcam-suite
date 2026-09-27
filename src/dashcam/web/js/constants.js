// Constants shared by the modules: colors, thresholds, labels, and icons.

// Categorical trip colors, assigned in this order to selected trips. More trips reuse them.
export const TRIP_COLORS = [
  "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4fa33a",
  "#7c6fe0", "#e34948", "#0e9aa7", "#b0764f", "#c366d6", "#7a8b1f",
];

// Sequential speed scale (one hue, light to dark). Samples without a speed are gray.
export const SPEED_BUCKETS = [
  {maxKmh: 20, color: "#86b6ef", label: "< 20"},
  {maxKmh: 40, color: "#5598e7", label: "20-40"},
  {maxKmh: 60, color: "#2a78d6", label: "40-60"},
  {maxKmh: 90, color: "#1c5cab", label: "60-90"},
  {maxKmh: 120, color: "#104281", label: "90-120"},
  {maxKmh: Infinity, color: "#0d366b", label: "120+"},
];
export const UNKNOWN_SPEED_COLOR = "#898781";

// Line styles. Every route and coverage line sits on a white halo, so that it stands out from
// the muted basemap at any zoom.
export const COVERAGE_COLOR = "#2a78d6";
export const HALO_COLOR = "#ffffff";
export const ROUTE_WEIGHT = 5;
export const ROUTE_HALO_WEIGHT = 9;
export const GAP_WEIGHT = 3;
export const COVERAGE_WEIGHT = 4;
export const COVERAGE_HALO_WEIGHT = 7;
export const COVERAGE_OPACITY = 0.8;

// In coverage mode, at this zoom and below, each trip also gets a dot, so that short trips stay
// visible when the map shows a whole city or region.
export const COVERAGE_DOTS_MAX_ZOOM = 12;

// Heatmap: one hue (orange, to stand apart from the blue coverage lines), light to dark. It
// shows how many trips passed through each cell of a ground grid, on a log scale.
export const HEAT_GRADIENT = {0.15: "#fed7aa", 0.45: "#fb923c", 0.75: "#ea580c", 1.0: "#c2410c"};
export const HEAT_CELL_M = 10;
export const HEAT_RADIUS_PX = 5;
export const HEAT_BLUR_PX = 4;
export const HEAT_MIN_OPACITY = 0.02;
// The heat of the least and the most visited cells, between 0 and 1.
export const HEAT_MIN_LEVEL = 0.25;
// About how many point circles overlap on a line. The plugin adds up their opacities, so each
// point gets a lower opacity to reach the intended level.
export const HEAT_OVERLAP = 4;
export const METERS_PER_DEGREE_LAT = 111320;
// Width of the world at the equator in pixels at zoom 0, as Leaflet draws it.
export const WORLD_WIDTH_PX = 256;
export const EARTH_CIRCUMFERENCE_M = 40075016.686;

// Sample statuses with coordinates.
export const LOCATED_STATUSES = new Set(["ok", "interpolated"]);

// Sample statuses as shown in the GPS coverage breakdown.
export const STATUS_LABELS = {
  ok: "good",
  interpolated: "interpolated",
  no_fix: "no fix",
  spoofed: "spoofed",
  unreadable: "unreadable",
};

// The playback marker moves smoothly between samples at most this far apart (seconds).
export const MAX_INTERPOLATION_STEP_S = 3;

// GPS coverage badge thresholds.
export const GOOD_COVERAGE = 0.95;
export const PARTIAL_COVERAGE = 0.7;

// A draw of at least this many trips shows its status on the map at once. Smaller draws show it
// only if they take longer than the delay (milliseconds).
export const LARGE_DRAW_TRIP_COUNT = 30;
export const MAP_STATUS_DELAY_MS = 200;

// The rename suggestion shows that it is loading only after this delay (milliseconds).
export const SUGGESTION_LOADING_DELAY_MS = 300;

// Longest trip video filename, including the extension (see `dashcam rename`).
export const MAX_FILENAME_LENGTH = 140;

// Where the position of the video panel is remembered between visits.
export const VIDEO_PANEL_POSITION_KEY = "dashcam.videoPanelPosition";

export const MILLISECONDS_PER_DAY = 86400000;

// Indexed by `Date.getUTCDay()`, which starts on Sunday.
export const WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Separators in text.
export const DOT = " \u00b7 ";
export const DASH = "\u2013";
export const ARROW = " \u2192 ";

// Stroke icons on a 24 x 24 grid, drawn with the current text color.
export const ICON_PATHS = {
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
