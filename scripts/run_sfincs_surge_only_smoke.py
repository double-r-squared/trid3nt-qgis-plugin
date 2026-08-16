"""SFINCS thin-dam SURGE-ONLY protected-side proof (ADR 0256 2nd addendum).

The earlier structure proofs put the co-occurring design-storm RAIN on both
sides of the barrier, so a levee that excludes Gulf surge from the south still
traps locally-falling rain behind the line -- the dammed run read WETTER behind
the dam, not drier, and A vs B looked near-identical. The rain lever
(``rainfall="none"``) now builds a SURGE-ONLY deck (no ``setup_precip_forcing``
block), so the ONLY water is the surge marching in from the sea, and a levee
does the textbook thing: the protected district floods in A and stays DRY in B.

DETERMINISTIC surge: both runs are driven by a single parametric design-storm
water-level boundary synthesized ONCE (peak scales with return period). The
composer's auto-wire would otherwise pull LIVE NOAA CO-OPS tides anchored on
wall-clock, so A and B could get different surge series and the proof would not
reproduce.

Placement is DATA-DRIVEN, never guessed:
  Phase 1 -- run A (surge-only, NO dam) and read its depth COG. Find the
  northern edge of the permanent Gulf/channel waterline (rows whose deep-cell
  span clears a min-span threshold, so isolated spurious deep DEM pixels are
  ignored) and place a shore-parallel levee line just inland of it.
  Phase 2 -- because the SFINCS water-level boundary also marks the LOW west/east
  domain edges as surge inlets, a bare shore-parallel line is FLANKED from the
  sides. Real levee districts solve this with RETURN WALLS that turn landward and
  tie into high ground, so the barrier is a U open to the high north edge (south
  line + west wall + east wall). Run B with that enclosure and compare the
  PROTECTED DISTRICT (inside the walls, dry land only, permanent deep water
  excluded): land-wet cell count + mean/max depth A vs B (B near zero), plus a
  wall-leak check (B near-wall vs interior depth).

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
  export TMPDIR=/home/nate/Documents/trid3nt-local/.tmp_staging
  mkdir -p "$TMPDIR"
  env PYTHONPATH=.:contracts venvs/agent/bin/python \
    scripts/run_sfincs_surge_only_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("run_sfincs_surge_only_smoke")

# Waveland, MS -- a GENTLE 0->~5 m coastal slope so a design-storm surge
# propagates well inland (room for a levee to matter). The south edge is the
# Gulf; the west/east domain edges are also low (bayou/sound), so the levee
# needs return walls to high ground (see the enclosure geometry below).
BBOX = (-89.40, 30.265, -89.36, 30.300)
RETURN_PERIOD_YR = 1000
DURATION_HR = 24

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

import numpy as np  # noqa: E402
import rasterio  # noqa: E402
from rasterio.io import MemoryFile  # noqa: E402
from rasterio.warp import Resampling, reproject, transform  # noqa: E402

from trid3nt_server.agent.tools.cache import read_object_bytes_s3  # noqa: E402
from trid3nt_server.agent.workflows.sfincs.flood.flood import sfincs_flood  # noqa: E402
from trid3nt_server.agent.workflows.sfincs.sfincs_forcing_autowire import (  # noqa: E402
    _synthesize_parametric_surge_forcing,
)

# DETERMINISTIC surge: build the parametric design-storm surge ONCE and drive
# BOTH runs with the identical water-level boundary. The composer's auto-wire
# would otherwise pull LIVE NOAA CO-OPS observed tides (anchored on wall-clock),
# so A and B could get different surge series and the proof would not reproduce.
# The parametric hydrograph's peak scales with return_period_yr (~4.4 m at
# 1000-yr) -- exactly the design-storm surge a shore-parallel levee is sized for.
_SURGE_WL = _synthesize_parametric_surge_forcing(
    BBOX, duration_hr=DURATION_HR, return_period_yr=RETURN_PERIOD_YR,
)


def _uri_of(result) -> str | None:
    if hasattr(result, "uri"):
        return result.uri
    if isinstance(result, dict):
        for lyr in (result.get("layers") or []):
            if isinstance(lyr, dict) and lyr.get("layer_type") == "raster":
                return lyr.get("uri")
    return None


def _read_depth(uri: str):
    with MemoryFile(read_object_bytes_s3(uri)) as mf, mf.open() as ds:
        arr = ds.read(1, masked=True).filled(0.0).astype("float64")
        return arr, ds.transform, ds.crs, ds.width, ds.height


def _row_lats(t, crs, w, h):
    """Latitude of each raster row (mid-column), EPSG:4326."""
    rows = np.arange(h)
    xs, ys = rasterio.transform.xy(t, rows, np.full(h, w // 2))
    _lon, lat = transform(crs, "EPSG:4326", xs, ys)
    return np.asarray(lat)


def _col_lons(t, crs, w, h):
    """Longitude of each raster column (mid-row), EPSG:4326."""
    cols = np.arange(w)
    xs, ys = rasterio.transform.xy(t, np.full(w, h // 2), cols)
    lon, _lat = transform(crs, "EPSG:4326", xs, ys)
    return np.asarray(lon)


# --------------------------------------------------------------------------- #
# Phase 1 -- run A (surge-only, no dam) and PROFILE it to place the dam.
# --------------------------------------------------------------------------- #
async def _run_a():
    log.info("=== PHASE 1 / RUN A: surge-only, NO dam (Waveland, MS) ===")
    return await sfincs_flood(
        bbox=BBOX, return_period_yr=RETURN_PERIOD_YR, duration_hr=DURATION_HR,
        compute_class="small", coastal=True, rainfall="none",
        surge_forcing={"waterlevel": dict(_SURGE_WL)},
    )


plain = asyncio.run(_run_a())
plain_uri = _uri_of(plain)
log.info("A (plain, surge-only) uri=%s", plain_uri)
if not plain_uri:
    log.error("run A did not publish a depth COG: %r", plain)
    sys.exit(2)

# Permanent deep water (a bed below sea level -- the Gulf/Sound + tidal
# channels) fills to the surge water surface, so a depth ABOVE the surge peak
# (~4.4 m at 1000-yr) is a resting water column, NOT dry-land surge flood. A
# shore-parallel levee is built just INLAND of that permanent waterline, so the
# dam is placed a short margin NORTH of the northernmost permanent-water row and
# the protected band (north of the line) is guaranteed dry-land surge flood the
# barrier can actually keep out. Deep permanent-water cells are excluded from the
# protected-side metric below (they are wet with or without the dam).
DEEP_M = 5.0
a, ta, ca, wa, ha = _read_depth(plain_uri)
lats_a = _row_lats(ta, ca, wa, ha)
lons_a = _col_lons(ta, ca, wa, ha)
cell_deg = float(abs(lats_a[1] - lats_a[0])) if ha > 1 else 0.0003
dlon = float(abs(lons_a[1] - lons_a[0])) if wa > 1 else 0.0003
# Northern edge of permanent deep water (the waterline a coastal levee sits
# behind). A REAL waterbody (Gulf/channel) spans many cells across a row, so a
# row counts as "permanent water" only when its deep-cell count clears a
# min-span threshold -- this rejects isolated spurious deep DEM pixels (pit
# artifacts) that would otherwise drag the waterline to the wrong latitude.
deep_row_min = max(5, int(0.05 * wa))
deep_per_row = (a > DEEP_M).sum(axis=1)
deep_lats = lats_a[deep_per_row >= deep_row_min]
if deep_lats.size == 0:
    log.error("run A shows no permanent deep water -- AOI has no coastal "
              "waterline to anchor a shore-parallel levee; pick a coastal AOI.")
    sys.exit(3)
perm_water_north_lat = float(deep_lats.max())
# Land surge flood = wet AND shallower than the permanent-water threshold.
land_wet = (a > 0.05) & (a <= DEEP_M)
# South shore-parallel levee line, a ~3-cell margin inland of the permanent
# waterline (just behind the shore, where a real coastal levee sits).
DAM_LAT = round(perm_water_north_lat + 3.0 * cell_deg, 5)

# LEVEE DISTRICT (return walls tie into high ground): the SFINCS water-level
# boundary marks EVERY active-domain edge cell at/below +2 m NAVD88 as a surge
# inlet, and along this coast the WEST and EAST domain edges are also low
# (bayou/sound), so a bare shore-parallel line is FLANKED -- surge enters from
# the side edges north of the line. Real levee districts solve this the same
# way: the shore-parallel levee turns landward at both ends (return walls) and
# ties into high ground. So the barrier is a U open to the NORTH -- a south
# line + a west wall + an east wall running to the high, non-boundary north
# edge -- sealing the protected district. The walls are inset from the low side
# edges so the boundary inlet cells sit OUTSIDE the enclosure; the protected
# metric is measured strictly INSIDE the walls.
inset_lon = 0.06 * (BBOX[2] - BBOX[0])
WALL_W_LON = round(BBOX[0] + inset_lon, 5)      # west return wall longitude
WALL_E_LON = round(BBOX[2] - inset_lon, 5)      # east return wall longitude
WALL_N_LAT = round(BBOX[3] + 0.012, 5)          # walls run past the high north edge
WALL_S_LAT = round(DAM_LAT - 3.0 * cell_deg, 5)  # walls overlap the south line at corners
# South line overlaps the walls by ~2 cells so the corners seal (no leak cell).
SOUTH_LINE = [[round(WALL_W_LON - 2 * dlon, 5), DAM_LAT],
              [round(WALL_E_LON + 2 * dlon, 5), DAM_LAT]]
WEST_WALL = [[WALL_W_LON, WALL_S_LAT], [WALL_W_LON, WALL_N_LAT]]
EAST_WALL = [[WALL_E_LON, WALL_S_LAT], [WALL_E_LON, WALL_N_LAT]]
STRUCTURE_LINES = [SOUTH_LINE, WEST_WALL, EAST_WALL]

# Protected DISTRICT = strictly inside the enclosure (north of the south line
# AND between the return walls). Deep permanent-water pixels are still excluded.
district = (
    (lats_a[:, None] >= DAM_LAT)
    & (lons_a[None, :] >= WALL_W_LON)
    & (lons_a[None, :] <= WALL_E_LON)
)
prot_land_wet_A = int((land_wet & district).sum())
prot_deep_A = int(((a > DEEP_M) & district).sum())
land_depths_north = a[land_wet & district]
prot_max_land_A = float(land_depths_north.max()) if land_depths_north.size else 0.0
log.info(
    "profile: perm-waterline north lat=%.5f -> south line lat=%.5f; return "
    "walls lon=[%.5f, %.5f] up to lat=%.5f. Protected district A land-wet=%d "
    "cells (max %.2f m), residual deep=%d",
    perm_water_north_lat, DAM_LAT, WALL_W_LON, WALL_E_LON, WALL_N_LAT,
    prot_land_wet_A, prot_max_land_A, prot_deep_A,
)
if prot_land_wet_A < 300 or prot_max_land_A < 1.0:
    log.error("protected district has too little A-flooding (land-wet=%d, "
              "max=%.2f m) -- surge does not reach past the levee line; "
              "nothing for the dam to demonstrate.",
              prot_land_wet_A, prot_max_land_A)
    sys.exit(3)


# --------------------------------------------------------------------------- #
# Phase 2 -- run B (surge-only, dam present at the profiled latitude).
# --------------------------------------------------------------------------- #
async def _run_b():
    log.info("=== PHASE 2 / RUN B: surge-only + THIN DAM levee district "
             "(south line lat=%.4f + return walls) ===", DAM_LAT)
    return await sfincs_flood(
        bbox=BBOX, return_period_yr=RETURN_PERIOD_YR, duration_hr=DURATION_HR,
        compute_class="small", coastal=True, rainfall="none",
        surge_forcing={"waterlevel": dict(_SURGE_WL)},
        structure_lines=STRUCTURE_LINES, structure_type="thd",
    )


thd = asyncio.run(_run_b())
thd_uri = _uri_of(thd)
log.info("B (thd, surge-only) uri=%s", thd_uri)

summary: dict = {
    "engine": "sfincs", "aoi": list(BBOX), "structure_type": "thd",
    "scenario": "surge_only_no_rainfall", "rainfall": "none",
    "location": "Waveland, MS", "return_period_yr": RETURN_PERIOD_YR,
    "duration_hr": DURATION_HR,
    "structure_lines": STRUCTURE_LINES, "south_line": SOUTH_LINE,
    "dam_lat": DAM_LAT, "wall_w_lon": WALL_W_LON, "wall_e_lon": WALL_E_LON,
    "wall_n_lat": WALL_N_LAT,
    "perm_water_north_lat": perm_water_north_lat,
    "protected_land_wet_A_at_placement": prot_land_wet_A,
    "plain_uri": plain_uri, "thd_uri": thd_uri,
    "plain_run_id": getattr(plain, "layer_id", None) or (plain.get("run_id") if isinstance(plain, dict) else None),
}

if plain_uri and thd_uri:
    b, tb, cb, wb, hb = _read_depth(thd_uri)
    b_on_a = np.zeros_like(a)
    reproject(source=b, destination=b_on_a, src_transform=tb, src_crs=cb,
              dst_transform=ta, dst_crs=ca, resampling=Resampling.bilinear)

    # PROTECTED DISTRICT = strictly inside the levee enclosure (north of the
    # south line AND between the return walls). Permanent deep water (> DEEP_M)
    # is a resting column the dam cannot (and should not) dry, so it is EXCLUDED
    # from the wet-count / depth stats -- the proof is about dry LAND the surge
    # floods in A and the levee district keeps dry in B.
    a_land = (a > 0.05) & (a <= DEEP_M) & district
    b_land = (b_on_a > 0.05) & (b_on_a <= DEEP_M) & district
    a_prot_depths = a[a_land]
    b_prot_depths = b_on_a[b_land]

    # Flanking check: split the district's WIDTH into a near-wall band (leftmost
    # + rightmost 15 % of the district columns) vs its interior. If surge leaks
    # THROUGH / AROUND a return wall, B's wetness concentrates at the near-wall
    # band; a sealed enclosure keeps the near-wall band as dry as the interior.
    dist_cols = np.where((lons_a >= WALL_W_LON) & (lons_a <= WALL_E_LON))[0]
    b_land_depth = np.where(b_land, b_on_a, 0.0)
    if dist_cols.size:
        c0, c1 = int(dist_cols.min()), int(dist_cols.max())
        band = max(1, int(0.15 * (c1 - c0 + 1)))
        wall_cols = list(range(c0, c0 + band)) + list(range(c1 - band + 1, c1 + 1))
        mid_cols = list(range(c0 + band, c1 - band + 1))
        b_wall_max = float(b_land_depth[:, wall_cols].max()) if wall_cols else 0.0
        b_mid_max = float(b_land_depth[:, mid_cols].max()) if mid_cols else 0.0
    else:
        b_wall_max = b_mid_max = 0.0

    a_prot_wet = int(a_land.sum())
    b_prot_wet = int(b_land.sum())
    summary.update({
        "deep_water_threshold_m": DEEP_M,
        "plain_wet_cells_total": int((a > 0.05).sum()),
        "thd_wet_cells_total": int((b_on_a > 0.05).sum()),
        "plain_max_depth_m": float(a.max()),
        "thd_max_depth_m": float(b_on_a.max()),
        "perm_water_north_lat": perm_water_north_lat,
        # PROTECTED DISTRICT (inside the enclosure, dry LAND only) -- the dry-side proof.
        "protected_cells_total": int(district.sum()),
        "protected_plain_wet_cells": a_prot_wet,
        "protected_thd_wet_cells": b_prot_wet,
        "protected_plain_mean_depth_m": float(a_prot_depths.mean()) if a_prot_depths.size else 0.0,
        "protected_thd_mean_depth_m": float(b_prot_depths.mean()) if b_prot_depths.size else 0.0,
        "protected_plain_max_depth_m": float(a_prot_depths.max()) if a_prot_depths.size else 0.0,
        "protected_thd_max_depth_m": float(b_prot_depths.max()) if b_prot_depths.size else 0.0,
        # Flanking diagnostic (B district, land only): near-wall vs interior.
        "protected_thd_edge_max_depth_m": b_wall_max,
        "protected_thd_mid_max_depth_m": b_mid_max,
        # Levee keeps the district dry: B district land-wet cells collapse vs A,
        # AND no leak finger at the walls (near-wall not materially wetter than
        # the interior).
        "protected_dry_in_b": bool(
            a_prot_wet >= 300
            and b_prot_wet <= 0.15 * max(1, a_prot_wet)
        ),
        "no_flanking": bool(b_wall_max <= max(0.30, 2.0 * b_mid_max + 0.10)),
    })

PROOF = Path(__file__).parent.parent / "docs" / "proof"
PROOF.mkdir(parents=True, exist_ok=True)
out = PROOF / "sfincs_surge_only_smoke_result.json"
out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
print("\n=== SFINCS SURGE-ONLY THIN-DAM PROOF (rainfall=none) ===")
print(json.dumps(summary, indent=2, default=str))

if not (plain_uri and thd_uri):
    log.error("one or both runs did not publish a depth COG")
    sys.exit(2)
if not summary.get("protected_dry_in_b"):
    log.error(
        "protected side (north of dam) did NOT go dry in B relative to A "
        "(A wet=%s, B wet=%s) -- dam not demonstrably protecting.",
        summary.get("protected_plain_wet_cells"),
        summary.get("protected_thd_wet_cells"),
    )
    sys.exit(4)
if not summary.get("no_flanking"):
    log.error(
        "flanking detected: B protected-edge depth %.2f m vs mid %.2f m -- surge "
        "sneaking around the dam ends.",
        summary.get("protected_thd_edge_max_depth_m", 0.0),
        summary.get("protected_thd_mid_max_depth_m", 0.0),
    )
    sys.exit(5)
print(
    "\nSFINCS surge-only thin-dam PROTECTED-SIDE proof PASSED: north of the line "
    "floods in A and stays dry in B, no flanking at the endpoints."
)
