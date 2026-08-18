"""the composer - MODFLOW PRT capture-zone composer.

The end-to-end higher-order workflow for the MODFLOW
``capture_zone`` and ``wellhead_protection`` archetypes: it turns a place (or
AOI point) + a pumping well location into a rendered capture-zone polygon  -
the zone of contribution delineated by backward particle tracking (MF6 PRT).

Canonical real-world pipeline mirrored here (a wellhead protection area /
zone-of-contribution delineation, the MODFLOW analogue of the EPA WHPA /
ZONEBUDGET approach):

    resolve the AOI point (geocode a place, or take an explicit lat/lon)
        -> the user supplies the well location (NEVER fabricated  -  a missing
           well is a typed USER_INPUT_REQUIRED failure, Invariant 9)
        -> assemble MODFLOWRunArgs(archetype='capture_zone', well, tiers, ...)
        -> run_modflow_archetype_job:
             GWF steady flow solve -> mf6
             -> gwt_adapter.build_and_run_prt_from_gwf (PRT backward tracking)
             -> postprocess_capture_zone (convex-hull isochrones + FlatGeobuf)
        -> CaptureZoneLayerURI (vector polygon + per-tier isochrone areas)

The difference between the two archetypes is framing and default travel-time
tiers only:

    ``capture_zone``       - general zone-of-contribution; defaults [1, 5, 10] yr
    ``wellhead_protection`` - EPA-style fixed-travel-time; defaults [2, 5, 10] yr
                             (EPA WHPA fixed-travel-time approach; SDWA Section
                             1428 / EPA 440/6-87-010 delineation guidance)

Both produce a ``CaptureZoneLayerURI`` (layer_type='vector'), which renders
client-side via the inline-GeoJSON path and the ``presetColorFor('capture_zone')``
violet branch in ``vector_rendering.ts``.

Invariants:
- **1 / 2 / 8: preserve** (typed numbers, deterministic composition, cancellable).
- **9. No fabricated model inputs.** A capture-zone run with no well location
  returns a typed ``USER_INPUT_REQUIRED`` failure -- the CONVEX HULL of
  backtracked pathlines is a physical delineation, not a guess; a missing well
  is never invented.
- **10. Minimal parameter surface: preserves.** The signature exposes intent (the
  place + the well + optional tiers / particle count); the grid, demo aquifer
  K / Sy, and PRT parameters are derived defaults, not user-supplied.

PRECISION CAVEAT (Invariant 1): the polygon is the CONVEX HULL of discrete
backtracked pathlines on a structured 100 m rectilinear grid with SoilGrids-derived
(or refused) aquifer parameters, NOT a calibrated regulatory wellhead protection
area. The agent must
narrate this caveat when presenting the layer.
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Any

from pydantic import Field

from trid3nt_contracts.common import GraceModel, SyntheticInput
from trid3nt_contracts.modflow_contracts import (
    CaptureZoneLayerURI,
    MODFLOWRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    substep,
)
from trid3nt_server.data import TOOL_REGISTRY, register_tool
from trid3nt_server.gates.input_review import physics_refusal_reason
from trid3nt_server.workflows.modflow._aquifer_resolve import (
    provenance_summary,
    resolve_aquifer_properties,
)
from trid3nt_server.workflows.modflow._input_review import (
    gate_and_stamp_modflow_inputs,
    review_modflow_entries,
)
from trid3nt_server.emission.layer_uri_emit import publish_input_layer
from trid3nt_server.workflows.modflow._template_card import TemplateCard
# Shared, engine-agnostic provenance seams (also importable by the Landlab
# groundwater templates): measured well heads -> a kriged / trend water-table
# surface. (Aquifer-K pedotransfer now lives in the shared _aquifer_resolve seam.)
from trid3nt_server.workflows.shared.water_table_interp import (
    interpolate_water_table,
)
# Reuse the shared archetype-run + AOI-resolve helpers from the sustainable_yield
# composer (one implementation, all archetypes).
from trid3nt_server.workflows.modflow.sustainable_yield.sustainable_yield import (
    _aquifer_overrides,
    _coerce_optional_latlon,
    _resolve_aoi_point,
    _run_archetype,
)

logger = logging.getLogger("trid3nt_server.workflows.modflow.capture_zone.capture_zone")

__all__ = [
    "CaptureZoneResult",
    "model_capture_zone_scenario",
    "modflow_capture_zone",
    "CaptureZoneScenarioError",
    "CaptureZoneInputError",
    "CAPTURE_ZONE_DEFAULT_TIERS",
    "WELLHEAD_PROTECTION_DEFAULT_TIERS",
    "TEMPLATE_CARD",
]

#: Default travel-time isochrone tiers (years) for ``capture_zone``.
#: One, five, and ten years is the common municipal-well zone-of-contribution
#: analysis period (e.g. USEPA Source Water Protection guidance).
CAPTURE_ZONE_DEFAULT_TIERS: list[float] = [1.0, 5.0, 10.0]

#: Default travel-time isochrone tiers (years) for ``wellhead_protection``.
#: Two, five, and ten years align with the EPA WHPA fixed-travel-time approach
#: (SDWA Section 1428 wellhead protection program; delineation methods per EPA
#: 440/6-87-010; the 2-year tier is the IMMEDIATE zone).
WELLHEAD_PROTECTION_DEFAULT_TIERS: list[float] = [2.0, 5.0, 10.0]

#: Plausible shallow-aquifer hydraulic-gradient bounds (m/m). The DEM-derived
#: topographic slope is clamped into this range: a near-flat AOI below the floor
#: makes the water-table proxy unreliable (fall back to demo); a cliff above the
#: ceiling would drive an unphysical regional gradient. 5e-4..5e-2 spans typical
#: valley-fill to steep-terrain water-table gradients.
GRADIENT_MIN_MM: float = 5.0e-4
GRADIENT_MAX_MM: float = 5.0e-2

#: Half-width (deg) of the DEM footprint fetched around the AOI to estimate the
#: regional gradient. ~0.025 deg ~= 2.7 km covers the 4.1 km PRT domain so the
#: planar fit reflects the regional slope the capture zone sits in.
DEM_GRADIENT_HALF_DEG: float = 0.025


# --------------------------------------------------------------------------- #
# DEM-derived regional water-table gradient (georeferenced-mode helpers)
# --------------------------------------------------------------------------- #


def _fit_plane(
    xs: list[float], ys: list[float], zs: list[float]
) -> tuple[float, float, float]:
    """Least-squares fit ``z = a*x + b*y + c``; return ``(a, b, c)``.

    ``(a, b)`` is the planar gradient of ``z`` in the ``x`` / ``y`` units. Pure
    (numpy lstsq); raises ValueError on < 3 points or a degenerate system.
    """
    import numpy as np

    if len(xs) < 3:
        raise ValueError("_fit_plane needs >= 3 points")
    A = np.column_stack([np.asarray(xs, float), np.asarray(ys, float), np.ones(len(xs))])
    z = np.asarray(zs, float)
    coeffs, *_ = np.linalg.lstsq(A, z, rcond=None)
    return float(coeffs[0]), float(coeffs[1]), float(coeffs[2])


def _planar_gradient_from_dem(
    dem_uri: str, lat0: float, lon0: float
) -> tuple[float, float, float, float] | None:
    """Estimate the regional water-table gradient from a DEM (screening proxy).

    Reads the fetched DEM, samples a decimated pixel grid, converts each pixel to
    local east/north metres about ``(lat0, lon0)``, and fits a plane. The returned
    ``(gx, gy)`` is the topographic slope vector (m/m, x=east y=north): under the
    shallow-unconfined subdued-replica assumption the water table mimics surface
    topography, so this slope is a SCREENING proxy for the hydraulic gradient (NOT
    a measured potentiometric surface). Magnitude is clamped to
    ``[GRADIENT_MIN_MM, GRADIENT_MAX_MM]`` (direction preserved); a below-floor
    (near-flat) AOI returns ``None`` so the caller REFUSES (law 9), never a demo gradient.

    Returns ``(gx, gy, magnitude, azimuth_deg)`` where azimuth is the compass
    bearing (deg CW from north) groundwater FLOWS toward (down-gradient), or
    ``None`` on any read failure / degenerate/flat DEM. NEVER raises.
    """
    try:
        import numpy as np
        import rasterio
        from pyproj import Transformer

        from trid3nt_server.data.processing._gdal_runner import read_raster_bytes

        # read_raster_bytes accepts s3:// or a bare local path; normalise file://.
        read_uri = dem_uri[len("file://"):] if dem_uri.startswith("file://") else dem_uri
        dem_bytes = read_raster_bytes(read_uri, on_error=lambda msg: RuntimeError(msg))
        with rasterio.MemoryFile(dem_bytes) as mf:
            with mf.open() as src:
                arr = src.read(1, masked=True)
                transform = src.transform
                src_crs = src.crs
                H, W = src.height, src.width
        step = max(1, max(H, W) // 80)
        data = np.ma.filled(arr.astype("float64"), np.nan)
        rr, cc = np.mgrid[0:H:step, 0:W:step]
        vals = data[rr, cc]
        # Pixel-centre coordinates in the dataset CRS.
        xs_ds, ys_ds = rasterio.transform.xy(transform, rr, cc)
        xs_ds = np.asarray(xs_ds, float).ravel()
        ys_ds = np.asarray(ys_ds, float).ravel()
        vals = np.asarray(vals, float).ravel()
        good = np.isfinite(vals)
        if good.sum() < 8:
            return None
        xs_ds, ys_ds, vals = xs_ds[good], ys_ds[good], vals[good]
        # Convert dataset-CRS coords -> lon/lat (identity when already 4326).
        if src_crs is not None and src_crs.to_epsg() != 4326:
            to_4326 = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
            lons, lats = to_4326.transform(xs_ds, ys_ds)
        else:
            lons, lats = xs_ds, ys_ds
        # Local east/north metres about the AOI centre (equirectangular).
        m_per_deg_lat = 110_540.0
        m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
        east = (np.asarray(lons, float) - lon0) * m_per_deg_lon
        north = (np.asarray(lats, float) - lat0) * m_per_deg_lat
        a, b, _c = _fit_plane(list(east), list(north), list(vals))
        mag = math.hypot(a, b)
        if not math.isfinite(mag) or mag < GRADIENT_MIN_MM:
            return None  # too flat: DEM proxy unreliable -> caller uses demo
        clamped = min(mag, GRADIENT_MAX_MM)
        scale = clamped / mag
        gx, gy = a * scale, b * scale
        # Down-gradient (flow) azimuth = bearing of -(gx, gy).
        az = math.degrees(math.atan2(-gx, -gy)) % 360.0
        return gx, gy, clamped, az
    except Exception as exc:  # noqa: BLE001 -- DEM gradient is best-effort
        logger.warning("capture_zone DEM-gradient estimate failed (non-fatal): %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Measured-head regional gradient (potentiometric plane from USGS wells)
# --------------------------------------------------------------------------- #

#: Feet -> metres (USGS groundwater levels are reported in feet).
FT_TO_M: float = 0.3048

#: Half-width (deg) of the well-search + land-surface-DEM footprint about the
#: well. ~0.1 deg ~= 11 km catches a screening-regional set of NWIS wells around
#: the 4.1 km PRT domain; a thin in-domain set is exactly why this is the modest
#: EXPANDED footprint (not the tight domain box).
WELL_SEARCH_HALF_DEG: float = 0.1

#: Coarse DEM resolution (m) for sampling land-surface elevation at depth-to-water
#: wells -- a metre-scale vertical datum, not terrain detail, so 30 m is ample and
#: keeps the wider footprint's pixel budget small.
WELL_DEM_RESOLUTION_M: int = 30

#: Recency window (years, knob default) for a usable well reading. Older readings
#: are excluded so the fitted gradient reflects a current water table.
MEASURED_RECENCY_YEARS: float = 10.0

#: Nominal NGVD29 -> NAVD88 vertical shift (m) for the central Great Plains
#: (regional VERTCON magnitude). Applied to NGVD29 groundwater-ELEVATION readings
#: to co-reference them with NAVD88 heads. A UNIFORM offset does not change a
#: fitted gradient (slope); it only matters where NGVD29 and NAVD88 wells are
#: MIXED, and up to ~2 m of national datum spread is why this normalization is a
#: stated screening approximation, not a rigorous point transform.
NGVD29_TO_NAVD88_M: float = -0.20

#: Minimum usable wells + spatial-spread thresholds for a non-degenerate plane
#: fit. The minor-axis (perpendicular) spread guards against a collinear/clustered
#: set that leaves the cross-gradient component unconstrained.
MIN_MEASURED_WELLS: int = 3
MIN_WELL_EXTENT_M: float = 500.0
MIN_WELL_MINOR_STD_M: float = 150.0

#: USGS parameter codes reporting a DEPTH (below land surface / measuring point) --
#: a head ELEVATION requires the land-surface elevation (DEM) minus this depth.
_DEPTH_PCODES = frozenset({"72019", "61055"})
#: USGS parameter codes reporting a groundwater ELEVATION + its native datum.
_ELEV_PCODE_DATUM = {"72150": "NAVD88", "62611": "NAVD88", "62610": "NGVD29"}


def _parse_iso_utc(value: Any) -> "datetime | None":  # noqa: F821
    """Parse an ISO-8601 timestamp (or bare date) to an aware UTC datetime, or None."""
    from datetime import datetime, timezone

    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    for cand in (s, s[:10]):
        try:
            dt = datetime.fromisoformat(cand)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _read_wells_features(wells_uri: str) -> list[dict[str, Any]]:
    """Read the fetched wells FlatGeobuf into ``[{lon, lat, props}]``. NEVER raises.

    An ``s3://`` artifact is fetched with the boto3 object reader (which honors
    the MinIO ``AWS_ENDPOINT_URL`` env block) into a temp file, then read with
    pyogrio -- NEVER handed to GDAL's ``/vsis3/``, which ignores the custom
    endpoint and fails with an ambient-credential error (no-ambient-AWS norm).
    """
    try:
        import tempfile

        import geopandas as gpd

        from trid3nt_server.workflows.modflow.run_modflow import (
            _read_vector_bytes,
        )

        suffix = ".geojson" if wells_uri.lower().endswith(
            (".json", ".geojson")
        ) else ".fgb"
        tmp = Path(tempfile.mkdtemp(prefix="wells-")) / f"wells{suffix}"
        tmp.write_bytes(_read_vector_bytes(wells_uri))
        gdf = gpd.read_file(str(tmp), engine="pyogrio")
        cols = [c for c in gdf.columns if c != "geometry"]
        feats: list[dict[str, Any]] = []
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            feats.append(
                {"lon": float(geom.x), "lat": float(geom.y),
                 "props": {c: row[c] for c in cols}}
            )
        return feats
    except Exception as exc:  # noqa: BLE001 -- reading wells is best-effort
        logger.warning("capture_zone: reading wells FGB failed (non-fatal): %s", exc)
        return []


def _sample_dem_at_points(
    dem_uri: str, lons: list[float], lats: list[float]
) -> list[float | None]:
    """Sample DEM elevation (m, 3DEP NAVD88) at each ``(lon, lat)``; None off-grid.

    The rasterio ``MemoryFile`` is held open across the whole sample loop (an
    orphaned MemoryFile GC-corrupts a lazy read). Reprojects the query points to
    the dataset CRS when it is not already EPSG:4326. NEVER raises.
    """
    try:
        import rasterio
        from pyproj import Transformer

        from trid3nt_server.data.processing._gdal_runner import read_raster_bytes

        read_uri = dem_uri[len("file://"):] if dem_uri.startswith("file://") else dem_uri
        dem_bytes = read_raster_bytes(read_uri, on_error=lambda msg: RuntimeError(msg))
        out: list[float | None] = []
        with rasterio.MemoryFile(dem_bytes) as mf:
            with mf.open() as src:
                src_crs = src.crs
                if src_crs is not None and src_crs.to_epsg() != 4326:
                    to_ds = Transformer.from_crs("EPSG:4326", src_crs, always_xy=True)
                    xs, ys = to_ds.transform(list(lons), list(lats))
                else:
                    xs, ys = list(lons), list(lats)
                nodata = src.nodata
                for val in src.sample(list(zip(xs, ys)), indexes=1):
                    v = float(val[0])
                    if (nodata is not None and v == float(nodata)) or not math.isfinite(v):
                        out.append(None)
                    else:
                        out.append(v)
        return out
    except Exception as exc:  # noqa: BLE001 -- DEM sampling is best-effort
        logger.warning("capture_zone: DEM point-sampling failed (non-fatal): %s", exc)
        return [None] * len(lons)


def _usable_well_heads(
    features: list[dict[str, Any]],
    dem_uri: str | None,
    lat0: float,
    lon0: float,
    *,
    now: "datetime",  # noqa: F821
    recency_years: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reduce fetched well readings to usable head ELEVATIONS (NAVD88 m) + provenance.

    THE DATUM LADDER (each reading -> a head elevation in NAVD88 metres):
      * DEPTH-to-water (pcode 72019 / 61055): head = DEM land-surface (3DEP NAVD88)
        minus the depth. A non-positive depth (water at/above land surface =
        flowing/artesian) is EXCLUDED (head ambiguous vs land surface).
      * groundwater ELEVATION, NAVD88 (72150 / 62611): head = the value directly.
      * groundwater ELEVATION, NGVD29 (62610): head = value + NGVD29_TO_NAVD88_M
        (nominal regional shift; a uniform offset does not bias the fitted slope).
      * any other / 'Local Assumed' vertical datum on an elevation reading:
        EXCLUDED (not vertically georeferenced).

    Readings are filtered to the recency window (most-recent per site retained),
    to a parseable value + timestamp, and to a non-rejected approval status.

    Returns ``(usable_wells, meta)``. Each usable well carries local east/north
    metres about ``(lat0, lon0)``, the head elevation, its basis, datum, date, and
    identity. ``meta`` counts totals / per-basis / exclusion reasons for narration.
    """
    cutoff = now.timestamp() - float(recency_years) * 365.25 * 86400.0
    m_per_deg_lat = 110_540.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    excluded: dict[str, int] = {}

    def _drop(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    # --- Pass 1: parse + classify usable-shaped readings ---------------------
    depth_idx: list[int] = []
    staged: list[dict[str, Any]] = []
    for f in features:
        p = f.get("props", {}) or {}
        try:
            wl = float(p.get("water_level"))
        except (TypeError, ValueError):
            _drop("no_value")
            continue
        if not math.isfinite(wl):
            _drop("no_value")
            continue
        dt = _parse_iso_utc(p.get("datetime"))
        if dt is None:
            _drop("no_date")
            continue
        if dt.timestamp() < cutoff:
            _drop("stale")
            continue
        status = str(p.get("approval_status") or "").lower()
        if "reject" in status or "delet" in status:
            _drop("rejected_status")
            continue
        pcode = str(p.get("parameter_code") or "").strip()
        unit = str(p.get("unit") or "").strip().lower()
        # NWIS groundwater levels are feet unless the unit says metres.
        wl_m = wl if unit.startswith("m") else wl * FT_TO_M
        datum_raw = str(p.get("vertical_datum") or "").strip().upper()
        label = str(p.get("parameter_label") or "").lower()

        if pcode in _DEPTH_PCODES or (not pcode and ("depth" in label or "below" in label)):
            kind, datum = "depth", "NAVD88"
        elif pcode in _ELEV_PCODE_DATUM or (not pcode and ("elev" in label or "level" in label)):
            datum = datum_raw or _ELEV_PCODE_DATUM.get(pcode, "")
            kind = "elev"
        else:
            _drop("unknown_parameter")
            continue

        rec = {
            "site_no": str(p.get("site_no") or "").strip() or f"{f['lon']:.5f},{f['lat']:.5f}",
            "lon": float(f["lon"]), "lat": float(f["lat"]),
            "wl_m": wl_m, "kind": kind, "datum": datum,
            "date": dt, "date_iso": dt.date().isoformat(),
            "parameter_code": pcode or None,
        }
        if kind == "depth":
            if wl_m <= 0.0:
                _drop("artesian_or_above_surface")
                continue
            depth_idx.append(len(staged))
        staged.append(rec)

    # --- Sample the DEM land surface for depth-to-water wells (one read) ------
    if depth_idx:
        if not dem_uri:
            for i in depth_idx:
                staged[i]["_dead"] = "no_dem"
        else:
            samples = _sample_dem_at_points(
                dem_uri,
                [staged[i]["lon"] for i in depth_idx],
                [staged[i]["lat"] for i in depth_idx],
            )
            for i, ls in zip(depth_idx, samples):
                staged[i]["_land_surface_m"] = ls

    # --- Pass 2: resolve head elevation (NAVD88 m) + basis --------------------
    resolved: list[dict[str, Any]] = []
    for rec in staged:
        if rec["kind"] == "depth":
            if rec.get("_dead") == "no_dem":
                _drop("no_dem_for_depth")
                continue
            ls = rec.get("_land_surface_m")
            if ls is None:
                _drop("dem_off_grid")
                continue
            head_m = float(ls) - rec["wl_m"]
            basis = "dem_minus_depth"
        else:
            datum = rec["datum"]
            if datum in ("NAVD88", "NAVD 88", "NAVD1988"):
                head_m = rec["wl_m"]
                basis = "elev_navd88"
            elif datum in ("NGVD29", "NGVD 29", "NGVD1929"):
                head_m = rec["wl_m"] + NGVD29_TO_NAVD88_M
                basis = "elev_ngvd29_shifted"
            else:
                _drop("elev_unusable_datum")
                continue
        east = (rec["lon"] - lon0) * m_per_deg_lon
        north = (rec["lat"] - lat0) * m_per_deg_lat
        resolved.append({
            "site_no": rec["site_no"], "lon": rec["lon"], "lat": rec["lat"],
            "east": east, "north": north, "head_m": head_m, "basis": basis,
            "datum": rec["datum"], "date": rec["date"], "date_iso": rec["date_iso"],
            "water_level_ft": rec["wl_m"] / FT_TO_M, "parameter_code": rec["parameter_code"],
        })

    # --- Most-recent reading per site ----------------------------------------
    by_site: dict[str, dict[str, Any]] = {}
    for w in resolved:
        prev = by_site.get(w["site_no"])
        if prev is None or w["date"] > prev["date"]:
            by_site[w["site_no"]] = w
    usable = sorted(by_site.values(), key=lambda w: w["site_no"])

    basis_counts: dict[str, int] = {}
    for w in usable:
        basis_counts[w["basis"]] = basis_counts.get(w["basis"], 0) + 1
    meta = {
        "readings_fetched": len(features),
        "usable_wells": len(usable),
        "by_basis": basis_counts,
        "excluded": excluded,
    }
    return usable, meta


def _fit_measured_gradient(
    usable: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """Fit a potentiometric plane over the usable wells -> gradient vector or None.

    Guards: >= MIN_MEASURED_WELLS wells; a spatial spread with minor-axis std >=
    MIN_WELL_MINOR_STD_M and extent >= MIN_WELL_EXTENT_M (collinear/clustered sets
    leave the cross-gradient component unconstrained); a finite gradient at or
    above GRADIENT_MIN_MM (a near-flat measured table gives an unreliable capture
    direction -> fall back); and a plane residual RMS not exceeding the head relief
    (a fit no better than the mean is noise). Magnitude is clamped to
    GRADIENT_MAX_MM (direction preserved), mirroring the DEM path.

    Returns ``(fit, reason)``: ``fit`` is a dict (gx, gy in m/m east/north; the
    clamped magnitude; flow azimuth deg; residual_m; n; head_range_m; date_min /
    date_max ISO) or ``None`` when degenerate, with ``reason`` naming the cause.
    """
    import numpy as np

    n = len(usable)
    if n < MIN_MEASURED_WELLS:
        return None, f"too_few_wells ({n} < {MIN_MEASURED_WELLS})"
    east = np.asarray([w["east"] for w in usable], float)
    north = np.asarray([w["north"] for w in usable], float)
    head = np.asarray([w["head_m"] for w in usable], float)

    cov = np.cov(np.vstack([east, north]))
    evals = np.linalg.eigvalsh(cov)  # ascending; [minor, major] variance
    minor_std = math.sqrt(max(float(evals[0]), 0.0))
    extent = math.hypot(float(east.max() - east.min()), float(north.max() - north.min()))
    if extent < MIN_WELL_EXTENT_M or minor_std < MIN_WELL_MINOR_STD_M:
        return None, (
            f"degenerate_spread (extent {extent:.0f} m, minor-axis std "
            f"{minor_std:.0f} m; need >= {MIN_WELL_EXTENT_M:.0f} / "
            f"{MIN_WELL_MINOR_STD_M:.0f} m)"
        )

    a, b, c = _fit_plane(list(east), list(north), list(head))
    resid = head - (a * east + b * north + c)
    rms = float(math.sqrt(float(np.mean(resid ** 2))))
    head_range = float(head.max() - head.min())
    mag = math.hypot(a, b)
    if not math.isfinite(mag) or mag < GRADIENT_MIN_MM:
        return None, f"near_flat (|grad| {mag:.2e} m/m < floor {GRADIENT_MIN_MM:.0e})"
    if head_range > 0.0 and rms > head_range:
        return None, f"poor_fit (residual {rms:.2f} m > head relief {head_range:.2f} m)"

    clamped = min(mag, GRADIENT_MAX_MM)
    scale = clamped / mag
    gx, gy = a * scale, b * scale
    az = math.degrees(math.atan2(-gx, -gy)) % 360.0
    dates = sorted(w["date_iso"] for w in usable)
    return (
        {
            "gx": gx, "gy": gy, "magnitude": clamped, "azimuth": az,
            "residual_m": rms, "n": n, "head_range_m": head_range,
            "date_min": dates[0], "date_max": dates[-1],
        },
        "ok",
    )


def _build_used_wells_layer(usable: list[dict[str, Any]], run_id: str) -> Any:
    """Emit the gradient wells as a point-FlatGeobuf ``LayerURI`` (context overlay).

    One Point per used well carrying head elevation (m NAVD88), the reading date,
    depth/elevation basis, datum, and identity, so the user SEES the observed data
    the measured gradient was fitted to. NEVER raises -- returns ``None`` on any
    write/upload failure (the solve is unaffected).
    """
    try:
        import geopandas as gpd
        from shapely.geometry import Point

        from trid3nt_contracts.execution import LayerURI
        from trid3nt_server.workflows.modflow.postprocess_modflow import _upload_fgb

        props = [
            {
                "site_no": w["site_no"],
                "head_elev_m": round(float(w["head_m"]), 3),
                "water_level_ft": round(float(w["water_level_ft"]), 2),
                "basis": w["basis"],
                "vertical_datum": w["datum"],
                "date": w["date_iso"],
                "parameter_code": w["parameter_code"] or "",
            }
            for w in usable
        ]
        geom = [Point(float(w["lon"]), float(w["lat"])) for w in usable]
        gdf = gpd.GeoDataFrame(props, geometry=geom, crs="EPSG:4326")
        import tempfile

        tmp = Path(tempfile.mkdtemp(prefix="cz_wells_")) / "gradient_wells_4326.fgb"
        gdf.to_file(str(tmp), driver="FlatGeobuf", engine="pyogrio")
        uri = _upload_fgb(tmp, run_id, None, fgb_filename="gradient_wells_4326.fgb")
        return LayerURI(
            layer_id=f"gradient-wells-{run_id}",
            name=f"Gradient wells (measured heads, n={len(usable)})",
            layer_type="vector",
            uri=uri,
            style_preset="usgs_groundwater",
            role="context",
            bbox=None,
        )
    except Exception as exc:  # noqa: BLE001 -- context layer is best-effort
        logger.warning("capture_zone: building wells context layer failed (non-fatal): %s", exc)
        return None



# --------------------------------------------------------------------------- #
# Kriged per-cell starting head (item 3)
# --------------------------------------------------------------------------- #


def _build_kriged_starting_head(
    surface: Any, lat: float, lon: float, wlat: float, wlon: float
) -> list[list[float]] | None:
    """Sample the kriged/trend water-table surface at each PRT cell centre.

    Returns the ``starting_head_by_cell`` (nrow x ncol, north-first) the worker
    writes as the GWF IC (item 3). The surface's local east/north frame
    is anchored at ``(wlat, wlon)`` (the ``_usable_well_heads`` origin); each cell
    centre is converted to that frame with the same equirectangular formula, the
    surface is sampled, and the field is RE-REFERENCED about the domain-centre
    head so it sits on the deck-local datum (aquifer top) consistent with the CHD
    plane. The interior thus carries the measured water-table CURVATURE a single
    gradient plane cannot. NEVER raises -- returns None so the caller keeps the
    uniform-IC fallback.
    """
    try:
        import numpy as np

        from trid3nt_server.workflows.modflow.run_modflow import (
            _import_gwt_adapter,
        )

        adapter = _import_gwt_adapter()
        geom = adapter.prt_grid_geometry(lat, lon)
        top = float(adapter.PRT_AQUIFER_TOP_M)
        clon = geom["cell_lon"]
        clat = geom["cell_lat"]
        m_per_deg_lat = 110_540.0
        m_per_deg_lon = 111_320.0 * math.cos(math.radians(wlat))
        east = (clon - wlon) * m_per_deg_lon
        north = (clat - wlat) * m_per_deg_lat
        heads = np.asarray(
            surface.sample(east.ravel(), north.ravel()), float
        ).reshape(clon.shape)
        centre_head = float(
            np.asarray(
                surface.sample(
                    (lon - wlon) * m_per_deg_lon, (lat - wlat) * m_per_deg_lat
                ),
                float,
            ).ravel()[0]
        )
        ic = top + (heads - centre_head)
        return [[float(v) for v in row] for row in ic]
    except Exception as exc:  # noqa: BLE001 -- kriged IC is best-effort
        logger.warning(
            "capture_zone: kriged starting-head build failed (non-fatal, uniform "
            "IC fallback): %s", exc
        )
        return None


# --------------------------------------------------------------------------- #
# Result envelope
# --------------------------------------------------------------------------- #


class CaptureZoneResult(GraceModel):
    """Return type for the composer.

    Bundles the capture-zone vector layer + the derived args + a narration
    summary dict. Invariant 1: every narrated number is a typed field  -
    ``capture_zone_layer`` carries ``capture_zone_area_km2``,
    ``travel_time_years``, ``isochrone_areas_km2``, and ``particle_count``.
    """

    schema_version: str = "v1"

    capture_zone_layer: CaptureZoneLayerURI
    derived_params: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class CaptureZoneScenarioError(RuntimeError):
    """Base class for the composer failures."""

    error_code: str = "CAPTURE_ZONE_SCENARIO_ERROR"
    retryable: bool = False


class CaptureZoneInputError(CaptureZoneScenarioError):
    """Caller supplied invalid / missing well or AOI input (honesty gate).

    Invariant 9: the well location is NEVER fabricated. A ``capture_zone`` run
    with no well location raises this error so the agent asks the user for the
    real well coordinates.
    """

    error_code = "CAPTURE_ZONE_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# The composer
# --------------------------------------------------------------------------- #


async def model_capture_zone_scenario(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | None = None,
    *,
    well_location_latlon: tuple[float, float] | None = None,
    travel_time_years: list[float] | None = None,
    n_particles: int = 16,
    archetype: str = "capture_zone",
    grid_type: str = "structured",
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    use_measured_heads: bool = True,
    measured_recency_years: float = MEASURED_RECENCY_YEARS,
    use_dem_gradient: bool = True,
    use_soil_k: bool = True,
    # --- multi-well WELLFIELD + transient + NHD RIV + kriged IC ---- #
    wells: list[Any] | None = None,
    transient: bool = False,
    sim_years: float | None = None,
    n_periods: int | None = None,
    use_nhd_river_boundaries: bool = False,
    compute_class: str = "standard",
    input_mode: str | None = None,
    pipeline_emitter: Any | None = None,
) -> CaptureZoneResult:
    """Compose place/AOI + a pumping well -> MODFLOW PRT -> CaptureZoneLayerURI.

    Args:
        location: a place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: an explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED  -  a
            missing well is a typed USER_INPUT_REQUIRED failure (never invented).
            Invariant 9: the CONVEX HULL of backtracked pathlines is a physical
            delineation computed by MF6 PRT; no coordinate is fabricated.
        travel_time_years: list of travel-time isochrone cutoffs, years. Each
            value defines one nested isochrone tier of the capture zone (particles
            that reach the well within this time bound define that zone). When None
            the archetype-specific default is used:
                ``capture_zone``        -> [1.0, 5.0, 10.0]
                ``wellhead_protection`` -> [2.0, 5.0, 10.0] (EPA WHPA tiers)
        n_particles: number of particles released around the pumping-well screen
            per PRT solve (default 16; range 4..256). More particles improve
            capture-zone shape fidelity at the cost of slightly longer runtime.
        archetype: ``'capture_zone'`` (zone-of-contribution) or
            ``'wellhead_protection'`` (EPA fixed-travel-time framing). The
            difference is framing and default tiers only; both produce the same
            carrier.
        aquifer_k_ms / porosity: optional overrides; else SoilGrids-derived at the
            AOI (Saxton-Rawls pedotransfer, a near-surface screening proxy narrated
            loudly) or refused when SoilGrids cannot serve (law 9 - no demo default).
        use_measured_heads: when True (default) the regional gradient is fit to
            recent USGS observed well water levels around the AOI (the measured
            water table); a too-thin / degenerate well set falls back (loud) to the
            DEM proxy, then REFUSES (law 9) rather than a demo gradient. The source
            ladder is narrated so the user sees which basis oriented their capture zone.
        measured_recency_years: recency window (years) for a usable well reading
            (default 10); the most-recent reading per well within the window is used.
        use_dem_gradient: when True (default) the DEM water-table proxy is the
            SECOND rung of the gradient ladder (used only when measured heads are
            unavailable / degenerate).
        use_soil_k: when True (default) AND the caller supplied no ``aquifer_k_ms``,
            the aquifer K is DERIVED from SoilGrids texture at the well via the
            Saxton-Rawls pedotransfer seam and threaded as a LABELED derived basis
            (a near-surface soil PROXY, narrated loudly, never presented as
            measured aquifer K). A soil fetch/sample failure REFUSES (law 9),
            never a demo default K. Ignored when ``aquifer_k_ms`` is supplied.
        compute_class: compute class. NOTE: PRT archetypes are
            LOCAL-ONLY (fast; the Batch path is never used).
        pipeline_emitter: optional PipelineEmitter for live progress cards.

    Returns:
        ``CaptureZoneResult`` with the ``CaptureZoneLayerURI`` (a vector polygon
        carrying per-tier isochrone areas) + derived args + a narration summary.

    Raises:
        CaptureZoneInputError: missing/invalid AOI or well (Invariant 9 gate).
        CaptureZoneScenarioError: a required step (geocode / solver) failed.
        Propagates ``asyncio.CancelledError`` (Invariant 8).
    """
    if archetype not in ("capture_zone", "wellhead_protection"):
        raise CaptureZoneInputError(
            f"model_capture_zone_scenario: archetype must be 'capture_zone' or "
            f"'wellhead_protection'; got {archetype!r}."
        )

    # --- Normalize the WELLFIELD: ``wells`` (list of WellSpec-like
    # objects/dicts) is the multi-well path; the single ``well_location_latlon``
    # is the back-compat path. When wells are supplied the primary well seeds the
    # legacy field so the honesty gate + AOI defaults still hold.
    well_dicts: list[dict[str, Any]] = []
    if wells:
        for w in wells:
            well_dicts.append(
                {
                    "lon": float(w["lon"] if isinstance(w, dict) else w.lon),
                    "lat": float(w["lat"] if isinstance(w, dict) else w.lat),
                    "rate_m3_day": float(
                        (w.get("rate_m3_day") if isinstance(w, dict) else w.rate_m3_day)
                        or 0.0
                    ),
                    "name": (w.get("name") if isinstance(w, dict) else w.name),
                }
            )
        if well_location_latlon is None:
            well_location_latlon = (well_dicts[0]["lat"], well_dicts[0]["lon"])

    # --- Honesty gate (Invariant 9): never fabricate the well -----------------
    if well_location_latlon is None:
        raise CaptureZoneInputError(
            f"{archetype} requires a pumping-well location (well_location_latlon "
            "or a non-empty wells list). The well coordinates are a user input and "
            "are NEVER invented; ask the user to supply the pumping-well lat/lon. "
            "The capture-zone polygon is computed by MF6 backward particle tracking "
            "from the real well cell."
        )

    # Apply archetype-specific default tiers when the caller did not supply them.
    if travel_time_years is None:
        if archetype == "wellhead_protection":
            tiers = list(WELLHEAD_PROTECTION_DEFAULT_TIERS)
        else:
            tiers = list(CAPTURE_ZONE_DEFAULT_TIERS)
    else:
        tiers = [float(t) for t in travel_time_years if t > 0]
        if not tiers:
            raise CaptureZoneInputError(
                "travel_time_years must contain at least one positive value; "
                f"got {travel_time_years!r}."
            )

    # declare the planned internal-tool count up front: geocode (only when a place
    # string was supplied) + measured-heads gradient (fetch wells + fetch DEM) +
    # DEM-proxy fallback (fetch DEM) + run_modflow_archetype_job (always). An
    # over-count is harmless (progress bar finishes a touch early on the common
    # measured-success path where the DEM proxy is never reached).
    _planned = 1
    has_loc = bool(location and location.strip())
    if has_loc:
        _planned += 1
    if use_measured_heads:
        _planned += 2
    if use_dem_gradient:
        _planned += 1
    # Aquifer-property resolution emits one fetch_soilgrids substep (the shared
    # resolver's sand+clay reads roll up into it). An over-count is harmless.
    _planned += 1
    begin_substeps(current_emitter(), _planned)

    lat, lon, location_name = await _resolve_aoi_point(
        location, aoi_latlon, pipeline_emitter=pipeline_emitter
    )

    try:
        wlat = float(well_location_latlon[0])
        wlon = float(well_location_latlon[1])
    except Exception as exc:  # noqa: BLE001
        raise CaptureZoneInputError(
            f"invalid well_location_latlon (expected (lat, lon)): {exc}"
        ) from exc

    # --- Regional-gradient source ladder (honest, best-basis first) ----------- #
    # The CHD boundary is oriented to a regional gradient so the capture zone
    # extends up-gradient toward recharge -- the "what land does my well draw from"
    # answer. Three rungs, each a LOUD downgrade to the next:
    #   1. MEASURED heads: a potentiometric plane fit to recent USGS observed well
    #      water levels (the real water table). Cross-dataset, so narrated loudly.
    #   2. DEM proxy: the shallow water table as a subdued replica of topography.
    #   3. Demo west->east: the last-resort typed placeholder direction.
    grad_x: float | None = None
    grad_y: float | None = None
    gradient_source = "demo_west_east"
    gradient_magnitude: float | None = None
    gradient_azimuth_deg: float | None = None
    measured_meta: dict[str, Any] = {}
    measured_fit: dict[str, Any] | None = None
    measured_fallback_reason: str | None = None
    used_wells: list[dict[str, Any]] = []
    interp_provenance: dict[str, Any] = {}
    wt_surface: Any = None  # the kriged/trend WaterTableSurface (per-cell IC source)

    if use_measured_heads:
        try:
            from datetime import datetime, timezone

            gw_entry = TOOL_REGISTRY.get("fetch_usgs_groundwater_levels")
            dem_entry = TOOL_REGISTRY.get("fetch_dem")
            if gw_entry is None or dem_entry is None:
                measured_fallback_reason = "usgs/dem fetcher not registered"
            else:
                d = WELL_SEARCH_HALF_DEG
                wells_bbox = [wlon - d, wlat - d, wlon + d, wlat + d]
                async with substep(current_emitter(), "fetch_usgs_groundwater_levels"):
                    wells_layer = await asyncio.to_thread(
                        lambda: gw_entry.fn(bbox=wells_bbox)
                    )
                wells_uri = (
                    wells_layer.get("uri")
                    if isinstance(wells_layer, dict)
                    else getattr(wells_layer, "uri", None)
                )
                ls_dem_uri = None
                if wells_uri:
                    async with substep(current_emitter(), "fetch_dem"):
                        ls_dem = await asyncio.to_thread(
                            lambda: dem_entry.fn(
                                bbox=wells_bbox, resolution_m=WELL_DEM_RESOLUTION_M
                            )
                        )
                    ls_dem_uri = (
                        ls_dem.get("uri")
                        if isinstance(ls_dem, dict)
                        else getattr(ls_dem, "uri", None)
                    )
                feats = await asyncio.to_thread(_read_wells_features, wells_uri) if wells_uri else []
                used_wells, measured_meta = await asyncio.to_thread(
                    _usable_well_heads,
                    feats, ls_dem_uri, wlat, wlon,
                    now=datetime.now(timezone.utc),
                    recency_years=float(measured_recency_years),
                )
                measured_fit, fit_reason = _fit_measured_gradient(used_wells)
                if measured_fit is not None:
                    grad_x = measured_fit["gx"]
                    grad_y = measured_fit["gy"]
                    gradient_magnitude = measured_fit["magnitude"]
                    gradient_azimuth_deg = measured_fit["azimuth"]
                    gradient_source = "measured_heads"
                    # Interpolate a water-table SURFACE from the same usable wells
                    # (regression kriging when the set is dense enough, else a
                    # trend plane; the shared seam states the rule). The surface's
                    # method + variogram are recorded for provenance and for the
                    # (worker-side) per-cell starting-head follow-on; the CHD
                    # gradient itself stays the measured plane fit above (the two
                    # agree - both fit the same regional trend).
                    surface = await asyncio.to_thread(
                        interpolate_water_table, used_wells
                    )
                    if surface is not None:
                        wt_surface = surface
                        interp_provenance = surface.provenance()
                else:
                    measured_fallback_reason = fit_reason
        except Exception as exc:  # noqa: BLE001 -- measured heads is best-effort
            measured_fallback_reason = f"measured-heads step error: {exc}"
            logger.warning(
                "capture_zone measured-heads step failed (non-fatal, dropping to "
                "the DEM proxy): %s",
                exc,
            )

    # --- Georeferenced-gradient mode (DEM water-table proxy, 2nd rung) -------- #
    # Runs only when measured heads did not yield a usable gradient. A DEM fetch
    # failure or a near-flat AOI is a LOUD fallback to the demo west->east
    # gradient, never a silent wrong-direction zone.
    if use_dem_gradient and gradient_source != "measured_heads":
        try:
            fetch_dem_entry = TOOL_REGISTRY.get("fetch_dem")
            if fetch_dem_entry is None:
                raise CaptureZoneScenarioError("fetch_dem tool is not registered")
            d = DEM_GRADIENT_HALF_DEG
            dem_bbox = [wlon - d, wlat - d, wlon + d, wlat + d]
            async with substep(current_emitter(), "fetch_dem"):
                dem_layer = await asyncio.to_thread(
                    lambda: fetch_dem_entry.fn(bbox=dem_bbox)
                )
            dem_uri = (
                dem_layer.get("uri")
                if isinstance(dem_layer, dict)
                else getattr(dem_layer, "uri", None)
            )
            if dem_uri:
                grad = await asyncio.to_thread(
                    _planar_gradient_from_dem, dem_uri, wlat, wlon
                )
                if grad is not None:
                    grad_x, grad_y, gradient_magnitude, gradient_azimuth_deg = grad
                    gradient_source = "dem"
        except Exception as exc:  # noqa: BLE001 -- DEM gradient is best-effort
            logger.warning(
                "capture_zone DEM-gradient step failed (non-fatal, using demo "
                "west->east gradient): %s",
                exc,
            )

    # --- Aquifer properties: resolve at the well or REFUSE (law 9) ----------- #
    # When the caller supplied no K, DERIVE it from SoilGrids texture at the well
    # via the shared Saxton-Rawls pedotransfer seam - a LABELED derived basis, a
    # NEAR-SURFACE screening proxy narrated loudly, never presented as measured
    # aquifer hydrogeology. When SoilGrids cannot serve it (fetch fails, off the
    # soil surface, or use_soil_k=False), the input-review gate REFUSES rather than
    # solving on an invented demo constant. The gate runs BEFORE the (PRT) solve.
    async with substep(current_emitter(), "fetch_soilgrids"):
        resolution = await resolve_aquifer_properties(
            wlat, wlon, aquifer_k_ms, porosity, allow_soil_derive=bool(use_soil_k),
        )
    _k_review = await review_modflow_entries(
        tool_name="modflow_capture_zone", entries=list(resolution.entries),
        params={"aquifer_k_ms": aquifer_k_ms, "porosity": porosity},
        input_mode=input_mode,
    )
    if _k_review.cancelled or not resolution.resolved:
        raise CaptureZoneInputError(
            _k_review.cancel_reason
            or physics_refusal_reason("modflow_capture_zone", resolution.entries)
            or "aquifer properties could not be resolved; the delineation was not finalized"
        )
    eff_k = float(resolution.k_ms)
    eff_porosity = float(resolution.porosity)
    k_source = resolution.k_source
    soil_k_meta = resolution.soil_meta

    # --- Kriged per-cell IC (item 3) --------------------------------- #
    # Sample the kriged/trend water-table surface at each PRT cell centre so the
    # GWF IC carries the measured water-table CURVATURE (matters for the transient
    # solve). Only when a surface was fitted (measured-heads success); otherwise
    # the worker keeps its uniform-IC fallback (loud, honest).
    starting_head_by_cell: list[list[float]] | None = None
    if wt_surface is not None:
        starting_head_by_cell = await asyncio.to_thread(
            _build_kriged_starting_head, wt_surface, lat, lon, wlat, wlon
        )

    # --- NHD river boundaries (item 4) ------------------------------- #
    # Fetch the NHD flowline network around the AOI and drape it as RIV cells. A
    # fetch/read failure degrades LOUDLY to the CHD ring alone (never fails the
    # solve). ``fetch_river_geometry`` returns a flowline artifact the shared
    # ``resolve_river_reaches_lonlat`` reads into per-reach lon/lat polylines.
    river_reaches: list[list[tuple[float, float]]] | None = None
    if use_nhd_river_boundaries:
        try:
            from trid3nt_server.workflows.modflow.run_modflow import (
                resolve_river_reaches_lonlat,
            )

            river_entry = TOOL_REGISTRY.get("fetch_river_geometry")
            if river_entry is not None:
                d = DEM_GRADIENT_HALF_DEG
                riv_bbox = [wlon - d, wlat - d, wlon + d, wlat + d]
                async with substep(current_emitter(), "fetch_river_geometry"):
                    riv_layer = await asyncio.to_thread(
                        lambda: river_entry.fn(bbox=riv_bbox)
                    )
                riv_uri = (
                    riv_layer.get("uri") if isinstance(riv_layer, dict)
                    else getattr(riv_layer, "uri", None)
                )
                if riv_uri:
                    reaches = await asyncio.to_thread(
                        resolve_river_reaches_lonlat, riv_uri
                    )
                    river_reaches = reaches or None
        except Exception as exc:  # noqa: BLE001 -- NHD boundaries are best-effort
            logger.warning(
                "capture_zone NHD river-boundary step failed (non-fatal, CHD ring "
                "only): %s", exc
            )

    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon),
            contaminant="n/a",       # GWF-only archetype: no solute (placeholder)
            release_rate_kg_s=1.0,   # ignored when archetype is set
            duration_days=1.0,       # ignored when archetype is set
            archetype=archetype,
            grid_type=grid_type,
            well_location_latlon=(wlat, wlon),
            capture_zone_travel_time_years=tiers,
            n_particles=int(n_particles),
            regional_gradient_x=grad_x,
            regional_gradient_y=grad_y,
            # multi-well WELLFIELD + transient + NHD RIV + kriged IC.
            wells=(well_dicts or None),
            capture_zone_transient=bool(transient),
            sim_years=sim_years,
            n_periods=n_periods,
            river_reaches=river_reaches,
            starting_head_by_cell=starting_head_by_cell,
            **_aquifer_overrides(eff_k, eff_porosity, None, None),
        )
    except Exception as exc:  # noqa: BLE001  -  pydantic ValidationError
        raise CaptureZoneInputError(
            f"invalid {archetype} run arguments: {exc}"
        ) from exc

    label = (
        f"Model {'wellhead protection area' if archetype == 'wellhead_protection' else 'capture zone'} "
        f"[{len(tiers)} tier(s), {n_particles} particles]"
    )
    layer = await _run_archetype(
        run_args,
        compute_class=compute_class,
        pipeline_emitter=pipeline_emitter,
        tool_label=label,
        expected_type=CaptureZoneLayerURI,
        error_code=f"{archetype.upper()}_RUN_FAILED",
        scenario_error=CaptureZoneScenarioError,
    )

    # The adapter labels any supplied gradient vector "dem" (it cannot tell measured
    # heads from a DEM proxy -- both are a vector). The composer is the authority on
    # provenance: relabel the layer when the vector came from measured heads so the
    # rendered/inspected layer reads honestly (magnitude/azimuth are already correct,
    # recomputed by the adapter from the same clamped vector).
    if gradient_source == "measured_heads":
        layer.gradient_source = "measured_heads"
        # Emit the used wells as a context overlay so the user SEES the observed
        # data the gradient was fit to (best-effort; never fails the solve).
        wells_layer = await asyncio.to_thread(
            _build_used_wells_layer, used_wells, layer.layer_id
        )
        if wells_layer is not None:
            await publish_input_layer(
                pipeline_emitter or current_emitter(), wells_layer, role="context"
            )
            measured_meta["wells_layer_uri"] = wells_layer.uri

    # Surface the backtracked PRT pathline fan as its OWN context layer beside the
    # convex-hull polygon (input-parity doctrine: all visualizable intermediate
    # data reaches the map -- the hull alone hides which trajectories delineated
    # it). postprocess built the separate FlatGeobuf + LayerURI; publish it here
    # exactly like the gradient wells (best-effort; never fails the solve).
    pathlines_layer = getattr(layer, "pathlines_layer", None)
    if pathlines_layer is not None:
        await publish_input_layer(
            pipeline_emitter or current_emitter(), pathlines_layer, role="context"
        )

    layer_grad_source = getattr(layer, "gradient_source", gradient_source)
    layer_grad_mag = getattr(layer, "gradient_magnitude", gradient_magnitude)
    layer_grad_az = getattr(layer, "gradient_azimuth_deg", gradient_azimuth_deg)
    derived = {
        "location_name": location_name,
        "aoi_latlon": [lat, lon],
        "well_location_latlon": [wlat, wlon],
        "archetype": archetype,
        "travel_time_years": tiers,
        "n_particles": n_particles,
        "gradient_source": layer_grad_source,
        "regional_gradient_x": grad_x,
        "regional_gradient_y": grad_y,
        "gradient_magnitude": layer_grad_mag,
        "gradient_azimuth_deg": layer_grad_az,
        "measured_heads": measured_meta,
        "measured_fallback_reason": measured_fallback_reason,
        "water_table_interpolation": interp_provenance,
        "aquifer_k_source": k_source,
        "aquifer_k_ms": eff_k,
        "porosity": eff_porosity,
        "soil_k": soil_k_meta,
        "used_wells": [
            {"site_no": w["site_no"], "lon": w["lon"], "lat": w["lat"],
             "head_elev_m": round(float(w["head_m"]), 3), "basis": w["basis"],
             "datum": w["datum"], "date": w["date_iso"]}
            for w in used_wells
        ] if layer_grad_source == "measured_heads" else [],
    }
    iso_areas = getattr(layer, "isochrone_areas_km2", {})
    if layer_grad_source == "measured_heads" and measured_fit is not None:
        _bc = ", ".join(f"{k}={v}" for k, v in (measured_meta.get("by_basis") or {}).items())
        gradient_caveat = (
            f"Regional gradient fit to MEASURED heads: {measured_fit['n']} USGS "
            f"observed wells ({measured_fit['date_min']}..{measured_fit['date_max']}), "
            f"magnitude {layer_grad_mag:.2g} m/m, groundwater flows toward azimuth "
            f"{layer_grad_az:.0f} deg (the capture zone extends the opposite, "
            f"up-gradient way). Potentiometric-plane fit residual "
            f"{measured_fit['residual_m']:.2f} m over a {measured_fit['head_range_m']:.1f} m "
            f"head relief; well basis [{_bc}]. Heads are co-referenced to NAVD88 "
            f"(depth-to-water anchored to 3DEP land surface; NGVD29 elevations "
            f"shifted a nominal {NGVD29_TO_NAVD88_M:+.2f} m). A screening measured "
            f"gradient (observed wells, cross-dataset), not a calibrated flow model."
        )
    elif layer_grad_source == "dem":
        _why = (
            f" (measured heads unusable: {measured_fallback_reason})"
            if measured_fallback_reason else ""
        )
        gradient_caveat = (
            f"Regional gradient DEM-derived{_why}: magnitude {layer_grad_mag:.2g} m/m, "
            f"groundwater flows toward azimuth {layer_grad_az:.0f} deg (the capture "
            "zone extends the opposite, up-gradient way). This is a SCREENING proxy "
            "-- the shallow water table taken as a subdued replica of surface "
            "topography (DEM slope), NOT a measured potentiometric surface."
        )
    else:
        _why = (
            f" (measured heads unusable: {measured_fallback_reason})"
            if measured_fallback_reason else ""
        )
        gradient_caveat = (
            f"Regional gradient is the DEMO west->east placeholder{_why} (no usable "
            "measured heads or DEM slope). The zone ORIENTATION is a placeholder, "
            "not the site's true flow direction -- narrate this."
        )
    summary = {
        "location_name": location_name,
        "archetype": archetype,
        "well_location_latlon": [wlat, wlon],
        "capture_zone_area_km2": layer.capture_zone_area_km2,
        "travel_time_years": layer.travel_time_years,
        "isochrone_areas_km2": iso_areas,
        "particle_count": layer.particle_count,
        "pathline_count": getattr(layer, "pathline_count", 0),
        "pathlines_layer_uri": (
            pathlines_layer.uri if pathlines_layer is not None else None
        ),
        "gradient_source": layer_grad_source,
        "gradient_magnitude_m_per_m": layer_grad_mag,
        "gradient_azimuth_deg": layer_grad_az,
        "gradient_well_count": (measured_fit["n"] if measured_fit is not None else None),
        "gradient_fit_residual_m": (
            measured_fit["residual_m"] if measured_fit is not None else None
        ),
        "gradient_date_range": (
            f"{measured_fit['date_min']}..{measured_fit['date_max']}"
            if measured_fit is not None else None
        ),
        "stagnation_distance_m": getattr(layer, "stagnation_distance_m", None),
        "capture_width_m": getattr(layer, "capture_width_m", None),
        "gradient_caveat": gradient_caveat,
        "water_table_interp_method": interp_provenance.get("method"),
        "aquifer_k_source": k_source,
        "aquifer_k_ms": eff_k,
        "aquifer_provenance": provenance_summary(resolution),
    }
    # The aquifer-K/porosity entries (already gated pre-solve) are stamped onto the
    # layer for the envelope provenance; the regional-gradient entry is gated here
    # too - it is physics-consequential and REFUSES if it fell back to a demo
    # west->east gradient (audit row 7: DEM-derive when possible, else refuse).
    _review_entries = [
        *resolution.entries,
        SyntheticInput(
            param="regional_gradient", value=layer_grad_source,
            basis=("fetched" if layer_grad_source == "measured_heads"
                   else "derived" if layer_grad_source == "dem" else "default_demo"), consequence="physics",
            real_source_if_any=(
                "USGS observed well heads" if layer_grad_source == "measured_heads"
                else "3DEP DEM slope" if layer_grad_source == "dem" else None),
            note=gradient_caveat,
        ),
    ]
    layer, _review = await gate_and_stamp_modflow_inputs(
        tool_name="modflow_capture_zone", layer=layer, entries=_review_entries,
        params={"archetype": archetype}, input_mode=input_mode,
    )
    if _review.cancelled:
        raise CaptureZoneInputError(
            _review.cancel_reason
            or "capture-zone input review not approved; the delineation was not finalized"
        )
    logger.info(
        "%s scenario complete location=%r capture_zone_area_km2=%.6g tiers=%s",
        archetype,
        location_name,
        layer.capture_zone_area_km2,
        layer.travel_time_years,
    )
    return CaptureZoneResult(
        capture_zone_layer=layer, derived_params=derived, summary=summary
    )


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrappers (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


TEMPLATE_CARD = TemplateCard(
    question=(
        "the capture zone / zone of contribution for a pumping well "
        "(backward particle tracking isochrones)"
    ),
    required_inputs=["location (or aoi_latlon)", "well_location_latlon"],
    knobs="travel_time_years, n_particles, grid_type, aquifer_k_ms, porosity",
)


_CAPTURE_ZONE_METADATA = AtomicToolMetadata(
    name="modflow_capture_zone",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="modflow",
    tier="template",
)


@register_tool(
    _CAPTURE_ZONE_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def modflow_capture_zone(
    location: str | None = None,
    aoi_latlon: tuple[float, float] | list[float] | None = None,
    well_location_latlon: tuple[float, float] | list[float] | None = None,
    travel_time_years: list[float] | None = None,
    n_particles: int = 16,
    grid_type: str = "structured",
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    # multi-well WELLFIELD + transient + NHD RIV boundaries.
    wells: list[Any] | None = None,
    transient: bool = False,
    sim_years: float | None = None,
    n_periods: int | None = None,
    use_nhd_river_boundaries: bool = False,
    compute_class: str = "standard",
    input_mode: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Delineate the capture zone (zone of contribution) for a pumping well.

    Fidelity: MODFLOW 6 local planning-grade groundwater envelope (aquifer
    K/porosity are SoilGrids-derived at the AOI or refused when unavailable, law 9), not a
    calibrated regulatory delineation. Off-scope: surface-water inundation
    flooding -> sfincs_flood; urban storm-sewer / pipe-network flooding ->
    swmm_urban_flood.

    Builds a MODFLOW 6 steady groundwater-flow model, then runs an MF6 PRT
    (Particle Tracking) backward-tracking solve that releases particles around
    the pumping-well screen and tracks them up-gradient to their capture origin.
    The convex hull of all backtracked pathlines at each requested travel-time
    threshold is the capture-zone isochrone for that tier. Produces a VECTOR
    polygon layer on the map (violet protection-zone colour).

    Use this when:
        - The user asks for the capture zone, zone of contribution, zone of
          influence, or zone of transport for a pumping well.
        - The user asks how far back in time the water in a well came from.

    Do NOT use this for:
        - A wellhead PROTECTION area with EPA WHPA framing (use
          ``modflow_wellhead_protection``).
        - A pumping-well DRAWDOWN cone (use ``modflow_sustainable_yield``).
        - A contaminant spill plume (use ``modflow_contaminant_plume``).

    PRECISION CAVEAT: the polygon is the CONVEX HULL of discrete backtracked
    pathlines on a structured 100 m rectilinear grid with SoilGrids-derived (or
    caller-supplied) aquifer parameters, refusing when no real source serves them
    (law 9), NOT a calibrated regulatory wellhead protection area. Always narrate
    this caveat.

    Params:
        location: place name (geocoded). Supply this OR ``aoi_latlon``.
        aoi_latlon: explicit ``(lat, lon)`` AOI point.
        well_location_latlon: the pumping-well ``(lat, lon)``. REQUIRED -- never
            invented; ask the user if absent (Invariant 9).
        travel_time_years: list of isochrone cutoffs in years. Default [1, 5, 10].
        n_particles: particles released around the well screen (default 16; range
            4..256). More = denser pathline fan = more representative shape.
        grid_type: 'structured' (default, uniform 100 m grid) or 'disv_quadrefined'
            (a gridgen 3-level quad-refined DISV vertex grid around the well, 12.5 m
            finest cell) that resolves the pumping cone the structured grid smears
            (; single-well steady only, needs the gridgen binary).
        aquifer_k_ms / porosity: optional overrides; else SoilGrids-derived at the AOI or refused (law 9).
        compute_class: compute class. Default ``'standard'``. PRT
            archetypes run LOCAL-ONLY (fast; Batch is not used).

    Returns:
        On success: a ``CaptureZoneResult`` JSON dict with the
        ``capture_zone_layer`` (a ``CaptureZoneLayerURI`` carrying
        ``capture_zone_area_km2`` + ``travel_time_years`` + per-tier
        ``isochrone_areas_km2`` + ``particle_count``). On a recoverable failure
        (incl. a missing well) the tool returns a typed error the agent narrates
        honestly -- it never fabricates a well.

    ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
    """
    aoi = _coerce_optional_latlon(aoi_latlon)
    well = _coerce_optional_latlon(well_location_latlon)
    try:
        result = await model_capture_zone_scenario(
            location=location,
            aoi_latlon=aoi,
            well_location_latlon=well,
            travel_time_years=(
                [float(t) for t in travel_time_years] if travel_time_years else None
            ),
            n_particles=int(n_particles),
            archetype="capture_zone",
            grid_type=grid_type,
            aquifer_k_ms=aquifer_k_ms,
            porosity=porosity,
            wells=wells,
            transient=bool(transient),
            sim_years=sim_years,
            n_periods=n_periods,
            use_nhd_river_boundaries=bool(use_nhd_river_boundaries),
            compute_class=compute_class,
            input_mode=input_mode,
            pipeline_emitter=None,
        )
    except CaptureZoneInputError as exc:
        return {
            "status": "error",
            "error_code": "USER_INPUT_REQUIRED",
            "error_message": str(exc),
        }
    except CaptureZoneScenarioError as exc:
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "CAPTURE_ZONE_SCENARIO_ERROR"),
            "error_message": str(exc),
        }
    return result.model_dump(mode="json")

