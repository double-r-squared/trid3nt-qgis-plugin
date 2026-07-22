"""Depth-aware SnapWave offshore wave-boundary placement + dtwave + storm ramp.

Covers the two confirmed defects from the live "empty + static" wave animation
(run 01KVSTC80F) at the agent boundary (the synthesized boundary points + depths;
the worker GIS is tested separately):

- DEFECT 1: depth-aware offshore-edge selection (deepest edge chosen, min-depth
  gate, seaward search, typed WaveBoundaryError when no deep candidate, and the
  graceful None-fallback when the DEM is unavailable).
- DEFECT 2: the ``dtwave`` knob is threaded into the build_spec snapwave block,
  and the synthesized boundary carries the raised-cosine storm-envelope ramp.
"""

from __future__ import annotations

import json
import math

import pytest

from trid3nt_server.workflows.model_flood_scenario import (
    WaveBoundaryError,
    _depth_aware_offshore_points,
    _synthesize_parametric_wave_boundary,
    _wave_storm_envelope_factor,
    _WAVE_BND_MIN_DEPTH_M,
    _WAVE_BND_TARGET_DEPTH_M,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_edges():
    """Four edge midpoints around a centre at (0, 0), each with an outward unit
    step vector (west=-x, east=+x, south=-y, north=+y). 1000 m bbox half-span."""
    return [
        {"name": "west", "x": -1000.0, "y": 0.0, "ox": -1.0, "oy": 0.0},
        {"name": "east", "x": 1000.0, "y": 0.0, "ox": 1.0, "oy": 0.0},
        {"name": "south", "x": 0.0, "y": -1000.0, "ox": 0.0, "oy": -1.0},
        {"name": "north", "x": 0.0, "y": 1000.0, "ox": 0.0, "oy": 1.0},
    ]


def _depth_sampler(depth_by_edge):
    """Return a sample_depths(pts) callable that resolves each probe to its edge's
    depth. ``depth_by_edge`` maps edge-name -> depth (constant per edge) OR
    edge-name -> callable(step_index) -> depth (for seaward-search shapes)."""

    def _sampler(pts):
        out = []
        for (x, y) in pts:
            # Classify the probe to an edge by its dominant axis + sign.
            if abs(x) >= abs(y):
                name = "east" if x > 0 else "west"
            else:
                name = "north" if y > 0 else "south"
            d = depth_by_edge[name]
            if callable(d):
                # Step index = distance from the 1000 m edge midpoint / step.
                base = 1000.0
                dist = (abs(x) if name in ("east", "west") else abs(y)) - base
                step = max(dist, 0.0) / 40.0  # _WAVE_BND_SEAWARD_STEP_FRAC*1000
                out.append(d(int(round(step))))
            else:
                out.append(d)
        return out

    return _sampler


# --------------------------------------------------------------------------- #
# DEFECT 1 — depth-aware edge selection
# --------------------------------------------------------------------------- #


def test_deepest_edge_is_chosen():
    """The SOUTH edge is deepest (Gulf to the south); it must be selected over the
    shallow east/north nearshore edges (the live failure picked the shallow ones)."""
    edges = _make_edges()
    # Mimic Mexico Beach: south/SW deep water (Gulf), east/north shallow nearshore
    # / land. Depths are POSITIVE-DOWN (depth>0 = water; <=0 = at/above the datum).
    depths = {"west": 10.0, "east": 0.99, "south": 12.0, "north": 0.10}
    chosen = _depth_aware_offshore_points(
        edges, cx=0.0, cy=0.0, sample_depths=_depth_sampler(depths)
    )
    assert chosen is not None
    # Deepest-first ordering: south (12 m) then west (10 m).
    assert chosen[0]["name"] == "south"
    assert chosen[0]["depth_m"] == pytest.approx(12.0)
    # The shallow east (0.99 m) and north (0.10 m) edges are DROPPED (below 5 m).
    kept = {c["name"] for c in chosen}
    assert "east" not in kept
    assert "north" not in kept
    # West (10 m) also clears the floor and is kept.
    assert "west" in kept


def test_min_depth_gate_drops_shallow_edges():
    """Edges below the hard floor (5 m) are dropped; only deep ones survive."""
    edges = _make_edges()
    depths = {"west": 4.9, "east": 4.99, "south": 6.0, "north": 4.0}
    chosen = _depth_aware_offshore_points(
        edges, cx=0.0, cy=0.0, sample_depths=_depth_sampler(depths)
    )
    assert chosen is not None
    assert [c["name"] for c in chosen] == ["south"]  # only the 6 m edge clears 5 m
    assert chosen[0]["depth_m"] >= _WAVE_BND_MIN_DEPTH_M


def test_seaward_search_reaches_deep_water():
    """A shallow edge MIDPOINT that deepens as you push seaward is recovered by the
    seaward search (it should clear the gate at an outer step)."""
    edges = _make_edges()

    def _deepening(step):
        # midpoint (step 0) = 1 m; deepens 2 m per step -> clears 5 m by step ~2,
        # reaches the 10 m target by step ~5.
        return 1.0 + 2.0 * step

    # Only the south edge deepens; all others stay shallow/land.
    depths = {"west": -2.0, "east": -2.0, "south": _deepening, "north": -2.0}
    chosen = _depth_aware_offshore_points(
        edges, cx=0.0, cy=0.0, sample_depths=_depth_sampler(depths)
    )
    assert chosen is not None
    assert [c["name"] for c in chosen] == ["south"]
    # The kept point is PUSHED SEAWARD (|y| > the 1000 m midpoint) into deep water.
    pt = chosen[0]
    assert abs(pt["y"]) > 1000.0
    assert pt["depth_m"] >= _WAVE_BND_TARGET_DEPTH_M


def test_typed_error_when_no_deep_candidate():
    """Fully-inland / enclosed AOI (every edge shallow even after the seaward
    search): a typed WaveBoundaryError, NOT a flat-zero field."""
    edges = _make_edges()
    depths = {"west": -2.0, "east": -1.0, "south": 0.5, "north": 0.1}
    with pytest.raises(WaveBoundaryError) as ei:
        _depth_aware_offshore_points(
            edges, cx=0.0, cy=0.0, sample_depths=_depth_sampler(depths)
        )
    assert ei.value.error_code == "WAVE_BOUNDARY_NO_DEEP_WATER"


def test_dem_unavailable_returns_none_for_fallback():
    """When the depth sampler returns None (DEM unreadable / missing), the selector
    returns None so the caller falls back to bathy-unaware placement (NOT error)."""
    edges = _make_edges()
    chosen = _depth_aware_offshore_points(
        edges, cx=0.0, cy=0.0, sample_depths=lambda pts: None
    )
    assert chosen is None


def test_nan_depths_are_ignored_not_chosen():
    """NaN samples (nodata / off-tile) do not count as deep water."""
    edges = _make_edges()
    depths = {"west": float("nan"), "east": float("nan"), "south": 8.0,
              "north": float("nan")}
    chosen = _depth_aware_offshore_points(
        edges, cx=0.0, cy=0.0, sample_depths=_depth_sampler(depths)
    )
    assert chosen is not None
    assert [c["name"] for c in chosen] == ["south"]


# --------------------------------------------------------------------------- #
# DEFECT 2 realism — raised-cosine storm envelope
# --------------------------------------------------------------------------- #


def test_storm_envelope_shape():
    """0 at the window ends, 1 at the centre, symmetric, monotone rise to peak."""
    win = 24.0 * 3600.0
    assert _wave_storm_envelope_factor(0.0, win) == pytest.approx(0.0, abs=1e-9)
    assert _wave_storm_envelope_factor(win, win) == pytest.approx(0.0, abs=1e-9)
    assert _wave_storm_envelope_factor(0.5 * win, win) == pytest.approx(1.0)
    # Symmetric about the centre.
    a = _wave_storm_envelope_factor(0.25 * win, win)
    b = _wave_storm_envelope_factor(0.75 * win, win)
    assert a == pytest.approx(b)
    # Monotone rise on [0, peak].
    prev = -1.0
    for k in range(0, 13):
        f = _wave_storm_envelope_factor(k / 24.0 * win, win)
        assert f >= prev - 1e-9
        prev = f


def test_storm_envelope_zero_window_is_constant():
    assert _wave_storm_envelope_factor(0.0, 0.0) == 1.0


# --------------------------------------------------------------------------- #
# Synthesized boundary: fallback (no DEM) + time-varying series
# --------------------------------------------------------------------------- #


def test_synthesize_no_dem_falls_back_to_four_edges_with_time_series():
    """No topobathy_uri -> bathy-unaware 4-edge placement (unchanged), but each
    point now carries the time-varying storm envelope (Defect 2 realism)."""
    bbox = (-85.55, 29.92, -85.35, 30.12)  # Mexico Beach-ish
    bc = _synthesize_parametric_wave_boundary(
        bbox, target_epsg=32616, return_period_yr=100, duration_hr=12.0,
        topobathy_uri=None,
    )
    pts = bc["points"]
    assert len(pts) == 4  # all four edges (worker derives the seaward one)
    assert bc["_prov_depth_aware"] is False
    assert bc["_prov_time_varying"] is True
    for pt in pts:
        # projected UTM coords
        assert abs(pt["x"]) > 1000.0 and abs(pt["y"]) > 1000.0
        assert 0.0 <= pt["wd"] <= 360.0
        # time-varying series present, shared length, ramps to the peak hs.
        ts = pt["time_s"]
        hs = pt["hs_series"]
        tp = pt["tp_series"]
        assert len(ts) == len(hs) == len(tp) >= 3
        assert ts[0] == 0.0 and ts[-1] == pytest.approx(12.0 * 3600.0)
        # Ramp: ends below the peak, centre at the peak scalar.
        peak = pt["hs"]
        assert hs[0] < peak
        assert max(hs) == pytest.approx(peak, rel=1e-3)
        # Tp tracks Hs (longer period at the peak).
        assert tp[len(tp) // 2] >= tp[0]


def test_synthesize_depth_aware_uses_dem(monkeypatch):
    """With a DEM available, the synthesis selects only the deep (seaward) edge(s)
    and tags the boundary depth-aware."""
    import trid3nt_server.workflows.model_flood_scenario as mfs

    # Stub the DEM depth sampler: south edge deep (Gulf), others shallow/land.
    def _fake_sample(uri, pts, epsg):
        out = []
        # Reproject not needed; classify by sign of the projected coord relative
        # to the AOI centroid in UTM (south = smaller y).
        ys = [y for (_x, y) in pts]
        cy = sum(ys) / len(ys)
        for (x, y) in pts:
            # south of centroid -> deep; else shallow.
            out.append(15.0 if y < cy else -1.0)
        return out

    monkeypatch.setattr(mfs, "_sample_dem_depth_m", _fake_sample)
    bbox = (-85.55, 29.92, -85.35, 30.12)
    bc = _synthesize_parametric_wave_boundary(
        bbox, target_epsg=32616, return_period_yr=100, duration_hr=6.0,
        topobathy_uri="s3://b/topo.tif",
    )
    assert bc["_prov_depth_aware"] is True
    # Only the deep (southern) candidate(s) survive -> fewer than 4 points.
    assert 1 <= len(bc["points"]) < 4
    for pt in bc["points"]:
        assert pt["_prov_depth_m"] >= _WAVE_BND_MIN_DEPTH_M


def test_synthesize_depth_aware_typed_error_propagates(monkeypatch):
    """All edges shallow with a real DEM -> WaveBoundaryError (honest dead-end)."""
    import trid3nt_server.workflows.model_flood_scenario as mfs

    monkeypatch.setattr(
        mfs, "_sample_dem_depth_m", lambda uri, pts, epsg: [-1.0] * len(pts)
    )
    with pytest.raises(WaveBoundaryError) as ei:
        _synthesize_parametric_wave_boundary(
            (-85.55, 29.92, -85.35, 30.12),
            target_epsg=32616, return_period_yr=100, duration_hr=6.0,
            topobathy_uri="s3://b/topo.tif",
        )
    assert ei.value.error_code == "WAVE_BOUNDARY_NO_DEEP_WATER"
