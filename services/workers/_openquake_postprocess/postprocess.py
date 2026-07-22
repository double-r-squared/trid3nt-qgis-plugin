"""Worker-side OpenQuake hazard-map CSV -> EPSG:4326 COG postprocess.

Byte-faithful port of ``trid3nt_server.workflows.postprocess_openquake`` (hazard
map rasterization path). Runs inside the Batch worker AFTER ``oq engine`` has
exported its hazard-map CSV; rasterizes the site values onto an EPSG:4326 COG
and builds the typed ``publish_manifest.json`` dict.

NEVER imports agent code. NEVER itself writes completion.json.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.workers._raster_postprocess import manifest as _manifest
from services.workers._raster_postprocess.band_stats import compute_band_stats

LOG = logging.getLogger("trid3nt.worker.openquake_postprocess")

SEISMIC_HAZARD_STYLE_PRESET: str = "continuous_seismic_pga"

#: Hazard values at/below this floor (g) are masked to NaN (no hazard).
HAZARD_FLOOR_VALUE: float = 0.001

_HAZARD_COG_FILENAME: str = "seismic_hazard_4326.tif"


@dataclass
class OpenQuakePostprocessResult:
    """What the entrypoint folds into completion.json + writes as the manifest."""

    status: str  # "ok" | "error"
    manifest: dict[str, Any] | None
    metrics: dict[str, Any] = field(default_factory=dict)
    cog_paths: list[Path] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


# --------------------------------------------------------------------------- #
# Hazard-map CSV parse (byte-faithful port from agent postprocess_openquake).
# --------------------------------------------------------------------------- #
def _parse_hazard_map_csv(text: str) -> tuple[list[tuple[float, float, float]], str]:
    """Parse an OpenQuake hazard-MAP CSV into ``[(lon, lat, value), ...]`` + header."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return [], "value"
    reader = csv.reader(io.StringIO("\n".join(lines)))
    header = next(reader, None)
    if not header:
        return [], "value"
    cols = [c.strip().lower() for c in header]
    try:
        lon_idx = cols.index("lon")
        lat_idx = cols.index("lat")
    except ValueError:
        return [], "value"
    val_idx = None
    for i, name in enumerate(cols):
        if i in (lon_idx, lat_idx):
            continue
        if name in {"depth", "vs30", "z1pt0", "z2pt5"}:
            continue
        val_idx = i
        break
    if val_idx is None:
        return [], "value"
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
    return rows, value_header


# --------------------------------------------------------------------------- #
# Rasterize (byte-faithful port of rasterize_hazard_sites with km-jitter clustering).
# --------------------------------------------------------------------------- #
def _rasterize_hazard_sites(
    rows: list[tuple[float, float, float]],
) -> tuple[Any, tuple[float, float, float, float], float] | None:
    """Place site values onto a regular EPSG:4326 grid with km-jitter clustering.

    Returns ``(grid, (min_lon, min_lat, max_lon, max_lat), cell_deg)`` or None on
    failure. Port of ``postprocess_openquake.rasterize_hazard_sites``.
    """
    try:
        import numpy as np  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None

    if not rows:
        return None

    def _median_step(vals: list[float]) -> float:
        if len(vals) < 2:
            return 0.05
        steps = [b - a for a, b in zip(vals, vals[1:]) if b > a]
        steps.sort()
        return steps[len(steps) // 2] if steps else 0.05

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

    lat_raw = sorted({round(r[1], 6) for r in rows})
    cell_ref = _median_step(list(lat_raw))
    tol = max(cell_ref * 0.3, 1e-4)
    lons = _cluster(sorted(r[0] for r in rows), tol)
    lats = _cluster(sorted(r[1] for r in rows), tol)
    if not lons or not lats:
        return None

    step_lon = _median_step(lons)
    step_lat = _median_step(lats)
    cell = float(min(step_lon, step_lat)) if step_lon > 0 and step_lat > 0 else max(step_lon, step_lat)
    if cell <= 0:
        cell = 0.05

    width = len(lons)
    height = len(lats)
    lon_index = {v: i for i, v in enumerate(lons)}
    lats_desc = list(reversed(lats))
    lat_index = {v: i for i, v in enumerate(lats_desc)}

    grid = np.full((height, width), np.nan, dtype="float32")
    for lon, lat, val in rows:
        li = lon_index.get(round(lon, 6))
        ri = lat_index.get(round(lat, 6))
        if li is None:
            li = min(range(width), key=lambda i: abs(lons[i] - lon))
        if ri is None:
            ri = min(range(height), key=lambda i: abs(lats_desc[i] - lat))
        grid[ri, li] = float(val)

    min_lon, max_lon = lons[0], lons[-1]
    min_lat, max_lat = lats[0], lats[-1]
    half = cell / 2.0
    bbox = (min_lon - half, min_lat - half, max_lon + half, max_lat + half)
    return grid, bbox, cell


def _compute_hazard_metrics(
    grid: Any, cell_deg: float, mean_lat: float
) -> tuple[float, float, int]:
    """Return ``(max_val, hazard_area_km2, n_sites)``."""
    import numpy as np  # noqa: PLC0415

    arr = np.asarray(grid, dtype="float64")
    finite = np.isfinite(arr)
    n_sites = int(np.count_nonzero(finite))
    if n_sites == 0:
        return 0.0, 0.0, 0
    max_val = float(np.nanmax(arr))
    km_per_deg = 111.32
    lat_km = cell_deg * km_per_deg
    lon_km = cell_deg * km_per_deg * abs(math.cos(math.radians(mean_lat)))
    cell_area = lat_km * lon_km
    above = np.logical_and(finite, arr > HAZARD_FLOOR_VALUE)
    hazard_area = float(np.count_nonzero(above)) * cell_area
    return max_val, hazard_area, n_sites


def _write_hazard_cog(
    grid: Any,
    bbox: tuple[float, float, float, float],
    out_path: Path,
) -> None:
    """Write the hazard grid as an EPSG:4326 COG; cells <= floor -> NaN."""
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.transform import from_bounds  # noqa: PLC0415

    arr = np.asarray(grid, dtype="float32")
    arr = np.where(arr > HAZARD_FLOOR_VALUE, arr, np.nan).astype("float32")
    height, width = arr.shape
    min_lon, min_lat, max_lon, max_lat = bbox
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, width, height)
    with rasterio.open(
        out_path, "w", driver="COG",
        width=width, height=height, count=1, dtype="float32",
        crs="EPSG:4326", transform=transform, nodata=float("nan"),
        compress="LZW",
    ) as dst:
        dst.write(arr, 1)


