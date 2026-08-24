#!/usr/bin/env python3
"""Stage the published CONUS groundwater-recharge grids as COGs in object storage.

Two independent published sources, staged side by side so the fetcher can serve
either one and a user can compare them:

  reitz2017   USGS Reitz, Sanford, Senay & Cazenas (2017), "Average annual rates
              of evapotranspiration, quick-flow runoff, and recharge for the
              CONUS, 2000-2013", doi 10.5066/F7PN93P0 (paper doi
              10.1111/1752-1688.12546). File TotalRecharge_0013.zip -> 0013/
              RC_0013.tif, 30 arc-sec (~800 m), EPSG:4269, METERS per year.
  wolock2003  USGS Wolock (2003), "Estimated mean annual natural ground-water
              recharge in the conterminous United States", doi 10.5066/P9FSSVF3.
              File rech48grd.zip -> an ESRI GRID, 1 km, EPSG:5070, MILLIMETERS
              per year, base-flow-index x mean-annual-runoff (methodologically
              independent of the Reitz empirical regressions).

The staged product is one float32 EPSG:4326 COG per source in MILLIMETERS per
year. The only value transform applied to reitz2017 is the m/yr -> mm/yr factor
of 1000 (recorded in the provenance sidecar); wolock2003 is already mm/yr and its
integers pass through unchanged. Reprojection is NEAREST so no source value is
ever interpolated into a new number.

Validation is not optional: the script re-opens the staged COG and checks it
against the numbers the publisher printed in the release metadata (grid
geometry, value domain, whole-grid statistics) plus the paper's stated
arid-west / humid-east gradient, and refuses to upload when a check fails.

Usage:
    python scripts/stage_groundwater_recharge.py --dataset all
    python scripts/stage_groundwater_recharge.py --dataset reitz2017 --no-upload
    python scripts/stage_groundwater_recharge.py --dataset all --work-dir /tmp/gwr
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
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

from trid3nt_server.tools.cache import PROVENANCE_SCHEMA  # noqa: E402

#: Target grid: 30 arc-sec (1/120 deg), the native Reitz posting. wolock2003 is
#: resampled ONTO this grid so both staged rasters share one cell geometry and a
#: cross-source comparison needs no regridding at read time.
TARGET_RES_DEG = 1.0 / 120.0

#: Object-store prefix. Outside the ``cache/<ttl_class>/`` tree on purpose: a
#: staged dataset is the product of a reviewed one-time conversion, not a cache
#: entry, and must never be reachable by TTL eviction.
STAGED_PREFIX = "staged/groundwater_recharge"

USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

_SB = "https://www.sciencebase.gov/catalog/file/get"

DATASETS: dict[str, dict[str, Any]] = {
    "reitz2017": {
        "url": (
            f"{_SB}/55d383a9e4b0518e35468e58"
            "?f=__disk__6e%2F94%2F11%2F6e94119954d56f6fc05ca7380c4db5be55d1bf08"
        ),
        "archive": "TotalRecharge_0013.zip",
        "member": "0013/RC_0013.tif",
        "version": "reitz2017-v1",
        "object": "recharge_total_2000_2013_mmyr.tif",
        "source_doi": "10.5066/F7PN93P0",
        "paper_doi": "10.1111/1752-1688.12546",
        "citation": (
            "Reitz, M., Sanford, W.E., Senay, G.B., Cazenas, J., 2017, Annual "
            "estimates of recharge, quick-flow runoff, and evapotranspiration for "
            "the contiguous U.S. using empirical regression equations: JAWRA "
            "53(4), 961-983."
        ),
        # The publisher's own numbers, read off the release metadata + the
        # GeoTIFF PAM sidecar shipped inside the archive. These are what the
        # staged product is proved against.
        "published": {
            "native_units": "m/yr",
            "unit_scale": 1000.0,
            "west": -125.020833333,
            "east": -66.479166669,
            # No "south"/"north"/"res_deg" here: validate() checks the grid
            # posting against TARGET_RES_DEG (this script's own target, not a
            # publisher figure) and the CONUS extent generically -- it has no
            # per-source south/north spot check, so declaring unverified
            # publisher corner values nothing reads would be dead + misleading.
            # PAM statistics from 0013/RC_0013.tif.aux.xml, in m/yr.
            "stat_min_native": 0.0,
            "stat_max_native": 4.4557666778564,
            "stat_mean_native": 0.14813994980833,
        },
    },
    "wolock2003": {
        "url": (
            f"{_SB}/63140610d34e36012efa3838"
            "?f=__disk__a5%2F64%2Fe6%2Fa564e6c8c769eadff66b195bf4aa11efb232b988"
        ),
        "archive": "rech48grd.zip",
        "member": "rech48grd",  # an ESRI GRID directory, opened by the AIG driver
        "version": "wolock2003-v1",
        "object": "recharge_bfi_runoff_mmyr.tif",
        "source_doi": "10.5066/P9FSSVF3",
        "paper_doi": None,
        "citation": (
            "Wolock, D.M., 2003, Estimated mean annual natural ground-water "
            "recharge in the conterminous United States: U.S. Geological Survey "
            "Open-File Report 03-311."
        ),
        "published": {
            "native_units": "mm/yr",
            "unit_scale": 1.0,
            # attrunit/rdommin/rdommax from rech48grd/metadata.xml.
            "domain_min": 0.0,
            "domain_max": 2150.0,
        },
    },
}

#: Gradient probes for the paper's central qualitative claim: recharge is the
#: remainder of precipitation after ET and quick flow, so it must track the CONUS
#: precipitation gradient. ``(label, lon, lat, lo, hi)`` in mm/yr. The bands are
#: deliberately wide and anchored on climate facts that hold independently of
#: either publisher's model -- this check catches a scrambled or mis-georeferenced
#: grid, it does not grade the science.
GRADIENT_PROBES = [
    ("Sonoran desert, AZ", -112.6, 33.0, 0.0, 60.0),
    ("Olympic Peninsula, WA", -123.8, 47.8, 500.0, 5000.0),
    ("Gulf coastal plain, MS", -89.5, 30.5, 50.0, 1500.0),
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
    print(f"  [get ] {url}")
    import httpx

    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
            tmp.rename(dest)
    print(f"  [ok  ] {dest.name} ({dest.stat().st_size:,} bytes)")


def extract(archive: Path, work: Path) -> None:
    marker = work / f".extracted-{archive.name}"
    if marker.exists():
        print(f"  [skip] {archive.name} already extracted")
        return
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(work)
    marker.touch()
    print(f"  [ok  ] extracted {archive.name}")


# --------------------------------------------------------------------------- #
# Convert
# --------------------------------------------------------------------------- #


def to_cog_4326(src_path: Path, out_path: Path, unit_scale: float) -> dict[str, Any]:
    """Reproject ``src_path`` to a float32 EPSG:4326 COG in mm/yr.

    NEAREST resampling: every staged value is a source value, so a spot check
    against the publication compares like with like. The unit scale is applied
    only to finite pixels; source nodata becomes NaN.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    with rasterio.open(src_path) as src:
        src_crs = src.crs
        src_nodata = src.nodata
        if src_crs.is_geographic and abs(abs(src.transform.a) - TARGET_RES_DEG) < 1e-6:
            # A geographic source already on the target posting (the Reitz grid is
            # 30 arc-sec NAD83) keeps its OWN cell geometry: NAD83 -> WGS84 is a
            # sub-meter datum shift, so a pixel-aligned nearest reproject copies
            # every value through untouched. Letting GDAL pick an unaligned target
            # grid instead makes each output cell take its nearest neighbour, which
            # drags nodata across coastlines and silently rewrites shoreline values.
            dst_transform, dst_w, dst_h = src.transform, src.width, src.height
        else:
            dst_transform, dst_w, dst_h = calculate_default_transform(
                src_crs, "EPSG:4326", src.width, src.height, *src.bounds,
                resolution=TARGET_RES_DEG,
            )
        dst = np.full((dst_h, dst_w), np.nan, dtype="float32")
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src_crs,
            src_nodata=src_nodata,
            dst_transform=dst_transform,
            dst_crs="EPSG:4326",
            dst_nodata=float("nan"),
            resampling=Resampling.nearest,
        )
        src_summary = {
            "src_crs": str(src_crs),
            "src_size": [src.width, src.height],
            "src_res": [abs(src.transform.a), abs(src.transform.e)],
            "src_bounds": [round(v, 9) for v in src.bounds],
            "src_dtype": src.dtypes[0],
            "src_nodata": None if src_nodata is None else float(src_nodata),
        }

    # A source sentinel that survived as a huge negative float (the Reitz
    # -3.4e38 fill) would poison every statistic; drop it with the finite mask
    # before scaling rather than trusting the driver's nodata tag alone.
    dst = np.where(np.isfinite(dst) & (dst > -1e30), dst, np.nan).astype("float32")
    if unit_scale != 1.0:
        dst = (dst * unit_scale).astype("float32")

    profile = {
        "driver": "COG",
        "dtype": "float32",
        "count": 1,
        "height": dst.shape[0],
        "width": dst.shape[1],
        "crs": "EPSG:4326",
        "transform": dst_transform,
        "nodata": float("nan"),
        "compress": "DEFLATE",
        "blocksize": 512,
        "overview_resampling": "average",
    }
    with rasterio.open(out_path, "w", **profile) as dstf:
        dstf.write(dst, 1)
        dstf.update_tags(units="mm/yr", quantity="groundwater_recharge")
    return src_summary


