#!/usr/bin/env python3
"""ELMFIRE input deck builder (FIRE-2).

Turns a declarative deck spec (AOI bbox + ignition point(s) + scenario
weather + paths/URIs to the fuels/topography rasters) into a run-ready
ELMFIRE case directory that the FIRE-1 proven container
(``trid3nt/elmfire:dev``, release 2025.0526) consumes as-is:

    <deck_dir>/
        inputs/
            fbfm40.tif cbh.tif cbd.tif cc.tif ch.tif      (Int16 fuels)
            dem.tif slp.tif asp.tif                       (Int16 topography)
            ws.tif wd.tif m1.tif m10.tif m100.tif         (Float32 weather)
            adj.tif phi.tif                               (Float32 constants)
            elmfire.data                                  (rendered namelist)
        outputs/                                          (empty, solver fills)
        scratch/                                          (empty, solver fills)
        deck_manifest.json                                (grid + checksums)

Run (mirrors ``tutorials/01-constant-wind/01-run.sh``):

    cd <deck_dir> && elmfire_2025.0526 ./inputs/elmfire.data

THE SAME-GRID PRECONDITION (the design doc's top silent-failure risk)
=====================================================================
ELMFIRE requires every GIS input to share ONE projection, resolution and
extent. This builder therefore (a) computes ONE target grid (EPSG:5070
Albers CONUS, 30 m, corners snapped to whole cell multiples) from the AOI,
(b) warps EVERY input onto that exact grid (nearest-neighbour for the
categorical ``fbfm40``; bilinear for continuous rasters), (c) generates the
weather/adj/phi constant rasters directly on that grid, and (d) HARD-ASSERTS
after writing that every raster in ``inputs/`` carries a byte-identical
geotransform + CRS + dimensions (:func:`verify_deck_grid`) — a mismatch is a
typed :class:`ElmfireGridMismatchError`, never a silently skewed run.

HONEST-FAILURE NORM
===================
A missing input path is :class:`ElmfireInputMissingError`; an unreadable
raster is :class:`ElmfireInputUnreadableError`; an input whose warped
footprint contains NO data over the AOI is :class:`ElmfireCoverageError`;
an ignition outside the computed domain is :class:`ElmfireIgnitionError`.
No input is ever silently defaulted to a constant raster.

NAMELIST
========
``render_namelist`` mirrors the EXACT key set of
``third_party/elmfire/tutorials/01-constant-wind/elmfire.data.in`` — the deck the
FIRE-1 container proof ran — with only the values templated (&INPUTS,
&OUTPUTS, &COMPUTATIONAL_DOMAIN, &TIME_CONTROL, &SIMULATOR, &MISCELLANEOUS).

UNITS TRAP (design doc section 6): ELMFIRE wind is **mph at 20 ft**, not
10 m m/s. The v1 scenario spec takes ``ws_mph_20ft`` directly; the HRRR
conversion for v2 is centralised here as :func:`wind_10m_ms_to_20ft_mph`.

Heavy imports (rasterio / numpy / boto3) are lazy so pure-Python validation
and namelist rendering are unit-testable anywhere (sfincs_deckbuilder
pattern).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

LOG = logging.getLogger("grace2.worker.elmfire.deck_builder")

# --------------------------------------------------------------------------- #
# Constants.
# --------------------------------------------------------------------------- #

#: Target grid defaults — EPSG:5070 (NAD83 / Conus Albers) at the LANDFIRE
#: native 30 m. Overridable per-spec (``grid.target_epsg`` / ``grid.cellsize_m``)
#: for tests and for UTM-zone runs, but 5070/30 is the FIRE-track canon.
DEFAULT_TARGET_EPSG = 5070
DEFAULT_CELLSIZE_M = 30.0

#: The nodata sentinel EVERY deck raster carries (tutorial-01 convention:
#: ``gdalwarp -dstnodata -9999``).
NODATA = -9999.0

#: Fuels + topography rasters (ELMFIRE &INPUTS *_FILENAME set), all Int16.
#: ``fbfm40`` is CATEGORICAL — nearest-neighbour resampling is mandatory.
INT_RASTERS: tuple[str, ...] = (
    "fbfm40", "cbh", "cbd", "cc", "ch", "dem", "slp", "asp",
)
CATEGORICAL_RASTERS: frozenset[str] = frozenset({"fbfm40"})

#: Constant-weather rasters (Float32) generated from the scenario values.
WEATHER_RASTERS: tuple[str, ...] = ("ws", "wd", "m1", "m10", "m100")

#: Generated Float32 constants: spread-rate adjustment + level-set init.
ADJ_VALUE = 1.0
PHI_VALUE = 1.0

#: Ignition cap (ELMFIRE &SIMULATOR supports up to 100 point ignitions).
MAX_IGNITIONS = 100

#: Manifest schema version.
MANIFEST_SCHEMA = "elmfire-deck/v1"

#: Wind conversion (v2 HRRR path): 10 m m/s -> 20 ft mph. Standard ~0.87
#: log-profile reduction 10 m -> 20 ft, then m/s -> mph.
_MS_TO_MPH = 2.236936
_WIND_10M_TO_20FT = 0.87


def wind_10m_ms_to_20ft_mph(ws_10m_ms: float) -> float:
    """Convert a 10 m wind speed in m/s to ELMFIRE's 20 ft mph convention."""
    return float(ws_10m_ms) * _MS_TO_MPH * _WIND_10M_TO_20FT


