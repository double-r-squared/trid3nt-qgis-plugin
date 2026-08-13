#!/usr/bin/env python3
"""ELMFIRE input deck builder.

Turns a declarative deck spec (AOI bbox + ignition point(s) + scenario
weather + paths/URIs to the fuels/topography rasters) into a run-ready
ELMFIRE case directory that the proven container
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
container proof ran — with only the values templated (&INPUTS,
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

LOG = logging.getLogger("trid3nt.worker.elmfire.deck_builder")

# --------------------------------------------------------------------------- #
# Constants.
# --------------------------------------------------------------------------- #

#: Target grid defaults — EPSG:5070 (NAD83 / Conus Albers) at the LANDFIRE
#: native 30 m. Overridable per-spec (``grid.target_epsg`` / ``grid.cellsize_m``)
#: for tests and for UTM-zone runs, but 5070/30 is the canon.
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


class ElmfireSpecUnknownFieldsError(ElmfireSpecError):
    """Deck spec carries a top-level field the parser does not read (ADR 0158)."""

    error_code = "ELMFIRE_DECK_SPEC_UNKNOWN_FIELDS"


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


#: PARSER VERSION -- bump on a top-level deck-spec shape change. Named in the
#: strict-field error (ADR 0158).
_PARSER_VERSION = "elmfire-spec-1"

#: Every top-level deck-spec key ``validate_deck_spec`` reads. The
#: ``simulator_extra``/``outputs_extra``/``inputs_extra`` namelist-knob
#: extension surface is NOT part of this dict-based spec -- it is a SEPARATE,
#: fully-typed Python kwarg path (``run_elmfire.build_constant_flat_deck`` etc
#: call ``render_namelist`` directly), so it does not belong in this allowlist.
_KNOWN_SPEC_FIELDS = frozenset(
    {"aoi", "ignitions", "weather", "duration_s", "inputs", "grid", "time"}
)


def _reject_unknown_spec_fields(spec: dict) -> None:
    """Raise loudly if ``spec`` carries a top-level key ``validate_deck_spec``
    never reads (ADR 0158 -- the ADR 0148 lesson: a stale image silently
    dropped unknown build_spec fields and two registered knob templates ran
    as no-ops)."""
    unknown = sorted(set(spec) - _KNOWN_SPEC_FIELDS)
    if unknown:
        raise ElmfireSpecUnknownFieldsError(
            f"deck-spec carries unknown field(s) {unknown} that parser "
            f"{_PARSER_VERSION} does not read -- this SILENTLY no-ops the "
            f"intended field rather than applying it. Either the caller has a "
            f"typo, or the worker image is stale (rebuild it -- ADR 0148). "
            f"Known fields: {sorted(_KNOWN_SPEC_FIELDS)}."
        )


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
    _reject_unknown_spec_fields(spec)

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
    write_constant_raster_typed(value, grid, dest, dtype="float32")


def write_constant_raster_typed(
    value: float, grid: dict, dest: Path, *, dtype: str = "float32"
) -> None:
    """Write a constant raster on the target grid at the requested dtype.

    Generalizes ``write_constant_raster`` so the verification deck can force a
    constant INT16 fuel-model raster (e.g. GR2 = FBFM code 102 -- a uniform
    grass fuel bed) and constant Int16 flat-topography rasters, alongside the
    Float32 weather constants."""
    import numpy as np
    import rasterio

    profile = _grid_profile(grid, dtype)
    arr = np.full((grid["ny"], grid["nx"]), value, dtype=dtype)
    with rasterio.open(dest, "w", **profile) as ds:
        ds.write(arr, 1)


def write_weather_bands(values_per_band: list[float], grid: dict, dest: Path) -> None:
    """Write a MULTI-BAND constant-in-space Float32 weather raster.

    ELMFIRE reads the ws/wd/m1/m10/m100 weather rasters as multi-band BSQ
    (``WS%NBANDS`` bands); band ``k`` is the value at meteorology time ``k``, and
    the solver linearly interpolates between the bracketing bands every
    ``DT_METEOROLOGY`` seconds (``elmfire_level_set.f90`` ITLO/ITHI_METEOROLOGY).
    Each band here is spatially UNIFORM (the value from ``values_per_band[k]``) --
    the synthetic transient-weather schedule varies a scalar over TIME, not space.
    One band reproduces the single-band constant raster byte-for-byte in intent
    (but callers use :func:`write_constant_raster_typed` for the 1-band case so
    the constant deck stays byte-identical). Raises :class:`ElmfireSpecError`
    on an empty band list."""
    import numpy as np
    import rasterio

    if not values_per_band:
        raise ElmfireSpecError("write_weather_bands: values_per_band is empty")
    nbands = len(values_per_band)
    profile = _grid_profile(grid, "float32")
    profile["count"] = nbands
    with rasterio.open(dest, "w", **profile) as ds:
        for k, value in enumerate(values_per_band, start=1):
            ds.write(
                np.full((grid["ny"], grid["nx"]), float(value), dtype="float32"), k
            )


#: weather-raster name -> the ELMFIRE-unit ``weather`` dict key it carries.
#: Mirrors ``run_elmfire._WEATHER_RASTER_KEYS`` (the typed flat-deck path) --
#: duplicated here (not imported) because ``run_elmfire`` imports THIS module,
#: not the reverse.
WEATHER_RASTER_KEYS: dict[str, str] = {
    "ws": "ws_mph_20ft", "wd": "wd_deg",
    "m1": "m1_pct", "m10": "m10_pct", "m100": "m100_pct",
}

#: schedule-entry alias -> canonical ELMFIRE weather-dict key. A schedule entry
#: may use the short raster names or the canonical ``*_pct``/``*_mph_20ft``/
#: ``*_deg`` keys interchangeably.
_SCHEDULE_ALIASES: dict[str, str] = {
    "ws": "ws_mph_20ft", "ws_mph_20ft": "ws_mph_20ft",
    "wd": "wd_deg", "wd_deg": "wd_deg",
    "m1": "m1_pct", "m1_pct": "m1_pct",
    "m10": "m10_pct", "m10_pct": "m10_pct",
    "m100": "m100_pct", "m100_pct": "m100_pct",
}


def normalize_weather_schedule(
    schedule: list[dict[str, float]], base_weather: dict[str, float]
) -> list[dict[str, float]]:
    """Expand a transient weather schedule into full per-band ELMFIRE-unit dicts.

    Each entry may specify any subset of ws/wd/m1/m10/m100 (short or canonical
    keys); an absent field inherits ``base_weather`` (the deck spec's base
    weather). Returns one dict per band carrying all five
    :data:`WEATHER_RASTER_KEYS` values. Raises :class:`ElmfireSpecError` on an
    empty schedule or an unknown key (never a silently dropped schedule
    field) -- the same honesty norm as the strict spec-field allowlist."""
    if not schedule:
        raise ElmfireSpecError("weather_schedule is empty")
    bands: list[dict[str, float]] = []
    for i, entry in enumerate(schedule):
        if not isinstance(entry, dict):
            raise ElmfireSpecError(
                f"weather_schedule[{i}] must be a dict, got {type(entry).__name__}"
            )
        band = {k: float(base_weather[k]) for k in WEATHER_RASTER_KEYS.values()}
        for k, v in entry.items():
            canon = _SCHEDULE_ALIASES.get(str(k))
            if canon is None:
                raise ElmfireSpecError(
                    f"weather_schedule[{i}] carries unknown key {k!r} "
                    f"(known: {sorted(_SCHEDULE_ALIASES)})"
                )
            band[canon] = float(v)
        bands.append(band)
    return bands


#: Nonburnable FBFM40 code for a fuel break (NB1 = "urban/developed", zero ROS).
FUEL_BREAK_NONBURNABLE_FBFM: int = 91


def write_fbfm_with_break(
    fuel_model: int,
    grid: dict,
    dest: Path,
    break_spec: dict | None,
) -> dict:
    """Write the Int16 ``fbfm40`` raster: uniform ``fuel_model`` with an optional
    NON-BURNABLE strip (a fuel break) the contiguous surface fire cannot cross.

    ``break_spec`` (or ``None`` for a uniform bed)::

        {"axis": "x"|"y",          # "x": a vertical strip spanning all rows (blocks
                                    #      an E-W head fire); "y": a horizontal strip
         "lo_frac": 0.0..1.0,       # strip start as a fraction of the axis extent
         "hi_frac": 0.0..1.0,       # strip end   (hi_frac > lo_frac)
         "fuel_model": 91}          # optional NB code (default NB1/91)

    Returns break provenance (``{"axis","cols"/"rows","break_fuel_model"}``) or
    ``{}`` for a uniform bed. Raises :class:`ElmfireSpecError` on a malformed spec
    (a degenerate/zero-width strip that would silently no-op)."""
    import numpy as np

    arr = np.full((grid["ny"], grid["nx"]), int(fuel_model), dtype="int16")
    prov: dict[str, Any] = {}
    if break_spec:
        axis = str(break_spec.get("axis", "x")).lower()
        lo = float(break_spec.get("lo_frac"))
        hi = float(break_spec.get("hi_frac"))
        nb = int(break_spec.get("fuel_model", FUEL_BREAK_NONBURNABLE_FBFM))
        if axis not in ("x", "y") or not (0.0 <= lo < hi <= 1.0):
            raise ElmfireSpecError(
                f"fuel_break malformed: axis={axis!r} lo_frac={lo} hi_frac={hi} "
                "(need axis in x/y and 0<=lo<hi<=1)"
            )
        if axis == "x":
            c0, c1 = int(lo * grid["nx"]), int(hi * grid["nx"])
            if c1 <= c0:
                raise ElmfireSpecError("fuel_break x-strip is zero cells wide")
            arr[:, c0:c1] = nb
            prov = {"axis": "x", "cols": [c0, c1], "break_fuel_model": nb}
        else:
            r0, r1 = int(lo * grid["ny"]), int(hi * grid["ny"])
            if r1 <= r0:
                raise ElmfireSpecError("fuel_break y-strip is zero cells wide")
            arr[r0:r1, :] = nb
            prov = {"axis": "y", "rows": [r0, r1], "break_fuel_model": nb}
    import rasterio

    with rasterio.open(dest, "w", **_grid_profile(grid, "int16")) as ds:
        ds.write(arr, 1)
    return prov


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


def _extra_lines(extra: dict[str, str] | None) -> str:
    """Render ``KEY = VALUE`` namelist lines from a pre-formatted-string dict.

    Values are injected VERBATIM (the caller formats floats / ``.TRUE.`` /
    per-fuel ``(:)`` array syntax), so the extension surface stays a thin
    string pass-through — the deck builder never re-interprets ELMFIRE units.
    An empty / ``None`` dict renders nothing (byte-identical to the base deck).
    """
    if not extra:
        return ""
    return "".join(f"{k} = {v}\n" for k, v in extra.items())


def render_namelist(
    grid: dict,
    ignitions_xy: list[dict],
    weather: dict,
    duration_s: float,
    dt_s: float = 30.0,
    dtdump_s: float = 3600.0,
    *,
    dt_meteorology_s: float = 3600.0,
    num_meteorology_times: int = 1,
    target_cfl: float | None = None,
    simulator_extra: dict[str, str] | None = None,
    outputs_extra: dict[str, str] | None = None,
    inputs_extra: dict[str, str] | None = None,
    time_control_extra: dict[str, str] | None = None,
    spotting_extra: dict[str, str] | None = None,
) -> str:
    """Render ``elmfire.data`` with the tutorial-01 key set (proven).

    Every base key below appears in
    ``third_party/elmfire/tutorials/01-constant-wind/elmfire.data.in`` — the deck
    the proven container consumed; only the values are templated. Paths are
    relative to the case dir (``cd <deck_dir> && elmfire_<VER>
    ./inputs/elmfire.data``), mirroring ``01-run.sh``.

    ONE ADDITIVE flag beyond the tutorial set: ``DUMP_FLAME_LENGTH = .TRUE.``
