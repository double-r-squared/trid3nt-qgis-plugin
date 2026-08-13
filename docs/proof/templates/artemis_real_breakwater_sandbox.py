"""Local-first REAL-breakwater ARTEMIS sandbox (ADR 0237 real-marina demo).

Runs INSIDE trid3nt-local/telemac:latest (needs the baked artemis binary +
opentelemac SELAFIN API). Proves the real-data demo per NATE norm #10 ("a real
marina with a real breaker ... the way it would be used in real life"):

  * the REAL surveyed breakwater geometry (OpenStreetMap man_made=breakwater ways
    at Marquette Lower Harbor / Cinder Pond Marina, Lake Superior) is meshed as a
    thin solid reflecting barrier over REAL NOAA lake-datum bathymetry;
  * a labeled realistic incident swell drives an ARTEMIS diffraction solve;
  * the proof-norm-#9 pair is the SAME AOI / bathy / incident wave with the real
    structure PRESENT (as surveyed) vs REMOVED -- the sheltering the actual
    breakwater provides its actual marina.

Invocation (bind-mount a rundir carrying manifest.json):
    docker run --rm -v <rundir>:/data \
      -v <repo>/services/workers/telemac/artemis_build.py:/opt/trid3nt/services/workers/telemac/artemis_build.py \
      -w /data trid3nt-local/telemac:latest \
      python /data/artemis_real_breakwater_sandbox.py

Writes present/agit_field.slf, removed/agit_field.slf, and pair_metrics.json back
into /data. Rendering + georeferencing is done agent-side (host) off the .slf.
ASCII only. No agent/product code imported.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, "/opt/trid3nt/services/workers/telemac")

import artemis_build as A  # noqa: E402

DATA = os.environ.get("TRID3NT_TELEMAC_DATA_DIR", "/data")


def _cfg(manifest, *, remove):
    return A.ArtemisConfig(
        name="artemis_real_marquette_breakwater",
        wave_mode="diffraction",
        bathy_source="noaa_greatlakes",
        bbox=tuple(manifest["bbox"]),
        breakwater_polylines=manifest["breakwater_polylines"],
        remove_structure=remove,
        wave_height_m=float(manifest.get("wave_height_m", 2.0)),
        wave_period_s=float(manifest.get("wave_period_s", 8.0)),
        wave_dir_deg=float(manifest["wave_dir_deg"]),
        reflection_coef=float(manifest.get("reflection_coef", 0.5)),
        target_resolution_m=float(manifest.get("target_resolution_m", 30.0)),
    )


def main():
    manifest = json.load(open(os.path.join(DATA, "manifest.json")))
    out = {"aoi_bbox": manifest["bbox"],
           "breakwater_osm_ids": manifest.get("breakwater_osm_ids"),
           "wave_dir_deg": manifest["wave_dir_deg"]}
    for label, remove in (("present", False), ("removed", True)):
        wd = os.path.join(DATA, label)
        os.makedirs(wd, exist_ok=True)
        print(f"=== solving {label} (remove_structure={remove}) ===", flush=True)
        m = A.solve(_cfg(manifest, remove=remove), wd, run_id=f"real-bw-{label}")
        out[label] = m
        print(f"  {label}: status={m.get('status')} kd_max={m.get('kd_max')} "
              f"kd_sheltered={m.get('kd_sheltered')} kd_exposed={m.get('kd_exposed')} "
              f"npoin={m.get('npoin')} wall_s={m.get('wall_s')}", flush=True)
    json.dump(out, open(os.path.join(DATA, "pair_metrics.json"), "w"), indent=2)
    print("wrote pair_metrics.json", flush=True)


if __name__ == "__main__":
    main()
