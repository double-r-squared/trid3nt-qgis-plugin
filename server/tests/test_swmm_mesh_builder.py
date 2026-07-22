"""End-to-end proof for the DEM -> quasi-2D SWMM mesh builder (sprint-16 P2,
``grace2_agent.workflows.swmm_mesh_builder``).

These tests PROVE the engine core runnable, not just constructable:

1. **Adaptive budget (RE-FIT from the LIVE urban anchor, BREAK D).** The perf
   model reproduces the FIRST LIVE urban run (1190 cells -> 983 s) - the
   synthetic P0 spike (400 cells -> 19 s) proved ~16x optimistic and is no
   longer the sizing anchor. The cell cap is sane (and conservative: smaller
   than under the old fit), and the resolution autoscaler coarsens a big AOI up
   the ``SWMM_RES_LADDER`` while never producing a degenerate grid.

2. **End-to-end build + RUN on a synthetic AOI.** We synthesize a small GeoTIFF
   DEM (a tilted plane draining to a low corner + a central pit, like the P0
   spike), a couple of building footprints, and ONE tagged barrier line carrying
   both a RED ``wall`` segment and a GREEN ``flap_gate`` segment. We build the
   ``.inp`` with swmm-api and RUN it headless via pyswmm, asserting:
     - the deck is a VALID runnable SWMM5 model (the run completes),
     - Flow Routing Continuity error < 5 % (the mass-balance honesty gate
       passes — no silently-wrong layer),
     - exactly ONE outfall fed by a SINGLE conduit (the P0 single-inlet rule),
     - the RED wall OMITS the overland conduit between its two cells,
     - the GREEN flap is an ORIFICE with ``has_flap_gate=True``,
     - building cells are DROPPED from the mesh (default ``building_rep="drop"``).

3. **Flap one-way physics (P0 carry-forward).** A focused 2-tank model proves a
   flap orifice PASSES forward flow but BLOCKS reverse flow, while a plain
   conduit leaks both ways — i.e. the flap is genuinely one-directional.

4. **Mass-balance honesty gate fires.** A tolerance set absurdly tight raises
   the typed ``SWMM_MASS_BALANCE_EXCEEDED`` rather than returning a layer.

pyswmm + swmm-api are required for tests 2-4 (skipped if absent). Test 1 needs
only the pure-Python budget code.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from grace2_agent.workflows import swmm_mesh_builder as mb
from grace2_agent.workflows.swmm_mesh_builder import (
    SWMM_RES_LADDER,
    SWMMMeshError,
    autoscale_swmm_resolution,
    build_swmm_mesh,
    compute_swmm_cell_cap,
    estimate_swmm_solve_seconds,
    read_flow_routing_continuity,
    run_swmm_deck,
)

swmm_api = pytest.importorskip("swmm_api")
pyswmm = pytest.importorskip("pyswmm")


# --------------------------------------------------------------------------- #
# Synthetic AOI fixtures (a real projected-metres GeoTIFF, NOT a stub).
# --------------------------------------------------------------------------- #
_N = 20  # grid is N x N cells (the P0 spike size -> ~19 s anchor)
_CELL = 10.0  # synthetic DEM native cell size, m
_EPSG = 32616  # UTM 16N (valid projected metres)
_OX, _OY = 500000.0, 4000000.0  # top-left origin


def _build_dem_array() -> np.ndarray:
    """Tilted plane draining to the (N-1, N-1) low corner + a central pit."""
    ii, jj = np.meshgrid(np.arange(_N), np.arange(_N), indexing="ij")
    slope = 0.02
    plane = 30.0 - slope * _CELL * (ii + jj)
    ci = cj = (_N - 1) / 2.0
    r2 = (ii - ci) ** 2 + (jj - cj) ** 2
    pit = 2.0 * np.exp(-r2 / (2.0 * 3.0**2))
    return (plane - pit).astype("float64")


def _write_dem_geotiff(path: Path) -> np.ndarray:
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    dem = _build_dem_array()
    transform = from_origin(_OX, _OY, _CELL, _CELL)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": _N,
        "width": _N,
        "crs": CRS.from_epsg(_EPSG),
        "transform": transform,
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(dem.astype("float32"), 1)
    return dem


def _cell_to_lonlat(i: int, j: int) -> tuple[float, float]:
    """Centre of grid cell (row i, col j) as WGS84 lon/lat.

    The synthetic DEM is in EPSG:32616 metres; barriers/buildings arrive as
    WGS84 (the OSM/GeoJSON convention), so we invert through the projection.
    """
    from rasterio.transform import from_origin, xy
    from rasterio.warp import transform as warp_transform

    transform = from_origin(_OX, _OY, _CELL, _CELL)
    x, y = xy(transform, i, j)  # cell centre in metres
    lons, lats = warp_transform(f"EPSG:{_EPSG}", "EPSG:4326", [x], [y])
    return lons[0], lats[0]


def _building_footprints() -> dict:
    """Two small building footprints (WGS84 polygons) on a couple of mid cells."""
    feats = []
    for (i, j) in [(6, 6), (12, 13)]:
        # ~1.2-cell square around the cell centre so it covers the cell.
        lon, lat = _cell_to_lonlat(i, j)
        # half-extent ~0.6 cells in metres -> degrees (rough, fine for a fixture)
        d = 0.00007  # ~7-8 m at this latitude
        ring = [
            [lon - d, lat - d],
            [lon + d, lat - d],
            [lon + d, lat + d],
            [lon - d, lat + d],
            [lon - d, lat - d],
        ]
        feats.append(
            {
                "type": "Feature",
                "properties": {"id": f"bldg_{i}_{j}"},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    return {"type": "FeatureCollection", "features": feats}


def _barriers() -> dict:
    """ONE FeatureCollection with a RED wall line and a GREEN flap-gate line.

    The wall runs across two columns on the uphill side so it dams the column;
    the flap gate sits between two cells where the static gradient would push
    flow toward the protected (uphill) cell.
    """
    # RED wall: a short line crossing the edge between (8,9) and (9,9).
    a_lon, a_lat = _cell_to_lonlat(8, 9)
    b_lon, b_lat = _cell_to_lonlat(9, 9)
    # GREEN flap: a line crossing the edge between (3,3) (protected/up) and (4,3).
    f_lon, f_lat = _cell_to_lonlat(3, 3)
    g_lon, g_lat = _cell_to_lonlat(4, 3)
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"barrier_type": "wall"},
                "geometry": {"type": "LineString", "coordinates": [[a_lon, a_lat], [b_lon, b_lat]]},
            },
            {
                "type": "Feature",
                "properties": {"barrier_type": "flap_gate", "protected_side": "left"},
                "geometry": {"type": "LineString", "coordinates": [[f_lon, f_lat], [g_lon, g_lat]]},
            },
        ],
    }


# --------------------------------------------------------------------------- #
# (1) Adaptive budget - RE-FIT from the LIVE urban anchor (BREAK D).
# --------------------------------------------------------------------------- #
def test_perf_model_reproduces_live_anchor():
    """T(1190) ~ 983 s - the FIRST LIVE urban anchor the model is re-fit to.

    This is the BREAK-D fix: the old fit (anchored to the synthetic 400-cell
    spike) predicted only ~63 s at 1190 cells (~16x optimistic), so the
    autoscaler under-coarsened. The re-fit must reproduce the real 983 s.
    """
    est = estimate_swmm_solve_seconds(1190)
    assert math.isclose(est, 983.0, rel_tol=0.02), est
    # And it must NOT regress to the optimistic old prediction at 1190 cells.
    assert est > 500.0, est


def test_perf_model_not_optimistic_vs_old_fit():
    """At 1190 cells the re-fit predicts ~16x what the old (spike) fit did.

    The old fit was A=2.604e-2, p=1.10 -> ~62.9 s at 1190 cells. The re-fit
    must be conservatively LARGER (so the autoscaler coarsens enough).
    """
    old_pred = 2.604e-2 * (1190.0 ** 1.10)  # ~62.9 s - the bug
    new_pred = estimate_swmm_solve_seconds(1190)
    assert new_pred > 10.0 * old_pred, (old_pred, new_pred)


def test_cell_cap_conservative_at_default_budget():
    """After the re-fit the default-budget cap is small + conservative.

    At 300 s budget / 0.35 overhead / p=1.10 / re-fit A the cap is ~273 cells
    (vs ~4.6k under the optimistic spike fit). It must stay floored and well
    BELOW the 1190-cell run that blew the budget.
    """
    cap = compute_swmm_cell_cap()
    assert cap >= mb.SWMM_MIN_CELL_CAP
    # The cap must keep the estimated solve at/under the net (post-overhead)
    # budget - the whole point of the autoscaler.
    solve_budget = mb.SWMM_SOLVE_BUDGET_S * (1.0 - mb.SWMM_OVERHEAD_FRACTION)
    assert estimate_swmm_solve_seconds(cap) <= solve_budget + 1.0, cap
    # And it must be far below the 1190-cell live run that took 983 s.
    assert cap < 1190, cap


def test_res_ladder_is_urban_scale():
    assert SWMM_RES_LADDER == (1.0, 2.0, 5.0, 10.0, 20.0)
    assert SWMM_RES_LADDER == tuple(sorted(SWMM_RES_LADDER))


def test_autoscale_keeps_small_aoi_at_base():
    # A count comfortably under the re-fit cap (~273 @ default budget) stays put.
    small = max(1, compute_swmm_cell_cap() // 2)
    a = autoscale_swmm_resolution(small, base_resolution_m=10.0)
    assert a.resolution_m == 10.0
    assert not a.coarsened
    assert a.estimated_active_cells == small


def test_autoscale_coarsens_big_aoi_without_degenerating():
    a = autoscale_swmm_resolution(200_000, base_resolution_m=1.0)
    assert a.coarsened
    assert a.resolution_m in SWMM_RES_LADDER
    # never degenerate: a real domain always keeps >=1 cell.
    assert a.estimated_active_cells >= 1
    # the chosen estimate is at/under the cap unless even the coarsest rung
    # overflows (then it clamps to the coarsest rung).
    assert a.resolution_m <= max(SWMM_RES_LADDER)


# --------------------------------------------------------------------------- #
# (2) End-to-end build + RUN on the synthetic AOI.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def synthetic_aoi(tmp_path: Path):
    dem_path = tmp_path / "dem.tif"
    dem = _write_dem_geotiff(dem_path)
    return {
        "dem_path": str(dem_path),
        "dem": dem,
        "buildings": _building_footprints(),
        "barriers": _barriers(),
        "tmp": tmp_path,
    }


def test_build_and_run_end_to_end(synthetic_aoi):
    """Build the deck, RUN it via pyswmm, assert it is a valid runnable model
    with continuity < 5 % and the structures present."""
    out_inp = synthetic_aoi["tmp"] / "mesh.inp"
    build = build_swmm_mesh(
        dem_path=synthetic_aoi["dem_path"],
        out_inp_path=str(out_inp),
        total_rain_depth_mm=120.0,
        storm_duration_hr=2.0,  # short storm keeps the test fast
        rain_interval_min=5,
        target_resolution_m=10.0,
        building_footprints=synthetic_aoi["buildings"],
        building_representation="drop",
        infiltration_method="none",
        barriers=synthetic_aoi["barriers"],
        # Autoscale OFF for the STRUCTURAL assertions: this test pins exact cell
        # coords (walls at (8,9)-(9,9), flap at (3,3)-(4,3), building at (6,6)),
        # which only hold at the requested 10 m. After the BREAK-D re-fit the
        # default cap (~273) is below this 20x20 (~398-cell) AOI, so leaving
        # autoscale ON would coarsen 10m->20m and shift every coordinate.
        # Autoscale coarsening is covered independently by the budget tests.
        enable_autoscale=False,
    )

    # --- the deck exists and the mesh provenance is sane ---
    assert Path(build.inp_path).exists()
    assert build.n_active_cells > 1
    assert build.n_storage_nodes == build.n_active_cells
    assert build.n_conduits >= build.n_active_cells - 1  # connected-ish overland net
    assert build.n_buildings_dropped >= 1  # at least one building cell removed
    assert build.resolution_m in SWMM_RES_LADDER or build.resolution_m == 10.0

    # --- nested hyetograph rode through: integrates to the input depth ---
    interval_hr = 5 / 60.0
    total = sum(intensity * interval_hr for _, intensity in build.hyetograph.timeseries)
    assert math.isclose(total, 120.0, rel_tol=1e-6), total

    # --- the written deck has EXACTLY ONE outfall fed by ONE conduit (P0 rule) ---
    from swmm_api import SwmmInput
    from swmm_api.input_file.section_labels import (
        OUTFALLS, CONDUITS, ORIFICES, STORAGE,
    )

    # Index (not .get) so swmm-api lazily parses each section into objects.
    inp = SwmmInput.read_file(build.inp_path)
    outfalls = inp[OUTFALLS]
    assert len(outfalls) == 1, f"expected exactly 1 outfall, got {len(outfalls)}"
    outfall_name = next(iter(outfalls))
    conduits = inp[CONDUITS]
    inlets_to_outfall = [
        c for c in conduits.values()
        if getattr(c, "to_node", None) == outfall_name or getattr(c, "from_node", None) == outfall_name
    ]
    assert len(inlets_to_outfall) == 1, "outfall must have exactly ONE inlet link (P0 rule)"

    # --- storage node count matches the active mesh ---
    storages = inp[STORAGE]
    assert len(storages) == build.n_active_cells

    # --- GREEN flap = an ORIFICE with has_flap_gate=True ---
    orifices = inp[ORIFICES]
    assert build.n_flap_gates >= 1, "expected at least one flap gate snapped"
    flap_with_gate = [o for o in orifices.values() if getattr(o, "has_flap_gate", False)]
    assert len(flap_with_gate) == build.n_flap_gates

    # --- RED wall = an OMITTED conduit: there must be no conduit between the
    #     two walled cells (8,9)-(9,9). ---
    walled = {mb._cell_node(8, 9), mb._cell_node(9, 9)}
    between = [
        c for c in conduits.values()
        if {getattr(c, "from_node", None), getattr(c, "to_node", None)} == walled
    ]
    assert between == [], "RED wall must OMIT the overland conduit between its cells"
    assert build.n_walls >= 1

    # --- building cells are DROPPED (no storage node for a dropped building) ---
    # cell (6,6) is a building under 'drop' -> it should NOT have a storage node.
    assert mb._cell_node(6, 6) not in storages

    # --- RUN headless via pyswmm + mass-balance gate ---
    run = run_swmm_deck(build, mass_balance_tolerance_pct=5.0)
    assert abs(run.continuity_error_pct) < 5.0, run.continuity_error_pct
    assert run.n_steps > 0
    assert run.max_depth_m >= 0.0
    assert run.peak_depth_grid.shape == build.grid_shape
    # the central pit should pond some water (it's the lowest interior region).
    assert run.max_depth_m > 0.0


def test_raise_representation_keeps_building_cells(synthetic_aoi):
    """building_representation='raise' keeps building cells (lifted invert),
    so they DO get storage nodes (contrast with 'drop')."""
    out_inp = synthetic_aoi["tmp"] / "mesh_raise.inp"
    build = build_swmm_mesh(
        dem_path=synthetic_aoi["dem_path"],
        out_inp_path=str(out_inp),
        storm_duration_hr=1.0,
        target_resolution_m=10.0,
        building_footprints=synthetic_aoi["buildings"],
        building_representation="raise",
        building_raise_m=2.0,
        barriers=None,
        enable_autoscale=False,
    )
    assert build.n_buildings_dropped == 0
    from swmm_api import SwmmInput
    from swmm_api.input_file.section_labels import STORAGE

    inp = SwmmInput.read_file(build.inp_path)
    storages = inp[STORAGE]
    # the building cell (6,6) is RETAINED under 'raise' and its invert lifted.
    assert mb._cell_node(6, 6) in storages
    raised_elev = storages[mb._cell_node(6, 6)].elevation

    # Compare against the SAME cell built with raise_m=0 (no lift) — an exact,
    # unambiguous proof the invert was lifted by the raise amount.
    flat_inp = synthetic_aoi["tmp"] / "mesh_raise0.inp"
    flat = build_swmm_mesh(
        dem_path=synthetic_aoi["dem_path"],
        out_inp_path=str(flat_inp),
        storm_duration_hr=1.0,
        target_resolution_m=10.0,
        building_footprints=synthetic_aoi["buildings"],
        building_representation="raise",
        building_raise_m=0.0,
        barriers=None,
        enable_autoscale=False,
    )
    flat_elev = SwmmInput.read_file(flat.inp_path)[STORAGE][mb._cell_node(6, 6)].elevation
    assert math.isclose(raised_elev - flat_elev, 2.0, abs_tol=1e-6), (raised_elev, flat_elev)


def test_mass_balance_gate_raises_on_tight_tolerance(synthetic_aoi):
    """An absurdly tight tolerance forces the typed honesty error rather than a
    silently-wrong result."""
    out_inp = synthetic_aoi["tmp"] / "mesh_gate.inp"
    build = build_swmm_mesh(
        dem_path=synthetic_aoi["dem_path"],
        out_inp_path=str(out_inp),
        storm_duration_hr=1.0,
        target_resolution_m=10.0,
        building_footprints=None,
        barriers=None,
        enable_autoscale=False,
    )
    with pytest.raises(SWMMMeshError) as exc:
        run_swmm_deck(build, mass_balance_tolerance_pct=0.0001)
    assert exc.value.error_code == "SWMM_MASS_BALANCE_EXCEEDED"
    assert "continuity_error_pct" in exc.value.details


def test_empty_mesh_raises(tmp_path: Path):
    """An all-nodata DEM raises the typed SWMM_EMPTY_MESH (no degenerate deck)."""
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    p = tmp_path / "nodata.tif"
    arr = np.full((10, 10), -9999.0, dtype="float32")
    with rasterio.open(
        p, "w", driver="GTiff", dtype="float32", count=1, height=10, width=10,
        crs=CRS.from_epsg(_EPSG), transform=from_origin(_OX, _OY, _CELL, _CELL),
        nodata=-9999.0,
    ) as dst:
        dst.write(arr, 1)
    with pytest.raises(SWMMMeshError) as exc:
        build_swmm_mesh(
            dem_path=str(p), out_inp_path=str(tmp_path / "x.inp"),
            storm_duration_hr=1.0, enable_autoscale=False,
        )
    assert exc.value.error_code == "SWMM_EMPTY_MESH"


# --------------------------------------------------------------------------- #
# (3) Flap one-way physics (P0 carry-forward) — a focused 2-tank proof.
# --------------------------------------------------------------------------- #
def _two_tank_final_depths(structure: str, primed: str, work: Path):
    """Build + run a 2-tank model; return (UP_depth, DOWN_depth) at the end.

    structure in {'flap','conduit'}; primed in {'up','down'}. Mirrors the P0
    spike's flap_experiment exactly (the proven one-way harness)."""
    from swmm_api import SwmmInput
    from swmm_api.input_file.section_labels import OPTIONS, REPORT
    from swmm_api.input_file.sections.node import Storage, Outfall
    from swmm_api.input_file.sections.link import Conduit, Orifice
    from swmm_api.input_file.sections.link_component import CrossSection
    from pyswmm import Simulation, Nodes

    inp = SwmmInput()
    inp[OPTIONS] = {
        "FLOW_UNITS": "CMS", "FLOW_ROUTING": "DYNWAVE", "LINK_OFFSETS": "DEPTH",
        "START_DATE": "01/01/2024", "START_TIME": "00:00:00",
        "REPORT_START_DATE": "01/01/2024", "REPORT_START_TIME": "00:00:00",
        "END_DATE": "01/01/2024", "END_TIME": "00:30:00",
        "REPORT_STEP": "00:01:00", "ROUTING_STEP": 1, "INERTIAL_DAMPING": "PARTIAL",
        "VARIABLE_STEP": 0.75, "MIN_SURFAREA": 1.0, "MAX_TRIALS": 8,
        "HEAD_TOLERANCE": 0.0015, "ALLOW_PONDING": "YES", "THREADS": 1,
    }
    inp[REPORT] = {"NODES": "ALL", "LINKS": "ALL"}
    up_init = 2.0 if primed == "up" else 0.0
    down_init = 2.0 if primed == "down" else 0.0
    inp.add_obj(Storage("UP", elevation=10.0, depth_max=5.0, depth_init=up_init,
                        kind=Storage.TYPES.FUNCTIONAL, data=[0.0, 0.0, 100.0]))
    inp.add_obj(Storage("DOWN", elevation=10.0, depth_max=5.0, depth_init=down_init,
                        kind=Storage.TYPES.FUNCTIONAL, data=[0.0, 0.0, 100.0]))
    inp.add_obj(Outfall("SINK", elevation=0.0, kind=Outfall.TYPES.FREE))
    inp.add_obj(Conduit("L_SINK", from_node="DOWN", to_node="SINK", length=10.0,
                        roughness=0.03, offset_upstream=4.9, offset_downstream=0))
    inp.add_obj(CrossSection(link="L_SINK", shape="RECT_OPEN", height=0.05, parameter_2=1.0))
    if structure == "flap":
        inp.add_obj(Orifice("GATE", from_node="UP", to_node="DOWN", orientation="SIDE",
                            offset=0.0, discharge_coefficient=0.65, has_flap_gate=True))
        inp.add_obj(CrossSection(link="GATE", shape="RECT_CLOSED", height=3.0, parameter_2=2.0))
    else:
        inp.add_obj(Conduit("GATE", from_node="UP", to_node="DOWN", length=5.0,
                            roughness=0.03, offset_upstream=0, offset_downstream=0))
        inp.add_obj(CrossSection(link="GATE", shape="RECT_OPEN", height=3.0, parameter_2=2.0))
    p = work / f"flap_{structure}_{primed}.inp"
    inp.write_file(str(p))
    with Simulation(str(p)) as sim:
        nodes = Nodes(sim)
        for _ in sim:
            pass
        return float(nodes["UP"].depth), float(nodes["DOWN"].depth)


def test_flap_gate_is_one_way(tmp_path: Path):
    """The flap orifice passes forward flow but BLOCKS reverse; a plain conduit
    leaks both ways. (P0 carry-forward, re-proven against the live engine.)"""
    fwd_up, fwd_down = _two_tank_final_depths("flap", "up", tmp_path)
    rev_up, rev_down = _two_tank_final_depths("flap", "down", tmp_path)
    ctrl_up, ctrl_down = _two_tank_final_depths("conduit", "down", tmp_path)

    # Forward: UP primed -> water passes to DOWN.
    assert fwd_down > 0.1, (fwd_up, fwd_down)
    # Reverse: DOWN primed -> the flap BLOCKS back-flow into UP.
    assert rev_up < 0.05, (rev_up, rev_down)
    # Control: a plain conduit LEAKS reverse flow back into UP (proves it's the
    # flap, not geometry, that blocks).
    assert ctrl_up > 0.1, (ctrl_up, ctrl_down)


# --------------------------------------------------------------------------- #
# (4) Continuity parser robustness.
# --------------------------------------------------------------------------- #
def test_read_continuity_returns_none_for_missing(tmp_path: Path):
    assert read_flow_routing_continuity(str(tmp_path / "nope.rpt")) is None
