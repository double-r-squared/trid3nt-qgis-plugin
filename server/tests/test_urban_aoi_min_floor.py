"""D2 coverage: the urban-flood AOI minimum-extent floor.

The live SWMM run (case 01KVH4MZ9JF7GGHQ88D5PSWZVH) bounded only a SINGLE
BUILDING because Nominatim returned a too-precise feature bbox and there was no
deterministic floor on the SWMM AOI path. ``_enforce_min_urban_aoi`` is the
minimal guardrail: it EXPANDS (centred) a sub-block AOI to a sensible urban
minimum and is a strict no-op for any reasonably-sized AOI. It never shrinks,
never moves a normal AOI, and degrades honestly (logged).

Pure-function tests — no pyswmm / rasterio / emitter needed.
"""

from __future__ import annotations

import math

from trid3nt_server.workflows.model_urban_flood_swmm import (
    _MIN_URBAN_AOI_SIDE_M,
    _enforce_min_urban_aoi,
)


def _side_lengths_m(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float]:
    min_lon, min_lat, max_lon, max_lat = bbox
    cen_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(cen_lat)), 1e-6)
    return (max_lon - min_lon) * m_per_deg_lon, (max_lat - min_lat) * m_per_deg_lat


def test_collapsed_single_building_bbox_is_expanded_to_floor() -> None:
    """A ~15 m single-building bbox (the live symptom) expands to the floor."""
    # ~15 m square near downtown (lon scaled by cos(lat)); both sides << 300 m.
    cen_lon, cen_lat = -84.388, 33.749  # Atlanta-ish
    half_deg_lat = (7.5) / 111_320.0
    half_deg_lon = (7.5) / (111_320.0 * math.cos(math.radians(cen_lat)))
    tiny = (
        cen_lon - half_deg_lon,
        cen_lat - half_deg_lat,
        cen_lon + half_deg_lon,
        cen_lat + half_deg_lat,
    )
    out = _enforce_min_urban_aoi(tiny)
    w_m, h_m = _side_lengths_m(out)
    assert abs(w_m - _MIN_URBAN_AOI_SIDE_M) < 1.0
    assert abs(h_m - _MIN_URBAN_AOI_SIDE_M) < 1.0
    assert out != tiny  # it was actually expanded


def test_floor_keeps_centroid() -> None:
    """Expansion is centred — the AOI is grown about its centroid, not moved."""
    cen_lon, cen_lat = -84.388, 33.749
    half = 5.0 / 111_320.0
    tiny = (cen_lon - half, cen_lat - half, cen_lon + half, cen_lat + half)
    out = _enforce_min_urban_aoi(tiny)
    out_cen_lon = 0.5 * (out[0] + out[2])
    out_cen_lat = 0.5 * (out[1] + out[3])
    assert math.isclose(out_cen_lon, cen_lon, abs_tol=1e-9)
    assert math.isclose(out_cen_lat, cen_lat, abs_tol=1e-9)


def test_normal_city_block_bbox_is_untouched() -> None:
    """A legitimate ~1 km neighbourhood AOI passes through byte-identical."""
    block = (-84.40, 33.74, -84.39, 33.75)  # ~0.9 km lat x ~0.9 km lon
    w_m, h_m = _side_lengths_m(block)
    assert w_m > _MIN_URBAN_AOI_SIDE_M and h_m > _MIN_URBAN_AOI_SIDE_M  # precondition
    assert _enforce_min_urban_aoi(block) == block  # exact no-op


def test_floor_never_shrinks_a_large_aoi() -> None:
    """A county-scale AOI is returned unchanged (floor only, never a cap)."""
    big = (-85.0, 33.0, -84.0, 34.0)  # ~100 km
    assert _enforce_min_urban_aoi(big) == big


def test_only_one_side_too_small_floors_both_at_least_to_min() -> None:
    """A skinny AOI (one side below the floor) brings the short side up to the
    floor without shrinking the long side below it."""
    # Long in lat (~600 m), short in lon (~20 m).
    cen_lon, cen_lat = -84.388, 33.749
    half_lon = 10.0 / (111_320.0 * math.cos(math.radians(cen_lat)))
    half_lat = 300.0 / 111_320.0
    skinny = (
        cen_lon - half_lon,
        cen_lat - half_lat,
        cen_lon + half_lon,
        cen_lat + half_lat,
    )
    out = _enforce_min_urban_aoi(skinny)
    w_m, h_m = _side_lengths_m(out)
    assert w_m >= _MIN_URBAN_AOI_SIDE_M - 1.0
    assert h_m >= _MIN_URBAN_AOI_SIDE_M - 1.0
