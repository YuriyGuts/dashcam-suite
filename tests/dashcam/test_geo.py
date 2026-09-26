import math

import pytest

from dashcam import geo


def test_haversine_m_for_one_degree_of_latitude():
    # GIVEN two points one degree of latitude apart

    # WHEN measuring the distance
    distance_m = geo.haversine_m(49.0, 24.0, 50.0, 24.0)

    # THEN it is about 111 km
    assert distance_m == pytest.approx(111_195, rel=1e-3)


def test_haversine_m_for_same_point():
    # GIVEN the same point twice

    # WHEN measuring the distance
    distance_m = geo.haversine_m(49.8, 24.0, 49.8, 24.0)

    # THEN it is zero
    assert distance_m == 0


def test_implied_speed_kmh():
    # GIVEN 30 meters covered in one second

    # WHEN computing the speed
    speed_kmh = geo.implied_speed_kmh(30, 1)

    # THEN it is 108 km/h
    assert speed_kmh == pytest.approx(108)


def test_implied_speed_kmh_without_elapsed_time():
    # GIVEN a distance covered in no time

    # WHEN computing the speed
    # THEN it is infinite when moving and zero when standing still
    assert geo.implied_speed_kmh(10, 0) == math.inf
    assert geo.implied_speed_kmh(0, 0) == 0