. Tutorial 01 simply does not enable it; the flag is a first-class
    ``&OUTPUTS`` dump documented at https://elmfire.io/user_guide/io.html and
    the composer publishes the flame-length raster as its own COG.

    KNOB-EXTENSION SURFACE (sensitivity templates): ``simulator_extra`` /
    ``outputs_extra`` / ``inputs_extra`` / ``time_control_extra`` append extra
    ``KEY = VALUE`` lines to the &SIMULATOR / &OUTPUTS / &INPUTS / &TIME_CONTROL
    groups respectively (each value a pre-formatted string the caller owns — e.g.
    ``{"MAX_LOW": "8.0000"}``, ``{"WIND_FLUCTUATIONS": ".TRUE."}``,
    ``{"DUMP_CROWN_FIRE_AREA": ".TRUE."}``, ``{"DT_INTERPOLATE_M1": "600.0"}``).
    Unset (the default) reproduces the base deck byte-for-byte.

    SPOTTING SURFACE (``spotting_extra``): when given, an entire ``&SPOTTING``
    namelist group is emitted carrying the caller's pre-formatted ``KEY = VALUE``
    lines (e.g. ``{"ENABLE_SPOTTING": ".TRUE.", "MEAN_SPOTTING_DIST_MIN": "25.0000"}``).
    ELMFIRE reads &SPOTTING as its OWN group (``READ_SPOTTING``) — the params do NOT
    live in &SIMULATOR — so this is a separate surface. NOTE the baked binary runs
    ``SET_SPOTTING_PARAMETERS`` whenever ``ENABLE_SPOTTING``, which OVERWRITES the
    scalar knobs (``MEAN_SPOTTING_DIST``, ``PIGN``, ``SPOT_FLIN_EXP``, ``SPOT_WS_EXP``,
    ``NEMBERS_MAX``, ``SURFACE_FIRE_SPOTTING_PERCENT``) from their ``_MIN/_MAX/_LO/_HI``
    bounds; surface-fire spotting stays OFF unless ``GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT_MIN``
    (default 0) is raised. So callers set the BOUNDS (MIN==MAX for a deterministic run),
    not the bare scalars. Unset (the default) emits NO &SPOTTING group (byte-identical
    base deck; ELMFIRE keeps ``ENABLE_SPOTTING=.FALSE.``).

    TRANSIENT WEATHER (multi-band decks): ``num_meteorology_times`` > 1 emits a
    ``&MONTE_CARLO`` group carrying ``NUM_METEOROLOGY_TIMES`` (read unconditionally
    by every run), and ``dt_meteorology_s`` sets ``DT_METEOROLOGY`` (the
    band-to-band spacing the solver linearly interpolates over). The weather
    rasters must then be MULTI-BAND (:func:`write_weather_bands`, one band per
    meteorology time). ``target_cfl`` emits ``TARGET_CFL`` in &TIME_CONTROL.
    At the defaults (single band, ``DT_METEOROLOGY = 3600.0``, no TARGET_CFL /
    time-control extras / MONTE_CARLO group) the deck is byte-identical to the
    constant tutorial-01 deck.
    """

    def _f(v: float) -> str:
        return f"{float(v):.4f}"

    sim_lines = [f"NUM_IGNITIONS = {len(ignitions_xy)}"]
    for i, ign in enumerate(ignitions_xy, start=1):
        sim_lines.append(f"X_IGN({i})      = {_f(ign['x'])}")
        sim_lines.append(f"Y_IGN({i})      = {_f(ign['y'])}")
        sim_lines.append(f"T_IGN({i})      = {_f(ign['t_ign_s'])}")
    sim_block = "\n".join(sim_lines)
    sim_extra_block = _extra_lines(simulator_extra)
    outputs_extra_block = _extra_lines(outputs_extra)
    inputs_extra_block = _extra_lines(inputs_extra)

    _dt_met = f"{float(dt_meteorology_s):.1f}"

    # &TIME_CONTROL: SIMULATION_DT + TSTOP always; TARGET_CFL + the interpolation-
    # frequency / DTMAX pass-through (``time_control_extra``) only when set, so the
    # default (constant) deck stays byte-identical.
    tc_lines = [f"SIMULATION_DT    = {_f(dt_s)}", f"SIMULATION_TSTOP = {_f(duration_s)}"]
    if target_cfl is not None:
        tc_lines.append(f"TARGET_CFL       = {_f(target_cfl)}")
    tc_body = "\n".join(tc_lines) + "\n" + _extra_lines(time_control_extra)

    # &MONTE_CARLO carries NUM_METEOROLOGY_TIMES (read unconditionally by every
    # run -- elmfire.f90 READ_MONTE_CARLO). Emitted ONLY for a transient
    # (multi-band) weather deck; a constant deck omits the group entirely (the
    # default of 1 is what the solver assumes when the group is absent), keeping
    # the constant deck byte-identical.
    montecarlo_block = ""
    if int(num_meteorology_times) > 1:
        montecarlo_block = (
            "\n&MONTE_CARLO\n"
            f"NUM_METEOROLOGY_TIMES = {int(num_meteorology_times)}\n"
            "/\n"
        )

    # &SPOTTING: its OWN namelist group (READ_SPOTTING), emitted ONLY when the
    # caller passes spotting knobs so the base (no-spotting) deck stays byte-
    # identical (ELMFIRE defaults ENABLE_SPOTTING=.FALSE. when the group is absent).
    spotting_block = ""
    if spotting_extra:
        spotting_block = "\n&SPOTTING\n" + _extra_lines(spotting_extra) + "/\n"

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
DT_METEOROLOGY                 = {_dt_met}
WEATHER_DIRECTORY              = './inputs'
WS_FILENAME                    = 'ws'
WD_FILENAME                    = 'wd'
M1_FILENAME                    = 'm1'
M10_FILENAME                   = 'm10'
M100_FILENAME                  = 'm100'
LH_MOISTURE_CONTENT            = {_f(weather["lh_pct"])}
LW_MOISTURE_CONTENT            = {_f(weather["lw_pct"])}
{inputs_extra_block}/

&OUTPUTS
OUTPUTS_DIRECTORY    = './outputs'
DTDUMP               = {_f(dtdump_s)}
DUMP_FLAME_LENGTH    = .TRUE.
DUMP_FLIN            = .TRUE.
DUMP_SPREAD_RATE     = .TRUE.
DUMP_TIME_OF_ARRIVAL = .TRUE.
CONVERT_TO_GEOTIFF   = .FALSE.
{outputs_extra_block}/

&COMPUTATIONAL_DOMAIN
A_SRS = 'EPSG: {grid["epsg"]}'
COMPUTATIONAL_DOMAIN_CELLSIZE = {_f(grid["cellsize_m"])}
COMPUTATIONAL_DOMAIN_XLLCORNER = {_f(grid["xll"])}
COMPUTATIONAL_DOMAIN_YLLCORNER = {_f(grid["yll"])}
/

&TIME_CONTROL
{tc_body}/

&SIMULATOR
{sim_block}
WX_BILINEAR_INTERPOLATION=.TRUE.
WSMFEFF_LOW_MULT = 0.011364
{sim_extra_block}/

&MISCELLANEOUS
PATH_TO_GDAL                   = '/usr/bin'
SCRATCH                        = './scratch'
/
{montecarlo_block}{spotting_block}"""


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
    rendered namelist) so a byte-level deck diff (the golden-deck
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


def build_deck(
    spec: dict,
    deck_dir: str | Path,
    *,
    spotting_extra: dict[str, str] | None = None,
    weather_schedule: list[dict[str, float]] | None = None,
    dt_meteorology_s: float = 3600.0,
) -> dict:
    """Build a run-ready ELMFIRE deck directory from ``spec``.

    Steps: validate spec -> compute the ONE target grid -> localize + warp
    the 8 fuels/topography rasters -> generate the 5 weather rasters + adj/phi
    -> HARD-ASSERT grid identity across all 15 rasters -> project ignitions
    (in-domain assert) -> render ``elmfire.data`` -> write
    ``deck_manifest.json``. Returns the manifest dict.

    ``spotting_extra`` (the typed Python namelist-knob path, NOT a dict-spec
    field -- see ``_KNOWN_SPEC_FIELDS``) emits a whole ``&SPOTTING`` group
    (:func:`render_namelist`); unset (default) reproduces the base no-spotting
    deck byte-for-byte. This is the REAL-DATA spotting surface: the same fetched
    LANDFIRE/DEM warp with the ``&SPOTTING`` group toggled OFF vs ON.

    ``weather_schedule`` (also a typed Python kwarg, NOT a dict-spec field) is
    the REAL-DATA transient-weather surface -- the same real fetched LANDFIRE/
    DEM warp, but the ws/wd/m1/m10/m100 rasters written MULTI-BAND
    (:func:`write_weather_bands`, via :func:`normalize_weather_schedule`)
    instead of single-band constants, with ``NUM_METEOROLOGY_TIMES`` set to the
    band count and ``DT_METEOROLOGY`` to ``dt_meteorology_s``. Unset (default)
    reproduces the base constant-weather deck byte-for-byte -- mirrors the
    synthetic flat-deck path's ``build_constant_flat_deck(weather_schedule=)``
    (ADR 0161) onto the real-data deck (ADR 0239 amendment 3).

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

    # adj/phi: always single-band Float32 constants (never scheduled).
    w = spec["weather"]
    for name, value in (("adj", ADJ_VALUE), ("phi", PHI_VALUE)):
        write_constant_raster(value, grid, inputs_dir / f"{name}.tif")
        provenance[name] = {"source": f"constant:{value}", "nodata_fraction": 0.0}

    # Weather: single constant band (default, byte-identical to the prior
    # behaviour), OR a multi-band transient schedule (``weather_schedule``).
    num_met_times = 1
    if weather_schedule:
        bands = normalize_weather_schedule(weather_schedule, w)
        num_met_times = len(bands)
        for name, key in WEATHER_RASTER_KEYS.items():
            write_weather_bands(
                [float(b[key]) for b in bands], grid, inputs_dir / f"{name}.tif"
            )
            provenance[name] = {
                "source": f"schedule[{num_met_times}]:{[round(b[key], 3) for b in bands]}",
                "nodata_fraction": 0.0,
            }
    else:
        for name, key in WEATHER_RASTER_KEYS.items():
            write_constant_raster(float(w[key]), grid, inputs_dir / f"{name}.tif")
            provenance[name] = {"source": f"constant:{w[key]}", "nodata_fraction": 0.0}

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
        dt_meteorology_s=float(dt_meteorology_s),
        num_meteorology_times=num_met_times,
        spotting_extra=spotting_extra,
    )
    (inputs_dir / "elmfire.data").write_text(namelist)

    manifest = compose_manifest(deck_dir, grid, spec, ignitions_xy, provenance)
    (deck_dir / "deck_manifest.json").write_text(json.dumps(manifest, indent=2))
    LOG.info("deck ready at %s (%d files)", deck_dir, len(manifest["files"]))
    return manifest
