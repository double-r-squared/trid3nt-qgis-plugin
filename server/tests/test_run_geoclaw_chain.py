"""End-to-end MODULE tests for the GeoClaw (Clawpack) shallow-water engine
(sprint-17), exercised in ISOLATION with run_solver / boto3 / network MOCKED.

GeoClaw is NEW + BATCH-only + not yet registry-wired by the orchestrator, so we
do NOT run the full agent suite or a live Clawpack solve. These tests pin the
agent-side MODULES the lane owns:

  1. **Contract round-trip + scenario alias normalization** —
     ``GeoClawRunArgs`` / ``GeoClawDepthLayerURI`` (no SWMM/clawpack dep).
  2. **build_spec assembly** — ``build_geoclaw_build_spec`` maps run args onto the
     worker's setrun_builder field dict, per scenario.
  3. **Solver registration** — ``'geoclaw'`` is a first-class entry in
     ``SOLVER_WORKFLOW_REGISTRY`` and the bridge tool is registered.
  4. **postprocess on a SYNTHETIC fort.q** — a hand-built GeoClaw fort.q frame
     parses + rasterizes + writes a VALID EPSG:4326 depth COG (upload stubbed),
     yielding the EXACT postprocess_flood (layers, metrics) shape.
  5. **Composer arg-assembly with run_solver MOCKED** — the composer stages a
     manifest, dispatches via a mocked run_solver/wait_for_completion, downloads
     a mocked Batch output, postprocesses, and returns the peak GeoClawDepthLayerURI
     + emits frames out-of-band — all without touching AWS or Clawpack.

rasterio + numpy are required for (4)+(5); they are in the agent venv.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from grace2_contracts.geoclaw_contracts import (
    GEOCLAW_DEPTH_STYLE_PRESET,
    GeoClawDepthLayerURI,
    GeoClawRunArgs,
)

_AOI = (-85.75, 29.55, -85.25, 30.20)

# The web parseFrameToken regex — the frame NAMES must match it or the
# sequential group never forms (same guard as test_postprocess_swmm/flood).
_WEB_STEP_TOKEN_RE = re.compile(r"\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b", re.I)


# ===========================================================================
# (1) Contract round-trip + alias normalization.
# ===========================================================================
def test_run_args_round_trip_and_scenario_aliases():
    a = GeoClawRunArgs(bbox=_AOI, scenario="breach", dam_break_depth_m=8.0)
    assert a.scenario == "dam_break"  # alias
    b = GeoClawRunArgs(bbox=_AOI, scenario="wave")
    assert b.scenario == "tsunami"  # alias
    c = GeoClawRunArgs(bbox=_AOI, scenario="storm_surge")
    assert c.scenario == "surge"  # alias
    # round-trip.
    a2 = GeoClawRunArgs(**a.model_dump())
    assert a2 == a


def test_run_args_rejects_bad_scenario_and_source_point():
    with pytest.raises(Exception):
        GeoClawRunArgs(bbox=_AOI, scenario="not_a_scenario")
    with pytest.raises(Exception):
        GeoClawRunArgs(bbox=_AOI, source_lonlat=(999.0, 0.0))


def test_depth_layer_uri_round_trip():
    lyr = GeoClawDepthLayerURI(
        layer_id="geoclaw-depth-peak-x",
        name="Peak flood depth",
        layer_type="raster",
        uri="s3://b/k.tif",
        style_preset=GEOCLAW_DEPTH_STYLE_PRESET,
        role="primary",
        units="meters",
        bbox=_AOI,
        max_depth_m=2.5,
        flooded_area_km2=1.2,
        max_inundation_m=1.1,
        scenario="tsunami",
    )
    assert GeoClawDepthLayerURI(**lyr.model_dump()) == lyr
    # reuses the shared depth preset (no new style key).
    assert lyr.style_preset == "continuous_flood_depth"


# ===========================================================================
# (2) build_spec assembly.
# ===========================================================================
def test_build_spec_dam_break():
    from grace2_agent.workflows.run_geoclaw import build_geoclaw_build_spec

    args = GeoClawRunArgs(
        bbox=_AOI, scenario="dam_break", dam_break_depth_m=12.0, output_frames=10
    )
    spec = build_geoclaw_build_spec(args, base_num_cells=(50, 60))
    assert spec["scenario"] == "dam_break"
    assert spec["bbox"] == list(_AOI)
    assert spec["topo_file"] == "topo.asc"
    assert spec["dam_break_depth_m"] == 12.0
    assert spec["base_num_cells"] == [50, 60]
    assert spec["output_frames"] == 10
    # no dtopo/surge keys for dam_break.
    assert "dtopo_file" not in spec
    assert "surge_forcing_file" not in spec


def test_build_spec_tsunami_with_staged_dtopo():
    from grace2_agent.workflows.run_geoclaw import build_geoclaw_build_spec

    args = GeoClawRunArgs(bbox=_AOI, scenario="tsunami", source_magnitude=8.4)
    spec = build_geoclaw_build_spec(args, dtopo_dest="dtopo.tt3")
    assert spec["scenario"] == "tsunami"
    assert spec["source_magnitude"] == 8.4
    assert spec["dtopo_file"] == "dtopo.tt3"


def test_build_spec_surge_with_forcing_and_source_point():
    from grace2_agent.workflows.run_geoclaw import build_geoclaw_build_spec

    args = GeoClawRunArgs(
        bbox=_AOI, scenario="surge", sea_level_m=2.0, source_lonlat=(-85.4, 29.8)
    )
    spec = build_geoclaw_build_spec(args, surge_dest="surge.csv")
    assert spec["scenario"] == "surge"
    assert spec["sea_level_m"] == 2.0
    assert spec["surge_forcing_file"] == "surge.csv"
    assert spec["source_lonlat"] == [-85.4, 29.8]


def test_build_spec_threads_domain_bbox_and_source_override():
    from grace2_agent.workflows.run_geoclaw import build_geoclaw_build_spec

    args = GeoClawRunArgs(
        bbox=_AOI, scenario="tsunami", source_lonlat=(-85.5, 29.9)
    )
    dom = (-86.5, 28.9, -85.0, 30.5)
    spec = build_geoclaw_build_spec(
        args, domain_bbox=dom, source_lonlat_override=(-86.3, 29.8)
    )
    # The offshore-extended domain is threaded (it differs from the AOI).
    assert spec["domain_bbox"] == [-86.5, 28.9, -85.0, 30.5]
    # The resolved offshore override WINS over the raw run_args.source_lonlat.
    assert spec["source_lonlat"] == [-86.3, 29.8]


def test_build_spec_omits_domain_bbox_when_equal_to_aoi():
    from grace2_agent.workflows.run_geoclaw import build_geoclaw_build_spec

    args = GeoClawRunArgs(bbox=_AOI, scenario="dam_break")
    spec = build_geoclaw_build_spec(args, domain_bbox=tuple(_AOI))
    # domain == AOI -> not threaded (the worker defaults domain -> AOI).
    assert "domain_bbox" not in spec


# ===========================================================================
# (2b) Offshore-domain planning + bathymetry-aware source placement.
# ===========================================================================
def test_plan_geoclaw_domain_extends_offshore_for_tsunami():
    from grace2_agent.workflows.run_geoclaw import plan_geoclaw_domain

    # tsunami: the domain extends off the AOI on all sides AND encloses the
    # requested offshore source with a buffer.
    src = (-86.20, 29.90)  # well west of the AOI's western edge (-85.75)
    dom = plan_geoclaw_domain(_AOI, "tsunami", src)
    assert dom[0] < _AOI[0] and dom[1] < _AOI[1]
    assert dom[2] > _AOI[2] and dom[3] > _AOI[3]
    # the requested source sits strictly INSIDE the planned domain.
    assert dom[0] < src[0] < dom[2] and dom[1] < src[1] < dom[3]


def test_plan_geoclaw_domain_unchanged_for_dam_break_and_surge():
    from grace2_agent.workflows.run_geoclaw import plan_geoclaw_domain

    assert plan_geoclaw_domain(_AOI, "dam_break", None) == tuple(_AOI)
    assert plan_geoclaw_domain(_AOI, "surge", (-85.4, 29.8)) == tuple(_AOI)


def test_plan_geoclaw_grid_coarse_base_and_bounded_amr():
    """The SOLVER_TIMEOUT fix: a COARSE base grid (a few thousand cells) over the
    full domain + AMR bounded so the AOI finest mesh stays within the cell budget.
    """
    from grace2_agent.workflows.run_geoclaw import (
        _GEOCLAW_BASE_CELLS_MAX,
        _GEOCLAW_FINEST_CELL_BUDGET,
        _GEOCLAW_MAX_AMR_LEVELS,
        plan_geoclaw_domain,
        plan_geoclaw_grid,
    )

    # A town-scale coastal AOI with an offshore-extended domain.
    aoi = (-124.22, 41.72, -124.16, 41.78)  # ~0.06 deg (~5 x 6.6 km)
    dom = plan_geoclaw_domain(aoi, "tsunami", (-125.6, 41.6))
    base, levels, est, prop_level, est_prop = plan_geoclaw_grid(
        dom, aoi, requested_amr_levels=4
    )

    # COARSE base: clamped per axis, a few thousand cells total (NOT one per pixel).
    assert base[0] <= _GEOCLAW_BASE_CELLS_MAX and base[1] <= _GEOCLAW_BASE_CELLS_MAX
    assert base[0] * base[1] <= 10_000
    # AMR bounded; finest AOI cells within the runtime budget.
    assert 1 <= levels <= _GEOCLAW_MAX_AMR_LEVELS
    assert 0 < est <= _GEOCLAW_FINEST_CELL_BUDGET
    # A town AOI reaches the tens-of-metres run-up target at the level cap.
    assert levels == _GEOCLAW_MAX_AMR_LEVELS
    # An INTERMEDIATE propagation tier (2 levels above base == level 3) is forced
    # over the offshore domain, below the finest AOI tier, with a bounded cell
    # estimate (the offshore propagation cost the tier adds).
    assert prop_level == 3 and prop_level < levels
    assert 0 < est_prop


def test_plan_geoclaw_grid_large_aoi_is_budget_clamped():
    """A LARGE AOI is clamped to fewer levels (coarser run-up) so the finest mesh
    never exceeds the budget -- the run still completes with non-zero inundation.
    """
    from grace2_agent.workflows.run_geoclaw import (
        _GEOCLAW_FINEST_CELL_BUDGET,
        plan_geoclaw_domain,
        plan_geoclaw_grid,
    )

    dom = plan_geoclaw_domain(_AOI, "tsunami", (-86.2, 29.9))
    _, levels_big, est_big, _prop_big, _ = plan_geoclaw_grid(
        dom, _AOI, requested_amr_levels=4
    )
    # request 4 but the big-AOI finest budget clamps it below the cap.
    assert levels_big < 4
    assert est_big <= _GEOCLAW_FINEST_CELL_BUDGET

    # A request for MORE levels than the budget allows is still clamped.
    _, levels_clamped, _, _, _ = plan_geoclaw_grid(dom, _AOI, requested_amr_levels=6)
    assert levels_clamped == levels_big


def test_geoclaw_propagation_level_mirrors_worker_formula():
    """The agent ``_geoclaw_propagation_level`` MUST equal the worker
    ``setrun_builder._propagation_level`` for every nest depth (the agent <->
    worker cross-check: the agent estimates the offshore propagation tier the
    worker emits). 2 levels above base (level 3), capped at one-below-finest,
    floored at the base.
    """
    from grace2_agent.workflows.run_geoclaw import _geoclaw_propagation_level

    # The worker formula, re-derived here independently (kept BYTE-mirrored):
    def _worker_propagation_level(amr_levels: int) -> int:
        return min(1 + 2, max(int(amr_levels) - 1, 1))

    for amr_levels in range(1, 8):
        assert _geoclaw_propagation_level(amr_levels) == _worker_propagation_level(
            amr_levels
        ), amr_levels
    # Spot-check the load-bearing values: a 5-level nest -> tier level 3 (< finest
    # 5); shallow nests collapse to no separate tier.
    assert _geoclaw_propagation_level(5) == 3
    assert _geoclaw_propagation_level(4) == 3
    assert _geoclaw_propagation_level(3) == 2
    assert _geoclaw_propagation_level(2) == 1
    assert _geoclaw_propagation_level(1) == 1


def test_resolve_offshore_source_picks_deepest_seaward_cell(tmp_path):
    # A synthetic bathy DEM: deep ocean on the WEST, dry land on the EAST. The
    # resolver must place the Okada source over the deepest WEST cell (seaward of
    # the AOI), never on land.
    import rasterio
    from rasterio.transform import from_bounds

    from grace2_agent.workflows.run_geoclaw import resolve_offshore_source

    dom = (-86.5, 28.9, -85.0, 30.5)
    width, height = 30, 32
    transform = from_bounds(dom[0], dom[1], dom[2], dom[3], width, height)
    # West half deep (-2000 m deepening westward), east half land (+50 m).
    arr = np.zeros((height, width), dtype="float32")
    lons = np.linspace(dom[0], dom[2], width)
    for j, lon in enumerate(lons):
        if lon < -85.9:  # ocean
            arr[:, j] = -2000.0 * ((-85.9 - lon) / (-85.9 - dom[0]) + 0.1)
        else:  # land
            arr[:, j] = 50.0
    dem = tmp_path / "bathy.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as ds:
        ds.write(arr, 1)

    pt = resolve_offshore_source(f"file://{dem}", dom, _AOI, None)
    assert pt is not None
    lon, lat = pt
    assert lon < _AOI[0]  # seaward (west) of the AOI
    # the deepest column is the far-west edge band, inset off the boundary.
    assert dom[0] < lon < -85.9


def test_resolve_offshore_source_honors_valid_requested_source(tmp_path):
    import rasterio
    from rasterio.transform import from_bounds

    from grace2_agent.workflows.run_geoclaw import resolve_offshore_source

    dom = (-86.5, 28.9, -85.0, 30.5)
    width, height = 24, 24
    transform = from_bounds(dom[0], dom[1], dom[2], dom[3], width, height)
    arr = np.full((height, width), -500.0, dtype="float32")  # all ocean
    dem = tmp_path / "ocean.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as ds:
        ds.write(arr, 1)

    req = (-86.0, 29.7)  # over water, inside the domain -> honored verbatim
    pt = resolve_offshore_source(f"file://{dem}", dom, _AOI, req)
    assert pt == req


def test_resolve_offshore_source_returns_none_on_dry_domain(tmp_path):
    import rasterio
    from rasterio.transform import from_bounds

    from grace2_agent.workflows.run_geoclaw import resolve_offshore_source

    dom = (-86.5, 28.9, -85.0, 30.5)
    width, height = 16, 16
    transform = from_bounds(dom[0], dom[1], dom[2], dom[3], width, height)
    arr = np.full((height, width), 100.0, dtype="float32")  # all land
    dem = tmp_path / "land.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as ds:
        ds.write(arr, 1)

    # No below-waterline cell -> None (caller keeps the requested source + logs).
    assert resolve_offshore_source(f"file://{dem}", dom, _AOI, (-86.0, 29.7)) is None


def _write_dem(tmp_path, name, bounds, arr):
    """Write a synthetic EPSG:4326 DEM and return its file:// URI."""
    import rasterio
    from rasterio.transform import from_bounds

    height, width = arr.shape
    transform = from_bounds(bounds[0], bounds[1], bounds[2], bounds[3], width, height)
    dem = tmp_path / name
    with rasterio.open(
        dem, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as ds:
        ds.write(arr.astype("float32"), 1)
    return f"file://{dem}"


def test_finalize_geoclaw_domain_encloses_offshore_source(tmp_path):
    # Issue #9: the fetched bathymetry reaches FURTHER offshore than the AOI-sized
    # domain, so the resolved deep-water source lands WEST of (outside) it. The
    # composer must re-size the domain to ENCLOSE the source. Reproduce: a DEM that
    # spans well west of the initial domain, deep water only in that far-west strip.
    from grace2_agent.workflows.run_geoclaw import (
        finalize_geoclaw_domain,
        plan_geoclaw_domain,
        resolve_offshore_source,
    )

    init_domain = plan_geoclaw_domain(_AOI, "tsunami", None)
    # DEM extends to -87.5 W (well past the initial domain west edge); ocean
    # deepens westward for lon < -86.6, land to the east.
    dem_bounds = (-87.5, 28.90, -84.60, 30.85)
    width, height = 80, 40
    lons = np.linspace(dem_bounds[0], dem_bounds[2], width)
    arr = np.full((height, width), 40.0, dtype="float32")  # land default
    for j, lon in enumerate(lons):
        if lon < -86.6:
            frac = (-86.6 - lon) / (-86.6 - dem_bounds[0])  # 0..1 westward
            arr[:, j] = -100.0 - 3000.0 * frac
    dem_uri = _write_dem(tmp_path, "wide_bathy.tif", dem_bounds, arr)

    # The resolved source lands over deep water WEST of the initial domain.
    src = resolve_offshore_source(dem_uri, init_domain, _AOI, None)
    assert src is not None
    slon, slat = src
    assert slon < init_domain[0]  # OUTSIDE the AOI-sized domain (the #9 bug)

    # finalize re-sizes the domain to ENCLOSE the source (clamped to DEM coverage).
    final = finalize_geoclaw_domain(_AOI, "tsunami", src, dem_uri)
    # Invariant: source strictly INSIDE the final domain ...
    assert final[0] < slon < final[2]
    assert final[1] < slat < final[3]
    # ... domain still spans the AOI coast ...
    assert final[0] <= _AOI[0] and final[2] >= _AOI[2]
    assert final[1] <= _AOI[1] and final[3] >= _AOI[3]
    # ... and stays within the fetched DEM coverage (GeoClaw topo covers domain).
    assert final[0] >= dem_bounds[0] and final[2] <= dem_bounds[2]


def test_finalize_geoclaw_domain_noop_for_dam_break_and_surge():
    # Non-offshore scenarios (internal/uniform source) keep domain == AOI.
    from grace2_agent.workflows.run_geoclaw import finalize_geoclaw_domain

    assert finalize_geoclaw_domain(_AOI, "dam_break", None, "file:///none") == tuple(_AOI)
    assert finalize_geoclaw_domain(_AOI, "surge", (-85.4, 29.8), "file:///none") == tuple(_AOI)


def test_finalize_geoclaw_domain_raises_when_source_uncoverable(tmp_path):
    # Guardrail (loud failure, like the flat-ocean gate): if the DEM coverage does
    # NOT reach the source longitude, clamping pulls the domain off the source ->
    # GEOCLAW_SOURCE_OUTSIDE_DOMAIN rather than a silent zero-inundation solve.
    from grace2_agent.workflows.run_geoclaw import (
        GeoClawWorkflowError,
        finalize_geoclaw_domain,
    )

    # A small DEM whose EAST edge (-86.0) is west of the (contrived) source -85.0.
    dem_bounds = (-86.5, 28.90, -86.0, 30.50)
    arr = np.full((16, 16), -500.0, dtype="float32")
    dem_uri = _write_dem(tmp_path, "narrow.tif", dem_bounds, arr)

    with pytest.raises(GeoClawWorkflowError) as ei:
        finalize_geoclaw_domain(_AOI, "tsunami", (-85.0, 29.7), dem_uri)
    assert ei.value.error_code == "GEOCLAW_SOURCE_OUTSIDE_DOMAIN"


# ===========================================================================
# (2b) CRS alignment — reproject the topo/bathy DEM to EPSG:4326 (the GeoClaw
# zero-inundation root cause). A projected-metres (UTM) topo extent has ZERO
# overlap with GeoClaw's lon/lat computational domain -> "topo arrays do not
# cover domain (area of overlap = 0.0)" -> zero fort.q frames.
# ===========================================================================
def _bounds_overlap_area(a, b):
    """Overlap area of two (min_x, min_y, max_x, max_y) boxes (0 if disjoint)."""
    ox = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    oy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ox * oy


def test_reproject_dem_to_4326_makes_utm_topo_overlap_lonlat_domain(tmp_path):
    # A UTM (projected-METRES) topo -- exactly what fetch_topobathy stages -- must
    # reproject to EPSG:4326 so its lon/lat bounds OVERLAP the GeoClaw lon/lat
    # domain. Pre-fix the metres extent had ZERO overlap and GeoClaw aborted.
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.warp import transform_bounds

    from grace2_agent.workflows.run_geoclaw import reproject_dem_to_4326

    # A Crescent City-like lon/lat GeoClaw domain (the failing live AOI's shape).
    dom = (-124.45, 41.5, -123.9, 42.1)
    utm = "EPSG:32616"  # the WRONG-zone UTM the merge fixed on (zone 10N is right)
    # Project the lon/lat domain into UTM metres -> the source raster's bounds.
    u_min_x, u_min_y, u_max_x, u_max_y = transform_bounds("EPSG:4326", utm, *dom)
    utm_bounds = (u_min_x, u_min_y, u_max_x, u_max_y)

    # Pre-fix invariant: the raw UTM-metres extent has ZERO overlap with the domain.
    assert _bounds_overlap_area(utm_bounds, dom) == 0.0

    width, height = 48, 52
    transform = from_bounds(u_min_x, u_min_y, u_max_x, u_max_y, width, height)
    arr = np.full((height, width), -50.0, dtype="float32")  # all ocean
    src = tmp_path / "topo_utm.tif"
    with rasterio.open(
        src, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs=utm, transform=transform,
    ) as ds:
        ds.write(arr, 1)

    new_uri = reproject_dem_to_4326(
        f"file://{src}", run_id="01TESTGEOCLAWREPROJECT0000000"
    )
    assert new_uri.startswith("file://")
    assert new_uri != f"file://{src}"

    with rasterio.open(new_uri[len("file://"):]) as ds:
        assert ds.crs.to_epsg() == 4326
        b = ds.bounds
    reproj_bounds = (b.left, b.bottom, b.right, b.top)
    # Post-fix: the reprojected lon/lat topo OVERLAPS the lon/lat domain (> 0).
    assert _bounds_overlap_area(reproj_bounds, dom) > 0.0


def test_reproject_dem_to_4326_passthrough_when_already_lonlat(tmp_path):
    # A DEM already in EPSG:4326 is returned UNCHANGED (idempotent, no rewrite).
    import rasterio
    from rasterio.transform import from_bounds

    from grace2_agent.workflows.run_geoclaw import reproject_dem_to_4326

    dom = (-124.45, 41.5, -123.9, 42.1)
    transform = from_bounds(*dom, 16, 16)
    dem = tmp_path / "topo_ll.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=16, width=16, count=1,
        dtype="float32", crs="EPSG:4326", transform=transform,
    ) as ds:
        ds.write(np.full((16, 16), -10.0, dtype="float32"), 1)

    assert reproject_dem_to_4326(f"file://{dem}") == f"file://{dem}"


