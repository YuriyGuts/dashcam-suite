"""Geographic helpers."""

import math

# Mean Earth radius in meters.
EARTH_RADIUS_M = 6_371_000

# Meters per degree of latitude (and of longitude at the equator).
METERS_PER_DEGREE = EARTH_RADIUS_M * math.pi / 180

# Largest possible absolute latitude and longitude, in degrees.
MAX_ABS_LAT = 90
MAX_ABS_LON = 180


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two points in meters."""
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = lat2_rad - lat1_rad
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, a)))


def implied_speed_kmh(distance_m: float, duration_s: float) -> float:
    """Return the speed needed to cover the distance in the given time, in km/h."""
    if duration_s <= 0:
        return math.inf if distance_m > 0 else 0.0
    return distance_m / duration_s * 3.6