# --------------------------------------------------------------------------- #
# Validate
# --------------------------------------------------------------------------- #


class ValidationFailure(RuntimeError):
    """A staged COG disagreed with the publication it claims to be."""


def _sample(src: Any, lon: float, lat: float, half_deg: float = 0.05) -> float:
    """Mean of the finite pixels in a small box around ``(lon, lat)``."""
    import numpy as np
    from rasterio.windows import from_bounds

    win = from_bounds(
        lon - half_deg, lat - half_deg, lon + half_deg, lat + half_deg,
        transform=src.transform,
    )
    arr = src.read(1, window=win)
    finite = arr[np.isfinite(arr)]
    return float("nan") if finite.size == 0 else float(finite.mean())


def validate(out_path: Path, key: str, cfg: dict[str, Any]) -> list[str]:
    """Re-open the staged COG and prove it against the publisher's own numbers."""
    import numpy as np
    import rasterio

    pub = cfg["published"]
    checks: list[str] = []
    failures: list[str] = []

    def _check(label: str, ok: bool, detail: str) -> None:
        checks.append(f"    [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
        if not ok:
            failures.append(label)

    with rasterio.open(out_path) as src:
        arr = src.read(1)
        finite = arr[np.isfinite(arr)]
        vmin, vmax, vmean = (
            float(finite.min()), float(finite.max()), float(finite.mean())
        )
        b = src.bounds

        _check(
            "CRS is EPSG:4326",
            src.crs.to_epsg() == 4326,
            f"crs={src.crs}",
        )
        _check(
            "grid posting is 30 arc-sec",
            abs(abs(src.transform.a) - TARGET_RES_DEG) < 1e-9,
            f"res={abs(src.transform.a):.9f} deg (target {TARGET_RES_DEG:.9f})",
        )
        _check(
            "internally tiled with overviews (COG layout)",
            bool(src.block_shapes[0][0] <= 512) and len(src.overviews(1)) > 0,
            f"block={src.block_shapes[0]} overviews={src.overviews(1)}",
        )
        _check(
            "extent covers CONUS",
            b.left <= -124.0 and b.right >= -67.0 and b.bottom <= 25.5 and b.top >= 49.0,
            f"bounds=({b.left:.4f}, {b.bottom:.4f}, {b.right:.4f}, {b.top:.4f})",
        )

        if key == "reitz2017":
            # SPOT CHECK 1 -- the publisher's declared corner coordinates.
            _check(
                "west/east edge matches the release metadata bounding coords",
                abs(b.left - pub["west"]) < 0.02 and abs(b.right - pub["east"]) < 0.02,
                f"staged W/E = {b.left:.6f}/{b.right:.6f} vs published "
                f"{pub['west']:.6f}/{pub['east']:.6f}",
            )
            # SPOT CHECK 2 -- the archive's own PAM statistics, unit-converted.
            scale = pub["unit_scale"]
            exp_max = pub["stat_max_native"] * scale
            exp_mean = pub["stat_mean_native"] * scale
            _check(
                "whole-grid maximum matches the archive PAM statistic",
                abs(vmax - exp_max) <= 0.02 * exp_max,
                f"staged max={vmax:.2f} mm/yr vs published "
                f"{pub['stat_max_native']:.6f} m/yr = {exp_max:.2f} mm/yr",
            )
            _check(
                "whole-grid mean matches the archive PAM statistic",
                abs(vmean - exp_mean) <= 0.03 * exp_mean,
                f"staged mean={vmean:.2f} mm/yr vs published "
                f"{pub['stat_mean_native']:.8f} m/yr = {exp_mean:.2f} mm/yr",
            )
            _check(
                "whole-grid minimum matches the archive PAM statistic",
                abs(vmin - pub["stat_min_native"] * scale) < 1e-3,
                f"staged min={vmin:.4f} mm/yr vs published "
                f"{pub['stat_min_native']:.1f} m/yr",
            )
        else:
            # SPOT CHECK 1+2 -- the metadata's declared attribute value domain.
            _check(
                "value domain matches the metadata attribute range",
                vmin >= pub["domain_min"] - 1e-6 and vmax <= pub["domain_max"] + 1e-6,
                f"staged [{vmin:.2f}, {vmax:.2f}] mm/yr vs published "
                f"[{pub['domain_min']:.0f}, {pub['domain_max']:.0f}] mm/yr",
            )
            _check(
                "domain maximum is actually reached (no silent clipping)",
                vmax >= 0.9 * pub["domain_max"],
                f"staged max={vmax:.2f} mm/yr vs published domain max "
                f"{pub['domain_max']:.0f} mm/yr",
            )

        # SPOT CHECK 3 -- the paper's arid-west / humid-east recharge gradient.
        probes: list[tuple[str, float]] = []
        for label, lon, lat, lo, hi in GRADIENT_PROBES:
            val = _sample(src, lon, lat)
            probes.append((label, val))
            _check(
                f"gradient probe {label}",
                np.isfinite(val) and lo <= val <= hi,
                f"{val:.1f} mm/yr (expected {lo:.0f}-{hi:.0f})",
            )
        arid = probes[0][1]
        humid = probes[1][1]
        _check(
            "arid southwest recharge is far below the humid southeast",
            np.isfinite(arid) and np.isfinite(humid) and humid > 4 * max(arid, 1.0),
            f"{probes[0][0]}={arid:.1f} vs {probes[1][0]}={humid:.1f} mm/yr",
        )

        stats = {
            "min_mm_yr": round(vmin, 4),
            "max_mm_yr": round(vmax, 4),
            "mean_mm_yr": round(vmean, 4),
            "valid_pixels": int(finite.size),
            "size": [src.width, src.height],
            "bounds": [round(v, 9) for v in b],
            "probes_mm_yr": {label: round(v, 2) for label, v in probes},
        }

    print("\n".join(checks))
    if failures:
        raise ValidationFailure(
            f"{key}: staged COG failed {len(failures)} check(s): {', '.join(failures)}"
        )
    return stats  # type: ignore[return-value]


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


def upload(
    out_path: Path,
    bucket: str,
    key: str,
    provenance: dict[str, Any],
) -> None:
    import boto3

    from _env_guard import require_local_endpoint

    s3 = boto3.client("s3", endpoint_url=require_local_endpoint())
    s3.upload_file(str(out_path), bucket, key, ExtraArgs={"ContentType": "image/tiff"})
    sidecar = f"{key.rsplit('.', 1)[0]}.provenance.json"
    s3.put_object(
        Bucket=bucket,
        Key=sidecar,
        Body=json.dumps(provenance, sort_keys=True, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    print(f"  [ok  ] s3://{bucket}/{key}")
    print(f"  [ok  ] s3://{bucket}/{sidecar}")


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def stage_one(key: str, work: Path, bucket: str, do_upload: bool) -> dict[str, Any]:
    cfg = DATASETS[key]
    print(f"\n=== {key} ({cfg['citation'].split(',')[0]}) ===")
    archive = work / cfg["archive"]
    download(cfg["url"], archive)
    checksum = _sha256(archive)
    print(f"  sha256({cfg['archive']}) = {checksum}")
    extract(archive, work)

    member = work / cfg["member"]
    if not member.exists():
        raise FileNotFoundError(f"{key}: archive member {cfg['member']} not found in {work}")

    out_path = work / cfg["object"]
    print(f"  [conv] {cfg['member']} -> {cfg['object']} (EPSG:4326 COG, mm/yr)")
    src_summary = to_cog_4326(member, out_path, cfg["published"]["unit_scale"])
    print(f"  [ok  ] {out_path.name} ({out_path.stat().st_size:,} bytes)")
    print("  [vald] checking the staged COG against the publication:")
    stats = validate(out_path, key, cfg)

    provenance = {
        "provenance_schema": PROVENANCE_SCHEMA,
        "dataset": key,
        "version": cfg["version"],
        "citation": cfg["citation"],
        "source_doi": cfg["source_doi"],
        "paper_doi": cfg["paper_doi"],
        "source_url": cfg["url"],
        "source_archive": cfg["archive"],
        "source_archive_sha256": checksum,
        "source_member": cfg["member"],
        "retrieved_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "conversion_commit": _commit(),
        "conversion_script": "scripts/stage_groundwater_recharge.py",
        "native_units": cfg["published"]["native_units"],
        "staged_units": "mm/yr",
        "unit_scale_applied": cfg["published"]["unit_scale"],
        "resampling": "nearest",
        "target_crs": "EPSG:4326",
        "target_res_deg": TARGET_RES_DEG,
        "source": src_summary,
        "staged_statistics": stats,
        "staged_sha256": _sha256(out_path),
    }
    obj_key = f"{STAGED_PREFIX}/{cfg['version']}/{cfg['object']}"
    if do_upload:
        upload(out_path, bucket, obj_key, provenance)
    else:
        print(f"  [dry ] would upload to s3://{bucket}/{obj_key}")
    provenance["object_key"] = obj_key
    provenance["object_bytes"] = out_path.stat().st_size
    return provenance


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", choices=[*DATASETS, "all"], default="all")
    ap.add_argument(
        "--work-dir",
        default=str(_REPO_ROOT / "scratchpad" / "staging" / "groundwater_recharge"),
        help="scratch directory for downloads + conversions",
    )
    ap.add_argument(
        "--bucket",
        default=os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache"),
        help="object-store bucket to stage into",
    )
    ap.add_argument("--no-upload", action="store_true", help="convert + validate only")
    ap.add_argument("--clean", action="store_true", help="wipe the work dir first")
    args = ap.parse_args(argv)

    work = Path(args.work_dir)
    if args.clean and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    keys = list(DATASETS) if args.dataset == "all" else [args.dataset]
    results = []
    for key in keys:
        results.append(stage_one(key, work, args.bucket, not args.no_upload))

    print("\n=== staged ===")
    for r in results:
        print(
            f"  s3://{args.bucket}/{r['object_key']}  "
            f"{r['object_bytes']:,} bytes  "
            f"mean={r['staged_statistics']['mean_mm_yr']} mm/yr"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