def test_reproject_dem_to_4326_degrades_on_unreachable_uri():
    # Best-effort: an unreadable URI returns the original unchanged (the run then
    # fails loudly downstream with the honest overlap message, never silently here).
    from grace2_agent.workflows.run_geoclaw import reproject_dem_to_4326

    bad = "file:///nonexistent/grace2/topo.tif"
    assert reproject_dem_to_4326(bad) == bad


# ===========================================================================
# (3) Solver registration + bridge tool registered.
# ===========================================================================
def test_geoclaw_registered_in_solver_workflow_registry():
    from grace2_agent.tools.solver import SOLVER_WORKFLOW_REGISTRY
    from grace2_agent.workflows.run_geoclaw import (
        GEOCLAW_SOLVER_NAME,
        register_geoclaw_solver,
    )

    register_geoclaw_solver()  # idempotent
    assert GEOCLAW_SOLVER_NAME in SOLVER_WORKFLOW_REGISTRY


def test_run_geoclaw_inundation_typed_error_on_missing_bbox():
    import asyncio

    from grace2_agent.tools.run_geoclaw_tool import run_geoclaw_inundation

    out = asyncio.run(run_geoclaw_inundation(bbox=None))
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "GEOCLAW_PARAMS_INCOMPLETE"

    out2 = asyncio.run(run_geoclaw_inundation(bbox="garbage"))
    assert out2["status"] == "error"
    assert out2["error_code"] == "GEOCLAW_PARAMS_INVALID"