def run_openquake_postprocess(
    run_id: str,
    scratch: str | Path,
    build_spec: dict[str, Any],
    runs_uri_for: Any,
) -> OpenQuakePostprocessResult:
    """Run OpenQuake hazard-map CSV -> COG postprocess.

    ``runs_uri_for`` is a callable ``rel -> uri``. The COG is written to scratch
    under a deterministic key; the entrypoint's output sweep uploads it.
    """
    import glob as _glob  # noqa: PLC0415

    scratch = Path(scratch)

    # Find hazard map CSV: prefer hazard_map-mean-*.csv, then any hazard_map*.csv,
    # then any *.csv under output/, then any *.csv in scratch.
    csv_path: Path | None = None
    for pattern in [
        "output/hazard_map-mean-*.csv",
        "output/hazard_map*.csv",
        "output/*.csv",
        "*.csv",
    ]:
        hits = sorted(_glob.glob(str(scratch / pattern)))
        if hits:
            csv_path = Path(hits[0])
            break

    if csv_path is None or not csv_path.exists():
        return OpenQuakePostprocessResult(
            status="error", manifest=None,
            error_code="OQ_HAZARD_EMPTY",
            error_message="no hazard-map CSV found in scratch",
        )

    try:
        text = csv_path.read_text(encoding="utf-8", errors="replace")
        rows, _value_header = _parse_hazard_map_csv(text)
    except Exception as exc:  # noqa: BLE001
        return OpenQuakePostprocessResult(
            status="error", manifest=None,
            error_code="OQ_HAZARD_READ_FAILED",
            error_message=f"CSV parse failed: {exc}",
        )

    if not rows:
        return OpenQuakePostprocessResult(
            status="error", manifest=None,
            error_code="OQ_HAZARD_EMPTY",
            error_message="hazard-map CSV has no parseable site rows",
        )

    result = _rasterize_hazard_sites(rows)
    if result is None:
        return OpenQuakePostprocessResult(
            status="error", manifest=None,
            error_code="OQ_HAZARD_EMPTY",
            error_message="could not rasterize hazard sites (no distinct coordinates)",
        )

    grid, bbox, cell_deg = result
    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = 0.5 * (min_lat + max_lat)
    max_val, hazard_area_km2, n_sites = _compute_hazard_metrics(grid, cell_deg, mean_lat)

    # Honesty gate.
    if n_sites == 0:
        return OpenQuakePostprocessResult(
            status="error", manifest=None,
            error_code="OQ_HAZARD_EMPTY",
            error_message="no valid hazard sites after rasterization",
        )

    cog_path = scratch / _HAZARD_COG_FILENAME
    try:
        _write_hazard_cog(grid, bbox, cog_path)
    except Exception as exc:  # noqa: BLE001
        return OpenQuakePostprocessResult(
            status="error", manifest=None,
            error_code="OQ_COG_WRITE_FAILED",
            error_message=f"hazard COG write failed: {exc}",
        )

    imt = str(build_spec.get("imt", "PGA"))
    poe = float(build_spec.get("poe", 0.1))
    inv_time = float(build_spec.get("investigation_time_years", 50))

    metrics: dict[str, Any] = {
        "max_pga_g": max_val,
        "hazard_area_km2": hazard_area_km2,
        "n_sites": n_sites,
        "imt": imt,
        "poe": poe,
        "investigation_time_years": inv_time,
    }

    try:
        band_stats = compute_band_stats(str(cog_path))
    except Exception:  # noqa: BLE001
        band_stats = {"min": None, "max": None, "p2": None, "p98": None,
                      "is_categorical": False, "is_rgba": False}

    cog_uri = runs_uri_for(_HAZARD_COG_FILENAME)
    layer = _manifest.build_layer_entry(
        layer_id_stem=f"seismic-hazard-{run_id}",
        name=f"Seismic hazard ({imt}, {poe:.0%} in {inv_time:.0f} yr)",
        role="primary",
        style_preset=SEISMIC_HAZARD_STYLE_PRESET,
        units="g",
        cog_uri=cog_uri,
        frame_no=None,
        bbox=list(bbox),
        band_stats=band_stats,
        metrics=metrics,
    )
    mf = _manifest.build_manifest(
        engine="openquake",
        run_id=run_id,
        status="ok",
        frame_count=1,
        metrics=metrics,
        layers=[layer],
    )
    LOG.info(
        "openquake postprocess run_id=%s imt=%s max_pga_g=%.4g "
        "hazard_area_km2=%.4g n_sites=%d cog=%s",
        run_id, imt, max_val, hazard_area_km2, n_sites, _HAZARD_COG_FILENAME,
    )
    return OpenQuakePostprocessResult(
        status="ok",
        manifest=mf,
        metrics=metrics,
        cog_paths=[cog_path],
    )