# --------------------------------------------------------------------------- #
# Typed errors (honest-failure norm).
# --------------------------------------------------------------------------- #


class ElmfireDeckError(RuntimeError):
    """Base class for deck-builder failures."""

    error_code: str = "ELMFIRE_DECK_ERROR"


class ElmfireSpecError(ElmfireDeckError):
    """Deck spec is malformed / missing required fields."""

    error_code = "ELMFIRE_DECK_SPEC_INVALID"


class ElmfireInputMissingError(ElmfireDeckError):
    """A required input raster path/URI does not exist."""

    error_code = "ELMFIRE_DECK_INPUT_MISSING"


class ElmfireInputUnreadableError(ElmfireDeckError):
    """An input raster exists but cannot be opened as a raster."""

    error_code = "ELMFIRE_DECK_INPUT_UNREADABLE"


class ElmfireCoverageError(ElmfireDeckError):
    """An input raster has NO valid data over the target grid (disjoint AOI)."""

    error_code = "ELMFIRE_DECK_INPUT_NO_COVERAGE"


class ElmfireGridMismatchError(ElmfireDeckError):
    """A deck raster's geotransform / CRS / dimensions differ from the target
    grid — the same-grid precondition would be violated."""

    error_code = "ELMFIRE_DECK_GRID_MISMATCH"


class ElmfireIgnitionError(ElmfireDeckError):
    """An ignition point falls outside the computed computational domain."""

    error_code = "ELMFIRE_DECK_IGNITION_OUTSIDE_DOMAIN"


# --------------------------------------------------------------------------- #
# Spec validation — pure Python, no heavy imports.
# --------------------------------------------------------------------------- #


def _require(d: dict, key: str, ctx: str) -> Any:
    if not isinstance(d, dict) or key not in d or d[key] is None:
        raise ElmfireSpecError(f"deck-spec missing required field {ctx}.{key}")
    return d[key]


