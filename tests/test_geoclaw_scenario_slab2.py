"""Offline tests for the ADR 0230 Slab2 SCENARIO source (the scenario rung).

All offline (no ScienceBase / MinIO / clawpack):
  * ``parse_slab2_grids`` -- PURE parse of Slab2-layout ``.grd`` grids (0-360 lon
    normalized, NaN edges preserved, depth/strike/dip on a shared mesh).
  * ``interface_lon_at`` -- the trench trace migrates with latitude (the curvature
    that makes the tiled fault follow the trench, not a straight bar).
  * ``strasser_interface_dimensions`` / moment budget -- the cited scaling laws.
  * ``resolve_slab2_scenario`` -- tiling geometry sanity: >1 subfault, strikes
    Slab2-sampled (vary along the curve), moment SUMS to the target Mw, footprint
    on the modeled interface, LOUD scenario provenance.
  * ``fetch_slab2_grids`` -- the ScienceBase I/O boundary with ``_http_get``
    monkeypatched (children API -> grid downloads -> parse) + the cache short-circuit.
  * the composer ladder LABEL -- ``basis="scenario_slab2"`` renders as a LOUD
    SCENARIO provenance line, never mistaken for a real event.
"""

from __future__ import annotations

import json
import math
import os

import numpy as np
import pytest

from trid3nt_server.agent.workflows.geoclaw import scenario_slab2 as s2
from trid3nt_server.agent.workflows.geoclaw.scenario_slab2 import (
    RIGIDITY_PA,
    ScenarioSlab2Error,
    fetch_slab2_grids,
    moment_to_mw,
    parse_slab2_grids,
    resolve_slab2_scenario,
    strasser_interface_dimensions,
)


# --------------------------------------------------------------------------- #
# Fixture: a small Slab2-layout Cascadia interface (real curved geometry).
# --------------------------------------------------------------------------- #
def _cascadia_trench_lon(lat):
    dl = np.asarray(lat, dtype=float) - 40.0
    return -124.5 - 0.35 * dl - 0.01 * dl * dl


def _build_grids(d_deg: float = 0.1):
    lon = np.arange(-130.0, -120.0 + d_deg / 2, d_deg)
    lat = np.arange(39.0, 51.0 + d_deg / 2, d_deg)
    LON, LAT = np.meshgrid(lon, lat)
    trench = _cascadia_trench_lon(LAT)
    east_km = (LON - trench) * 111.0 * np.cos(np.radians(LAT))
    depth_km = -np.tan(np.radians(11.0)) * east_km
    mask = (east_km < 0.0) | (-depth_km > 60.0)
    depth_km = np.where(mask, np.nan, depth_km)
    strike = np.where(mask, np.nan, (1.6 * (LAT - 45.0)) % 360.0)
    dip = np.where(mask, np.nan, 11.0 + 0.06 * (-depth_km))
    # store lon 0..360 like the real grids
    lon360 = np.where(lon < 0.0, lon + 360.0, lon)
    order = np.argsort(lon360)
    return lon360[order], lat, depth_km[:, order], strike[:, order], dip[:, order]


def _write_grd(path, lon, lat, z):
    import xarray as xr

    ds = xr.Dataset(
        {"z": (("y", "x"), z.astype("float32"))},
        coords={"x": lon.astype("float64"), "y": lat.astype("float64")},
    )
    ds.to_netcdf(path)
    ds.close()
    return str(path)


@pytest.fixture()
def slab2_grid_paths(tmp_path):
    lon, lat, dep, strike, dip = _build_grids()
    return {
        "dep": _write_grd(tmp_path / "cas_slab2_dep.grd", lon, lat, dep),
        "str": _write_grd(tmp_path / "cas_slab2_str.grd", lon, lat, strike),
        "dip": _write_grd(tmp_path / "cas_slab2_dip.grd", lon, lat, dip),
    }


@pytest.fixture()
def grids(slab2_grid_paths):
    return parse_slab2_grids(
        slab2_grid_paths["dep"], slab2_grid_paths["str"], slab2_grid_paths["dip"],
        zone_code="cas",
    )


# --------------------------------------------------------------------------- #
# parse_slab2_grids (pure).
# --------------------------------------------------------------------------- #
def test_parse_normalizes_lon_and_preserves_nan_edges(grids):
    # lon stored 0..360 in the file must come back -180..180 ascending.
    assert grids.lon.min() >= -180.0 and grids.lon.max() <= 0.0
    assert np.all(np.diff(grids.lon) > 0)
    # NaN edges preserved (the slab does not fill the rectangle).
    assert np.isnan(grids.depth_km).any()
    assert np.isfinite(grids.depth_km).any()
    # shared mesh: strike/dip match the depth grid shape.
    assert grids.strike_deg.shape == grids.depth_km.shape == grids.dip_deg.shape


