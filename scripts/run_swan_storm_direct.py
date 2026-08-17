#!/usr/bin/env python
"""row 3 driver: SWAN NONSTATIONARY time-marching storm evolution.

Drives model_swan_wave_field directly (no LLM) with a TIME-VARYING storm
boundary (build-peak-decay Hs over 24-48 h) at a US coastal site, producing
time-stamped Hs frames + a peak-Hs field through the native SWAN solver + the
rebuilt worker image.

Env (MinIO): set -a; source .env.local; set +a
Usage: venvs/agent/bin/python scripts/run_swan_storm_direct.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("swan_storm")

# Mexico Beach / Tyndall FL shelf (the SWAN shelf AOI, matching the
# showcase _APALACHEE box). Real continuous below-datum CUDEM bathymetry, ~86%
# wet -- open ocean to the SOUTH -> boundary side S. NOTE: the Big Bend box near
# -84.0 was ABANDONED for the proof because its CUDEM topobathy tile carries a
# large interior elevation==0.0 void (a source tile gap), which the depth sampler
# reads as dry (depth 0 < DEPMIN) -> ~half the AOI meshes dry and the wave field
# fills only the connected wet strip. The deck grid extent is correct; the void
# is a bathymetry-DATA artifact, so the fix is a void-free shelf AOI.
BBOX = (-85.55, 29.70, -85.40, 29.85)
SIM_HOURS = 36.0
PEAK_HOUR = 18.0
BASELINE_HS = 1.0
PEAK_HS = 6.0


async def _run():
    from trid3nt_server.workflows.swan.wave_field.wave_field import (
        model_swan_wave_field, build_storm_hydrograph,
    )
    from trid3nt_contracts.swan_contracts import SwanRunArgs, SwanWaveBoundary

    boundary = SwanWaveBoundary(hs_m=BASELINE_HS, tp_s=8.0, dir_deg=180.0,
                                spread_deg=25.0, side="S")
    series = build_storm_hydrograph(
        BASELINE_HS, PEAK_HS, 8.0, 180.0, 25.0, SIM_HOURS * 3600.0, PEAK_HOUR, 13)
    log.info("storm hydrograph (hr, Hs): %s",
             [(round(r[0] / 3600, 1), r[1]) for r in series])
    run_args = SwanRunArgs(
        mode="nonstationary", bbox=BBOX, boundary=boundary,
        sim_duration_s=SIM_HOURS * 3600.0, time_step_s=600.0, output_frames=18,
        storm_boundary_timeseries=series,
    )
    return await model_swan_wave_field(run_args)


result = asyncio.run(_run())
log.info("returned type=%s", type(result).__name__)
if hasattr(result, "model_dump"):
    d = result.model_dump(mode="json")
    log.info("max_hs_m=%s mean_tp_s=%s uri=%s", d.get("max_hs_m"),
             d.get("mean_tp_s"), d.get("uri"))
    # find the run_id from the uri (s3://runs/<run_id>/...)
    uri = d.get("uri", "")
    run_id = None
    if "s3://" in uri:
        parts = uri.split("s3://", 1)[1].split("/")
        if len(parts) >= 2:
            run_id = parts[1]
    print(json.dumps({"status": "ok", "run_id": run_id,
                      "max_hs_m": d.get("max_hs_m"), "uri": uri}, indent=2))
else:
    print(json.dumps({"status": "error", "result": str(result)[:500]}, indent=2))
    sys.exit(1)