def validate_deck_spec(spec: dict) -> dict:
    """Validate the deck spec shape; return a normalized deep-ish copy.

    Required shape::

        {
          "aoi":       {"bbox": [min_lon, min_lat, max_lon, max_lat]},   # EPSG:4326
          "ignitions": [{"lon": ..., "lat": ..., "t_ign_s": 0.0}, ...],  # 1..100
          "weather":   {"ws_mph_20ft": ..., "wd_deg": ...,
                        "m1_pct": ..., "m10_pct": ..., "m100_pct": ...,
                        "lh_pct": 30.0, "lw_pct": 60.0},                 # lh/lw optional
          "duration_s": ...,                                             # > 0
          "inputs":    {"fbfm40": path, "cbh": path, "cbd": path,
                        "cc": path, "ch": path, "dem": path,
                        "slp": path, "asp": path},                       # local or s3://
          "grid":      {"target_epsg": 5070, "cellsize_m": 30.0},        # optional
          "time":      {"dt_s": 30.0, "dtdump_s": 3600.0},               # optional
        }
    """
    if not isinstance(spec, dict):
        raise ElmfireSpecError("deck-spec must be a dict")

    aoi = _require(spec, "aoi", "")
    bbox = _require(aoi, "bbox", "aoi")
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        raise ElmfireSpecError(f"aoi.bbox must be [min_lon,min_lat,max_lon,max_lat]; got {bbox!r}")
    bbox = [float(v) for v in bbox]
    if not all(math.isfinite(v) for v in bbox):
        raise ElmfireSpecError(f"aoi.bbox contains non-finite values: {bbox!r}")
    if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ElmfireSpecError(f"aoi.bbox is degenerate: {bbox!r}")

    ignitions_raw = _require(spec, "ignitions", "")
    if not isinstance(ignitions_raw, list) or not ignitions_raw:
        raise ElmfireSpecError("ignitions must be a non-empty list of points")
    if len(ignitions_raw) > MAX_IGNITIONS:
        raise ElmfireSpecError(
            f"ELMFIRE supports at most {MAX_IGNITIONS} point ignitions; "
            f"got {len(ignitions_raw)}"
        )
    ignitions: list[dict] = []
    for i, ign in enumerate(ignitions_raw):
        lon = float(_require(ign, "lon", f"ignitions[{i}]"))
        lat = float(_require(ign, "lat", f"ignitions[{i}]"))
        t_ign = float(ign.get("t_ign_s", 0.0))
        if not (math.isfinite(lon) and math.isfinite(lat)) or t_ign < 0:
            raise ElmfireSpecError(f"ignitions[{i}] invalid: {ign!r}")
        ignitions.append({"lon": lon, "lat": lat, "t_ign_s": t_ign})

    weather_in = _require(spec, "weather", "")
    weather = {
        "ws_mph_20ft": float(_require(weather_in, "ws_mph_20ft", "weather")),
        "wd_deg": float(_require(weather_in, "wd_deg", "weather")),
        "m1_pct": float(_require(weather_in, "m1_pct", "weather")),
        "m10_pct": float(_require(weather_in, "m10_pct", "weather")),
        "m100_pct": float(_require(weather_in, "m100_pct", "weather")),
        "lh_pct": float(weather_in.get("lh_pct", 30.0)),
        "lw_pct": float(weather_in.get("lw_pct", 60.0)),
    }
    if weather["ws_mph_20ft"] < 0:
        raise ElmfireSpecError("weather.ws_mph_20ft must be >= 0")
    if not (0.0 <= weather["wd_deg"] <= 360.0):
        raise ElmfireSpecError("weather.wd_deg must be in [0, 360]")
    for k in ("m1_pct", "m10_pct", "m100_pct", "lh_pct", "lw_pct"):
        if not (0.0 < weather[k] <= 300.0):
            raise ElmfireSpecError(f"weather.{k} out of range: {weather[k]}")

    duration_s = float(_require(spec, "duration_s", ""))
    if not (math.isfinite(duration_s) and duration_s > 0):
        raise ElmfireSpecError(f"duration_s must be > 0; got {duration_s!r}")

    inputs_in = _require(spec, "inputs", "")
    inputs: dict[str, str] = {}
    for name in INT_RASTERS:
        inputs[name] = str(_require(inputs_in, name, "inputs"))

    grid = spec.get("grid") or {}
    target_epsg = int(grid.get("target_epsg", DEFAULT_TARGET_EPSG))
    cellsize_m = float(grid.get("cellsize_m", DEFAULT_CELLSIZE_M))
    if cellsize_m <= 0:
        raise ElmfireSpecError(f"grid.cellsize_m must be > 0; got {cellsize_m}")

    time_blk = spec.get("time") or {}
    dt_s = float(time_blk.get("dt_s", 30.0))
    dtdump_s = float(time_blk.get("dtdump_s", 3600.0))
    if dt_s <= 0 or dtdump_s <= 0:
        raise ElmfireSpecError("time.dt_s and time.dtdump_s must be > 0")

    return {
        "aoi": {"bbox": bbox},
        "ignitions": ignitions,
        "weather": weather,
        "duration_s": duration_s,
        "inputs": inputs,
        "grid": {"target_epsg": target_epsg, "cellsize_m": cellsize_m},
        "time": {"dt_s": dt_s, "dtdump_s": dtdump_s},
    }