def test_finite_lat_span_is_the_modeled_extent(grids):
    lo, hi = grids.finite_lat_span
    assert 38.9 <= lo < hi <= 51.1


def test_interface_lon_migrates_with_latitude_the_curve(grids):
    # The trench trace at a fixed depth must move WEST as latitude increases (Cascadia
    # bows convex-west) -- this is the curvature the tiling follows.
    lon42 = grids.interface_lon_at(42.0, -10.0)
    lon48 = grids.interface_lon_at(48.0, -10.0)
    assert lon42 is not None and lon48 is not None
    assert lon48 < lon42 - 1.0  # visibly further west at higher latitude
    # a depth deeper than the modeled band at this lat -> off-interface -> None.
    assert grids.interface_lon_at(45.0, -500.0) is None


def test_sample_returns_none_off_slab(grids):
    # far west of the trench is off-slab (NaN) -> None.
    assert grids.sample(-129.9, 45.0) is None
    got = grids.sample(-124.0, 45.0)
    assert got is not None and math.isfinite(got[0])


# --------------------------------------------------------------------------- #
# Scaling laws + moment budget (cited).
# --------------------------------------------------------------------------- #
def test_strasser_m9_dimensions():
    area, length, width = strasser_interface_dimensions(9.0)
    assert 1.0e5 < area < 1.5e5      # ~1.24e5 km2
    assert 550 < length < 700        # ~614 km
    assert 150 < width < 230         # ~189 km


def test_moment_round_trips_mw():
    assert abs(moment_to_mw(s2.target_moment_nm(9.0)) - 9.0) < 1e-9


# --------------------------------------------------------------------------- #
# resolve_slab2_scenario (tiling geometry sanity).
# --------------------------------------------------------------------------- #
def test_scenario_tiles_multiple_subfaults(grids):
    m = resolve_slab2_scenario("Cascadia", 9.0, epicenter_lonlat=(-125.5, 45.0),
                               target_resolution_m=20000.0, grids=grids)
    assert m.n_subfaults > 50
    # every patch is on-slab with sane geometry.
    for p in m.patches:
        assert -130.0 < p.lon < -120.0 and 39.0 < p.lat < 51.0
        assert p.depth_m > 0.0 and p.length_m > 0.0 and p.width_m > 0.0
        assert p.rake_deg == 90.0  # pure-thrust megathrust


def test_scenario_moment_sums_to_target_mw(grids):
    for mw in (8.5, 9.0, 9.2):
        m = resolve_slab2_scenario("Cascadia", mw, epicenter_lonlat=(-125.5, 45.0),
                                   target_resolution_m=20000.0, grids=grids)
        realized = sum(RIGIDITY_PA * p.length_m * p.width_m * p.slip_m
                       for p in m.patches)
        assert abs(moment_to_mw(realized) - mw) < 0.02  # tapered slip -> target Mw


def test_scenario_strikes_follow_the_interface(grids):
    # subfault strikes are Slab2-SAMPLED at each centroid -> they VARY along the
    # curved margin (a straight rectangle would carry one constant strike). Compare
    # each patch strike against the grid sample at its own centroid.
    m = resolve_slab2_scenario("Cascadia", 9.0, epicenter_lonlat=(-125.5, 45.0),
                               target_resolution_m=20000.0, grids=grids)
    strikes = np.array([p.strike_deg for p in m.patches])
    # unwrap around the 0/360 seam before measuring spread.
    spread = np.ptp(np.unwrap(np.radians(strikes)))
    assert spread > math.radians(3.0)  # strikes genuinely vary along the trench
    for p in m.patches:
        s = grids.sample(p.lon, p.lat)
        assert s is not None and abs(((p.strike_deg - s[1] + 180) % 360) - 180) < 1e-6


def test_scenario_centroids_track_the_curve(grids):
    # the rupture is NOT a straight fixed-lon bar: patch centroid lon is correlated
    # with latitude (the trench migrates west northward).
    m = resolve_slab2_scenario("Cascadia", 9.0, epicenter_lonlat=(-125.5, 45.0),
                               target_resolution_m=20000.0, grids=grids)
    lons = np.array([p.lon for p in m.patches])
    lats = np.array([p.lat for p in m.patches])
    assert abs(float(np.corrcoef(lons, lats)[0, 1])) > 0.3


