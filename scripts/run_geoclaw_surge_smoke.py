"""Direct-manifest GeoClaw storm-surge smoke through the trid3nt-local/geoclaw image.

Proves the parametric-Holland surge front end-to-end WITHOUT the agent/composer:
authors a synthetic Gulf-shelf bathymetry (topotype-3 ASCII), stages it + a
surge build_spec to MinIO, runs the geoclaw worker image (the SAME docker run
line the local-docker backend uses), then reads the fort.q frames + coastal gauge
to extract the peak surge surface elevation + onshore inundation depth.

Two variants:
  ike    -- the PUBLISHED Hurricane Ike 2008 Gulf-landfall best track (NHC ATCF
            bal092008), Garratt drag: the anchor smoke (peak surge vs ~4.5-6 m
            observed at Bolivar/Galveston; qualitative -- idealized shelf).
  ab     -- a synthetic demo track run TWICE (Garratt vs Powell drag): the
            wind-drag-law A/B (the knob must measurably change the surge).

Usage (from repo root, FULL MinIO env):
  set -a; source .env.local; set +a
  sg docker -c 'venvs/agent/bin/python scripts/run_geoclaw_surge_smoke.py ike'
  sg docker -c 'venvs/agent/bin/python scripts/run_geoclaw_surge_smoke.py ab'
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import boto3
import numpy as np

# --------------------------------------------------------------------------- #
# AOI + synthetic Gulf shelf bathymetry (Galveston / Bolivar, Ike landfall).
# --------------------------------------------------------------------------- #
BBOX = (-95.2, 29.0, -94.2, 29.8)         # min_lon, min_lat, max_lon, max_lat
COAST_LAT = 29.45                          # ~ shoreline latitude
SOUTH_DEPTH_M = -15.0                      # bathy at the south (offshore) edge
SHELF_GRADIENT = SOUTH_DEPTH_M / (BBOX[1] - COAST_LAT)   # m per deg lat (~+33)
GAUGE = (-94.75, 29.44)                    # coastal tide gauge (near the shore)

# PUBLISHED Hurricane Ike (AL092008) Gulf-approach best track -- NHC ATCF
# bal092008.dat (http://ftp.nhc.noaa.gov/atcf/archive/2008/bal092008.dat.gz),
# NOAA/NHC public-domain best-track. Landfall reference: 2008-09-13 0700 UTC at
# Bolivar Peninsula (29.3N, 94.7W). Columns below are the published synoptic
# fixes; t_s = seconds RELATIVE TO LANDFALL. Units converted to GeoClaw SI:
#   vmax kt -> m/s (x0.514444); RMW nm -> m (x1852); Pmin mb -> Pa (x100).
# storm_radius (ROCI) fixed at 500 km (Ike was an exceptionally large storm).
_KT = 0.514444
_NM = 1852.0
_HPA = 100.0
_IKE_RAW = [
    # (UTC "MMDDHH", lat_deg, lon_deg, vmax_kt, pmin_mb, rmw_nm)
    ("091200", 26.1, -90.0, 85, 954, 80),
    ("091206", 26.4, -91.1, 90, 954, 50),
    ("091212", 26.9, -92.2, 95, 954, 50),
    ("091218", 27.5, -93.2, 95, 954, 50),
    ("091300", 28.3, -94.0, 95, 952, 40),
    ("091306", 29.1, -94.6, 95, 951, 30),
    ("091307", 29.3, -94.7, 95, 950, 30),   # LANDFALL (t=0)
    ("091312", 30.3, -95.2, 85, 959, 35),
]
_LANDFALL_HR = (13, 7)  # day 13, hour 07 UTC


def _ike_track() -> list[list[float]]:
    """The published Ike Gulf-approach track as GeoClaw storm rows (t_s from landfall)."""
    lf_day, lf_hr = _LANDFALL_HR
    rows: list[list[float]] = []
    for (stamp, lat, lon, vkt, pmb, rnm) in _IKE_RAW:
        day = int(stamp[2:4])
        hr = int(stamp[4:6])
        t_s = ((day - lf_day) * 24 + (hr - lf_hr)) * 3600.0
        rows.append([
            t_s, lon, lat, vkt * _KT, rnm * _NM, pmb * _HPA, 500000.0,
        ])
    return rows


def _bathy_z(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    """Analytic synthetic shelf: linear in latitude (deep south -> land north)."""
    return SHELF_GRADIENT * (lat - COAST_LAT) + 0.0 * lon


def _write_topotype3(path: Path, nx: int = 80, ny: int = 64) -> None:
    """Write the synthetic Gulf-shelf bathymetry as a GeoClaw topotype-3 ASCII."""
    lons = np.linspace(BBOX[0], BBOX[2], nx)
    lats = np.linspace(BBOX[1], BBOX[3], ny)
    dx = float(lons[1] - lons[0])
    Z = _bathy_z(lons[None, :], lats[:, None])  # (ny, nx), row 0 = south
    with path.open("w") as fh:
        fh.write(f"{nx:>6d}                              ncols\n")
        fh.write(f"{ny:>6d}                              nrows\n")
        fh.write(f"{BBOX[0]:.15e}              xlower\n")
        fh.write(f"{BBOX[1]:.15e}              ylower\n")
        fh.write(f"{dx:.15e}              cellsize\n")
        fh.write(f"{-99999:>6}                          nodata_value\n")
        # north-first rows (GeoClaw topotype-3 is north row first).
        for j in range(ny - 1, -1, -1):
            fh.write(" ".join(f"{Z[j, i]: .7e}" for i in range(nx)) + " \n")


# --------------------------------------------------------------------------- #
# MinIO staging + docker run (mirror the local-docker backend argv).
# --------------------------------------------------------------------------- #
def _s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_ENDPOINT_URL"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


CACHE = os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache")
RUNS = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")
IMAGE = os.environ.get("TRID3NT_GEOCLAW_IMAGE", "trid3nt-local/geoclaw:latest")


def _stage_and_run(run_id: str, build_spec: dict, topo_path: Path) -> dict:
    s3 = _s3()
    topo_key = f"cache/static-30d/geoclaw_surge_smoke/{run_id}/topo.asc"
    s3.upload_file(str(topo_path), CACHE, topo_key)
    topo_uri = f"s3://{CACHE}/{topo_key}"
    manifest = {
        "inputs": [{"gs_uri": topo_uri, "dest": "topo.asc"}],
        "build_spec": build_spec,
        "outputs": [
            "_output/fort.q*", "_output/fort.t*", "_output/fort.h*",
            "_output/fort.b*", "_output/fgmax*.txt", "_output/fgmax_grids.data",
            "_output/gauge*.txt", "deck_manifest.json",
        ],
    }
    man_key = f"cache/static-30d/geoclaw_surge_smoke/{run_id}/manifest.json"
    s3.put_object(Bucket=CACHE, Key=man_key,
                  Body=json.dumps(manifest).encode(), ContentType="application/json")
    man_uri = f"s3://{CACHE}/{man_key}"

    cmd = [
        "docker", "run", "--rm", "--name", run_id, "--network", "host",
        "-e", f"TRID3NT_RUNS_BUCKET={RUNS}",
        "-e", "TRID3NT_OBJECT_STORE=s3",
        "-e", "TRID3NT_GEOCLAW_SCRATCH=/opt/trid3nt/work",
        "-e", f"AWS_REGION={os.environ.get('AWS_REGION', 'us-east-1')}",
        "-e", f"AWS_ENDPOINT_URL={os.environ['AWS_ENDPOINT_URL']}",
        "-e", f"AWS_ACCESS_KEY_ID={os.environ['AWS_ACCESS_KEY_ID']}",
        "-e", f"AWS_SECRET_ACCESS_KEY={os.environ['AWS_SECRET_ACCESS_KEY']}",
        "-e", "PYTHONUNBUFFERED=1",
        IMAGE, "--run-id", run_id, "--manifest-uri", man_uri,
    ]
    t0 = time.time()
    print(f"[{run_id}] docker run ...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.time() - t0
    print(f"[{run_id}] container rc={proc.returncode} wall={dt:.0f}s", flush=True)
    if proc.returncode != 0:
        print(proc.stdout[-2000:]); print(proc.stderr[-2000:])
    # read completion
    comp = json.loads(s3.get_object(Bucket=RUNS, Key=f"{run_id}/completion.json")["Body"].read())
    return comp


def _download_outputs(run_id: str, dest: Path) -> None:
    s3 = _s3()
    dest.mkdir(parents=True, exist_ok=True)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=RUNS, Prefix=f"{run_id}/"):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            rel = key[len(run_id) + 1:]
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(RUNS, key, str(out))


# --------------------------------------------------------------------------- #
# Surge extraction from fort.q (h + analytic B -> eta) + the coastal gauge.
# --------------------------------------------------------------------------- #
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server" / "src"))
from trid3nt_server.agent.workflows.geoclaw.postprocess_geoclaw import (  # noqa: E402
    parse_fort_q_frame,
    _frame_time_from_t_header,
)


def _surge_metrics(out_dir: Path) -> dict:
    """Peak surge surface elevation + onshore inundation depth across all frames.

    Reads every fort.q frame, and for each finest-level patch cell computes the
    analytic bed B at the cell centre (we authored the bathymetry), so
    eta = B + h (wet cells), onshore inundation = h where B > 0."""
    frames = sorted((out_dir / "_output").glob("fort.q*"))
    peak_eta = -1e9
    peak_eta_loc = None
    peak_onshore_h = 0.0
    peak_onshore_loc = None
    peak_frame_time = None
    # Coastal SURGE band: the genuine storm-surge run-up sits on the shoreline
    # (bed elevation between ~-3 m nearshore and ~+4 m just-onshore). Restricting
    # to this band excludes a thin numerical film that can wet the far high-land
    # domain corner (bed ~+11 m) -- that is a boundary artifact, not surge.
    peak_coastal = -1e9
    peak_coastal_loc = None
    peak_coastal_t = None
    per_frame = []
    for fq in frames:
        patches = parse_fort_q_frame(fq.read_text())
        ftxt = fq.with_name(fq.name.replace("fort.q", "fort.t"))
        t = _frame_time_from_t_header(ftxt.read_text()) if ftxt.exists() else None
        maxlev = max((p.level for p in patches), default=1)
        fr_eta = -1e9
        for p in patches:
            if p.level != maxlev:
                continue
            xs = p.xlow + (np.arange(p.mx) + 0.5) * p.dx
            ys = p.ylow + (np.arange(p.my) + 0.5) * p.dy
            B = _bathy_z(xs[None, :], ys[:, None])       # (my, mx)
            h = p.h
            wet = h > 1e-2
            eta = np.where(wet, B + h, np.nan)
            if np.isfinite(eta).any():
                fr_eta = max(fr_eta, float(np.nanmax(eta)))
                idx = np.unravel_index(np.nanargmax(np.where(wet, B + h, -1e9)), eta.shape)
                if B[idx] + h[idx] > peak_eta:
                    peak_eta = float(B[idx] + h[idx])
                    peak_eta_loc = (float(xs[idx[1]]), float(ys[idx[0]]))
                    peak_frame_time = t
            onshore = wet & (B > 0.0)
            if onshore.any():
                oi = np.unravel_index(np.nanargmax(np.where(onshore, h, -1)), h.shape)
                if h[oi] > peak_onshore_h:
                    peak_onshore_h = float(h[oi])
                    peak_onshore_loc = (float(xs[oi[1]]), float(ys[oi[0]]))
            band = wet & (B > -3.0) & (B < 4.0)
            if band.any():
                ci = np.unravel_index(np.argmax(np.where(band, B + h, -1e9)), h.shape)
                if B[ci] + h[ci] > peak_coastal:
                    peak_coastal = float(B[ci] + h[ci])
                    peak_coastal_loc = (float(xs[ci[1]]), float(ys[ci[0]]))
                    peak_coastal_t = t
        per_frame.append({"t_s": t, "max_eta_m": None if fr_eta < -1e8 else round(fr_eta, 3)})
    return {
        # The headline SURGE number: peak water-surface elevation in the coastal
        # run-up band (excludes the far high-land wet-film artifact).
        "peak_coastal_surge_elev_m": None if peak_coastal < -1e8 else round(peak_coastal, 3),
        "peak_coastal_surge_loc_lonlat": peak_coastal_loc,
        "peak_coastal_surge_frame_time_s": peak_coastal_t,
        "peak_onshore_inundation_m": round(peak_onshore_h, 3),
        "peak_onshore_loc_lonlat": peak_onshore_loc,
        # Whole-domain max eta (diagnostic; may include a high-land wet-film cell).
        "domain_max_eta_m": None if peak_eta < -1e8 else round(peak_eta, 3),
        "domain_max_eta_loc_lonlat": peak_eta_loc,
        "per_frame_max_eta": per_frame,
    }


def _gauge_waveform(out_dir: Path) -> dict:
    """Parse the coastal gauge (id 1): surface elevation (eta) time series."""
    gfiles = sorted((out_dir / "_output").glob("gauge00001.txt"))
    if not gfiles:
        return {}
    rows = []
    for ln in gfiles[0].read_text().splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split()
        if len(parts) < 6:
            continue
        # GeoClaw gauge columns: level, t, q[0]=h, q[1]=hu, q[2]=hv, eta
        try:
            t = float(parts[1]); h = float(parts[2]); eta = float(parts[-1])
        except ValueError:
            continue
        rows.append((t, h, eta))
    if not rows:
        return {}
    ts = np.array([r[0] for r in rows]); etas = np.array([r[2] for r in rows])
    hs = np.array([r[1] for r in rows])
    return {
        "gauge_t_s": ts.tolist(),
        "gauge_eta_m": etas.tolist(),
        "gauge_h_m": hs.tolist(),
        "gauge_peak_eta_m": round(float(np.nanmax(etas)), 3),
        "gauge_min_eta_m": round(float(np.nanmin(etas)), 3),
        "gauge_peak_h_m": round(float(np.nanmax(hs)), 3),
    }


# --------------------------------------------------------------------------- #
def _base_spec(drag: str, track: list[list[float]] | None, t0_s: float,
               sim_s: float, frames: int) -> dict:
    spec = {
        "scenario": "surge",
        "bbox": list(BBOX),
        "topo_file": "topo.asc",
        "sim_duration_s": sim_s,
        "t0_s": t0_s,
        "output_frames": frames,
        "amr_levels": 2,
        "base_num_cells": [30, 24],
        "manning_n": 0.025,
        "sea_level_m": 0.0,
        "wind_drag_law": drag,
        "coastal_gauge_lonlat": list(GAUGE),
    }
    if track is not None:
        spec["storm_track"] = track
    return spec


def _new_run_id(tag: str) -> str:
    return f"surgesmoke-{tag}-{int(time.time())}"


def main() -> int:
    variant = sys.argv[1] if len(sys.argv) > 1 else "ike"
    art = Path("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
               "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/geoclaw_surge")
    art.mkdir(parents=True, exist_ok=True)
    topo = art / "topo.asc"
    _write_topotype3(topo)
    print("bathy min/max:", round(SHELF_GRADIENT * (BBOX[1] - COAST_LAT), 1),
          "/", round(SHELF_GRADIENT * (BBOX[3] - COAST_LAT), 1), "m", flush=True)

    results = {}
    if variant == "ike":
        variants = [("ike-garratt", "garratt", _ike_track(), -43200.0, 54000.0, 12)]
    elif variant == "ab":
        # synthetic demo track (worker-generated), Garratt vs Powell A/B.
        variants = [
            ("ab-garratt", "garratt", None, -32400.0, 43200.0, 10),
            ("ab-powell", "powell", None, -32400.0, 43200.0, 10),
        ]
    else:
        print(f"unknown variant {variant!r}"); return 2

    for (tag, drag, track, t0_s, sim_s, frames) in variants:
        rid = _new_run_id(tag)
        spec = _base_spec(drag, track, t0_s, sim_s, frames)
        comp = _stage_and_run(rid, spec, topo)
        print(f"[{rid}] status={comp.get('status')} error={comp.get('error')}", flush=True)
        outdir = art / rid
        _download_outputs(rid, outdir)
        m = _surge_metrics(outdir) if comp.get("status") == "ok" else {}
        g = _gauge_waveform(outdir) if comp.get("status") == "ok" else {}
        results[tag] = {"run_id": rid, "drag": drag, "status": comp.get("status"),
                        "surge": m, "gauge": {k: v for k, v in g.items()
                                              if not k.startswith("gauge_t") and k != "gauge_eta_m" and k != "gauge_h_m"}}
        # persist full gauge waveform for the chart
        if g:
            (outdir / "gauge_waveform.json").write_text(json.dumps(g))
        print(json.dumps(results[tag], indent=2), flush=True)

    (art / f"surge_smoke_{variant}.json").write_text(json.dumps(results, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