# --------------------------------------------------------------------------- #
# Target-grid computation.
# --------------------------------------------------------------------------- #


def compute_target_grid(
    bbox_4326: list[float] | tuple[float, float, float, float],
    target_epsg: int = DEFAULT_TARGET_EPSG,
    cellsize_m: float = DEFAULT_CELLSIZE_M,
) -> dict:
    """Compute the ONE projected grid every deck raster is warped onto.

    Transforms the AOI bbox into ``target_epsg``, snaps the lower-left corner
    DOWN and the upper-right corner UP to whole ``cellsize_m`` multiples (so
    the grid registration is deterministic for a given bbox — identical
    re-builds produce identical geotransforms), and returns::

        {"epsg", "cellsize_m", "xll", "yll", "nx", "ny", "transform"}

    where ``transform`` is the north-up affine (a, b, c, d, e, f) tuple.
    """
    from rasterio.transform import from_origin
    from rasterio.warp import transform_bounds

    minx, miny, maxx, maxy = transform_bounds(
        "EPSG:4326", f"EPSG:{target_epsg}", *[float(v) for v in bbox_4326]
    )
    cs = float(cellsize_m)
    xll = math.floor(minx / cs) * cs
    yll = math.floor(miny / cs) * cs
    nx = int(math.ceil((maxx - xll) / cs))
    ny = int(math.ceil((maxy - yll) / cs))
    if nx <= 1 or ny <= 1:
        raise ElmfireSpecError(
            f"AOI bbox degenerates to a {nx}x{ny} grid at {cs} m — too small"
        )
    transform = from_origin(xll, yll + ny * cs, cs, cs)
    return {
        "epsg": int(target_epsg),
        "cellsize_m": cs,
        "xll": xll,
        "yll": yll,
        "nx": nx,
        "ny": ny,
        "transform": tuple(transform)[:6],
    }


# --------------------------------------------------------------------------- #
# Input localisation (local path or s3://) — honest-failure typed errors.
# --------------------------------------------------------------------------- #


def _localize_input(name: str, path_or_uri: str, scratch: Path) -> Path:
    """Resolve an input raster to a local file, downloading s3:// URIs.

    Raises :class:`ElmfireInputMissingError` when the local path does not
    exist / the S3 object is absent, and :class:`ElmfireSpecError` for an
    unsupported URI scheme. NEVER substitutes a default raster.
    """
    if path_or_uri.startswith("s3://"):
        import boto3  # lazy
        from botocore.exceptions import ClientError  # lazy

        rest = path_or_uri[len("s3://"):]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            raise ElmfireSpecError(f"inputs.{name}: malformed S3 URI {path_or_uri!r}")
        dest = scratch / f"{name}_src.tif"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            boto3.client(
                "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
            ).download_file(bucket, key, str(dest))
        except ClientError as exc:
            raise ElmfireInputMissingError(
                f"inputs.{name}: S3 object not fetchable: {path_or_uri} ({exc})"
            ) from exc
        return dest
    if "://" in path_or_uri:
        raise ElmfireSpecError(
            f"inputs.{name}: unsupported URI scheme in {path_or_uri!r} "
            "(expected a local path or s3://)"
        )
    local = Path(path_or_uri)
    if not local.is_file():
        raise ElmfireInputMissingError(
            f"inputs.{name}: raster not found at {path_or_uri!r}"
        )
    return local


# --------------------------------------------------------------------------- #
# Raster warping + writing.
# --------------------------------------------------------------------------- #


