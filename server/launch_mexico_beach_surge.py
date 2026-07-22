"""HEADLESS launch of the Mexico Beach SURGE-ONLY inundation run.

Drives the EXISTING coastal deck-build seam directly (no live agent box, no
chatbot, no LLM): fetch_topobathy -> _synthesize_parametric_surge_forcing ->
build_sfincs_model (HydroMT, NLCD gate) -> a regular-grid SFINCS solve.

Surge-only (SnapWave OFF) is the goal: a clean bathtub comparison with no wave
field contaminating the inundation extent.

------------------------------------------------------------------------------
DECK-BUILD PATH FINDING (load-bearing -- read before re-running):

  The combined ``grace2-sfincs-quadtree`` Batch worker is NOT a viable surge-only
  path. Its SFINCS binary is a SnapWave-enabled build (Build-Revision
  ``SnapWave_IG``) with ``snapwave=1`` baked into the deck, so it REQUIRES a
  ``snapwave.bnd`` file even when ``is_coastal=False`` suppresses the wave
  boundary -- the worker writes no ``snapwave.bnd`` and SFINCS aborts with
  "snapwave_bnd file not found! SFINCS has stopped!" (exit 2). PROVEN live:
  quadtree run 01KVVT6F0HAJ4F3ZQFBRYCFJW0 / job ed99eac8-... built the deck
  cleanly (42465 cells, surge waterlevel attached, "no SnapWave boundary points")
  but the solve died on the missing snapwave.bnd.

  THE WORKING SURGE-ONLY PATH (what this driver does): build the REGULAR-grid
  HydroMT deck (``build_sfincs_model`` -> ``model_setup.setup_uri`` manifest, a
  plain SFINCS deck with ``sfincs.bnd`` + ``sfincs.bzs`` and NO snapwave keyword)
  and submit it on the REGULAR ``grace2-sfincs`` job-def via ``run_solver`` --
  the plain SFINCS binary has no SnapWave requirement. VERIFIED live: regular run
  01KVVX1PT7C19GV2NAR2W1XQMW / job 13cce910-... SUCCEEDED (exit 0), wrote
  sfincs_map.nc; postprocess_flood emitted flood_depth_peak.tif + 81 frames; the
  interior surge climbed 0.30 m -> 3.80 m and all 3 verifier parts PASS. (The
  earlier run 01KVVTF55Q... was all-but-inert: the deck had a bzs surge series but
  NO msk==2 water-level boundary cells, so the surge never entered -- fixed by the
  setup_mask_bounds emission + 10 h deck window + rising-limb forcing; see
  verify_mexico_beach_surge.py's VERIFIED LIVE RUN note.)

  This driver therefore: (a) builds the regular-grid surge deck via
  build_sfincs_model (30 m, autoscale, no subgrid, storm_surge forcing_type ->
  no precip component), then (b) submits the regular grace2-sfincs job.
------------------------------------------------------------------------------

Prints the submitted run-id so the verifier can be pointed at the completed run:
    python verify_mexico_beach_surge.py --verify-run-id <run-id> \\
        --topobathy <topobathy COG uri>

Requires (set inline by the caller from the discovered live infra):
  AWS creds (this machine's ~/.aws), us-west-2, the grace2-solvers queue, the
  grace2-sfincs job-def (regular), the runs + cache buckets.
"""
from __future__ import annotations

import asyncio
import os
import sys

# --- the scoped case (mirrors verify_mexico_beach_surge.SURGE_BBOX) ---
SURGE_BBOX = (-85.4250, 29.9300, -85.3950, 30.0050)
DURATION_HR = 10
RETURN_PERIOD_YR = 100
GRID_RESOLUTION_M = 30.0
OUTPUT_INTERVAL_MIN = 7.5


