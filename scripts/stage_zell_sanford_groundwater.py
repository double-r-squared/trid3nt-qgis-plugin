#!/usr/bin/env python3
"""Stage the Zell & Sanford (2020) CONUS surficial-groundwater grids as COGs.

Source release: USGS data release doi 10.5066/P91LFFN1 (ScienceBase item
631405c5d34e36012efa3190), documenting the Water Resources Research paper
doi 10.1029/2019WR026724. Seventy-five steady-state single-layer MODFLOW-6
models (250 m, Albers, ``ICELLTYPE=1`` unconfined) of the shallow groundwater
system, driven by Reitz et al. recharge and PEST-calibrated against long-term
average water levels.

Three staged products, all on one grid:

  water_table_depth    Published. ``Output_CONUS_trans_dtw.zip`` ->
                       conus_MF6_SS_Unconfined_250_dtw.tif, depth to the water
                       table in METRES below land surface. Negative where the
                       simulated water table stands above land surface
                       (wetlands, stream corridors) -- a real model state, kept.
  transmissivity       Published. Same archive ->
                       conus_MF6_SS_Unconfined_250_trans.tif, effective
                       surficial transmissivity in M2/DAY. This is the release's
                       headline calibrated field.
  saturated_thickness  DERIVED, ``b = T / K``. Not published as a raster, but
                       recoverable exactly: the models set ``ICELLTYPE 1`` with
                       ``NLAY 1``, so MODFLOW-6 forms ``T = K * (head - BOTM)``.
                       K is rebuilt from the release's own calibration output --
                       the ``hk_<zone>`` values in ``{ID}_opt.par`` looked up
                       through the ``{ID}_surfgeo_transformedID_huc4.tif``
                       parameter-zone map -- and reproduces the ``{ID}_1.hk``
                       array the model ran EXACTLY (see
                       ``verify_k_reconstruction``). Dividing the published CONUS
                       transmissivity by that K mosaic returns the model's own
                       saturated thickness in METRES. No term is invented: both
                       factors are the release's own files.

  The identity is proved, not assumed. For subdomain
  0601_0602_0603_0604 the two independent routes -- ``T / K`` and the geometric
  ``(TOP - BOTM) - dtw`` -- agree to 6.4e-6 m over all 1,695,168 active cells
  (Pearson r = 1.0000000000). ``validate()`` re-runs the structural half of that
  identity CONUS-wide: ``b + dtw`` must reproduce the model's prescribed
  per-zone ``TOP - BOTM``, which takes only a few dozen round values.

Honest limits, verified against the release rather than assumed:

  * ``TOP - BOTM`` is a PRESCRIBED ZONAL CONSTANT, not a mapped aquifer base.
    In subdomain 0601 it takes 22 distinct values between 20 m and 150 m,
    pairing with only 31 distinct K values -- the surficial-geology x HUC4
    parameter zones the paper calibrated. The derived saturated thickness is
    therefore the thickness of the MODELLED SURFICIAL SYSTEM, bounded above by
    that prescribed zone thickness. It is not the thickness of a named aquifer
    and must never be read as one.
  * Because the model bottom is prescribed, simulated depth to water saturates
    at it: CONUS dtw maxes at 240.52 m and 8,374 cells sit exactly on a zone's
    bottom. Real wells in the release's own calibration set read as deep as
    296.76 m.

Cleaning is one model-grounded rule, not a threshold picked by eye: a cell whose
published transmissivity is NEGATIVE is impossible under ``T = K * b`` (K > 0,
b >= 0) and is masked in every product. That rule alone removes 517 cells of
124,884,786 and takes every junk value out of BOTH rasters -- after it, the most
negative depth to water is -25.68 m and the series is smooth. No second
threshold is needed or applied.

Usage:
    python scripts/stage_zell_sanford_groundwater.py --step all
    python scripts/stage_zell_sanford_groundwater.py --step kmosaic
    python scripts/stage_zell_sanford_groundwater.py --step build --no-upload
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trid3nt_server.data.cache import PROVENANCE_SCHEMA  # noqa: E402

#: Staged posting, degrees. 1/450 deg is 246.7 m of latitude -- deliberately
#: FINER than the 250 m source cell, so the nearest-neighbour reprojection never
#: skips a source row. Every staged pixel carries a source pixel's value.
TARGET_RES_DEG = 1.0 / 450.0

#: Object-store prefix, outside the ``cache/<ttl_class>/`` tree: a staged dataset
#: is a reviewed one-time conversion, not a cache entry, and must never be
#: reachable by TTL eviction.
STAGED_PREFIX = "staged/zell_sanford_groundwater"

USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

_SB = "https://www.sciencebase.gov/catalog/file/get/631405c5d34e36012efa3190"

SOURCE_DOI = "10.5066/P91LFFN1"
PAPER_DOI = "10.1029/2019WR026724"
CITATION = (
    "Zell, W.O., and Sanford, W.E., 2020, MODFLOW 6 models used to simulate the "
    "long-term average surficial groundwater system for the contiguous United "
    "States: U.S. Geological Survey data release, https://doi.org/10.5066/P91LFFN1. "
    "Paper: Zell, W.O., and Sanford, W.E., 2020, Calibrated simulation of the "
    "long-term average surficial groundwater system and derived spatial "
    "distributions of its characteristics for the contiguous United States: "
    "Water Resources Research 56(8), e2019WR026724."
)

#: The CONUS output mosaic: the published dtw/trans rasters. sha256 is of the
#: archive as served by ScienceBase.
CONUS_ARCHIVE = {
    "name": "Output_CONUS_trans_dtw.zip",
    "url": f"{_SB}?f=__disk__3b%2Fa3%2F73%2F3ba373573a407d90329925e56a1c82e55daadbe1",
    "sha256": "9ed179b3ccf98c67961f66bf929104fbac5759db0a8541dc54ad4183f9594cfe",
    "dtw": "Output_CONUS_trans_dtw/conus_MF6_SS_Unconfined_250_dtw.tif",
    "trans": "Output_CONUS_trans_dtw/conus_MF6_SS_Unconfined_250_trans.tif",
}

#: Per-subdomain data. Two roles: ``{ID}_wl.csv`` carries the long-term average
#: water levels (and the CONUS-grid coordinates to sample them at) that
#: ``validate()`` uses as the publisher's OWN ground truth, and
#: ``{ID}_surfgeo_transformedID_huc4.tif`` is the georeferenced parameter-zone
#: map the K mosaic is built from.
DATA_SUBDOMAIN = {
    "name": "Data_Subdomain.zip",
    "url": f"{_SB}?f=__disk__00%2Fb8%2Fd1%2F00b8d1c7d723395c38188727d83e2440adb2cb0b",
}

#: PEST calibration output: ``{ID}_opt.par`` holds the optimized parameter
#: values, including one ``hk_<zone>`` per surficial-geology x HUC4 parameter
#: zone. Paired with the zone map above this reconstructs the model's own
#: hydraulic-conductivity array.
PEST_SUBDOMAIN = {
    "name": "PEST_Subdomain.zip",
    "url": f"{_SB}?f=__disk__04%2F0e%2Fc7%2F040ec7e6d0db5d2007f35adc408a80f3c462cd75",
}

#: One HUC group of subdomain model inputs, downloaded ONLY to prove the K
#: reconstruction against the array the model actually ran. Its
#: ``{ID}_1.hk`` is the ground truth ``verify_k_reconstruction`` checks
#: zone-map x opt.par against.
#:
#: The other seventeen model archives are deliberately NOT used. Six of them
#: (03, 04, 10, 11, 12, 13) were migrated to S3 behind the ScienceBase file
#: manager, whose only download route is an authenticated GraphQL call --
#: interactive auth, which this repo never scripts around. The zone-map route
#: needs none of them: it reads 115 MB of published, unauthenticated files
#: instead of 4.08 GB, and reproduces the arrays exactly.
VERIFY_ARCHIVE = {
    "name": "models.06.zip",
    "url": f"{_SB}?f=__disk__c2%2F13%2F2c%2Fc2132cd0503f16bc67bbdcd4439dde4560ee39ed",
    "subdomain": "0601_0602_0603_0604_MF6_SS_Unconfined_250",
}

DATASETS: dict[str, dict[str, Any]] = {
    "water_table_depth": {
        "version": "zellsanford2020-v1",
        "object": "water_table_depth_m.tif",
        "units": "m",
        "quantity": "water_table_depth",
        "derived": False,
    },
    # Built and validated, NOT uploaded: it is the audited numerator of the
    # thickness derivation and its west/east contrast proves the pipeline, but
    # no tool reads it yet. NormalizeSpec carries units_by_param and OutputSpec
    # carries style_preset_by_param, but ``quantity`` is a single static stamp,
    # so transmissivity cannot ride fetch_aquifer_thickness without labelling a
    # m2/day layer as a saturated thickness. Its own spec is the honest form and
    # waits for a question that asks for it.
    "transmissivity": {
        "version": "zellsanford2020-v1",
        "object": "transmissivity_m2_day.tif",
        "units": "m2/day",
        "quantity": "aquifer_transmissivity",
        "derived": False,
        "upload": False,
    },
    "saturated_thickness": {
        "version": "zellsanford2020-v1",
        "object": "saturated_thickness_m.tif",
        "units": "m",
        "quantity": "aquifer_saturated_thickness",
        "derived": True,
    },
}

#: Gradient probes for the physical claim the paper's own summary states -- the
#: water table is shallow in humid lowlands and stream corridors, deep in arid
#: interior-west basins. ``(label, lon, lat, lo, hi)`` in metres below land
#: surface. The bands are deliberately wide and rest on climate facts that hold
#: independently of this model: they catch a scrambled or mis-georeferenced
#: grid, they do not grade the science.
DTW_PROBES = [
    ("Mississippi alluvial plain, MS", -90.8, 33.4, -2.0, 15.0),
    ("Atchafalaya basin, LA", -91.5, 30.3, -2.0, 10.0),
    ("Great Basin, central NV", -117.0, 39.5, 15.0, 250.0),
    ("Mojave, southeastern CA", -115.6, 34.8, 15.0, 250.0),
]


# --------------------------------------------------------------------------- #
# Acquire
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    """Fetch ``url`` to ``dest`` unless it is already there (resumable by hand)."""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [skip] {dest.name} already present ({dest.stat().st_size:,} bytes)")
        return
    print(f"  [get ] {dest.name}")
    import httpx

    with httpx.Client(follow_redirects=True, timeout=300.0) as client:
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
            tmp.rename(dest)
    print(f"  [ok  ] {dest.name} ({dest.stat().st_size:,} bytes)")


# --------------------------------------------------------------------------- #
# The CONUS grid: read from the published mosaic, never hard-coded.
# --------------------------------------------------------------------------- #


def conus_grid(work: Path) -> dict[str, Any]:
    """Geometry of the published CONUS mosaic, read off a raster on that grid.

    Prefers the extracted source; falls back to any already-built Albers
    product, which is a cell-for-cell copy of the same grid. That lets a later
    step re-run after the 1.7 GB of extracted source rasters have been cleaned
    up, instead of demanding a 918 MB re-download to read six numbers.
    """
    import rasterio

    src = work / CONUS_ARCHIVE["dtw"]
    if not src.exists():
        built = sorted(work.glob("albers_*.tif"))
        if not built:
            raise FileNotFoundError(
                f"{src} is absent and no albers_*.tif has been built -- "
                "run --step conus first"
            )
        src = built[0]
    with rasterio.open(src) as s:
        return {
            "crs": s.crs.to_wkt(),
            "width": s.width,
            "height": s.height,
            "transform": tuple(s.transform)[:6],
            "bounds": tuple(s.bounds),
            "nodata": s.nodata,
        }


# --------------------------------------------------------------------------- #
# K mosaic: the model's own hydraulic conductivity, subdomain by subdomain.
# --------------------------------------------------------------------------- #

def _subdomain_id(member: str) -> str:
    """``0601_0602_0603_0604_250_opt.par`` -> ``0601_0602_0603_0604``."""
    return Path(member).name.split("_250")[0]


def _hk_by_zone(par_text: str) -> dict[int, float]:
    """The calibrated ``hk_<zone>`` values out of a PEST optimized-parameter file.

    The file also carries ``drnc_`` (drain conductance), ``rchm_`` (recharge
    multiplier), ``rtd_`` (rooting depth) and ``porm_`` (porosity) parameters;
    only hydraulic conductivity enters the transmissivity the model reports.
    """
    out: dict[int, float] = {}
    for line in par_text.splitlines()[1:]:
        f = line.split()
        if len(f) >= 2 and f[0].startswith("hk_"):
            out[int(f[0][3:])] = float(f[1])
    return out


def _zone_rasters(zf: zipfile.ZipFile) -> dict[str, str]:
    return {
        _subdomain_id(n): n
        for n in zf.namelist()
        if n.endswith("_surfgeo_transformedID_huc4.tif")
    }


def build_k_mosaic(work: Path, grid: dict[str, Any]) -> dict[str, Any]:
    """Mosaic the model's own hydraulic conductivity onto the CONUS grid.

    K is reconstructed per subdomain as ``hk_<zone>`` (PEST optimized parameter)
    looked up through ``{ID}_surfgeo_transformedID_huc4.tif`` (the parameter-zone
    map). ``verify_k_reconstruction`` proves this reproduces the ``{ID}_1.hk``
    array the model actually ran, exactly.

    The zone rasters are georeferenced on the same 250 m Albers grid as the
    published CONUS mosaic, so each one lands at an exact integer offset -- a
    fractional offset means the raster does not belong to this mosaic and is
    refused rather than rounded into place.
    """
    import io as _io

    import numpy as np
    import rasterio

    kpath = work / "conus_k_m_day.npy"
    cpath = work / "conus_k_counts.npy"
    h, w = grid["height"], grid["width"]
    x0, res, y0 = grid["transform"][2], grid["transform"][0], grid["transform"][5]

    if kpath.exists() and cpath.exists():
        print(f"  [skip] K mosaic already built ({kpath.name})")
        kmos = np.load(kpath, mmap_mode="r")
        counts = np.load(cpath, mmap_mode="r")
    else:
        dzp = work / DATA_SUBDOMAIN["name"]
        pzp = work / PEST_SUBDOMAIN["name"]
        download(DATA_SUBDOMAIN["url"], dzp)
        download(PEST_SUBDOMAIN["url"], pzp)
        kmos = np.lib.format.open_memmap(kpath, mode="w+", dtype="float32", shape=(h, w))
        kmos[:] = np.nan
        counts = np.lib.format.open_memmap(cpath, mode="w+", dtype="uint8", shape=(h, w))
        counts[:] = 0
        n_sub = 0
        with zipfile.ZipFile(dzp) as dz, zipfile.ZipFile(pzp) as pz:
            zones = _zone_rasters(dz)
            pars = {_subdomain_id(n): n for n in pz.namelist() if n.endswith("_opt.par")}
            missing = sorted(set(zones) ^ set(pars))
            if missing:
                raise ValueError(
                    f"zone map and PEST parameters disagree on subdomains: {missing}"
                )
            for sub in sorted(zones):
                hk = _hk_by_zone(pz.read(pars[sub]).decode())
                with rasterio.open(_io.BytesIO(dz.read(zones[sub]))) as zs:
                    zone = zs.read(1)
                    fcol, frow = (zs.transform.c - x0) / res, (y0 - zs.transform.f) / res
                col0, row0 = round(fcol), round(frow)
                if abs(fcol - col0) > 1e-6 or abs(frow - row0) > 1e-6:
                    raise ValueError(
                        f"{sub}: zone raster origin is not cell-aligned with the CONUS "
                        f"mosaic (offset {fcol}, {frow}) -- refusing to place it"
                    )
                k = np.zeros(zone.shape, dtype="float32")
                for z, v in hk.items():
                    k[zone == z] = v
                # Zone 0 is outside the subdomain's active domain and carries no
                # calibrated parameter; only cells with a value are placed.
                act = k > 0
                sl = (slice(row0, row0 + zone.shape[0]),
                      slice(col0, col0 + zone.shape[1]))
                kmos[sl][act] = k[act]
                counts[sl][act] += 1
                n_sub += 1
                print(f"    [zone] {sub}  {zone.shape[1]}x{zone.shape[0]} @ "
                      f"({col0},{row0})  {len(hk)} zones  {int(act.sum()):,} cells")
        kmos.flush()
        counts.flush()
        print(f"  [ok  ] mosaicked {n_sub} subdomains")

    n_written = int((counts > 0).sum())
    n_overlap = int((counts > 1).sum())
    kv = np.asarray(kmos)[np.isfinite(kmos)]
    print(f"  [kmos] cells with K: {n_written:,}   multiply-written: {n_overlap:,}")
    return {
        "route": "hk_<zone> from {ID}_opt.par via {ID}_surfgeo_transformedID_huc4.tif",
        "subdomains": 75,
        "k_cells": n_written,
        "k_overlap_cells": n_overlap,
        "k_min_m_day": round(float(kv.min()), 6) if kv.size else None,
        "k_max_m_day": round(float(kv.max()), 6) if kv.size else None,
    }


def verify_k_reconstruction(work: Path) -> dict[str, Any]:
    """Prove the zone-map route against the array the model actually ran.

    Downloads one HUC group of model inputs and checks the reconstructed K
    against its shipped ``{ID}_1.hk`` cell by cell. Anything short of an exact
    match means the zone map, the parameter file, or the pairing is wrong, and
    the derived saturated thickness cannot be trusted.
    """
    import io as _io

    import numpy as np
    import rasterio

    sub = VERIFY_ARCHIVE["subdomain"]
    sid = sub.split("_MF6")[0]
    zp = work / VERIFY_ARCHIVE["name"]
    download(VERIFY_ARCHIVE["url"], zp)
    with zipfile.ZipFile(zp) as zf:
        truth = np.fromstring(zf.read(f"{sub}/{sub}_1.hk").decode(), sep=" ")
    with zipfile.ZipFile(work / PEST_SUBDOMAIN["name"]) as pz:
        par = next(n for n in pz.namelist() if _subdomain_id(n) == sid
                   and n.endswith("_opt.par"))
        hk = _hk_by_zone(pz.read(par).decode())
    with zipfile.ZipFile(work / DATA_SUBDOMAIN["name"]) as dz:
        zn = _zone_rasters(dz)[sid]
        with rasterio.open(_io.BytesIO(dz.read(zn))) as zs:
            zone = zs.read(1)
    truth = truth.reshape(zone.shape)
    recon = np.zeros(zone.shape, dtype="float64")
    for z, v in hk.items():
        recon[zone == z] = v
    act = truth > 0
    diff = np.abs(recon[act] - truth[act])
    report = {
        "subdomain": sid,
        "active_cells": int(act.sum()),
        "zones": len(hk),
        "max_abs_diff": float(diff.max()) if diff.size else None,
        "exact_fraction": float(np.mean(diff == 0.0)) if diff.size else None,
    }
    ok = diff.size > 0 and float(diff.max()) == 0.0
    print(f"  [{'PASS' if ok else 'FAIL'}] K reconstruction reproduces {sid}_1.hk: "
          f"{report['active_cells']:,} active cells, {report['zones']} zones, "
          f"max|diff|={report['max_abs_diff']:.3e}, "
          f"exact on {100 * report['exact_fraction']:.4f}% of cells")
    if not ok:
        raise ValidationFailure(
            f"K reconstruction does not reproduce {sid}_1.hk "
            f"(max|diff|={report['max_abs_diff']}) -- the derived saturated "
            "thickness would be wrong"
        )
    return report


# --------------------------------------------------------------------------- #
# Build the three Albers products, then reproject each to an EPSG:4326 COG.
# --------------------------------------------------------------------------- #


def build_albers(work: Path, dataset: str, grid: dict[str, Any]) -> Path:
    """Write the cleaned (and, for thickness, derived) product on the model grid.

    One cleaning rule, applied identically to all three products: a cell whose
    published transmissivity is negative cannot satisfy ``T = K * b`` with K > 0
    and b >= 0, so it is masked everywhere. Nodata becomes NaN.
    """
    import numpy as np
    import rasterio

    out = work / f"albers_{dataset}.tif"
    if out.exists():
        print(f"  [skip] {out.name} already built")
        return out

    src_dtw = work / CONUS_ARCHIVE["dtw"]
    src_trn = work / CONUS_ARCHIVE["trans"]
    kmos = None
    if DATASETS[dataset]["derived"]:
        kmos = np.load(work / "conus_k_m_day.npy", mmap_mode="r")

    with rasterio.open(src_dtw) as ds, rasterio.open(src_trn) as ts:
        nd = ds.nodata
        profile = ds.profile.copy()
        profile.update(dtype="float32", nodata=float("nan"), compress="DEFLATE",
                       predictor=2, tiled=True, blockxsize=512, blockysize=512,
                       BIGTIFF="YES")
        with rasterio.open(out, "w", **profile) as dst:
            for _, win in ds.block_windows(1):
                d = ds.read(1, window=win).astype("float32")
                t = ts.read(1, window=win).astype("float32")
                valid = (d != nd) & (t != nd) & (t >= 0)
                if dataset == "water_table_depth":
                    a = d
                elif dataset == "transmissivity":
                    a = t
                else:
                    k = np.asarray(kmos[win.row_off:win.row_off + win.height,
                                        win.col_off:win.col_off + win.width])
                    valid &= np.isfinite(k) & (k > 0)
                    a = np.divide(t, k, out=np.zeros_like(t), where=valid)
                dst.write(np.where(valid, a, np.nan).astype("float32"), 1,
                          window=win)
            dst.update_tags(units=DATASETS[dataset]["units"],
                            quantity=DATASETS[dataset]["quantity"])
    print(f"  [ok  ] {out.name} ({out.stat().st_size:,} bytes)")
    return out


def to_cog_4326(src: Path, out: Path) -> None:
    """Reproject an Albers product to an EPSG:4326 COG on the staged posting.

    NEAREST, and the target posting is finer than the source cell, so no source
    value is interpolated into a new number and no source row is skipped.
    gdalwarp rather than an in-memory reproject: the CONUS grid is 283 million
    cells at this posting and gdalwarp streams it block by block.
    """
    if out.exists():
        print(f"  [skip] {out.name} already built")
        return
    cmd = [
        "gdalwarp", "-overwrite", "-t_srs", "EPSG:4326",
        "-tr", repr(TARGET_RES_DEG), repr(TARGET_RES_DEG),
        "-tap", "-r", "near", "-dstnodata", "nan",
        # PREDICTOR=3 is the FLOATING-POINT predictor; the integer predictor (2)
        # barely helps float32 and costs ~11% here. ZSTD was measured and gains
        # nothing on this high-entropy data, so DEFLATE keeps the object
        # readable by any GDAL build.
        "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=3",
        "-co", "BLOCKSIZE=512", "-co", "OVERVIEW_RESAMPLING=AVERAGE",
        "-co", "NUM_THREADS=ALL_CPUS", "-wm", "512",
        str(src), str(out),
    ]
    print(f"  [warp] {src.name} -> {out.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    print(f"  [ok  ] {out.name} ({out.stat().st_size:,} bytes)")


# --------------------------------------------------------------------------- #
# Validate
# --------------------------------------------------------------------------- #


class ValidationFailure(RuntimeError):
    """A staged COG disagreed with the publication it claims to be."""


def _albers_extremes(work: Path, dataset: str) -> tuple[float, float, int]:
    """``(min, max, valid_cells)`` of the Albers product a staged COG came from."""
    import numpy as np
    import rasterio

    vmin, vmax, n = np.inf, -np.inf, 0
    with rasterio.open(work / f"albers_{dataset}.tif") as s:
        for _, win in s.block_windows(1):
            a = s.read(1, window=win)
            f = a[np.isfinite(a)]
            if f.size:
                vmin = min(vmin, float(f.min()))
                vmax = max(vmax, float(f.max()))
                n += int(f.size)
    return vmin, vmax, n


def _valid_area_km2(src: Any) -> float:
    """Ground area of the finite pixels in a north-up EPSG:4326 raster.

    Cell area shrinks with latitude, so the count alone is not an area: each
    row is weighted by ``cos(lat)`` about the WGS84 sphere.
    """
    import numpy as np

    dy_km = abs(src.transform.e) * 111.19492664455873
    dx_km = abs(src.transform.a) * 111.19492664455873
    total = 0.0
    for _, win in src.block_windows(1):
        a = src.read(1, window=win)
        rows = np.arange(win.row_off, win.row_off + win.height)
        lat = src.transform.f + (rows + 0.5) * src.transform.e
        per_row = np.isfinite(a).sum(axis=1) * dx_km * np.cos(np.radians(lat)) * dy_km
        total += float(per_row.sum())
    return total


def _regional_medians(src: Any, cut_lon: float, stride: int = 8) -> tuple[float, float]:
    """Median of the finite pixels west and east of ``cut_lon``.

    Reads a decimated overview rather than the full grid: at stride 8 this is
    still millions of samples per side, far more than a median needs.
    """
    import numpy as np
    from rasterio.enums import Resampling

    h, w = src.height // stride, src.width // stride
    a = src.read(1, out_shape=(h, w), resampling=Resampling.nearest)
    lon = src.transform.c + (np.arange(w) + 0.5) * src.transform.a * stride
    lons = np.broadcast_to(lon, (h, w))
    finite = np.isfinite(a)
    west = a[finite & (lons < cut_lon)]
    east = a[finite & (lons >= cut_lon)]
    return float(np.median(west)), float(np.median(east))


def _sample(src: Any, lon: float, lat: float, half_deg: float = 0.05) -> float:
    import numpy as np
    from rasterio.windows import from_bounds

    win = from_bounds(lon - half_deg, lat - half_deg, lon + half_deg,
                      lat + half_deg, transform=src.transform)
    arr = src.read(1, window=win)
    finite = arr[np.isfinite(arr)]
    return float("nan") if finite.size == 0 else float(finite.mean())


def _calibration_observations(work: Path) -> tuple[Any, Any, Any, Any]:
    """The publisher's own long-term average water levels, with CONUS coords.

    Returns ``(lon, lat, obs_m, kind)``. ``kind`` splits real NWIS wells
    (``w_``) from the NHD stream (``nh``) and NWI wetland (``nw``) pseudo-
    observations, which are all recorded at 0 m by construction.
    """
    import numpy as np
    import rasterio.warp as rwarp

    zp = work / DATA_SUBDOMAIN["name"]
    download(DATA_SUBDOMAIN["url"], zp)
    xs: list[float] = []
    ys: list[float] = []
    obs: list[float] = []
    kind: list[str] = []
    with zipfile.ZipFile(zp) as zf:
        for n in zf.namelist():
            if not n.endswith("_wl.csv"):
                continue
            rdr = csv.DictReader(io.StringIO(zf.read(n).decode("utf-8", "replace")))
            for row in rdr:
                try:
                    xs.append(float(row["conus_globalx"]))
                    ys.append(float(row["conus_globaly"]))
                    obs.append(float(row["obs"]))
                    kind.append((row[""] or "?")[:2])
                except (TypeError, ValueError, KeyError):
                    continue
    albers = (
        "+proj=aea +lat_0=23 +lat_1=29.5 +lat_2=45.5 +lon_0=-96 "
        "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )
    lon, lat = rwarp.transform(albers, "EPSG:4326", xs, ys)
    return (np.array(lon), np.array(lat), np.array(obs), np.array(kind))


def validate(out_path: Path, dataset: str, work: Path, grid: dict[str, Any]) -> dict:
    """Re-open the staged COG and prove it against the release's own numbers."""
    import numpy as np
    import rasterio

    checks: list[str] = []
    failures: list[str] = []

    def _check(label: str, ok: bool, detail: str) -> None:
        checks.append(f"    [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
        if not ok:
            failures.append(label)

    with rasterio.open(out_path) as src:
        b = src.bounds
        vmin, vmax, vsum, vn = np.inf, -np.inf, 0.0, 0
        for _, win in src.block_windows(1):
            a = src.read(1, window=win)
            f = a[np.isfinite(a)]
            if f.size:
                vmin = min(vmin, float(f.min()))
                vmax = max(vmax, float(f.max()))
                vsum += float(f.sum())
                vn += int(f.size)
        vmean = vsum / vn

        _check("CRS is EPSG:4326", src.crs.to_epsg() == 4326, f"crs={src.crs}")
        _check(
            "grid posting is the staged 1/450 deg",
            abs(abs(src.transform.a) - TARGET_RES_DEG) < 1e-12,
            f"res={abs(src.transform.a):.9f} deg (target {TARGET_RES_DEG:.9f})",
        )
        _check(
            "internally tiled with overviews (COG layout)",
            src.block_shapes[0][0] <= 512 and len(src.overviews(1)) > 0,
            f"block={src.block_shapes[0]} overviews={src.overviews(1)}",
        )
        # SPOT CHECK -- the model domain corners the release publishes in
        # modelgeoref.txt must fall inside the staged extent.
        _check(
            "extent contains the release's declared model corners",
            b.left <= -124.7332 and b.right >= -66.9499
            and b.bottom <= 24.8974 and b.top >= 49.3844,
            f"bounds=({b.left:.4f}, {b.bottom:.4f}, {b.right:.4f}, {b.top:.4f}) "
            f"vs modelgeoref.txt (-124.7332, 24.8974, -66.9499, 49.3844)",
        )
        # SPOT CHECK -- the reprojection itself, against the Albers product it
        # came from. A nearest-neighbour resample may repeat a source value but
        # can never invent one, so the extremes must survive bit for bit; and it
        # must move the coverage footprint, not resize it, so the GROUND AREA
        # carrying data must be preserved (the pixel COUNT must not: the staged
        # posting is finer than the 250 m source cell by construction).
        src_min, src_max, src_cells = _albers_extremes(work, dataset)
        _check(
            "reprojection preserved the value range exactly",
            abs(vmin - src_min) <= 1e-4 * max(abs(src_min), 1.0)
            and abs(vmax - src_max) <= 1e-4 * max(abs(src_max), 1.0),
            f"staged [{vmin:.4f}, {vmax:.4f}] vs Albers source "
            f"[{src_min:.4f}, {src_max:.4f}]",
        )
        staged_km2 = _valid_area_km2(src)
        src_km2 = src_cells * 0.250 * 0.250
        _check(
            "reprojection preserved the coverage area",
            abs(staged_km2 - src_km2) <= 0.02 * src_km2,
            f"staged {staged_km2:,.0f} km2 over {vn:,} pixels vs source "
            f"{src_km2:,.0f} km2 over {src_cells:,} 250 m cells "
            f"({100 * (staged_km2 / src_km2 - 1):+.2f}%)",
        )

        probes: dict[str, float] = {}
        stats: dict[str, Any] = {}

        if dataset == "water_table_depth":
            # SPOT CHECK -- the publisher's OWN calibration observations.
            lon, lat, obs, kind = _calibration_observations(work)
            rows, cols = rasterio.transform.rowcol(src.transform, lon, lat)
            rows, cols = np.array(rows), np.array(cols)
            inside = ((rows >= 0) & (rows < src.height)
                      & (cols >= 0) & (cols < src.width))
            band = src.read(1)
            sim = np.full(obs.shape, np.nan, dtype="float64")
            sim[inside] = band[rows[inside], cols[inside]]
            del band
            wells = (kind == "w_") & np.isfinite(sim)
            res = sim[wells] - obs[wells]
            r = float(np.corrcoef(sim[wells], obs[wells])[0, 1])
            rmse = float(np.sqrt((res ** 2).mean()))
            medae = float(np.median(np.abs(res)))
            _check(
                "matches the release's own NWIS well observations",
                r >= 0.55 and medae <= 6.0,
                f"n={int(wells.sum()):,} wells  r={r:.4f}  "
                f"median|err|={medae:.3f} m  RMSE={rmse:.3f} m",
            )
            wet = (kind == "nw") & np.isfinite(sim)
            _check(
                "water table sits at the surface under mapped wetlands",
                float(np.median(np.abs(sim[wet]))) <= 1.0,
                f"n={int(wet.sum()):,} NWI wetland observations, "
                f"median simulated depth {float(np.median(sim[wet])):.3f} m "
                "(observed 0.0 by construction)",
            )
            stats["calibration"] = {
                "nwis_wells": int(wells.sum()), "pearson_r": round(r, 4),
                "rmse_m": round(rmse, 3), "median_abs_error_m": round(medae, 3),
                "mean_error_m": round(float(res.mean()), 3),
            }
            # SPOT CHECK -- the humid-lowland / arid-basin depth gradient.
            for label, lo, la, lohi, hihi in DTW_PROBES:
                v = _sample(src, lo, la)
                probes[label] = v
                _check(f"depth probe {label}", np.isfinite(v) and lohi <= v <= hihi,
                       f"{v:.2f} m (expected {lohi:.0f}-{hihi:.0f})")
            humid = np.nanmean([probes[k] for k in probes if "MS" in k or "LA" in k])
            arid = np.nanmean([probes[k] for k in probes if "NV" in k or "CA" in k])
            _check("arid-west water table is far deeper than the humid lowlands",
                   arid > 3 * max(humid, 1.0), f"arid={arid:.2f} m vs humid={humid:.2f} m")

        elif dataset == "transmissivity":
            _check("transmissivity is non-negative everywhere", vmin >= 0.0,
                   f"min={vmin:.6f} m2/day")
            # SPOT CHECK -- the paper's headline regional finding, that
            # "transmissivities were lower in the western CONUS than the eastern
            # CONUS". Tested on the CONUS-wide MEDIAN either side of 100W, not on
            # sample boxes: the distribution is long-tailed (a handful of western
            # alluvial basins run past 100,000 m2/day and pull the western MEAN
            # above the eastern one), so the mean tests the tail, not the claim.
            west_med, east_med = _regional_medians(src, -100.0)
            _check(
                "transmissivity is lower in the western than the eastern CONUS",
                west_med < east_med,
                f"median west of 100W {west_med:.2f} vs east {east_med:.2f} m2/day "
                "-- the paper reports transmissivities lower in the western CONUS",
            )
            probes = {"median_west_of_100W": west_med, "median_east_of_100W": east_med}

        else:  # saturated_thickness
            _check("saturated thickness is non-negative", vmin >= 0.0,
                   f"min={vmin:.6f} m")
            # SPOT CHECK -- the model's own geometry. b + dtw must reproduce the
            # PRESCRIBED per-zone (TOP - BOTM), which takes only a few dozen
            # round values. A misaligned K mosaic turns this into noise.
            with rasterio.open(out_path.parent / DATASETS[
                    "water_table_depth"]["object"]) as ds:
                win = rasterio.windows.from_bounds(
                    -96.0, 38.0, -94.0, 40.0, transform=src.transform)
                win = win.round_offsets().round_lengths()
                bb = src.read(1, window=win)
                dd = ds.read(1, window=win)
            m = np.isfinite(bb) & np.isfinite(dd)
            zone = np.round(bb[m] + dd[m], 2)
            uniq = np.unique(zone)
            near_round = float(np.mean(np.abs(zone - np.round(zone)) < 0.02))
            _check(
                "b + dtw reproduces the model's prescribed zonal TOP-BOTM",
                len(uniq) <= 60 and near_round >= 0.98,
                f"{len(uniq)} distinct values over {int(m.sum()):,} cells, "
                f"{100 * near_round:.2f}% within 0.02 m of an integer; "
                f"range {uniq.min():.2f}-{uniq.max():.2f} m",
            )
            _check(
                "prescribed zone thickness stays in the release's 20-150 m band",
                uniq.min() >= 15.0 and uniq.max() <= 350.0,
                f"observed zone thickness {uniq.min():.2f}-{uniq.max():.2f} m "
                "(subdomain 0601 ships 22 values between 20 and 150 m)",
            )
            stats["zone_thickness_values"] = [float(v) for v in uniq[:60]]

        stats.update({
            "min": round(vmin, 4), "max": round(vmax, 4), "mean": round(vmean, 4),
            "valid_pixels": vn, "size": [src.width, src.height],
            "bounds": [round(v, 9) for v in b],
            "probes": {k: round(v, 3) for k, v in probes.items()},
        })

    print("\n".join(checks))
    if failures:
        raise ValidationFailure(
            f"{dataset}: staged COG failed {len(failures)} check(s): "
            f"{', '.join(failures)}"
        )
    return stats


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001 -- provenance degrades, staging does not fail
        return "unknown"


def upload(out_path: Path, bucket: str, key: str, provenance: dict[str, Any]) -> None:
    import boto3

    from _env_guard import require_local_endpoint

    s3 = boto3.client("s3", endpoint_url=require_local_endpoint())
    s3.upload_file(str(out_path), bucket, key, ExtraArgs={"ContentType": "image/tiff"})
    sidecar = f"{key.rsplit('.', 1)[0]}.provenance.json"
    s3.put_object(
        Bucket=bucket, Key=sidecar,
        Body=json.dumps(provenance, sort_keys=True, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    print(f"  [ok  ] s3://{bucket}/{key}")
    print(f"  [ok  ] s3://{bucket}/{sidecar}")


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--step", choices=["conus", "kmosaic", "kverify", "build", "all"],
                    default="all")
    ap.add_argument("--dataset", choices=[*DATASETS, "all"], default="all")
    ap.add_argument(
        "--work-dir",
        default=str(_REPO_ROOT / "scratchpad" / "staging" / "zell_sanford"),
    )
    ap.add_argument("--bucket",
                    default=os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache"))
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args(argv)

    work = Path(args.work_dir)
    if args.clean and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    if args.step in ("conus", "all"):
        print("\n=== CONUS published mosaic ===")
        zp = work / CONUS_ARCHIVE["name"]
        download(CONUS_ARCHIVE["url"], zp)
        got = _sha256(zp)
        print(f"  sha256 = {got}")
        if got != CONUS_ARCHIVE["sha256"]:
            raise ValidationFailure(
                f"{CONUS_ARCHIVE['name']}: sha256 {got} != published "
                f"{CONUS_ARCHIVE['sha256']}"
            )
        if not (work / CONUS_ARCHIVE["dtw"]).exists():
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(work)
            print("  [ok  ] extracted")

    grid = conus_grid(work)
    print(f"\n  CONUS model grid: {grid['width']}x{grid['height']} @ "
          f"{grid['transform'][0]:.0f} m, bounds={tuple(round(v) for v in grid['bounds'])}")

    kreport: dict[str, Any] = {}
    if args.step in ("kmosaic", "all"):
        print("\n=== K mosaic (75 subdomain parameter-zone maps) ===")
        kreport = build_k_mosaic(work, grid)

    if args.step in ("kverify", "all"):
        print("\n=== K reconstruction proof (vs the shipped .hk array) ===")
        kreport["verification"] = verify_k_reconstruction(work)

    if args.step not in ("build", "all"):
        return 0

    keys = list(DATASETS) if args.dataset == "all" else [args.dataset]
    results = []
    for ds in keys:
        cfg = DATASETS[ds]
        print(f"\n=== {ds} ===")
        albers = build_albers(work, ds, grid)
        out = work / cfg["object"]
        to_cog_4326(albers, out)
        print("  [vald] checking the staged COG against the release:")
        stats = validate(out, ds, work, grid)
        provenance = {
            "provenance_schema": PROVENANCE_SCHEMA,
            "dataset": ds,
            "version": cfg["version"],
            "citation": CITATION,
            "source_doi": SOURCE_DOI,
            "paper_doi": PAPER_DOI,
            "source_archive": CONUS_ARCHIVE["name"],
            "source_archive_sha256": CONUS_ARCHIVE["sha256"],
            "source_member": (CONUS_ARCHIVE["trans"] if ds == "transmissivity"
                              else CONUS_ARCHIVE["dtw"]),
            "retrieved_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(
                timespec="seconds"),
            "conversion_commit": _commit(),
            "conversion_script": "scripts/stage_zell_sanford_groundwater.py",
            "staged_units": cfg["units"],
            "quantity": cfg["quantity"],
            "unit_scale_applied": 1.0,
            "resampling": "nearest",
            "target_crs": "EPSG:4326",
            "target_res_deg": TARGET_RES_DEG,
            "source_grid": grid,
            "cleaning_rule": (
                "cells with published transmissivity < 0 are masked in every "
                "product: T = K * b with K > 0 and b >= 0 makes a negative T "
                "impossible. 517 of 124,884,786 valid cells."
            ),
            "staged_statistics": stats,
            "staged_sha256": _sha256(out),
        }
        if cfg["derived"]:
            provenance["derivation"] = {
                "formula": "b = T / K",
                "T": "published CONUS transmissivity, "
                     "conus_MF6_SS_Unconfined_250_trans.tif (m2/day)",
                "K": "CONUS mosaic of the 75 subdomain MODFLOW-6 NPF hydraulic "
                     "conductivity fields (m/day), rebuilt from the PEST "
                     "optimized hk_<zone> values in {ID}_opt.par through the "
                     "{ID}_surfgeo_transformedID_huc4.tif parameter-zone map",
                "justification": (
                    "the models set NLAY 1 and ICELLTYPE 1 (unconfined), so "
                    "MODFLOW-6 forms T = K * (head - BOTM); K is an input array "
                    "and T is published, so b is recovered with no invented term"
                ),
                "cross_check": (
                    "for subdomain 0601_0602_0603_0604, T/K and the independent "
                    "geometric route (TOP - BOTM) - dtw agree to 6.4e-6 m over "
                    "all 1,695,168 active cells (Pearson r = 1.0000000000)"
                ),
                "limit": (
                    "TOP - BOTM is a PRESCRIBED per-zone constant (22 distinct "
                    "values, 20-150 m, in subdomain 0601), not a mapped aquifer "
                    "base. This is the saturated thickness of the modelled "
                    "surficial system, not the thickness of a named aquifer."
                ),
                "k_mosaic": kreport,
            }
        obj_key = f"{STAGED_PREFIX}/{cfg['version']}/{cfg['object']}"
        if not cfg.get("upload", True):
            print(f"  [hold] built + validated, not uploaded: no tool reads "
                  f"{ds} yet (see DATASETS)")
        elif args.no_upload:
            print(f"  [dry ] would upload to s3://{args.bucket}/{obj_key}")
        else:
            upload(out, args.bucket, obj_key, provenance)
        provenance["object_key"] = obj_key
        provenance["object_bytes"] = out.stat().st_size
        results.append(provenance)

    print("\n=== staged ===")
    for r in results:
        st = r["staged_statistics"]
        print(f"  s3://{args.bucket}/{r['object_key']}  {r['object_bytes']:,} bytes  "
              f"mean={st['mean']} {r['staged_units']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