def _grid_profile(grid: dict, dtype: str) -> dict:
    """rasterio profile for a deck raster on the target grid."""
    from rasterio.transform import Affine

    return {
        "driver": "GTiff",
        "width": grid["nx"],
        "height": grid["ny"],
        "count": 1,
        "dtype": dtype,
        "crs": f"EPSG:{grid['epsg']}",
        "transform": Affine(*grid["transform"]),
        "nodata": NODATA,
        "compress": "deflate",
        "zlevel": 9,
        "tiled": False,
    }


def warp_to_grid(name: str, src_path: Path, grid: dict, dest: Path) -> dict:
    """Warp one input raster onto the target grid and write it as Int16.

    Nearest-neighbour for categorical rasters (``fbfm40`` fuel-model codes
    MUST NOT be interpolated); bilinear for continuous rasters. Cells outside
    the source footprint become ``NODATA``. Returns per-raster provenance
    (``{"source", "nodata_fraction"}``) for the manifest.

    Raises :class:`ElmfireInputUnreadableError` when the source cannot be
    opened and :class:`ElmfireCoverageError` when the warped result contains
    no valid data at all (the input does not cover the AOI).
    """
    import numpy as np
    import rasterio
    from rasterio.errors import RasterioIOError
    from rasterio.transform import Affine
    from rasterio.warp import Resampling, reproject

    try:
        src_ds = rasterio.open(src_path)
    except RasterioIOError as exc:
        raise ElmfireInputUnreadableError(
            f"inputs.{name}: cannot open raster {src_path} ({exc})"
        ) from exc

    with src_ds:
        if src_ds.crs is None:
            raise ElmfireInputUnreadableError(
                f"inputs.{name}: raster {src_path} carries NO CRS — refusing "
                "to guess (same-grid precondition)"
            )
        src_arr = src_ds.read(1).astype("float64")
        src_nodata = src_ds.nodata
        resampling = (
            Resampling.nearest
            if name in CATEGORICAL_RASTERS
            else Resampling.bilinear
        )
        dst = np.full((grid["ny"], grid["nx"]), NODATA, dtype="float64")
        reproject(
            source=src_arr,
            destination=dst,
            src_transform=src_ds.transform,
            src_crs=src_ds.crs,
            src_nodata=src_nodata,
            dst_transform=Affine(*grid["transform"]),
            dst_crs=f"EPSG:{grid['epsg']}",
            dst_nodata=NODATA,
            resampling=resampling,
        )

    valid = dst != NODATA
    nodata_fraction = 1.0 - (float(valid.sum()) / dst.size)
    if not valid.any():
        raise ElmfireCoverageError(
            f"inputs.{name}: warped raster has NO valid data over the target "
            f"grid (EPSG:{grid['epsg']} xll={grid['xll']} yll={grid['yll']} "
            f"{grid['nx']}x{grid['ny']}) — the input does not cover the AOI"
        )
    if nodata_fraction > 0.5:
        LOG.warning(
            "inputs.%s: %.1f%% of the target grid is nodata after warp",
            name,
            nodata_fraction * 100.0,
        )

    # Int16 write: round continuous values; clamp into int16 range, keeping
    # the NODATA sentinel exact.
    out = np.where(valid, np.rint(dst), NODATA)
    out = np.clip(out, -32768, 32767).astype("int16")
    profile = _grid_profile(grid, "int16")
    with rasterio.open(dest, "w", **profile) as dst_ds:
        dst_ds.write(out, 1)
    return {"source": str(src_path), "nodata_fraction": round(nodata_fraction, 6)}


def write_constant_raster(value: float, grid: dict, dest: Path) -> None:
    """Write a constant Float32 raster on the target grid (weather/adj/phi)."""
    import numpy as np
    import rasterio

    profile = _grid_profile(grid, "float32")
    arr = np.full((grid["ny"], grid["nx"]), np.float32(value), dtype="float32")
    with rasterio.open(dest, "w", **profile) as ds:
        ds.write(arr, 1)


# --------------------------------------------------------------------------- #
# The same-grid HARD ASSERT.
# --------------------------------------------------------------------------- #