async def main() -> int:
    from trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry import (
        fetch_river_geometry,
    )
    from trid3nt_server.tools.fetchers.ocean.fetch_topobathy import fetch_topobathy
    from trid3nt_server.tools.fetchers.terrain.fetch_landcover import fetch_landcover
    from trid3nt_server.tools.simulation.solver import run_solver, wait_for_completion
    from trid3nt_server.workflows.model_flood_scenario import (
        _build_surge_forcing_members,
        _resolve_surge_forcing_from_fetchers,
        _synthesize_parametric_surge_forcing,
    )
    from trid3nt_server.workflows.sfincs_builder import (
        BuildOptions,
        ForcingSpec,
        build_sfincs_model,
    )

    bbox = SURGE_BBOX
    print("=== HEADLESS Mexico Beach SURGE-only deck-build + submit ===")
    print(f"  bbox: {bbox}  duration_hr={DURATION_HR}  RP={RETURN_PERIOD_YR}yr")

    # --- Step 1: real topobathy (CUDEM+3DEP NAVD88). FAIL LOUD if land-only. ---
    print("\n[1/5] fetch_topobathy (CUDEM+3DEP, 10 m)...")
    topo = fetch_topobathy(bbox, resolution_m=10)
    print(f"      uri={topo.uri}")
    print(f"      bathymetry_present={topo.bathymetry_present} "
          f"cudem_tiles={getattr(topo,'cudem_tile_count',None)}")
    if not topo.bathymetry_present:
        print("FAIL: topobathy degraded to land-only (bathymetry_present=False). "
              "A surge run needs real bathymetry. Aborting.")
        return 2

    # --- Step 2: strong synthetic surge boundary (~+3.5 m on +0.3 m tidal base) ---
    print("\n[2/5] _synthesize_parametric_surge_forcing (RP=100 -> ~+3.5 m peak)...")
    surge_wl = _synthesize_parametric_surge_forcing(
        bbox, duration_hr=float(DURATION_HR), return_period_yr=RETURN_PERIOD_YR
    )
    surge_forcing = {"waterlevel": surge_wl}
    print(f"      surge peak provenance: {surge_wl.get('_prov_peak_m')} m "
          f"(timeseries={surge_wl.get('timeseries_uri')!r})")
    surge_forcing = _resolve_surge_forcing_from_fetchers(
        surge_forcing, bbox, window_hours=float(DURATION_HR), data_sources=[]
    )
    _wl, _dq, _wind, _press = _build_surge_forcing_members(surge_forcing)
    if _wl is None:
        print("FAIL: surge waterlevel member did not materialise (no bzs). Aborting.")
        return 2

    # storm_surge forcing_type: a CLEAN surge-only deck. build_sfincs_model emits
    # NO precip component for storm_surge (only the pluvial_* branches add rain),
    # so the only driver is the bzs/bnd water-level boundary -> a clean bathtub
    # comparison with NO rainfall contaminating the inundation extent.
    forcing_spec = ForcingSpec(
        forcing_type="storm_surge",
        duration_hours=float(DURATION_HR),
        return_period_years=RETURN_PERIOD_YR,
        waterlevel=_wl,
        discharge=_dq,
        wind=_wind,
        pressure=_press,
    )

    # --- Step 3: build_sfincs_model (regular HydroMT build for grid params) ---
    print("\n[3/5] build_sfincs_model (HydroMT, 30 m, no subgrid, no obstacles)...")
    # The landcover fetch is required by build_sfincs_model's NLCD gate; the
    # river fetch is BEST-EFFORT (None is fine for a surge deck). Call the SAME
    # tool functions the workflow's _fetcher_chain uses so the inputs are
    # identical to a live run.
    options = BuildOptions(
        grid_resolution_m=GRID_RESOLUTION_M,
        simulation_hours=float(DURATION_HR),
        compute_class="standard",
        enable_subgrid=False,
        output_interval_min=OUTPUT_INTERVAL_MIN,
    )
    landcover_result = fetch_landcover(bbox, dataset="nlcd_2021")
    landcover_uri = landcover_result["layer"].uri
    nlcd_vintage_year = landcover_result.get("nlcd_vintage_year")
    try:
        river_uri = fetch_river_geometry(bbox, source="nhdplus_hr").uri
    except Exception as exc:  # noqa: BLE001 - river is best-effort for surge
        print(f"      (river fetch skipped: {type(exc).__name__}: {exc})")
        river_uri = None
    model_setup = build_sfincs_model(
        dem_uri=topo.uri,
        landcover_uri=landcover_uri,
        river_geometry_uri=river_uri,
        forcing=forcing_spec,
        bbox=bbox,
        options=options,
        nlcd_vintage_year=nlcd_vintage_year,
    )
    setup_uri = getattr(model_setup, "setup_uri", None)
    print(f"      model_setup.setup_uri={setup_uri}")
    if not setup_uri:
        print("FAIL: build_sfincs_model produced no staged setup_uri manifest.")
        return 2

    # --- Step 4: submit the REGULAR grace2-sfincs Batch job on the manifest ---
    # NOT the quadtree path: the combined grace2-sfincs-quadtree worker's SFINCS
    # binary requires snapwave.bnd (see the module docstring), so a surge-only
    # deck dies there. The regular grace2-sfincs worker runs the plain SFINCS
    # binary on the HydroMT manifest (sfincs.bnd + sfincs.bzs, no snapwave) -> a
    # clean surge-only solve. run_solver picks the job-def from
    # TRID3NT_AWS_BATCH_JOB_DEF_SFINCS / TRID3NT_AWS_BATCH_JOB_DEF.
    print("\n[4/5] run_solver('sfincs', manifest) -> submit to grace2-solvers...")
    handle = run_solver("sfincs", model_setup_uri=setup_uri, compute_class="standard")
    print(f"      SUBMITTED run_id={handle.run_id} "
          f"batch_jobId={handle.workflows_execution_id}")
    print(f"      output will be at: s3://{os.environ.get('TRID3NT_RUNS_BUCKET','?')}/"
          f"{handle.run_id}/")

    # --- Step 5: wait for completion (Ctrl-C after the run_id is printed is OK) ---
    print("\n[5/5] wait_for_completion (blocks for the Batch run; the run_id above "
          "is enough to verify later)...")
    result = await wait_for_completion(handle)
    print(f"\n  run_id={result.run_id} status={result.status} "
          f"output_uri={result.output_uri}")
    print("\n  VERIFY with:  python verify_mexico_beach_surge.py "
          f"--verify-run-id {result.run_id} --topobathy {topo.uri}")
    print("  (the regular worker writes sfincs_map.nc; run postprocess_flood to "
          "emit flood_depth_peak.tif + flood_depth_frame_NN.tif before verifying)")
    return 0 if result.status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
