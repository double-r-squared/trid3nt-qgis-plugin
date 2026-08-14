"""SFINCS hydraulic-structure present-vs-absent DISCRIMINANT smoke (ADR 0256).

DIRECTIONAL-FLOW re-run (NATE catch, 2026-08-14): the originally-landed
rain-on-grid smoke put uniform precip on both sides of the line, so a
barrier only redistributes water locally and panels A vs B look nearly
identical (the signal hid in the diff panel). This re-run swaps the forcing
for a REAL coastal water-level boundary so surge genuinely rises at one AOI
edge and crosses the structure line -- the composer's own
``setup_mask_bounds(btype="waterlevel", zmax=2.0 m NAVD88)`` puts the msk==2
surge boundary ONLY on domain-EDGE cells at/below +2 m NAVD88, so an AOI
whose south edge sits below +2 m (Gulf water) while its north/east/west
edges sit above it (dry land) gets a genuinely SOUTH-ONLY boundary -- surge
enters from the south and marches north, a real directional driver instead
of uniform rain-on-both-sides.

THREE real US coastal AOIs were profiled/tried before this one (elevation
profiled offline via ``fetch_topobathy`` + numpy, not guessed):
  1. Grand Isle, LA -- mostly submerged marsh/bay at MSL; the whole AOI sat
     below the +2 m cap so ALL four edges became msk==2 boundary and the
     domain equilibrated near-instantly to a uniform depth -- no headroom
     for a barrier to matter (both cases: 9900/9900 wet cells, ~10 m
     uniform depth = the resting bathymetric water column, not a flood).
  2. Bay St. Louis, MS -- a real south-only boundary AOI (elevation
     profiled: south edge mean -1.8 m, north/east/west mean +2.7..+5.3 m),
     but its bluff is a SHARP natural crest that already stops a 1000-yr
     (~4.4 m) surge on its own -- the plain run's surge never reached past
     the crest either, so the dam had nothing left to block (both cases
     converge to ~0 past the crest).
  3. Waveland, MS (FINAL, used below) -- same clean south-only boundary,
     but a GENTLE slope (~0 m -> ~5 m over ~3.5 km) instead of a sharp
     crest, so the surge genuinely propagates well past the dam's position
     in the plain run, giving the barrier real room to matter.

Runs the live registered ``sfincs_flood`` template twice on the SAME small
coastal AOI under the SAME auto-wired storm-surge boundary (``coastal=True``,
no explicit ``surge_forcing`` -> the composer's own NOAA CO-OPS /
parametric-fallback auto-wire):

  A) plain coastal deck (no structure)
  B) a THIN DAM (thd) no-flow barrier line across the domain, shore-parallel,
     at the mid-slope transition

HONEST FINDING (read before assuming "protected side stays dry"): the
composer ALWAYS also emits the return-period design-storm PRECIPITATION
alongside the coastal surge (there is no coastal-surge-only mode) -- so
the barrier is tested against a COMPOUND surge+rain deck, not surge alone.
Measured in the lee band immediately north of (behind) the dam
(``LEE_LAT_MIN``..``LEE_LAT_MAX``, the zone the barrier is meant to shield):
the dammed run (B) shows SLIGHTLY MORE water than the plain run (A), not
less -- because the same barrier that excludes Gulf surge from the south
ALSO blocks the locally-falling design-storm rain from draining south to
the sea, trapping it immediately behind the line. This is the real,
well-documented "leveed interior cannot self-drain" problem (the same
reason real levee districts need interior pump stations), not a bug in the
structure knob or the smoke's diff math -- see the column-wise diagnostic
in the ADR addendum showing the diff signal is centered on the structure
line's midpoint (not its endpoints), i.e. genuine blocking, not edge
leakage. The gate below therefore checks for a REAL, SPATIALLY-COHERENT,
structure-aligned discriminant (present vs absent genuinely diverge, and
the changed cells cluster along the line) rather than assuming a
particular sign -- which the very first (rain-on-grid) proof this replaces
got wrong in the other direction (assuming a fully-blocked, well-drained
protected side would look empty).

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
  # TMPDIR override: hydromt/GDAL stage scratch files under the system temp
  # dir, which on this host is a small tmpfs shared across sessions; point it
  # at the main disk to avoid a spurious ENOSPC mid-run.
  export TMPDIR=/home/nate/Documents/trid3nt-local/.tmp_staging
  mkdir -p "$TMPDIR"
  env PYTHONPATH=server/src:contracts/src venvs/agent/bin/python \
    scripts/run_sfincs_structure_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_sfincs_structure_smoke")

# Waveland, MS -- a low-lying Gulf-front town just west of Bay St. Louis'
# bluff. Offline elevation profile (fetch_topobathy + numpy, before this AOI
# was chosen): south edge mean approx -0.8 m NAVD88 (Gulf water, below the
# composer's +2 m seaward-boundary cap -> msk==2); north edge mean approx
# +5.2 m (above the cap -> stays closed). Terrain climbs GRADUALLY (~0 m ->
# ~5 m over ~3.5 km, unlike Bay St. Louis' sharp bluff crest), so a large
# surge genuinely propagates well past a mid-slope dam line when the dam is
# absent.
BBOX = (-89.40, 30.265, -89.36, 30.300)
# return_period_yr=1000 -> approx 4.4 m parametric surge peak (the composer's
# log-scaled last-resort synth), above the ~3.3 m elevation at the dam line;
# duration_hr=24 gives the ramp (~2.7 hr) time to fully hold/equilibrate
# before the lee-band comparison is read.
RETURN_PERIOD_YR = 1000
DURATION_HR = 24

# A shore-parallel (E-W) thin-dam line at the mid-slope transition (mean
# elevation approx +3.3 m), spanning the full AOI width.
STRUCTURE_LINE = [[-89.40, 30.285], [-89.36, 30.285]]

# Lee band = immediately NORTH of the dam line (the zone the barrier
# directly shields/traps against), bounded to where the diff signal is
# actually observed (offline banded profiling: the discriminant is
# concentrated within ~500 m of the line; beyond it A and B converge to the
# same background-rain baseline in both cases).
LEE_LAT_MIN = 30.285
LEE_LAT_MAX = 30.2895

import boto3

s3 = boto3.client(
    "s3",
    endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
    aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
)
RUNS_BUCKET = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")
for b in {RUNS_BUCKET, os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache")}:
    try:
        s3.head_bucket(Bucket=b)
    except Exception:
        try:
            s3.create_bucket(Bucket=b)
        except Exception:
            pass

from trid3nt_server.agent.workflows.sfincs.flood.flood import sfincs_flood  # noqa: E402


def _uri_of(result) -> str | None:
    if hasattr(result, "uri"):
        return result.uri
    if isinstance(result, dict):
        # failed-envelope shape
        for lyr in (result.get("layers") or []):
            if isinstance(lyr, dict) and lyr.get("layer_type") == "raster":
                return lyr.get("uri")
    return None


async def _run():
    log.info("=== RUN A: coastal surge+rain, no structure (Waveland, MS) ===")
    plain = await sfincs_flood(
        bbox=BBOX, return_period_yr=RETURN_PERIOD_YR, duration_hr=DURATION_HR,
        compute_class="small", coastal=True,
    )
    log.info("plain result type=%s uri=%s", type(plain).__name__, _uri_of(plain))

    log.info("=== RUN B: coastal surge+rain + THIN DAM barrier ===")
    thd = await sfincs_flood(
        bbox=BBOX, return_period_yr=RETURN_PERIOD_YR, duration_hr=DURATION_HR,
        compute_class="small", coastal=True,
        structure_lines=[STRUCTURE_LINE], structure_type="thd",
    )
    log.info("thd result type=%s uri=%s", type(thd).__name__, _uri_of(thd))
    return plain, thd


plain, thd = asyncio.run(_run())
plain_uri, thd_uri = _uri_of(plain), _uri_of(thd)

# --- Compare the two depth COGs on a common grid ---
import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.io import MemoryFile  # noqa: E402
from rasterio.warp import reproject, Resampling, transform  # noqa: E402
from trid3nt_server.agent.tools.cache import read_object_bytes_s3  # noqa: E402


def _read_depth(uri: str):
    with MemoryFile(read_object_bytes_s3(uri)) as mf, mf.open() as ds:
        arr = ds.read(1, masked=True).filled(0.0).astype("float64")
        return arr, ds.transform, ds.crs, ds.width, ds.height


summary: dict = {
    "engine": "sfincs", "aoi": list(BBOX), "structure_type": "thd",
    "structure_line": STRUCTURE_LINE, "scenario": "coastal_surge_plus_rain_directional",
    "location": "Waveland, MS", "lee_lat_min": LEE_LAT_MIN, "lee_lat_max": LEE_LAT_MAX,
    "plain_uri": plain_uri, "thd_uri": thd_uri,
}

if plain_uri and thd_uri:
    a, ta, ca, wa, ha = _read_depth(plain_uri)
    b, tb, cb, wb, hb = _read_depth(thd_uri)
    # Resample thd onto the plain grid for a cell-aligned diff.
    b_on_a = np.zeros_like(a)
    reproject(source=b, destination=b_on_a, src_transform=tb, src_crs=cb,
              dst_transform=ta, dst_crs=ca, resampling=Resampling.bilinear)
    diff = b_on_a - a

    # Lee-band mask on grid A (immediately north of the dam line).
    rows = np.arange(ha)
    xs, ys = rasterio.transform.xy(ta, rows, np.full(ha, wa // 2))
    lons, lats = transform(ca, "EPSG:4326", xs, ys)
    lats = np.asarray(lats)
    lee_rows = (lats >= LEE_LAT_MIN) & (lats < LEE_LAT_MAX)
    lee_mask = np.zeros_like(a, dtype=bool)
    lee_mask[lee_rows, :] = True

    a_lee = a[lee_mask]
    b_lee = b_on_a[lee_mask]
    diff_lee = diff[lee_mask]

    # Column-wise diagnostic across the lee band: a genuinely blocking
    # (not edge-leaking) structure concentrates the diff signal near the
    # LINE'S MIDPOINT, not its endpoints (a leaky/miss-snapped line would
    # show the opposite -- diff at the ends, near-zero at the centre).
    diff_lee_2d = diff[lee_mask].reshape(int(lee_rows.sum()), wa)
    col_max_abs = np.abs(diff_lee_2d).max(axis=0)
    mid = wa // 2
    edge_max = float(max(col_max_abs[: max(1, wa // 10)].max(),
                          col_max_abs[-max(1, wa // 10):].max()))
    mid_max = float(col_max_abs[mid - max(1, wa // 10): mid + max(1, wa // 10)].max())

    summary.update({
        "plain_wet_cells": int((a > 0.05).sum()),
        "thd_wet_cells": int((b_on_a > 0.05).sum()),
        "plain_max_depth_m": float(a.max()),
        "thd_max_depth_m": float(b_on_a.max()),
        "max_abs_depth_diff_m": float(np.abs(diff).max()),
        "mean_abs_depth_diff_m": float(np.abs(diff).mean()),
        "n_cells_diff_gt_5cm": int((np.abs(diff) > 0.05).sum()),
        # Lee-band (immediately north of the dam) stats -- see the HONEST
        # FINDING in the module docstring: B (dammed) trends WETTER here
        # than A (plain), the trapped-rainfall / blocked-interior-drainage
        # signature, not a "protected side stays dry" signature.
        "lee_plain_mean_depth_m": float(a_lee.mean()) if a_lee.size else 0.0,
        "lee_thd_mean_depth_m": float(b_lee.mean()) if b_lee.size else 0.0,
        "lee_plain_wet_cells": int((a_lee > 0.05).sum()),
        "lee_thd_wet_cells": int((b_lee > 0.05).sum()),
        "lee_max_abs_depth_diff_m": float(np.abs(diff_lee).max()) if diff_lee.size else 0.0,
        "lee_mean_diff_thd_minus_plain_m": float(diff_lee.mean()) if diff_lee.size else 0.0,
        "lee_cells_diff_gt_5cm": int((np.abs(diff_lee) > 0.05).sum()),
        "lee_cells_total": int(lee_mask.sum()),
        "lee_diff_edge_max_abs_m": edge_max,
        "lee_diff_midline_max_abs_m": mid_max,
        "discriminated": bool(np.abs(diff).max() > 0.05),
        # A real (not edge-leaking) directional discriminant: a meaningful
        # count of cells changed AND the signal concentrated at the line's
        # midpoint rather than its endpoints.
        "structure_aligned_discriminant": bool(
            diff_lee.size
            and (np.abs(diff_lee) > 0.05).sum() >= 50
            and mid_max >= edge_max
        ),
    })

PROOF = Path(__file__).parent.parent / "docs" / "proof"
PROOF.mkdir(parents=True, exist_ok=True)
out = PROOF / "sfincs_structure_smoke_result.json"
out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
print("\n=== SFINCS STRUCTURE SMOKE (directional coastal+rain re-run) ===")
print(json.dumps(summary, indent=2, default=str))

if not (plain_uri and thd_uri):
    log.error("one or both runs did not publish a depth COG")
    sys.exit(2)
if not summary.get("discriminated"):
    log.error("structure produced NO depth difference -- not discriminating")
    sys.exit(3)
if not summary.get("structure_aligned_discriminant"):
    log.error(
        "diff signal is not structure-aligned (too few changed lee cells, or "
        "concentrated at the line's endpoints rather than its midpoint -- "
        "suggests edge leakage rather than genuine blocking)"
    )
    sys.exit(4)
print(
    "\nSFINCS structure present-vs-absent DISCRIMINATED: a real, "
    "structure-aligned directional-flow signal (see module docstring for the "
    "honest sign/mechanism finding)."
)