def verify_deck_grid(inputs_dir: Path, grid: dict) -> list[str]:
    """HARD-ASSERT every raster in ``inputs_dir`` sits on the target grid.

    Byte-identical geotransform (exact float equality on all 6 affine
    coefficients), identical CRS, identical nx/ny. This is the mitigation for
    the design doc's top silent-failure risk — ELMFIRE trusts its inputs to
    be co-registered and will produce garbage (not an error) on a skewed
    deck. Returns the list of verified raster names; raises
    :class:`ElmfireGridMismatchError` on the FIRST mismatch with full detail.
    """
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import Affine

    expected_transform = Affine(*grid["transform"])
    expected_crs = CRS.from_epsg(grid["epsg"])
    tifs = sorted(inputs_dir.glob("*.tif"))
    if not tifs:
        raise ElmfireGridMismatchError(f"no rasters found in {inputs_dir}")
    verified: list[str] = []
    for tif in tifs:
        with rasterio.open(tif) as ds:
            if tuple(ds.transform)[:6] != tuple(expected_transform)[:6]:
                raise ElmfireGridMismatchError(
                    f"{tif.name}: geotransform {tuple(ds.transform)[:6]} != "
                    f"expected {tuple(expected_transform)[:6]}"
                )
            if ds.crs != expected_crs:
                raise ElmfireGridMismatchError(
                    f"{tif.name}: CRS {ds.crs} != expected {expected_crs}"
                )
            if (ds.width, ds.height) != (grid["nx"], grid["ny"]):
                raise ElmfireGridMismatchError(
                    f"{tif.name}: dimensions {ds.width}x{ds.height} != "
                    f"expected {grid['nx']}x{grid['ny']}"
                )
        verified.append(tif.name)
    return verified


# --------------------------------------------------------------------------- #
# Ignition transform.
# --------------------------------------------------------------------------- #


def project_ignitions(ignitions: list[dict], grid: dict) -> list[dict]:
    """Transform lon/lat ignitions into domain coordinates; assert in-domain.

    Returns ``[{"x", "y", "t_ign_s"}, ...]`` in EPSG:``grid.epsg``. An
    ignition outside [xll, xll+nx*cs] x [yll, yll+ny*cs] is a typed
    :class:`ElmfireIgnitionError` (never silently clamped).
    """
    from rasterio.warp import transform as warp_transform

    lons = [i["lon"] for i in ignitions]
    lats = [i["lat"] for i in ignitions]
    xs, ys = warp_transform("EPSG:4326", f"EPSG:{grid['epsg']}", lons, lats)
    xmax = grid["xll"] + grid["nx"] * grid["cellsize_m"]
    ymax = grid["yll"] + grid["ny"] * grid["cellsize_m"]
    out: list[dict] = []
    for ign, x, y in zip(ignitions, xs, ys):
        if not (grid["xll"] <= x <= xmax and grid["yll"] <= y <= ymax):
            raise ElmfireIgnitionError(
                f"ignition at lon={ign['lon']} lat={ign['lat']} projects to "
                f"({x:.1f}, {y:.1f}) EPSG:{grid['epsg']}, outside the domain "
                f"x=[{grid['xll']:.1f}, {xmax:.1f}] y=[{grid['yll']:.1f}, {ymax:.1f}]"
            )
        out.append({"x": float(x), "y": float(y), "t_ign_s": ign["t_ign_s"]})
    return out


# --------------------------------------------------------------------------- #
# Namelist rendering — EXACT key set of tutorials/01-constant-wind.
# --------------------------------------------------------------------------- #


