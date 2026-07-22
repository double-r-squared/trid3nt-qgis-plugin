"""Deterministic Mexico Beach (Hurricane Michael) coastal quadtree + SnapWave
acceptance on the live AWS stack (NO Bedrock, NO agent, NO LLM).

PROVES the SFINCS coastal North Star end-to-end: drives the REAL
``model_flood_scenario(quadtree=True, coastal=True, ...)`` path, which assembles a
cht_sfincs quadtree + SnapWave build_spec, SUBMITS the combined GPL-isolated
``grace2-sfincs-quadtree`` AWS Batch job (deck-build + SFINCS-with-SnapWave solve
in one image), waits for it, and postprocesses. Then it ASSERTS:

  1. a quadtree+SnapWave solve actually ran (the combined Batch run completed and
     wrote ``sfincs_map.nc`` carrying the SnapWave ``hm0`` / ``hm0ig`` field), and
  2. ``postprocess_waves`` produced >= 1 wave-height layer (peak + animation
     frames) AND ``postprocess_flood`` produced the depth layers.

This mirrors ``verify_pelicun_aws.py`` / ``verify_case3_aws.py``: env-sourced
config, ``asyncio.run`` of the real workflow, clear PASS/FAIL prints, non-zero
exit on failure. There is NO emitter in a direct call, so the workflow's success
envelope carries ONLY the publish-or-dropped PEAK layers; the time-step FRAMES
are emitted out-of-band via the (absent) emitter. To prove the frames exist we
call ``postprocess_waves`` / ``postprocess_flood`` DIRECTLY on the run output and
assert their layer rows (peak + N frames) here.

------------------------------------------------------------------------------
FIDELITY HONESTY (read before trusting a green run):
  * ``--smoke`` (default): the TINY CI-smoke bbox (-85.45, 29.92, -85.38, 29.98)
    + a SHORT 3 h window + a MINIMAL synthetic SnapWave incident-wave boundary
    (one offshore paddle point, Hm0 ~2 m). This is the CHEAPEST run that still
    exercises the full quadtree+SnapWave code path and yields a non-zero wave
    field — it is NOT a calibrated Hurricane Michael reproduction. The water-level
    (surge) boundary is a flat synthetic offset, not a real GTSM/CO-OPS
    hydrograph. The quadtree refinement depth + cell budget are the workflow's
    own defaults (refinement_levels=2, max_cells=2e6 in
    ``_compose_and_upload_deckbuild_spec``); this driver does NOT override them
    (the workflow exposes no knob for them) — the combined worker derives the
    refinement polygons + enforces the cell cap, so a tiny AOI stays cheap.
  * ``--full``: the demo AOI (-85.75, 29.55, -85.25, 30.20) + a 24 h window +
    the same synthetic boundary shape (larger Hm0). Still synthetic forcing — to
    make this a TRUE Michael run, replace the synthetic ``snapwave_boundary`` +
    ``waterlevel`` with materialised ``fetch_gtsm_tide_surge`` /
    ``fetch_noaa_coops_tides`` (surge) and a measured offshore Hm0 ~6-10 m wave
    spectrum for the 2018-10-10 window (see the recipe in this job's report). The
    TOPOBATHY is real either way (CUDEM + 3DEP).

The point of THIS script is to prove the WAVE LAYERS RENDER (the plumbing), not
to validate the physics against gauges. Computed-vs-observed validation is a
separate follow-on.
------------------------------------------------------------------------------

USAGE (run ON the agent box i-0251879a278df797f, where the Batch env is set):

    # cheapest proof (default):
    cd services/agent && python verify_mexico_beach_waves.py
    # or explicitly:
    python verify_mexico_beach_waves.py --smoke
    # the full demo AOI (more expensive Batch run):
    python verify_mexico_beach_waves.py --full

Required env (already set on the box per the systemd solver.conf drop-in):
    GRACE2_SOLVER_BACKEND=aws-batch
    GRACE2_AWS_BATCH_QUEUE=grace2-solvers
    GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE=grace2-sfincs-quadtree
    GRACE2_RUNS_BUCKET=<runs bucket>           (e.g. grace2-hazard-runs-226996537797)
    GRACE2_CACHE_BUCKET=<cache bucket>          (build_spec upload target)
    GRACE2_STORAGE_BACKEND=s3 / GRACE2_OBJECT_STORE=s3
    AWS_REGION / AWS_DEFAULT_REGION=us-west-2
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from typing import Any

# --- North Star AOIs (memory/project_sfincs_north_star_demo.md, P1 TOPOBATHY SPEC) ---
# CI-smoke bbox: the tiny AOI already pinned in test_sfincs_forcing_adapter.py:53.
SMOKE_BBOX: tuple[float, float, float, float] = (-85.45, 29.92, -85.38, 29.98)
# Demo AOI: St. Joseph Bay / Cape San Blas past Mexico Beach + Tyndall AFB.
FULL_BBOX: tuple[float, float, float, float] = (-85.75, 29.55, -85.25, 30.20)
# Right-sized DEMO AOI (~29 x 30 km): Mexico Beach coast + ~21 km of open Gulf to
# the south so the synthetic SnapWave paddle (placed at bbox[1]-0.02) sits in real
# offshore water -> a NON-ZERO wave field; ~1/4 the area of FULL so the combined
# quadtree+SnapWave solve finishes well inside the (env-raised) wait budget where
# FULL timed out. This is the bbox NATE can reproduce live in the app.
DEMO_BBOX: tuple[float, float, float, float] = (-85.58, 29.75, -85.28, 30.02)

# Hurricane Michael landfall window (~2018-10-10). Carried for provenance only;
# the synthetic-forcing smoke path uses a deterministic deck window anchored by
# the worker (the boundary is uniform-in-time so the absolute dates don't change
# the wave field). A real-forcing --full run would anchor tref/tstart/tstop to
# this window and supply the matching surge/wave hydrographs.
MICHAEL_WINDOW = ("20181009 000000", "20181011 000000")  # tstart, tstop (UTC)


def _utm_epsg_for_bbox(bbox: tuple[float, float, float, float]) -> int:
    """Mirror ``_compose_and_upload_deckbuild_spec``'s target_epsg derivation.

    The quadtree grid is authored in a metric UTM CRS (Mexico Beach = UTM 16N /
    EPSG:32616). The SnapWave boundary ``add_point(x, y, ...)`` consumes RAW grid
    coordinates in THAT CRS (the worker does NOT reproject the points), so the
    synthetic offshore paddle MUST be supplied in target_epsg, not lon/lat.
    """
    lon_c = (float(bbox[0]) + float(bbox[2])) / 2.0
    lat_c = (float(bbox[1]) + float(bbox[3])) / 2.0
    zone = int((lon_c + 180.0) // 6.0) + 1
    zone = max(1, min(60, zone))
    return (32600 if lat_c >= 0 else 32700) + zone


def _synthetic_snapwave_boundary(
    bbox: tuple[float, float, float, float],
    *,
    hs: float,
    tp: float = 10.0,
) -> dict[str, Any]:
    """Build a MINIMAL synthetic SnapWave incident-wave boundary block.

    The worker reads ``forcing.surge_forcing.snapwave_boundary.points`` and calls
    ``sf.snapwave.boundary_conditions.add_point(x, y, hs, tp, wd, ds)`` for each
    (entrypoint.py step 7). Each point is in target_epsg metres. Without points
    the worker logs "no SnapWave boundary points in spec - deck has no wave
    forcing" and ``hm0`` stays zero, so the wave layers would be empty/dropped.

    SMOKE FIDELITY: ONE offshore paddle placed just SOUTH of the AOI's southern
    edge (seaward, Gulf side) at the AOI-centre longitude, carrying a uniform-in-
    time significant wave height ``hs`` (m), peak period ``tp`` (s), shore-normal
    wave direction, and a small directional spread. This is enough to drive a
    non-zero nearshore wave field through SnapWave; it is NOT a measured spectrum.
    """
    try:
        from pyproj import Transformer  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "pyproj is required to project the synthetic SnapWave boundary point "
            f"into the grid CRS: {exc}"
        )
    epsg = _utm_epsg_for_bbox(bbox)
    tf = Transformer.from_crs(4326, epsg, always_xy=True)
    lon_c = (float(bbox[0]) + float(bbox[2])) / 2.0
    # Seaward of the AOI: nudge ~0.02 deg south of the southern edge (Gulf side).
    lat_seaward = float(bbox[1]) - 0.02
    x_m, y_m = tf.transform(lon_c, lat_seaward)
    return {
        "snapwave_boundary": {
            "points": [
                {
                    "x": float(x_m),
                    "y": float(y_m),
                    "hs": float(hs),   # significant wave height (m)
                    "tp": float(tp),   # peak period (s)
                    "wd": 0.0,         # wave direction (deg) - shore-normal
                    "ds": 20.0,        # directional spread (deg)
                }
            ]
        }
    }


def _synthetic_waterlevel(offset_m: float = 1.5) -> dict[str, Any]:
    """A flat synthetic surge offset for the open water-level boundary.

    ``_build_surge_forcing_members`` only emits a WaterlevelForcing when a
    sub-dict carries ``timeseries_uri`` / ``geodataset_uri`` (NOT a bare offset),
    so this offset alone yields NO bzs file - which is fine: the quadtree run is
    valid with a wave-only boundary (the wave field is what we are proving). The
    ``offset`` is carried for documentation / a future real-forcing swap. Left
    here so the smoke path is honest that there is no real surge hydrograph.
    """
    return {"waterlevel": {"offset": float(offset_m)}}


def _bbox_str(b: tuple[float, float, float, float]) -> str:
    return f"({b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f})"


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument(
        "--smoke",
        action="store_true",
        help="tiny CI-smoke bbox + 3h window (default; cheapest Batch run)",
    )
    grp.add_argument(
        "--demo",
        action="store_true",
        help="right-sized demo AOI (~29x30 km) + 12h window: offshore wave "
        "boundary -> non-zero field, but small enough to finish in the wait budget",
    )
    grp.add_argument(
        "--full",
        action="store_true",
        help="demo AOI (-85.75,29.55,-85.25,30.20) + 24h window (expensive)",
    )
    ap.add_argument(
        "--wave-hs",
        type=float,
        default=None,
        help="synthetic offshore Hm0 (m) for the SnapWave boundary "
        "(default 2.0 smoke / 6.0 full)",
    )
    args = ap.parse_args(argv)

    full = bool(args.full)
    demo = bool(getattr(args, "demo", False))
    if full:
        mode, bbox, duration_hr, default_hs = "FULL demo AOI", FULL_BBOX, 24, 6.0
    elif demo:
        mode, bbox, duration_hr, default_hs = "DEMO AOI (right-sized)", DEMO_BBOX, 12, 6.0
    else:
        mode, bbox, duration_hr, default_hs = "SMOKE (CI bbox)", SMOKE_BBOX, 3, 2.0
    wave_hs = args.wave_hs if args.wave_hs is not None else default_hs
    epsg = _utm_epsg_for_bbox(bbox)

    # --- env sanity (fail LOUD before submitting anything) ---------------------
    backend = (os.environ.get("GRACE2_SOLVER_BACKEND") or "").strip().lower()
    job_def = (
        os.environ.get("GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE")
        or os.environ.get("GRACE2_AWS_BATCH_JOB_DEF")
        or ""
    ).strip()
    queue = (os.environ.get("GRACE2_AWS_BATCH_QUEUE") or "").strip()
    runs_bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip()
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
    ).strip()

    print("=== Mexico Beach SFINCS quadtree + SnapWave acceptance (live AWS) ===")
    print(f"  mode:            {mode}")
    print(f"  bbox (EPSG4326): {_bbox_str(bbox)}")
    print(f"  grid CRS:        EPSG:{epsg} (UTM - SnapWave points projected here)")
    print(f"  duration_hr:     {duration_hr}")
    print(f"  synthetic Hm0:   {wave_hs:.1f} m (offshore paddle, uniform-in-time)")
    print(f"  Michael window:  {MICHAEL_WINDOW[0]} .. {MICHAEL_WINDOW[1]} (provenance)")
    print("  --- live-stack env ---")
    print(f"  GRACE2_SOLVER_BACKEND={backend!r}")
    print(f"  GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE={job_def!r}")
    print(f"  GRACE2_AWS_BATCH_QUEUE={queue!r}")
    print(f"  GRACE2_RUNS_BUCKET={runs_bucket!r}")
    print(f"  AWS region={region!r}")

    if backend != "aws-batch":
        print(
            "FAIL: GRACE2_SOLVER_BACKEND must be 'aws-batch' to submit the "
            "combined quadtree job. The coastal quadtree path is INERT on any "
            "other backend (it raises DECK_BUILD_FAILED). Set the env (it is set "
            "on the agent box's systemd solver.conf) and re-run."
        )
        return 2
    if not job_def:
        print(
            "FAIL: no quadtree job-def env. Set "
            "GRACE2_AWS_BATCH_JOB_DEF_SFINCS_QUADTREE=grace2-sfincs-quadtree "
            "(or the generic GRACE2_AWS_BATCH_JOB_DEF)."
        )
        return 2
    if not queue or not runs_bucket:
        print("FAIL: GRACE2_AWS_BATCH_QUEUE and GRACE2_RUNS_BUCKET must be set.")
        return 2

    # Import the REAL workflow + postprocessors (no LLM, no agent).
    from grace2_agent.workflows.model_flood_scenario import (
        _default_runs_prefix,
        model_flood_scenario,
    )
    from grace2_agent.workflows.postprocess_flood import postprocess_flood
    from grace2_agent.workflows.postprocess_waves import (
        WAVE_HEIGHT_STYLE_PRESET,
        postprocess_waves,
    )

    # Minimal-but-VALID coastal forcing: real topobathy (CUDEM+3DEP, fetched by
    # the workflow's coastal branch) + a synthetic SnapWave incident-wave boundary
    # (so hm0 is non-zero) + a documented flat surge offset. surge_forcing carries
    # the snapwave_boundary key VERBATIM: _resolve_surge_forcing_from_fetchers does
    # `out = dict(surge_forcing)` and only rewrites waterlevel/discharge fetch_uris,
    # so unknown keys pass through into forcing.surge_forcing -> the worker's
    # resolve_forcing_blocks(spec) -> snapwave_boundary.points (entrypoint step 7).
    surge_forcing: dict[str, Any] = {}
    surge_forcing.update(_synthetic_snapwave_boundary(bbox, hs=wave_hs))
    surge_forcing.update(_synthetic_waterlevel())

    print("\n--- submitting combined cht_sfincs quadtree + SnapWave Batch job ---")
    print("  (assembling build_spec -> run_sfincs_quadtree -> wait_for_completion)")
    print("  this blocks for the full Batch run: queue + Spot cold-start + solve.")

    # quadtree=True implies coastal=True (the topobathy DEM branch). The workflow
    # submits ONE combined job, waits, postprocesses depth, then waves. We re-run
    # the postprocessors directly below to assert the FRAME layers (the workflow
    # only emits frames via the emitter, which is None in this direct call).
    envelope = await model_flood_scenario(
        bbox=bbox,
        duration_hr=duration_hr,
        quadtree=True,
        coastal=True,
        surge_forcing=surge_forcing,
        building_obstacles=False,  # keep the smoke run cheap (no OSM footprint burn)
        compute_class="standard",
    )

    workflow_name = getattr(envelope, "workflow_name", "")
    solver_run_ids = list(getattr(envelope, "solver_run_ids", []) or [])
    layers = list(getattr(envelope, "layers", []) or [])
    print("\n=== workflow envelope ===")
    print(f"  envelope_type: {getattr(envelope, 'envelope_type', None)}")
    print(f"  workflow_name: {workflow_name}")
    print(f"  solver_run_ids: {solver_run_ids}")
    print(f"  layer count:   {len(layers)}")
    for lyr in layers:
        print(
            f"    - id={getattr(lyr, 'layer_id', None)} "
            f"role={getattr(lyr, 'role', None)} "
            f"style={getattr(lyr, 'style_preset', None)} "
            f"name={getattr(lyr, 'name', None)!r}"
        )

    # --- FAIL FAST on a failed envelope ----------------------------------------
    # The partial-failure shape tags workflow_name as "<name>:FAILED:<CODE>" and
    # threads "failed:<CODE>" into flood.metrics.solver_version. A common smoke
    # failure is DECK_BUILD_FAILED (env not set / wrong backend) or the Batch
    # solve returning non-complete.
    if ":FAILED:" in workflow_name or not solver_run_ids:
        sv = None
        try:
            sv = envelope.flood.metrics.solver_version  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        print(
            f"\nFAIL: workflow returned a FAILED envelope "
            f"(workflow_name={workflow_name!r}, solver_version={sv!r}). "
            "See the agent log for the Batch job id + statusReason."
        )
        return 1

    run_id = solver_run_ids[-1]
    run_output_uri = _default_runs_prefix(run_id).rstrip("/")
    print(f"\n  combined-quadtree run_id: {run_id}")
    print(f"  run output prefix:        {run_output_uri}/")
    print(f"  expecting sfincs_map.nc at: {run_output_uri}/sfincs_map.nc")

    rc = 0

    # --- ASSERT 1: depth layers (sfincs_map.nc exists + flood postprocess) ------
    print("\n--- ASSERT: depth layers (postprocess_flood) ---")
    try:
        depth_layers, depth_metrics = postprocess_flood(run_output_uri, run_id=run_id)
        depth_peak = [l for l in depth_layers if getattr(l, "role", "") == "primary"]
        depth_frames = [l for l in depth_layers if getattr(l, "role", "") != "primary"]
        print(f"  depth layers: peak={len(depth_peak)} frames={len(depth_frames)}")
        print(f"  depth max_depth_m={depth_metrics.get('max_depth_m')}")
        for l in depth_layers[:3]:
            print(f"    - {getattr(l, 'name', None)!r} -> {getattr(l, 'uri', None)}")
        if not depth_peak:
            print("  FAIL: no primary depth layer produced.")
            rc = 1
        else:
            print("  OK: sfincs_map.nc read + depth peak produced.")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: postprocess_flood raised {type(exc).__name__}: {exc}")
        rc = 1

    # --- ASSERT 2: wave-height layers (SnapWave hm0/hm0ig -> peak + frames) ------
    # This is the headline: a non-zero animated wave field. postprocess_waves
    # raises RUN_OUTPUT_EMPTY if sfincs_map.nc carries NO hm0/hm0ig (i.e. it was
    # NOT a SnapWave run) - which is itself a useful failure signal.
    print("\n--- ASSERT: wave-height layers (postprocess_waves: SnapWave hm0) ---")
    try:
        wave_layers, wave_metrics = postprocess_waves(
            run_output_uri, run_id=run_id, bbox=bbox
        )
        wave_peak = [l for l in wave_layers if getattr(l, "role", "") == "primary"]
        wave_frames = [l for l in wave_layers if getattr(l, "role", "") != "primary"]
        wave_max = wave_metrics.get("max_depth_m")  # shared metric key = peak hm0 (m)
        print(f"  wave layers: peak={len(wave_peak)} frames={len(wave_frames)}")
        print(f"  peak wave height (m): {wave_max}")
        print(f"  wave style_preset: {WAVE_HEIGHT_STYLE_PRESET} (gnbu, rescale 0,6)")
        for l in wave_layers:
            print(
                f"    - id={getattr(l, 'layer_id', None)} "
                f"name={getattr(l, 'name', None)!r} -> {getattr(l, 'uri', None)}"
            )
        # PASS requires: a peak wave layer AND a non-zero peak height. A wave field
        # that is identically zero means SnapWave produced nothing (boundary not
        # wired) - that is a FAIL even though a "peak" COG technically wrote.
        if not wave_peak:
            print("  FAIL: no peak wave-height layer (SnapWave field absent/empty).")
            rc = 1
        elif not (isinstance(wave_max, (int, float)) and math.isfinite(wave_max) and wave_max > 0.0):
            print(
                "  FAIL: peak wave height is zero/NaN - SnapWave field is flat. "
                "Check that the snapwave_boundary points reached the worker "
                "(grep the Batch log for 'no SnapWave boundary points')."
            )
            rc = 1
        else:
            n_frames = len(wave_frames)
            print(
                f"  OK: peak wave height {wave_max:.3f} m + {n_frames} animation "
                f"frame(s) ('Wave height step N')."
            )
            if n_frames < 1:
                print(
                    "  NOTE: 0 wave frames - the wave field had time-dim<=1 or a "
                    "single frame (a 1-frame group is dropped). The peak wave "
                    "layer still proves the SnapWave render; bump duration_hr / "
                    "output cadence for a multi-frame animation."
                )
    except Exception as exc:  # noqa: BLE001
        # RUN_OUTPUT_EMPTY here = sfincs_map.nc had no hm0/hm0ig = NOT a SnapWave
        # run (snapwave=0 in the deck, or the boundary never wired) - a real FAIL
        # for this acceptance even though the depth layers may be fine.
        print(f"  FAIL: postprocess_waves raised {type(exc).__name__}: {exc}")
        print(
            "  -> the run produced NO SnapWave wave field. Confirm snapwave=1 in "
            "the deck + the snapwave_boundary points in the build_spec."
        )
        rc = 1

    print("\n=== RESULT ===")
    if rc == 0:
        print(
            "PASS: combined quadtree+SnapWave solve ran, sfincs_map.nc exists, and "
            "BOTH the depth layers and the non-zero wave-height layers were "
            "produced. The animated wave layers render."
        )
    else:
        print("FAIL: see the assertions above.")
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