def test_scenario_footprint_and_provenance(grids):
    m = resolve_slab2_scenario("Cascadia", 9.0, epicenter_lonlat=(-125.5, 45.0),
                               grids=grids)
    lo_lon, lo_lat, hi_lon, hi_lat = m.footprint_bbox
    assert -130 < lo_lon < hi_lon < -120 and 40 < lo_lat < hi_lat < 50
    assert m.product_id == "scenario_slab2_cas"
    assert m.magnitude == 9.0


def test_coarser_resolution_yields_fewer_subfaults(grids):
    fine = resolve_slab2_scenario("Cascadia", 9.0, epicenter_lonlat=(-125.5, 45.0),
                                  target_resolution_m=10000.0, grids=grids)
    coarse = resolve_slab2_scenario("Cascadia", 9.0, epicenter_lonlat=(-125.5, 45.0),
                                    target_resolution_m=40000.0, grids=grids)
    assert coarse.n_subfaults < fine.n_subfaults


def test_unknown_zone_raises():
    with pytest.raises(ScenarioSlab2Error) as ei:
        resolve_slab2_scenario("Atlantis", 9.0, grids=None, _http_get_fn=lambda u: b"")
    assert ei.value.error_code == "SLAB2_ZONE_UNKNOWN"


# --------------------------------------------------------------------------- #
# fetch_slab2_grids (I/O boundary, monkeypatched) + cache short-circuit.
# --------------------------------------------------------------------------- #
def test_fetch_via_children_api_monkeypatched(tmp_path, slab2_grid_paths, monkeypatch):
    monkeypatch.setenv("TRID3NT_CACHE_DIR", str(tmp_path / "cache"))
    children = {
        "items": [{
            "title": "Cascadia",
            "files": [
                {"name": "cas_slab2_dep_02.24.18.grd",
                 "downloadUri": "https://example/dep.grd"},
                {"name": "cas_slab2_str_02.24.18.grd",
                 "downloadUri": "https://example/str.grd"},
                {"name": "cas_slab2_dip_02.24.18.grd",
                 "downloadUri": "https://example/dip.grd"},
            ],
        }],
    }
    file_bytes = {
        "https://example/dep.grd": open(slab2_grid_paths["dep"], "rb").read(),
        "https://example/str.grd": open(slab2_grid_paths["str"], "rb").read(),
        "https://example/dip.grd": open(slab2_grid_paths["dip"], "rb").read(),
    }

    def fake_get(url: str) -> bytes:
        if "parentId" in url:
            return json.dumps(children).encode()
        return file_bytes[url]

    grids = fetch_slab2_grids("Cascadia", _http_get_fn=fake_get)
    assert np.isfinite(grids.depth_km).any()
    # cache is now populated -> a second fetch must NOT hit the network.
    def boom(url):  # noqa: ANN001
        raise AssertionError("cache short-circuit should skip the network")
    grids2 = fetch_slab2_grids("Cascadia", _http_get_fn=boom)
    assert grids2.depth_km.shape == grids.depth_km.shape


def test_fetch_missing_grid_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("TRID3NT_CACHE_DIR", str(tmp_path / "cache"))
    children = {"items": [{"title": "Cascadia", "files": [
        {"name": "cas_slab2_dep_x.grd", "downloadUri": "https://example/dep.grd"},
    ]}]}  # str + dip missing

    def fake_get(url: str) -> bytes:
        return json.dumps(children).encode()

    with pytest.raises(ScenarioSlab2Error) as ei:
        fetch_slab2_grids("Cascadia", _http_get_fn=fake_get)
    assert ei.value.error_code == "SLAB2_GRIDS_NOT_FOUND"


# --------------------------------------------------------------------------- #
# Composer ladder label: scenario renders LOUD, never a real event.
# --------------------------------------------------------------------------- #
def test_scenario_provenance_renders_loud():
    from trid3nt_contracts.common import SyntheticInput, render_assumptions_line

    line = render_assumptions_line([SyntheticInput(
        param="scenario_fault", value="Slab2 Cascadia M9.0: 279 subfaults",
        basis="scenario_slab2", real_source_if_any="USGS Slab2 (DOI 10.5066/F7PV6JNV)",
    )])
    assert line is not None
    assert "SCENARIO" in line and "NOT a real event" in line