def render_namelist(
    grid: dict,
    ignitions_xy: list[dict],
    weather: dict,
    duration_s: float,
    dt_s: float = 30.0,
    dtdump_s: float = 3600.0,
) -> str:
    """Render ``elmfire.data`` with the tutorial-01 key set (FIRE-1 proven).

    Every key below appears in
    ``third_party/elmfire/tutorials/01-constant-wind/elmfire.data.in`` — the deck
    the proven container consumed; only the values are templated. Paths are
    relative to the case dir (``cd <deck_dir> && elmfire_<VER>
    ./inputs/elmfire.data``), mirroring ``01-run.sh``.

    ONE ADDITIVE flag beyond the tutorial set: ``DUMP_FLAME_LENGTH = .TRUE.``
    (FIRE-3). Tutorial 01 simply does not enable it; the flag is a first-class
    ``&OUTPUTS`` dump documented at https://elmfire.io/user_guide/io.html and
    the composer publishes the flame-length raster as its own COG.
    """

    def _f(v: float) -> str:
        return f"{float(v):.4f}"

    sim_lines = [f"NUM_IGNITIONS = {len(ignitions_xy)}"]
    for i, ign in enumerate(ignitions_xy, start=1):
        sim_lines.append(f"X_IGN({i})      = {_f(ign['x'])}")
        sim_lines.append(f"Y_IGN({i})      = {_f(ign['y'])}")
        sim_lines.append(f"T_IGN({i})      = {_f(ign['t_ign_s'])}")
    sim_block = "\n".join(sim_lines)

    return f"""&INPUTS
FUELS_AND_TOPOGRAPHY_DIRECTORY = './inputs'
ASP_FILENAME                   = 'asp'
CBD_FILENAME                   = 'cbd'
CBH_FILENAME                   = 'cbh'
CC_FILENAME                    = 'cc'
CH_FILENAME                    = 'ch'
DEM_FILENAME                   = 'dem'
FBFM_FILENAME                  = 'fbfm40'
SLP_FILENAME                   = 'slp'
ADJ_FILENAME                   = 'adj'
PHI_FILENAME                   = 'phi'
DT_METEOROLOGY                 = 3600.0
WEATHER_DIRECTORY              = './inputs'
WS_FILENAME                    = 'ws'
WD_FILENAME                    = 'wd'
M1_FILENAME                    = 'm1'
M10_FILENAME                   = 'm10'
M100_FILENAME                  = 'm100'
LH_MOISTURE_CONTENT            = {_f(weather["lh_pct"])}
LW_MOISTURE_CONTENT            = {_f(weather["lw_pct"])}
/

&OUTPUTS
OUTPUTS_DIRECTORY    = './outputs'
DTDUMP               = {_f(dtdump_s)}
DUMP_FLAME_LENGTH    = .TRUE.
DUMP_FLIN            = .TRUE.
DUMP_SPREAD_RATE     = .TRUE.
DUMP_TIME_OF_ARRIVAL = .TRUE.
CONVERT_TO_GEOTIFF   = .FALSE.
/

&COMPUTATIONAL_DOMAIN
A_SRS = 'EPSG: {grid["epsg"]}'
COMPUTATIONAL_DOMAIN_CELLSIZE = {_f(grid["cellsize_m"])}
COMPUTATIONAL_DOMAIN_XLLCORNER = {_f(grid["xll"])}
COMPUTATIONAL_DOMAIN_YLLCORNER = {_f(grid["yll"])}
/

&TIME_CONTROL
SIMULATION_DT    = {_f(dt_s)}
SIMULATION_TSTOP = {_f(duration_s)}
/

&SIMULATOR
{sim_block}
WX_BILINEAR_INTERPOLATION=.TRUE.
WSMFEFF_LOW_MULT = 0.011364
/

&MISCELLANEOUS
PATH_TO_GDAL                   = '/usr/bin'
SCRATCH                        = './scratch'
/
"""


# --------------------------------------------------------------------------- #
# Manifest.
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compose_manifest(
    deck_dir: Path,
    grid: dict,
    spec: dict,
    ignitions_xy: list[dict],
    provenance: dict[str, dict],
) -> dict:
    """Compose ``deck_manifest.json``: grid, inputs, per-file sha256 checksums.

    The ``files`` map covers every file under ``inputs/`` (rasters + the
    rendered namelist) so a byte-level deck diff (the FIRE-2 golden-deck
    acceptance pattern) is a manifest diff.
    """
    inputs_dir = deck_dir / "inputs"
    files = {
        f"inputs/{p.name}": _sha256(p)
        for p in sorted(inputs_dir.iterdir())
        if p.is_file()
    }
    return {
        "schema": MANIFEST_SCHEMA,
        "grid": dict(grid),
        "aoi_bbox_4326": list(spec["aoi"]["bbox"]),
        "duration_s": spec["duration_s"],
        "weather": dict(spec["weather"]),
        "ignitions_lonlat": list(spec["ignitions"]),
        "ignitions_domain_xy": ignitions_xy,
        "sources": {k: dict(v) for k, v in provenance.items()},
        "files": files,
    }


