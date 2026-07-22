"""OpenQuake PSHA hazard-map postprocessing (sprint-17).

``postprocess_openquake(hazard_map_csv, *, run_id, run_args, ...) ->
SeismicHazardLayerURI`` reads OpenQuake's exported hazard-MAP CSV (one
``lon,lat,<imt-value>`` row per PSHA site), rasterizes the per-site hazard value
onto a regular EPSG:4326 grid, writes a COG, computes the narration scalars, and
returns the typed :class:`~trid3nt_contracts.openquake_contracts.SeismicHazardLayerURI`.

The OpenQuake analogue of ``postprocess_modflow`` (MF6-GWT plume) /
``postprocess_swmm`` (urban depth) / ``postprocess_flood`` (SFINCS). The defining
difference: OpenQuake emits a SCATTERED set of site values in a CSV, NOT a grid —
we interpolate/rasterize the point hazard onto a raster ourselves (the engine's
site grid is regular, so a nearest/linear fill onto the lon/lat lattice is the
honest reconstruction). The hazard map is in EPSG:4326 already (the site grid was
laid in lon/lat), so unlike the MODFLOW UTM plume there is no reprojection step.

Reuse (do NOT reinvent): the COG-write profile + ``_cog_bbox_4326`` zoom-to +
``_dispatch_publish_layer`` non-fatal publish pattern from ``postprocess_modflow``
(adapted for an already-EPSG:4326 site lattice). The honesty floor (Invariant 1 /
FR-AS-7): the hazard scalars are computed with plain arithmetic from the site
values — no LLM anywhere; the agent narrates the typed fields, never invents them.

Tier separation (Invariant 5): the COG lands in the runs bucket (scheme-aware via
``cache.storage_scheme()``); the agent does not re-render — ``publish_layer`` /
TiTiler serves the tiles from the URI on the envelope.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from pathlib import Path
from typing import Any

from trid3nt_contracts.openquake_contracts import SeismicHazardLayerURI

from . import cog_io
from .cog_io import CogIoError

logger = logging.getLogger("trid3nt_server.workflows.postprocess_openquake")

__all__ = [
    "PostprocessOpenQuakeError",
    "postprocess_openquake",
    "publish_openquake_quantities",
    "parse_hazard_map_csv",
    "parse_hazard_curve_csv",
    "parse_uhs_csv",
    "rasterize_hazard_sites",
    "compute_hazard_metrics",
    "SEISMIC_HAZARD_STYLE_PRESET",
    "HAZARD_FLOOR_VALUE",
]


#: The publish_layer style preset key the seismic hazard map renders with (the
#: magma ramp 0..1 in g; an ADDITIVE registry preset, disjoint from the existing
#: flood/plume keys — never mutated). Merged into _TITILER_STYLE_REGISTRY by the
#: orchestrator (see shared_appends.publish_layer_preset).
SEISMIC_HAZARD_STYLE_PRESET: str = "continuous_seismic_pga"

#: Hazard values at/below this floor (g) are masked to NaN so the COG renders
#: only the meaningful hazard (a near-zero PGA site is "no hazard").
HAZARD_FLOOR_VALUE: float = 0.001


class PostprocessOpenQuakeError(RuntimeError):
    """Raised on read / rasterize / COG-write / upload failures.

    ``error_code`` matches the open-set A.6 surface so the agent emitter renders
    a typed error frame. Codes:

    - ``OQ_HAZARD_READ_FAILED`` — could not open / parse the hazard-map CSV.
    - ``OQ_HAZARD_EMPTY`` — the CSV carries no site rows — nothing to rasterize.
    - ``OQ_DEPENDENCY_MISSING`` — numpy / rasterio not importable in the runtime.
    - ``OQ_COG_WRITE_FAILED`` — rasterio could not write the hazard COG.
    - ``OQ_COG_UPLOAD_FAILED`` — the runs-bucket upload of the COG failed.
    """

    error_code: str = "POSTPROCESS_OPENQUAKE_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# Hazard-map CSV parse.
# --------------------------------------------------------------------------- #
def parse_hazard_map_csv(text: str) -> tuple[list[tuple[float, float, float]], str]:
    """Parse an OpenQuake hazard-MAP CSV into ``[(lon, lat, value), ...]`` + the
    value-column header.

    OpenQuake's ``hazard_map-mean-<IMT>_<...>.csv`` has a one-line ``#`` comment
    banner, then a header row ``lon,lat,<IMT>-<poe>`` (e.g. ``lon,lat,PGA-0.1``),
    then one row per site. We locate the lon/lat columns by name and take the
    remaining numeric column as the hazard value. Robust to the leading comment
    line + arbitrary value-column naming.

    Returns ``(rows, value_header)``. Raises on a structurally unreadable CSV.
    """
    # Drop OpenQuake's leading ``#`` banner comment line(s).
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise PostprocessOpenQuakeError(
            "OQ_HAZARD_EMPTY", message="hazard-map CSV has no data lines"
        )
    reader = csv.reader(io.StringIO("\n".join(lines)))
    header = next(reader, None)
    if not header:
        raise PostprocessOpenQuakeError(
            "OQ_HAZARD_READ_FAILED", message="hazard-map CSV missing a header row"
        )
    cols = [c.strip().lower() for c in header]
    try:
        lon_idx = cols.index("lon")
        lat_idx = cols.index("lat")
    except ValueError as exc:
        raise PostprocessOpenQuakeError(
            "OQ_HAZARD_READ_FAILED",
            message=f"hazard-map CSV header missing lon/lat columns: {header!r}",
        ) from exc
    # The hazard VALUE is the first column that is neither lon nor lat (and not a
    # depth/vs30 site column).
    val_idx = None
    for i, name in enumerate(cols):
        if i in (lon_idx, lat_idx):
            continue
        if name in {"depth", "vs30", "z1pt0", "z2pt5"}:
            continue
        val_idx = i
        break
    if val_idx is None:
        raise PostprocessOpenQuakeError(
            "OQ_HAZARD_READ_FAILED",
            message=f"hazard-map CSV has no hazard-value column: {header!r}",
        )
    value_header = header[val_idx].strip()

    rows: list[tuple[float, float, float]] = []
    for raw in reader:
        if not raw or len(raw) <= max(lon_idx, lat_idx, val_idx):
            continue
        try:
            lon = float(raw[lon_idx])
            lat = float(raw[lat_idx])
            val = float(raw[val_idx])
        except (TypeError, ValueError):
            continue
        rows.append((lon, lat, val))
    if not rows:
        raise PostprocessOpenQuakeError(
            "OQ_HAZARD_EMPTY", message="hazard-map CSV has no parseable site rows"
        )
    return rows, value_header


# --------------------------------------------------------------------------- #
# Rasterize the scattered site values onto a regular lon/lat lattice.
# --------------------------------------------------------------------------- #
def rasterize_hazard_sites(
    rows: list[tuple[float, float, float]],
) -> tuple[Any, tuple[float, float, float, float], float]:
    """Place the OpenQuake site values onto a regular EPSG:4326 raster grid.

    NATE 2026-06-26: OpenQuake's ``region_grid_spacing`` is in KM, so the site
    grid is NOT a clean lat/lon lattice - the lon of each row is offset slightly
    by latitude (km->deg for lon depends on lat), giving ~3e-5 deg jitter within a
    column. The old ``round(., 6)`` treated those jittered near-duplicates as
    DISTINCT lons (e.g. 34 "columns" for a real ~5, a striped raster + area=0,
    proven by a real local oq run). We now CLUSTER near-duplicate axis values
    within a tolerance (derived from the clean lat spacing) so the true lattice is
    recovered, then snap each site to its nearest clustered node. Returns
    ``(grid, (min_lon,min_lat,max_lon,max_lat), cell_deg)``. ``grid`` is row 0 =
    NORTH (north-up, ready for ``from_origin(west, north, ...)``).
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessOpenQuakeError(
            "OQ_DEPENDENCY_MISSING", message=f"numpy unavailable: {exc}"
        ) from exc

    if not rows:
        raise PostprocessOpenQuakeError(
            "OQ_HAZARD_EMPTY", message="no distinct site coordinates"
        )

    def _median_step(vals: list[float]) -> float:
        if len(vals) < 2:
            return 0.05
        steps = [b - a for a, b in zip(vals, vals[1:]) if b > a]
        steps.sort()
        return steps[len(steps) // 2] if steps else 0.05

    # Cluster sorted values: merge any value within ``tol`` of the running
    # cluster into it, returning each cluster's centroid (the recovered lattice
    # node). Collapses the per-row km-grid lon jitter into one node per column.
    def _cluster(vals: list[float], tol: float) -> list[float]:
        if not vals:
            return []
        clusters: list[list[float]] = [[vals[0]]]
        for v in vals[1:]:
            if v - clusters[-1][-1] <= tol:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [sum(c) / len(c) for c in clusters]

    # Derive a reference cell size from the LAT axis (km-grid lats are regular -
    # only the lon jitters), then cluster BOTH axes with a tolerance well below
    # the real spacing but well above the jitter.
    lat_raw = sorted({round(r[1], 6) for r in rows})
    cell_ref = _median_step(lat_raw)  # ~ the km grid spacing in degrees
    tol = max(cell_ref * 0.3, 1e-4)
    lons = _cluster(sorted(r[0] for r in rows), tol)
    lats = _cluster(sorted(r[1] for r in rows), tol)
    if len(lons) < 1 or len(lats) < 1:
        raise PostprocessOpenQuakeError(
            "OQ_HAZARD_EMPTY", message="no distinct site coordinates"
        )

    step_lon = _median_step(lons)
    step_lat = _median_step(lats)
    cell = float(min(step_lon, step_lat))

    width = len(lons)
    height = len(lats)
    lon_index = {v: i for i, v in enumerate(lons)}
    # row 0 = north -> iterate lats descending.
    lats_desc = list(reversed(lats))
    lat_index = {v: i for i, v in enumerate(lats_desc)}

    grid = np.full((height, width), np.nan, dtype="float32")
    for lon, lat, val in rows:
        # Snap to the nearest lattice node (handles float jitter).
        li = lon_index.get(round(lon, 6))
        ri = lat_index.get(round(lat, 6))
        if li is None:
            li = min(range(width), key=lambda i: abs(lons[i] - lon))
        if ri is None:
            ri = min(range(height), key=lambda i: abs(lats_desc[i] - lat))
        grid[ri, li] = float(val)

    min_lon, max_lon = lons[0], lons[-1]
    min_lat, max_lat = lats[0], lats[-1]
    # Expand the bbox by half a cell so the raster bounds frame the site centers.
    half = cell / 2.0
    bbox = (min_lon - half, min_lat - half, max_lon + half, max_lat + half)
    return grid, bbox, cell


# --------------------------------------------------------------------------- #
# Metrics (the narration scalars).
# --------------------------------------------------------------------------- #
def compute_hazard_metrics(
    grid: Any, cell_deg: float, mean_lat: float
) -> tuple[float, float, int]:
    """Compute ``(max_hazard_value, hazard_area_km2, n_sites)`` from the grid.

    ``hazard_area_km2`` is the footprint of cells above ``HAZARD_FLOOR_VALUE``
    (each cell's area = the cell extent in km^2, accounting for the lat-dependent
    longitude foreshortening). ``n_sites`` counts the non-NaN cells (the PSHA
    sites). Plain arithmetic — no LLM.
    """
    import numpy as np  # type: ignore[import-not-found]

    arr = np.asarray(grid, dtype="float64")
    finite = np.isfinite(arr)
    n_sites = int(np.count_nonzero(finite))
    if n_sites == 0:
        return 0.0, 0.0, 0
    max_val = float(np.nanmax(arr))
    # Cell area in km^2: (cell_deg * 111.32) for lat extent;
    # (cell_deg * 111.32 * cos(lat)) for lon extent.
    km_per_deg = 111.32
    lat_km = cell_deg * km_per_deg
    lon_km = cell_deg * km_per_deg * abs(math.cos(math.radians(mean_lat)))
    cell_area = lat_km * lon_km
    above = np.logical_and(finite, arr > HAZARD_FLOOR_VALUE)
    hazard_area = float(np.count_nonzero(above)) * cell_area
    return max_val, hazard_area, n_sites


# --------------------------------------------------------------------------- #
# COG write (already EPSG:4326 — no reprojection).
# --------------------------------------------------------------------------- #
#: stage -> (OpenQuake error_code) map (STEP 1 dedupe; byte-identical codes).
_OQ_STAGE_CODES: dict[str, str] = {
    "DEPENDENCY": "OQ_DEPENDENCY_MISSING",
    "WRITE": "OQ_COG_WRITE_FAILED",
    "REPROJECT": "OQ_COG_WRITE_FAILED",
    "CRS_MISMATCH": "OQ_COG_WRITE_FAILED",
    "UPLOAD": "OQ_COG_UPLOAD_FAILED",
}


def _reraise_cogio(exc: CogIoError) -> "PostprocessOpenQuakeError":
    """Map a cog_io ``CogIoError`` onto the OpenQuake typed error (preserves codes)."""
    code = _OQ_STAGE_CODES.get(exc.stage, "POSTPROCESS_OPENQUAKE_FAILED")
    return PostprocessOpenQuakeError(code, message=exc.message, details=dict(exc.details))


def _write_cog(grid: Any, bbox: tuple[float, float, float, float]) -> Path:
    """Write the hazard grid to an EPSG:4326 COG. Sub-floor cells -> NaN.

    Thin shim over ``cog_io.write_cog_4326_from_grid`` (STEP 1 dedupe;
    ``reproject=False`` - the hazard grid is ALREADY EPSG:4326): build the affine
    from the bbox + shape, mask cells at/below ``HAZARD_FLOOR_VALUE`` to NaN
    (declared ``mask``), write the COG directly. NO CRS round-trip guard
    (byte-identical to the pre-dedupe writer, which trusted the upstream lattice).
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        from rasterio.transform import from_bounds  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessOpenQuakeError(
            "OQ_DEPENDENCY_MISSING",
            message=f"numpy/rasterio unavailable: {exc}",
        ) from exc

    arr = np.asarray(grid, dtype="float32")
    height, width = arr.shape
    min_lon, min_lat, max_lon, max_lat = bbox
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, width, height)

    def _mask(a: Any) -> Any:
        return np.where(a > HAZARD_FLOOR_VALUE, a, np.nan).astype("float32")

    try:
        return cog_io.write_cog_4326_from_grid(
            arr,
            src_crs="EPSG:4326",
            src_transform=transform,
            reproject=False,
            mask=_mask,
            crs_roundtrip_guard=False,
            dst_suffix="_hazard_4326.tif",
        )
    except CogIoError as exc:
        # cog_io's generic WRITE message ("COG write failed: ...") is mapped to
        # the OQ code; the historic text was "hazard COG write failed: ..." but
        # only the error_code is contract-pinned.
        raise _reraise_cogio(exc) from exc


def _cog_bbox_4326(cog_path: Path) -> tuple[float, float, float, float] | None:
    """Return the COG's (min_lon, min_lat, max_lon, max_lat) for zoom-to."""
    return cog_io.cog_bbox_4326(cog_path)


def _upload_cog(local_cog: Path, run_id: str, runs_bucket: str | None) -> str:
    """Upload the EPSG:4326 hazard COG to the runs bucket; return its object URI.

    Thin shim over ``cog_io.upload_cog`` (STEP 1 dedupe; byte-identical):
    scheme-aware per ``cache.storage_scheme()``. ``s3`` via boto3 (NO
    ``ContentType`` header - matches the historic ``put_object``) FAILS TYPED on a
    missing ``TRID3NT_RUNS_BUCKET`` / upload error. The ``gs`` branch uses the
    ``google.cloud.storage`` client (NOT fsspec) with a best-effort ``file://``
    fallback (offline/local dev), and returns ``file://`` immediately when no
    bucket is configured.
    """
    try:
        return cog_io.upload_cog(
            local_cog,
            run_id,
            runs_bucket,
            dest_filename="seismic_hazard_4326.tif",
            content_type=None,  # historic put_object set no ContentType
            gs_backend="gcs_client",
            gs_fallback_to_file=True,
            runs_bucket_default=None,  # gs path returns file:// when no bucket
            log_label="hazard COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc


# --------------------------------------------------------------------------- #
# publish_layer dispatch (callable; mocked in tests).
# --------------------------------------------------------------------------- #
def _dispatch_publish_layer(cog_uri: str, layer_id: str) -> str | None:
    """Publish the hazard COG; return the WMS URL / tile template or None.

    Non-fatal (mirrors postprocess_modflow): a publish failure falls back to the
    COG URI so the rest of the envelope is usable. Skips publish for
    non-object-store URIs (local mode has nothing for a tile server to read).
    """
    if not (cog_uri.startswith("gs://") or cog_uri.startswith("s3://")):
        logger.warning(
            "publish_layer SKIPPED for %s: COG URI is not gs:// or s3:// (%s); "
            "the hazard map will NOT render as a map layer.",
            layer_id,
            cog_uri,
        )
        return None
    try:
        from ..tools.publish_layer import publish_layer

        wms_url = publish_layer(
            layer_uri=cog_uri,
            layer_id=layer_id,
            style_preset=SEISMIC_HAZARD_STYLE_PRESET,
        )
        logger.info("publish_layer succeeded layer_id=%s wms_url=%s", layer_id, wms_url)
        return wms_url
    except Exception as exc:  # noqa: BLE001
        logger.warning("publish_layer failed for %s: %s", layer_id, exc)
        return None


# --------------------------------------------------------------------------- #
# Top-level postprocess.
# --------------------------------------------------------------------------- #
def postprocess_openquake(
    hazard_map_csv_text: str,
    *,
    run_id: str,
    imt: str,
    poe: float,
    investigation_time_years: float,
    runs_bucket: str | None = None,
    publish: bool = True,
) -> SeismicHazardLayerURI:
    """Convert an OpenQuake hazard-MAP CSV into a ``SeismicHazardLayerURI``.

    Parses the per-site hazard values, rasterizes them onto a regular EPSG:4326
    grid, writes a COG, computes the hazard metrics, uploads + (optionally)
    publishes the COG, and returns the typed seismic-hazard layer.

    Args:
        hazard_map_csv_text: the text of OpenQuake's exported hazard-map CSV.
        run_id: the run identifier the COG is keyed under in the runs bucket.
        imt: the Intensity Measure Type the map represents (echoed onto the layer).
        poe: the probability of exceedance the map was computed at.
        investigation_time_years: the PoE window, years (for the return-period
            scalar the agent narrates).
        runs_bucket: optional override for the runs bucket name.
        publish: when True, dispatch ``publish_layer`` (mocked in tests).

    Returns:
        ``SeismicHazardLayerURI`` with ``max_hazard_value`` + ``hazard_area_km2``
        + ``return_period_years`` and (when published) a WMS ``uri``, else the
        COG URI.

    Raises:
        PostprocessOpenQuakeError: any read / rasterize / write / upload step
            failed; ``error_code`` identifies the stage.
    """
    rows, _value_header = parse_hazard_map_csv(hazard_map_csv_text)
    grid, bbox, cell_deg = rasterize_hazard_sites(rows)
    mean_lat = (bbox[1] + bbox[3]) / 2.0
    max_val, hazard_area_km2, n_sites = compute_hazard_metrics(grid, cell_deg, mean_lat)

    # Return period implied by the PoE over the investigation time.
    try:
        rp_years = (
            -float(investigation_time_years) / math.log(1.0 - float(poe))
            if 0.0 < poe < 1.0 and investigation_time_years > 0.0
            else 0.0
        )
    except (ValueError, ZeroDivisionError):
        rp_years = 0.0

    logger.info(
        "postprocess_openquake run_id=%s imt=%s poe=%.4g rp=%.0fyr "
        "max_hazard=%.6g hazard_area_km2=%.6g n_sites=%d",
        run_id,
        imt,
        poe,
        rp_years,
        max_val,
        hazard_area_km2,
        n_sites,
    )

    cog_path = _write_cog(grid, bbox)
    bbox_4326 = _cog_bbox_4326(cog_path) or bbox
    cog_uri = _upload_cog(cog_path, run_id, runs_bucket)

    layer_id = f"seismic-hazard-{run_id}"
    final_uri = cog_uri
    if publish:
        wms_url = _dispatch_publish_layer(cog_uri, layer_id)
        if wms_url:
            final_uri = wms_url

    # Units: PGA/SA in g, PGV in cm/s.
    units = "cm/s" if imt.upper().startswith("PGV") else "g"
    name = f"Seismic hazard ({imt}, {int(round(rp_years))}-yr return period)"

    return SeismicHazardLayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=final_uri,
        style_preset=SEISMIC_HAZARD_STYLE_PRESET,
        role="primary",
        units=units,
        bbox=bbox_4326,
        imt=imt,
        poe=poe,
        investigation_time_years=investigation_time_years,
        return_period_years=rp_years,
        max_hazard_value=max_val,
        hazard_area_km2=hazard_area_km2,
        n_sites=n_sites,
    )


# --------------------------------------------------------------------------- #
# levers STEP 3 -- NEW non-raster quantities: hazard CURVES + UHS (ScalarField).
#
# The classical PSHA run ALREADY exports a mean hazard CURVE CSV
# (hazard_curve-mean-<IMT>_*.csv); enabling uniform_hazard_spectra also exports
# a UHS CSV (hazard_uhs-mean_*.csv). These are NON-RASTER products: they do not
# fit cog / cog_timeseries. Per the levers plan we route them to the run METRICS
# (the ScalarField emitter branch of publish_quantities) + leave the
# conversational-analysis CHART producer (a Vega-Lite PoE-vs-IML / SA-vs-period
# line chart) as a FOLLOW-UP (the chart_tools producers consume layer URIs, not
# the OpenQuake curve CSVs -- a new producer is a separate deliverable). A true
# non-raster product table (the full curve / spectrum as a structured payload)
# is likewise DEFERRED; here we surface the load-bearing summary scalars.
# --------------------------------------------------------------------------- #
def parse_hazard_curve_csv(text: str) -> dict[str, Any]:
    """Parse a mean hazard-CURVE CSV into summary scalars (no LLM).

    OpenQuake's ``hazard_curve-mean-<IMT>_*.csv`` carries a ``#`` banner, then a
    header ``lon,lat,depth,poe-<iml1>,poe-<iml2>,...`` and one row per site (the
    PoE at each IML). We extract the IML ladder (from the ``poe-<iml>`` column
    names) and the MEAN PoE across sites at each IML, returning a compact
    summary: the IML list, the mean-PoE-by-IML list, and the count of sites.
    """
    lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not lines:
        return {}
    reader = csv.reader(io.StringIO("\n".join(lines)))
    header = next(reader, None)
    if not header:
        return {}
    cols = [c.strip() for c in header]
    # PoE columns are named "poe-<iml>" (OpenQuake convention).
    iml_cols: list[tuple[int, float]] = []
    for i, name in enumerate(cols):
        low = name.lower()
        if low.startswith("poe-"):
            try:
                iml_cols.append((i, float(name.split("-", 1)[1])))
            except (ValueError, IndexError):
                continue
    if not iml_cols:
        return {}
    sums = [0.0] * len(iml_cols)
    n = 0
    for raw in reader:
        if not raw:
            continue
        try:
            for k, (ci, _iml) in enumerate(iml_cols):
                sums[k] += float(raw[ci])
            n += 1
        except (ValueError, IndexError):
            continue
    if n == 0:
        return {}
    imls = [iml for _ci, iml in iml_cols]
    mean_poe = [s / n for s in sums]
    return {
        "hazard_curve_imls_g": imls,
        "hazard_curve_mean_poe": mean_poe,
        "hazard_curve_n_sites": n,
    }


def parse_uhs_csv(text: str) -> dict[str, Any]:
    """Parse a UHS CSV into summary scalars (no LLM).

    OpenQuake's ``hazard_uhs-mean_*.csv`` carries a ``#`` banner, then a header
    ``lon,lat,depth,<poe>~SA(<period>),...`` (or ``~PGA``) and one row per site:
    the spectral acceleration at each period for the fixed PoE. We extract the
    period ladder + the MEAN SA across sites at each period.
    """
    lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not lines:
        return {}
    reader = csv.reader(io.StringIO("\n".join(lines)))
    header = next(reader, None)
    if not header:
        return {}
    cols = [c.strip() for c in header]
    period_cols: list[tuple[int, float]] = []
    for i, name in enumerate(cols):
        # column like "0.1~SA(0.2)" or "0.1~PGA" -> period 0.2 / 0.0.
        if "SA(" in name:
            try:
                period = float(name.split("SA(", 1)[1].rstrip(")"))
                period_cols.append((i, period))
            except (ValueError, IndexError):
                continue
        elif name.upper().endswith("PGA"):
            period_cols.append((i, 0.0))
    if not period_cols:
        return {}
    sums = [0.0] * len(period_cols)
    n = 0
    for raw in reader:
        if not raw:
            continue
        try:
            for k, (ci, _p) in enumerate(period_cols):
                sums[k] += float(raw[ci])
            n += 1
        except (ValueError, IndexError):
            continue
    if n == 0:
        return {}
    periods = [p for _ci, p in period_cols]
    mean_sa = [s / n for s in sums]
    return {
        "uhs_periods_s": periods,
        "uhs_mean_sa_g": mean_sa,
        "uhs_n_sites": n,
    }


def publish_openquake_quantities(
    *,
    run_id: str,
    register_manifest_layers: Any,
    hazard_curve_csv_text: str | None = None,
    uhs_csv_text: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> Any:
    """Publish the NEW OpenQuake quantities (hazard curves + UHS) as SCALARS.

    Both are non-raster products: the readers return ``ScalarField`` so the
    shared executor merges their summary into the manifest METRICS (no layer).
    A ``None`` CSV / an unparseable CSV yields an empty ScalarField (skipped).
    Returns the executor result.
    """
    from dataclasses import replace as _dc_replace

    from trid3nt_contracts.output_quantities import (
        ScalarField,
        get_output_registry,
    )

    from . import publish_quantities as _pq

    def _curve_reader(_ctx: Any) -> ScalarField:
        return ScalarField(
            values=parse_hazard_curve_csv(hazard_curve_csv_text or "")
        )

    def _uhs_reader(_ctx: Any) -> ScalarField:
        return ScalarField(values=parse_uhs_csv(uhs_csv_text or ""))

    readers = {"hazard-curves": _curve_reader, "uhs": _uhs_reader}
    specs = [
        _dc_replace(spec, reader=readers[spec.quantity_id])
        for spec in get_output_registry("openquake")
        if spec.quantity_id in readers
    ]

    def _upload(cog: Path, rid: str, _bucket: Any = None, *, dest_filename: str) -> str:
        # Scalars never write a COG; this is only here to satisfy the executor
        # signature (it is never invoked for a ScalarField).
        return _upload_cog(cog, rid, None)

    return _pq.publish_quantities(
        "openquake",
        run_id=run_id,
        upload=_upload,
        register_manifest_layers=register_manifest_layers,
        specs=specs,
        bbox=bbox,
    )
