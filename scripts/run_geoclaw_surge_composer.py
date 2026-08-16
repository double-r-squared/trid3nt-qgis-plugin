"""Run the GeoClaw storm-surge template end-to-end through the REAL composer
(model_geoclaw_inundation): real Gulf topo-bathy fetch -> deck -> geoclaw image
solve -> agent postprocess (depth COG + mesh.geojson + eta frames). This is the
DEFAULT-path proof run for the parametric-Holland surge front (ADR 0168).

Ike 2008 published NHC best track (bal092008) over Galveston/Bolivar, Garratt drag.

Run (repo root, env loaded):
  set -a; source .env.local; set +a
  sg docker -c 'env $(grep -v "^#" .env.local | xargs) \
    PYTHONPATH=.:contracts venvs/agent/bin/python \
    scripts/run_geoclaw_surge_composer.py'
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_geoclaw_surge_composer")

sys.path.insert(0, "scripts")
from run_geoclaw_surge_smoke import _ike_track  # reuse the published Ike track

from trid3nt_contracts.geoclaw_contracts import GeoClawRunArgs, StormTrackPoint
from trid3nt_server.workflows.geoclaw.inundation.inundation import (
    model_geoclaw_inundation,
)

BBOX = (-95.0, 29.15, -94.55, 29.55)   # Bolivar / east-Galveston coastal AOI (cheap)
GAUGE = (-94.75, 29.37)                 # coastal tide gauge


def _track_points() -> list[StormTrackPoint]:
    pts = []
    for (t, lon, lat, v, r, pc, rs) in _ike_track():
        pts.append(StormTrackPoint(
            t_s=t, lon=lon, lat=lat, max_wind_speed_ms=v,
            max_wind_radius_m=r, central_pressure_pa=pc, storm_radius_m=rs))
    return pts


async def _run():
    run_args = GeoClawRunArgs(
        bbox=BBOX,
        scenario="surge",
        sim_duration_s=39600.0,        # 11 h window (cheap)
        surge_t0_s=-32400.0,           # open 9 h before landfall
        output_frames=10,
        amr_levels=2,
        wind_drag_law="garratt",
        storm_track=_track_points(),
        coastal_gauge_lonlat=GAUGE,
    )
    log.info("dispatching surge composer bbox=%s drag=%s track_pts=%d",
             run_args.bbox, run_args.wind_drag_law, len(run_args.storm_track))
    return await model_geoclaw_inundation(run_args, emit_gauge_series=True,
                                          cleanup_outputs=False)


def main() -> int:
    result = asyncio.run(_run())
    if hasattr(result, "model_dump"):
        d = result.model_dump(mode="json")
        keep = {k: d.get(k) for k in (
            "layer_id", "uri", "scenario", "max_depth_m", "flooded_area_km2",
            "max_inundation_m", "run_id",
            "gauge_max_surface_elevation_m", "gauge_min_surface_elevation_m",
            "gauge_max_amplitude_m", "gauge_max_depth_m")}
        print("SURGE COMPOSER RESULT:")
        print(json.dumps(keep, indent=2, default=str))
    else:
        print("SURGE COMPOSER RESULT (non-layer):")
        print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