# --------------------------------------------------------------------------- #
# The deck builder.
# --------------------------------------------------------------------------- #


def build_deck(spec: dict, deck_dir: str | Path) -> dict:
    """Build a run-ready ELMFIRE deck directory from ``spec``.

    Steps: validate spec -> compute the ONE target grid -> localize + warp
    the 8 fuels/topography rasters -> generate the 5 constant weather rasters
    + adj/phi -> HARD-ASSERT grid identity across all 15 rasters -> project
    ignitions (in-domain assert) -> render ``elmfire.data`` -> write
    ``deck_manifest.json``. Returns the manifest dict.

    Every failure mode is a typed :class:`ElmfireDeckError` subclass — a deck
    that returns from this function is co-registered, complete and runnable::

        cd <deck_dir> && elmfire_2025.0526 ./inputs/elmfire.data
    """
    spec = validate_deck_spec(spec)
    deck_dir = Path(deck_dir)
    inputs_dir = deck_dir / "inputs"
    # NOTE: ``scratch/`` is ELMFIRE's OWN scratch dir (namelist SCRATCH key);
    # downloaded source rasters go to ``_srcs/`` so the solver scratch stays
    # clean and the deck's inputs/ checksums cover only deck files.
    srcs_dir = deck_dir / "_srcs"
    for d in (inputs_dir, deck_dir / "outputs", deck_dir / "scratch"):
        d.mkdir(parents=True, exist_ok=True)

    grid = compute_target_grid(
        spec["aoi"]["bbox"],
        target_epsg=spec["grid"]["target_epsg"],
        cellsize_m=spec["grid"]["cellsize_m"],
    )
    LOG.info(
        "target grid: EPSG:%d %.0f m, %dx%d cells, xll=%.1f yll=%.1f",
        grid["epsg"], grid["cellsize_m"], grid["nx"], grid["ny"],
        grid["xll"], grid["yll"],
    )

    # Fuels + topography — warp every input onto THE grid.
    provenance: dict[str, dict] = {}
    for name in INT_RASTERS:
        src = _localize_input(name, spec["inputs"][name], srcs_dir)
        provenance[name] = warp_to_grid(name, src, grid, inputs_dir / f"{name}.tif")

    # Constant weather + adj/phi, generated directly on THE grid (Float32).
    w = spec["weather"]
    constant_values = {
        "ws": w["ws_mph_20ft"],
        "wd": w["wd_deg"],
        "m1": w["m1_pct"],
        "m10": w["m10_pct"],
        "m100": w["m100_pct"],
        "adj": ADJ_VALUE,
        "phi": PHI_VALUE,
    }
    for name, value in constant_values.items():
        write_constant_raster(value, grid, inputs_dir / f"{name}.tif")
        provenance[name] = {"source": f"constant:{value}", "nodata_fraction": 0.0}

    # HARD ASSERT: every raster sits on the byte-identical grid.
    verified = verify_deck_grid(inputs_dir, grid)
    LOG.info("grid identity verified across %d rasters", len(verified))

    # Ignitions -> domain coordinates (in-domain assert) -> namelist.
    ignitions_xy = project_ignitions(spec["ignitions"], grid)
    namelist = render_namelist(
        grid,
        ignitions_xy,
        w,
        duration_s=spec["duration_s"],
        dt_s=spec["time"]["dt_s"],
        dtdump_s=spec["time"]["dtdump_s"],
    )
    (inputs_dir / "elmfire.data").write_text(namelist)

    manifest = compose_manifest(deck_dir, grid, spec, ignitions_xy, provenance)
    (deck_dir / "deck_manifest.json").write_text(json.dumps(manifest, indent=2))
    LOG.info("deck ready at %s (%d files)", deck_dir, len(manifest["files"]))
    return manifest