# ===========================================================================
# (4) postprocess on a SYNTHETIC fort.q frame (upload stubbed).
# ===========================================================================
def _synthetic_fort_q(mx: int, my: int, bbox, depth_fn, level: int = 1) -> str:
    """Build a GeoClaw fort.qNNNN frame text for a single AMR patch.

    Format: 8 header lines ("<value>    <name>"), a blank line, then mx*my data
    rows of "h hu hv" with i (x) inner, j (y) outer ascending; a blank line
    separates consecutive j-rows. ``depth_fn(i, j) -> h`` supplies q[0].
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    dx = (max_lon - min_lon) / mx
    dy = (max_lat - min_lat) / my
    lines = [
        "1    grid_number",
        f"{level}    AMR_level",
        f"{mx}    mx",
        f"{my}    my",
        f"{min_lon:.8f}    xlow",
        f"{min_lat:.8f}    ylow",
        f"{dx:.8f}    dx",
        f"{dy:.8f}    dy",
        "",
    ]
    for j in range(my):
        for i in range(mx):
            h = depth_fn(i, j)
            lines.append(f"{h:.6e}  0.0  0.0")
        lines.append("")  # blank line ends a j-row block
    return "\n".join(lines) + "\n"


def test_parse_fort_q_frame_reads_patch_depths():
    from grace2_agent.workflows.postprocess_geoclaw import parse_fort_q_frame

    # a 4x3 patch with depth = i + 10*j so each cell is identifiable.
    text = _synthetic_fort_q(4, 3, _AOI, lambda i, j: float(i + 10 * j))
    patches = parse_fort_q_frame(text)
    assert len(patches) == 1
    p = patches[0]
    assert (p.mx, p.my, p.level) == (4, 3, 1)
    # h[j, i] == i + 10*j (row 0 = ylow = south).
    assert p.h[0, 0] == pytest.approx(0.0)
    assert p.h[0, 3] == pytest.approx(3.0)
    assert p.h[2, 1] == pytest.approx(21.0)


def test_rasterize_and_metrics_on_synthetic_frame():
    from grace2_agent.workflows.postprocess_geoclaw import (
        compute_geoclaw_depth_metrics,
        parse_fort_q_frame,
        rasterize_frame_to_grid,
    )

    # half the patch wet at 2.0 m, half dry (0 -> masked).
    text = _synthetic_fort_q(
        8, 8, _AOI, lambda i, j: 2.0 if j >= 4 else 0.0
    )
    patches = parse_fort_q_frame(text)
    grid = rasterize_frame_to_grid(patches, _AOI, (32, 32))
    wet = np.isfinite(grid)
    assert wet.any()
    # all wet cells carry 2.0.
    assert np.nanmax(grid) == pytest.approx(2.0)
    m = compute_geoclaw_depth_metrics(grid, bbox=_AOI)
    assert m["max_depth_m"] == pytest.approx(2.0)
    assert m["flooded_cell_count"] > 0
    assert m["flooded_area_km2"] > 0.0


def _mk_patch(level, mx, my, xlow, ylow, dx, dy, depth_fn):
    """Build a _Patch directly (bypassing fort.q text) for rasterize tests."""
    from grace2_agent.workflows.postprocess_geoclaw import _Patch

    h = np.full((my, mx), np.nan, dtype="float64")
    for j in range(my):
        for i in range(mx):
            h[j, i] = depth_fn(i, j)
    return _Patch(level, mx, my, xlow, ylow, dx, dy, h)


def test_compute_geoclaw_grid_shape_adaptive_floor_and_cap():
    """Adaptive shape: ~8 km AOI -> ~300-400 px/side at 25 m; huge AOI capped;
    tiny AOI floored at the legacy 256 (never coarser)."""
    from grace2_agent.workflows.postprocess_geoclaw import (
        GEOCLAW_MAX_PX_PER_SIDE,
        GEOCLAW_MAX_TOTAL_CELLS,
        GEOCLAW_MIN_PX_PER_SIDE,
        compute_geoclaw_grid_shape,
    )

    # ~8 km square AOI near 30N: 8 km / 25 m = 320 px/side (in the 300-400 band).
    lat0 = 30.0
    dlat = 8000.0 / 111_320.0
    import math

    dlon = 8000.0 / (111_320.0 * math.cos(math.radians(lat0)))
    aoi8 = (-85.0, lat0, -85.0 + dlon, lat0 + dlat)
    h, w = compute_geoclaw_grid_shape(aoi8)
    assert 300 <= h <= 400 and 300 <= w <= 400
    # Much denser than the legacy 256x256 speck grid.
    assert h * w > 256 * 256

    # Tiny AOI (~500 m): floored at 256, never coarser than the legacy grid.
    dlat_t = 500.0 / 111_320.0
    dlon_t = 500.0 / (111_320.0 * math.cos(math.radians(lat0)))
    aoi_tiny = (-85.0, lat0, -85.0 + dlon_t, lat0 + dlat_t)
    ht, wt = compute_geoclaw_grid_shape(aoi_tiny)
    assert (ht, wt) == (GEOCLAW_MIN_PX_PER_SIDE, GEOCLAW_MIN_PX_PER_SIDE)

    # Huge AOI (~5 deg): capped on both the per-side and total-cell limits.
    aoi_huge = (-90.0, 25.0, -85.0, 30.0)
    hh, wh = compute_geoclaw_grid_shape(aoi_huge)
    assert hh <= GEOCLAW_MAX_PX_PER_SIDE and wh <= GEOCLAW_MAX_PX_PER_SIDE
    assert hh * wh <= GEOCLAW_MAX_TOTAL_CELLS
    assert hh >= GEOCLAW_MIN_PX_PER_SIDE and wh >= GEOCLAW_MIN_PX_PER_SIDE


def test_rasterize_paint_fill_is_gap_free_and_finest_wins():
    """A COARSE patch painted onto a FINER output grid must produce NO interior
    NaN holes within its wetted footprint (paint-fill, not nearest-scatter); a
    finer nested patch OVERWRITES it in the overlap (finest-wins)."""
    from grace2_agent.workflows.postprocess_geoclaw import rasterize_frame_to_grid

    min_lon, min_lat, max_lon, max_lat = _AOI
    # Coarse level-1 patch (10x10) covering the whole AOI, uniformly wet at 1.0.
    coarse = _mk_patch(
        1, 10, 10, min_lon, min_lat,
        (max_lon - min_lon) / 10.0, (max_lat - min_lat) / 10.0,
        lambda i, j: 1.0,
    )
    # Finer level-2 patch (8x8) over the CENTRAL quarter, wet at 5.0.
    cx0 = min_lon + 0.25 * (max_lon - min_lon)
    cy0 = min_lat + 0.25 * (max_lat - min_lat)
    cw = 0.5 * (max_lon - min_lon)
    ch = 0.5 * (max_lat - min_lat)
    fine = _mk_patch(
        2, 8, 8, cx0, cy0, cw / 8.0, ch / 8.0, lambda i, j: 5.0,
    )

    # Output grid FINER than either patch (120x120 >> 10x10 / 8x8).
    grid = rasterize_frame_to_grid([coarse, fine], _AOI, (120, 120))

    # (a) GAP-FREE: the coarse patch fills the AOI interior -> no NaN holes.
    #     (Exclude the outermost ring: output-cell centres just inside the AOI
    #     edge still map into the patch, but be robust to boundary rounding.)
    interior = grid[1:-1, 1:-1]
    assert np.isfinite(interior).all(), (
        f"{int(np.isnan(interior).sum())} interior NaN holes (speckle) remain"
    )

    # (b) FINEST-WINS: the central quarter carries the finer patch's 5.0.
    assert np.nanmax(grid) == pytest.approx(5.0)
    cr = grid[45:75, 45:75]  # a block inside the central quarter
    assert np.allclose(cr, 5.0)

    # (c) The coarse-only margin still carries 1.0 (finer did not erase it).
    assert grid[5, 5] == pytest.approx(1.0)


def test_rasterize_paint_fill_beats_nearest_scatter_wet_count():
    """Before/after: at a finer-than-patch output grid the paint-fill fills the
    whole wetted footprint where a nearest-scatter would leave most cells NaN."""
    from grace2_agent.workflows.postprocess_geoclaw import (
        NODATA_DEPTH_M,
        rasterize_frame_to_grid,
    )

    min_lon, min_lat, max_lon, max_lat = _AOI
    # One coarse 16x16 patch, fully wet, over the whole AOI.
    coarse = _mk_patch(
        1, 16, 16, min_lon, min_lat,
        (max_lon - min_lon) / 16.0, (max_lat - min_lat) / 16.0,
        lambda i, j: 2.0,
    )
    out_shape = (256, 256)
    grid = rasterize_frame_to_grid([coarse], _AOI, out_shape)
    wet_paint = int(np.isfinite(grid).sum())

    # A nearest-scatter (one output cell per patch cell) tops out at 16*16=256
    # wet cells; the paint-fill covers ~ the whole 256x256 output (65536).
    n_patch_cells = 16 * 16
    assert wet_paint > 10 * n_patch_cells
    # Gap-free interior (the holey scatter symptom is gone).
    assert np.isfinite(grid[1:-1, 1:-1]).all()
    del NODATA_DEPTH_M  # (imported to document the wet threshold used above)


def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="x.tif"):
    # assert the COG is a valid EPSG:4326 raster before "uploading".
    import rasterio

    with rasterio.open(local_cog) as ds:
        assert str(ds.crs) == "EPSG:4326"
        assert ds.count == 1
    return f"s3://fake-runs/{run_id}/{dest_filename}"


def test_postprocess_geoclaw_end_to_end_shape(tmp_path: Path):
    """A multi-frame synthetic run yields the EXACT (layers, metrics) shape:
    peak primary + contiguous 'Flood depth step N' frames, all VALID COGs."""
    from grace2_agent.workflows import postprocess_geoclaw as pg

    out = tmp_path / "_output"
    out.mkdir()
    # 5 frames; depth rises then falls so the peak is the middle frame.
    amps = [0.5, 1.5, 3.0, 1.0, 0.2]
    for fi, amp in enumerate(amps):
        text = _synthetic_fort_q(
            10, 10, _AOI, (lambda a: (lambda i, j: a if (i + j) % 2 == 0 else 0.0))(amp)
        )
        (out / f"fort.q{fi:04d}").write_text(text)
        (out / f"fort.t{fi:04d}").write_text(f"{fi * 60.0:.6e}    time\n")

    with patch.object(pg, "_upload_cog_to_runs_bucket", _fake_upload):
        layers, metrics = pg.postprocess_geoclaw(
            tmp_path, _AOI, run_id="RID123", scenario="dam_break", grid_shape=(40, 40)
        )

    # layers[0] = peak primary.
    peak = layers[0]
    assert isinstance(peak, GeoClawDepthLayerURI)
    assert peak.role == "primary"
    assert peak.name == "Peak flood depth"
    assert peak.style_preset == "continuous_flood_depth"
    assert peak.scenario == "dam_break"
    assert peak.max_depth_m == pytest.approx(3.0)  # the middle frame amplitude
    assert peak.uri.startswith("s3://fake-runs/RID123/geoclaw_depth_peak.tif")
    assert metrics["max_depth_m"] == pytest.approx(3.0)
    # No fgmax in this fixture -> read_fgmax_output returns None: fort.q metrics
    # are unchanged and arrival is HONESTLY None (never a fabricated time).
    assert peak.arrival_time_s is None
    assert metrics["arrival_time_s"] is None

    # layers[1:] = contiguous 'Flood depth step N' frames, distinct URIs.
    frames = layers[1:]
    assert len(frames) >= 2
    uris = set()
    for n, fr in enumerate(frames, start=1):
        assert fr.role == "context"
        assert fr.name == f"Flood depth step {n}"
        assert _WEB_STEP_TOKEN_RE.search(fr.name)
        uris.add(fr.uri)
    assert len(uris) == len(frames)  # distinct keys -> no dedup collapse


def test_postprocess_geoclaw_empty_output_raises(tmp_path: Path):
    from grace2_agent.workflows.postprocess_geoclaw import (
        PostprocessGeoClawError,
        postprocess_geoclaw,
    )

    with pytest.raises(PostprocessGeoClawError) as ei:
        postprocess_geoclaw(tmp_path, _AOI, run_id="X", scenario="dam_break")
    assert ei.value.error_code == "GEOCLAW_OUTPUT_EMPTY"


# ===========================================================================
# (4b) fgmax (fixed-grid maximum) reader + override (GAP1).
# ===========================================================================
# A SENTINEL: GeoClaw stamps |t| > 1e8 in a time column for a never-arrived point.
_FGMAX_SENTINEL = 9.999_999_999e9
# The REAL GeoClaw never-SET sentinel (FG_NOTSET, fgmax_module.f90): every fgmax
# valuemax (h, B, tmax, arrival) is initialized to this and only overwritten where
# the wave updated it. It is FINITE and NEGATIVE -> must be masked or it poisons
# nanmax(h) into a negative max_depth_m.
_FG_NOTSET = -0.99999e99


def _synthetic_fgmax(rows: list[tuple]) -> str:
    """Build a real-format fgmax{NNNN}.txt body (num_fgmax_val=2 -> 9 columns).

    Columns: x y amr_level B h s t_hmax t_smax arrival_time. Each row is a tuple
    in that order. A leading ``#`` comment header mirrors a real GeoClaw file
    (np.loadtxt(comments='#') skips it).
    """
    lines = ["# x y level B h s t_hmax t_smax arrival_time"]
    for (x, y, lvl, B, h, s, t_h, t_s, t_a) in rows:
        lines.append(
            f"{x:.6f} {y:.6f} {int(lvl)} {B:.6f} {h:.6f} {s:.6f} "
            f"{t_h:.6e} {t_s:.6e} {t_a:.6e}"
        )
    return "\n".join(lines) + "\n"


def _write_fgmax_fixture(out: Path, rows: list[tuple]) -> None:
    """Write _output/fgmax0001.txt + _output/fgmax_grids.data under ``out``."""
    sub = out / "_output"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "fgmax0001.txt").write_text(_synthetic_fgmax(rows))
    # The grids header need only EXIST for the reader to proceed (it reads the
    # txt, not this file); a minimal stub mirrors a real fgmax_grids.data.
    (sub / "fgmax_grids.data").write_text(
        "# fgmax grid geometry stub\n1    num_fgmax_grids\n"
    )


def test_read_fgmax_output_parses_depth_and_arrival(tmp_path: Path):
    """read_fgmax_output maps the 9-col layout -> max_depth/inundation/arrival,
    with never-arrived sentinels -> NaN (so they do NOT poison the min)."""
    from grace2_agent.workflows.postprocess_geoclaw import read_fgmax_output

    # rows: (x, y, level, B, h, s, t_hmax, t_smax, arrival_time)
    rows = [
        # offshore (B<0), wave arrives at t=120 s, depth 4.0 m.
        (-85.50, 29.90, 2, -3.0, 4.0, 1.2, 130.0, 130.0, 120.0),
        # on land (B>0), inundated 2.5 m, arrives at t=300 s (the run-up).
        (-85.45, 29.95, 2, 1.5, 2.5, 0.8, 305.0, 305.0, 300.0),
        # on land, deeper run-up 3.1 m, arrives LATER at t=360 s.
        (-85.44, 29.96, 2, 0.8, 3.1, 0.9, 365.0, 365.0, 360.0),
        # never-arrived dry point: sentinel time + zero depth -> NaN, excluded.
        (-85.40, 30.10, 1, 5.0, 0.0, 0.0, _FGMAX_SENTINEL, _FGMAX_SENTINEL,
         _FGMAX_SENTINEL),
        # another never-arrived point, NEGATIVE sentinel time -> NaN.
        (-85.39, 30.11, 1, 6.0, 0.0, 0.0, -_FGMAX_SENTINEL, -_FGMAX_SENTINEL,
         -1.0),
    ]
    _write_fgmax_fixture(tmp_path, rows)

    res = read_fgmax_output(tmp_path)
    assert res is not None
    # max depth over ALL points = 4.0 (the offshore peak).
    assert res["max_depth_m"] == pytest.approx(4.0)
    # inundation = max depth on LAND (B>0) = 3.1.
    assert res["max_inundation_m"] == pytest.approx(3.1)
    # earliest arrival among WET points = 120 s (sentinels mapped to NaN so the
    # never-arrived rows do not collapse the min to a bogus huge/negative value).
    assert res["arrival_time_s"] == pytest.approx(120.0)
    # the arrival grid carries NaN at the two sentinel rows.
    assert np.isnan(res["grid"]["arrival_time"][3])
    assert np.isnan(res["grid"]["arrival_time"][4])


def test_read_fgmax_output_all_never_set_is_zero_not_negative(tmp_path: Path):
    """REGRESSION (adversarial review): an fgmax grid where NO point was ever set
    (every h/B/arrival == FG_NOTSET = -0.99999e99) must yield max_depth_m == 0.0
    and arrival None -- NOT a huge negative depth that crashes the
    GeoClawDepthLayerURI(ge=0.0) validator."""
    from grace2_agent.workflows.postprocess_geoclaw import read_fgmax_output

    rows = [
        (-85.50, 29.90, 1, _FG_NOTSET, _FG_NOTSET, _FG_NOTSET, _FG_NOTSET,
         _FG_NOTSET, _FG_NOTSET),
        (-85.45, 29.95, 1, _FG_NOTSET, _FG_NOTSET, _FG_NOTSET, _FG_NOTSET,
         _FG_NOTSET, _FG_NOTSET),
    ]
    _write_fgmax_fixture(tmp_path, rows)

    res = read_fgmax_output(tmp_path)
    assert res is not None
    assert res["max_depth_m"] == 0.0  # masked, not -9.9999e98
    assert res["max_inundation_m"] == 0.0
    assert res["arrival_time_s"] is None
    # the masked depth/B are NaN, not the raw FG_NOTSET.
    assert np.isnan(res["grid"]["h"]).all()


def test_read_fgmax_output_absent_returns_none(tmp_path: Path):
    """No fgmax file (dam_break / surge / fgmax disabled) -> None, NOT an error."""
    from grace2_agent.workflows.postprocess_geoclaw import read_fgmax_output

    (tmp_path / "_output").mkdir()
    assert read_fgmax_output(tmp_path) is None


def test_postprocess_geoclaw_fgmax_overrides_fortq(tmp_path: Path):
    """With fgmax present, postprocess OVERRIDES the fort.q max_depth with the
    fgmax between-frame peak and sets arrival_time_s on the peak layer."""
    from grace2_agent.workflows import postprocess_geoclaw as pg

    out = tmp_path / "_output"
    out.mkdir()
    # fort.q frames whose peak is 2.0 m (the discrete snapshot peak).
    amps = [0.5, 2.0, 1.0]
    for fi, amp in enumerate(amps):
        text = _synthetic_fort_q(
            8, 8, _AOI, (lambda a: (lambda i, j: a if (i + j) % 2 == 0 else 0.0))(amp)
        )
        (out / f"fort.q{fi:04d}").write_text(text)
        (out / f"fort.t{fi:04d}").write_text(f"{fi * 60.0:.6e}    time\n")

    # fgmax records a HIGHER true between-frame peak (5.5 m) + an arrival time.
    rows = [
        (-85.50, 29.90, 2, -2.0, 5.5, 1.5, 95.0, 95.0, 90.0),
        (-85.45, 29.95, 2, 1.0, 3.0, 0.7, 200.0, 200.0, 180.0),
        (-85.40, 30.10, 1, 4.0, 0.0, 0.0, _FGMAX_SENTINEL, _FGMAX_SENTINEL,
         _FGMAX_SENTINEL),
    ]
    _write_fgmax_fixture(tmp_path, rows)

    with patch.object(pg, "_upload_cog_to_runs_bucket", _fake_upload):
        layers, metrics = pg.postprocess_geoclaw(
            tmp_path, _AOI, run_id="FGRID", scenario="tsunami", grid_shape=(32, 32)
        )

    peak = layers[0]
    # fort.q peak (2.0) is OVERRIDDEN by the fgmax between-frame peak (5.5).
    assert metrics["max_depth_m"] == pytest.approx(5.5)
    assert peak.max_depth_m == pytest.approx(5.5)
    # inundation = land peak (3.0); arrival = earliest wet arrival (90 s).
    assert metrics["max_inundation_m"] == pytest.approx(3.0)
    assert metrics["arrival_time_s"] == pytest.approx(90.0)
    assert peak.arrival_time_s == pytest.approx(90.0)


# ===========================================================================
# (4c) Overland ocean mask (INITIAL-WET -> nodata) for offshore/coastal scenarios.
#
# The ocean (permanent water) is the set of cells WET AT t=0 (grids[0] = GeoClaw's
# still-water initial condition). Overland inundation = cells DRY at t=0 that are
# wet in a later frame. This is robust on ANY coast (it replaced the old `topo<0`
# test that FAILED on ETOPO coasts where nearshore bathy reads ~0 m, not negative).
# ===========================================================================
def _write_coastal_run(
    out: Path,
    n_frames: int = 3,
    *,
    ocean_depth: float = 10.0,
    land_depth: float = 2.0,
    land_wet_at_t0: bool = False,
) -> None:
    """Write a REALISTIC coastal fort.q run over ``_AOI`` (32x32 patch).

    Column split: WEST half (i<16) = LAND, EAST half (i>=16) = OCEAN. Frame 0 is
    the STILL-WATER initial condition: the ocean is wet (``ocean_depth``) and the
    land is DRY (unless ``land_wet_at_t0``). Every LATER frame wets the land
    (``land_depth``) — the inundation — while the ocean stays wet. So the
    initial-wet criterion tags exactly the EAST (ocean) band as permanent water and
    the WEST (land) band as newly-wetted overland inundation.
    """
    out.mkdir(parents=True, exist_ok=True)
    for fi in range(n_frames):
        land_here = land_depth if (fi > 0 or land_wet_at_t0) else 0.0

        def _depth(i, j, _lh=land_here):  # noqa: ANN001
            return _lh if i < 16 else ocean_depth

        (out / f"fort.q{fi:04d}").write_text(_synthetic_fort_q(32, 32, _AOI, _depth))
        (out / f"fort.t{fi:04d}").write_text(f"{fi * 60.0:.6e}    time\n")


def _split_topo(land_west: float = 5.0, ocean_east: float = -5.0) -> "np.ndarray":
    """A (32,32) topo grid: WEST half (col<16) = land elevation, EAST half = ocean,
    aligned cell-for-cell with the split depth run above."""
    topo = np.empty((32, 32), dtype="float64")
    topo[:, :16] = land_west
    topo[:, 16:] = ocean_east
    return topo


def _capturing_upload():
    """An upload stub that reads back each written COG into a name->array dict AND a
    name->nodata dict (masked cells read as NaN) so a test can assert the published
    pixels + that an explicit nodata value is stamped on the COG."""
    captured: dict[str, "np.ndarray"] = {}
    nodata: dict[str, "float | None"] = {}

    def _upload(local_cog, run_id, runs_bucket=None, *, dest_filename="x.tif"):  # noqa: ANN001
        import rasterio

        with rasterio.open(local_cog) as ds:
            assert str(ds.crs) == "EPSG:4326"
            captured[dest_filename] = ds.read(1)
            nodata[dest_filename] = ds.nodata
        return f"s3://fake-runs/{run_id}/{dest_filename}"

    return captured, nodata, _upload


def test_ocean_mask_publishes_overland_only(tmp_path: Path):
    """mask_ocean=True (tsunami/surge): the published PEAK + FRAME COGs are NaN over
    the INITIALLY-WET ocean band and PRESERVED over the newly-wetted LAND; metrics
    reflect the OVERLAND field (land run-up depth + smaller flooded area); an
    explicit nodata value is stamped on every COG."""
    from grace2_agent.workflows import postprocess_geoclaw as pg

    _write_coastal_run(tmp_path / "_output", n_frames=3)
    topo = _split_topo()
    captured, nodata, upload = _capturing_upload()

    with patch.object(pg, "_upload_cog_to_runs_bucket", upload):
        layers, metrics = pg.postprocess_geoclaw(
            tmp_path,
            _AOI,
            run_id="MASKRID",
            scenario="tsunami",
            grid_shape=(32, 32),
            topo_grid=topo,
            mask_ocean=True,
        )

    peak_arr = captured["geoclaw_depth_peak.tif"]
    # EAST half (ocean, wet at t=0) masked to nodata (NaN); WEST (land) kept 2.0.
    assert np.isnan(peak_arr[:, 16:]).all()
    assert np.allclose(peak_arr[:, :16], 2.0, equal_nan=False)
    # Explicit nodata stamped on the COG (matches postprocess_flood's NaN nodata).
    assert nodata["geoclaw_depth_peak.tif"] is not None
    assert np.isnan(nodata["geoclaw_depth_peak.tif"])
    # A per-FRAME COG is masked identically (peak + frames consistent): the EAST
    # ocean band is ALWAYS nodata; the WEST land is dry (nan) at t=0 and 2.0 once
    # inundated — never the masked ocean 10.0.
    frame_keys = [k for k in captured if k.startswith("geoclaw_depth_frame_")]
    assert frame_keys
    for k in frame_keys:
        assert np.isnan(captured[k][:, 16:]).all()
        west = captured[k][:, :16]
        finite = west[np.isfinite(west)]
        assert finite.size == 0 or np.allclose(finite, 2.0)
        assert nodata[k] is not None and np.isnan(nodata[k])
    # Metrics reflect OVERLAND: max_depth is the LAND run-up (2.0), NOT the masked
    # ocean 10.0; flooded area counts only the 32x16 land cells.
    assert metrics["max_depth_m"] == pytest.approx(2.0)
    assert metrics["max_inundation_m"] == pytest.approx(2.0)
    assert metrics["flooded_cell_count"] == 32 * 16
    assert layers[0].max_depth_m == pytest.approx(2.0)


def test_ocean_mask_initial_wet_catches_zero_topo_ocean(tmp_path: Path):
    """THE EXACT BUG: on an ETOPO coast the nearshore ocean bathymetry reads ~0 m
    (topo NOT < 0), so the old `topo<0` mask caught ~none of the ocean. The
    initial-wet criterion tags it correctly — the ocean band (wet at t=0) is masked
    even though its topo is 0.0. Before/after: `topo<0` alone masks 0 cells; the
    initial-wet mask removes the whole 32x16 ocean band."""
    from grace2_agent.workflows import postprocess_geoclaw as pg

    _write_coastal_run(tmp_path / "_output", n_frames=3)
    # Ocean topo reads ~0 m (flat, NOT negative) — the ETOPO-coast failure mode.
    topo = _split_topo(land_west=5.0, ocean_east=0.0)

    # Sanity: the OLD `topo<0` criterion would catch ZERO ocean cells here.
    assert int((topo < 0.0).sum()) == 0

    captured, nodata, upload = _capturing_upload()
    with patch.object(pg, "_upload_cog_to_runs_bucket", upload):
        _layers, metrics = pg.postprocess_geoclaw(
            tmp_path,
            _AOI,
            run_id="ZEROTOPO",
            scenario="tsunami",
            grid_shape=(32, 32),
            topo_grid=topo,
            mask_ocean=True,
        )

    peak_arr = captured["geoclaw_depth_peak.tif"]
    masked_fraction = float(np.isnan(peak_arr).mean())
    # The initial-wet mask removed the whole EAST ocean band (topo<0 would remove 0).
    assert np.isnan(peak_arr[:, 16:]).all()
    assert np.allclose(peak_arr[:, :16], 2.0)
    assert masked_fraction == pytest.approx(0.5, abs=1e-6)  # 32x16 of 32x32
    assert metrics["max_depth_m"] == pytest.approx(2.0)  # land run-up, not ocean 10
    assert metrics["flooded_cell_count"] == 32 * 16


def test_ocean_mask_topo_or_catches_below_datum_dry_start(tmp_path: Path):
    """Belt-and-suspenders OR: a below-datum (topo<0) cell that is DRY at t=0 but
    floods later is still ocean and must be masked (the CUDEM regression guard). The
    initial-wet criterion alone would MISS it (dry at t=0); the `topo<0` OR-term
    catches it."""
    from grace2_agent.workflows import postprocess_geoclaw as pg

    # EAST ocean is DRY at t=0 (land_wet_at_t0=False + we make ocean start dry by
    # writing land-only frame 0), but topo marks the east as below-datum sea.
    out = tmp_path / "_output"
    out.mkdir(parents=True, exist_ok=True)
    # Frame 0: EVERYTHING dry (no standing water at all). Later: both sides wet.
    for fi in range(3):
        depth = (lambda i, j: 0.0) if fi == 0 else (
            lambda i, j: 2.0 if i < 16 else 10.0
        )
        (out / f"fort.q{fi:04d}").write_text(_synthetic_fort_q(32, 32, _AOI, depth))
        (out / f"fort.t{fi:04d}").write_text(f"{fi * 60.0:.6e}    time\n")

    topo = _split_topo(land_west=5.0, ocean_east=-5.0)  # east below datum
    captured, nodata, upload = _capturing_upload()
    with patch.object(pg, "_upload_cog_to_runs_bucket", upload):
        _layers, metrics = pg.postprocess_geoclaw(
            tmp_path,
            _AOI,
            run_id="BELOWDATUM",
            scenario="surge",
            grid_shape=(32, 32),
            topo_grid=topo,
            mask_ocean=True,
        )

    peak_arr = captured["geoclaw_depth_peak.tif"]
    # initial-wet is EMPTY (frame 0 all dry), but topo<0 OR masks the east band.
    assert np.isnan(peak_arr[:, 16:]).all()
    assert np.allclose(peak_arr[:, :16], 2.0)
    assert metrics["flooded_cell_count"] == 32 * 16


def test_ocean_mask_noop_when_no_permanent_water(tmp_path: Path):
    """mask_ocean=True but NO permanent water (no cell wet at t=0 AND all topo>=0):
    a strict NO-OP — every later-wetted cell is preserved, nothing masked."""
    from grace2_agent.workflows import postprocess_geoclaw as pg

    # Frame 0 all dry; later frames wet the whole domain (a pure inland flood, no sea).
    out = tmp_path / "_output"
    out.mkdir(parents=True, exist_ok=True)
    for fi in range(3):
        depth = (lambda i, j: 0.0) if fi == 0 else (
            lambda i, j: 2.0 if i < 16 else 10.0
        )
        (out / f"fort.q{fi:04d}").write_text(_synthetic_fort_q(32, 32, _AOI, depth))
        (out / f"fort.t{fi:04d}").write_text(f"{fi * 60.0:.6e}    time\n")

    topo = _split_topo(land_west=5.0, ocean_east=3.0)  # BOTH sides land (>=0)
    captured, nodata, upload = _capturing_upload()

    with patch.object(pg, "_upload_cog_to_runs_bucket", upload):
        _layers, metrics = pg.postprocess_geoclaw(
            tmp_path,
            _AOI,
            run_id="NOOPRID",
            scenario="surge",
            grid_shape=(32, 32),
            topo_grid=topo,
            mask_ocean=True,
        )

    peak_arr = captured["geoclaw_depth_peak.tif"]
    assert not np.isnan(peak_arr).any()  # nothing masked
    assert metrics["max_depth_m"] == pytest.approx(10.0)
    assert metrics["flooded_cell_count"] == 32 * 32


def test_ocean_mask_off_preserves_inland_flood(tmp_path: Path):
    """dam_break (mask_ocean=False): the mask is NOT applied, so the legitimate
    inland flood over EVERY cell (incl. the initially-wet 10.0 side) is preserved
    (the mask can never erase an inland flood)."""
    from grace2_agent.workflows import postprocess_geoclaw as pg

    _write_coastal_run(tmp_path / "_output", n_frames=3)
    topo = _split_topo(land_west=5.0, ocean_east=-5.0)
    captured, nodata, upload = _capturing_upload()

    with patch.object(pg, "_upload_cog_to_runs_bucket", upload):
        _layers, metrics = pg.postprocess_geoclaw(
            tmp_path,
            _AOI,
            run_id="INLANDRID",
            scenario="dam_break",
            grid_shape=(32, 32),
            topo_grid=topo,
            mask_ocean=False,  # composer keeps this False for dam_break
        )

    peak_arr = captured["geoclaw_depth_peak.tif"]
    # Nothing masked: the east (10.0) and the wetted west (2.0) are BOTH preserved.
    assert not np.isnan(peak_arr).any()
    assert np.allclose(peak_arr[:, 16:], 10.0)
    assert np.allclose(peak_arr[:, :16], 2.0)
    assert metrics["max_depth_m"] == pytest.approx(10.0)
    assert metrics["flooded_cell_count"] == 32 * 32


# ===========================================================================
# (5) Composer arg-assembly with run_solver / wait_for_completion MOCKED.
# ===========================================================================
class _FakeHandle:
    run_id = "BATCHRID"
    workflow_name = "aws-batch"


class _FakeRunResult:
    run_id = "BATCHRID"
    status = "complete"
    output_uri = "s3://runs/BATCHRID/"
    error_code = None
    error_message = None
    cancellation_reason = None
    batch_compute_meta = {"instance_type": "c7i.2xlarge"}


def test_composer_arg_assembly_and_dispatch(tmp_path: Path):
    """The composer stages a manifest, dispatches via a MOCKED run_solver, and
    returns the peak GeoClawDepthLayerURI — no AWS, no Clawpack. Asserts the
    run_solver call carries solver='geoclaw' + the staged manifest_uri."""
    import asyncio

    from grace2_agent.workflows import model_dambreak_geoclaw_scenario as comp
    from grace2_agent.workflows.run_geoclaw import GeoClawStaging

    run_args = GeoClawRunArgs(bbox=_AOI, scenario="dam_break", output_frames=4)

    captured: dict = {}

    def _fake_stage(ra, *, dem_uri, run_id=None, dtopo_uri=None, surge_uri=None,
                    extra_dem_uris=None, base_num_cells=(40, 40),
                    domain_bbox=None, source_lonlat_override=None,
                    amr_levels_override=None):
        captured["dem_uri"] = dem_uri
        captured["extra_dem_uris"] = extra_dem_uris
        captured["domain_bbox"] = domain_bbox
        captured["source_lonlat_override"] = source_lonlat_override
        captured["base_num_cells"] = base_num_cells
        captured["amr_levels_override"] = amr_levels_override
        return GeoClawStaging(
            run_id="STAGERID",
            manifest_uri="s3://cache/geoclaw_setup/STAGERID/manifest.json",
            build_spec={"scenario": "dam_break"},
            run_args=ra,
            bbox=tuple(ra.bbox),
            n_active_cells=1600,
        )

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):
        captured["solver"] = solver
        captured["model_setup_uri"] = model_setup_uri
        captured["compute_class"] = compute_class
        return _FakeHandle()

    async def _fake_wait(handle):
        return _FakeRunResult()

    def _fake_download(run_id):
        return str(tmp_path)  # a dir with no _output -> postprocess is mocked too

    def _fake_postprocess(out_dir, bbox, *, run_id, scenario,
                          fgmax_arrival_tol_m=0.01, **_kw):
        captured["fgmax_arrival_tol_m"] = fgmax_arrival_tol_m
        peak = GeoClawDepthLayerURI(
            layer_id=f"geoclaw-depth-peak-{run_id}",
            name="Peak flood depth",
            layer_type="raster",
            uri=f"s3://runs/{run_id}/geoclaw_depth_peak.tif",
            style_preset=GEOCLAW_DEPTH_STYLE_PRESET,
            role="primary",
            units="meters",
            bbox=tuple(bbox),
            max_depth_m=3.3,
            flooded_area_km2=2.1,
            max_inundation_m=1.4,
            arrival_time_s=None,
            scenario=scenario,
        )
        return [peak], {"max_depth_m": 3.3, "arrival_time_s": None}

    def _fake_publish(raw_peak, run_id):
        return raw_peak.model_copy(update={"uri": "https://tiles/peak.png"})

    # The composer imports run_solver / wait_for_completion / EmitterBinding /
    # set_emitter_binding INSIDE the function (from ..tools.solver import ...), so
    # they must be patched at the SOURCE module, not on the composer module.
    from grace2_agent.tools import solver as solver_mod

    with patch.object(comp, "_fetch_topo_for_geoclaw", lambda b, **k: "s3://cache/topo.tif"), \
         patch.object(comp, "stage_geoclaw_manifest", _fake_stage), \
         patch.object(solver_mod, "run_solver", _fake_run_solver), \
         patch.object(solver_mod, "wait_for_completion", _fake_wait), \
         patch.object(solver_mod, "set_emitter_binding", lambda *a, **k: None), \
         patch.object(comp, "mint_dispatch_and_sim_cards", _amock(None)), \
         patch.object(comp, "route_sim_terminal", _amock(None)), \
         patch.object(comp, "_download_batch_geoclaw_outputs", _fake_download), \
         patch.object(comp, "postprocess_geoclaw", _fake_postprocess), \
         patch.object(comp, "_publish_peak_layer", _fake_publish), \
         patch.object(comp, "current_emitter", lambda: None), \
         patch.object(comp, "drive_live_solve_progress", _amock(None)):
        peak = asyncio.run(comp.model_dambreak_geoclaw_scenario(run_args))

    assert isinstance(peak, GeoClawDepthLayerURI)
    assert peak.uri == "https://tiles/peak.png"
    assert peak.max_depth_m == pytest.approx(3.3)
    assert captured["solver"] == "geoclaw"
    assert captured["model_setup_uri"].endswith("manifest.json")
    assert captured["dem_uri"] == "s3://cache/topo.tif"
    # dam_break keeps domain == AOI and never relocates the source offshore.
    assert tuple(captured["domain_bbox"]) == tuple(_AOI)
    assert captured["source_lonlat_override"] is None


def _amock(ret):
    """Build an async no-op returning ``ret`` (for the emitter helpers)."""
    async def _inner(*a, **k):
        return ret
    return _inner
