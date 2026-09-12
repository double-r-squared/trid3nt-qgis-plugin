"""Chart emission: the one Vega-Lite chart-envelope builder and its read helpers.

Every chart tool and engine postprocessor routes its spec through
``build_chart_payload``. This module registers no tool of its own.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

import numpy as np

from trid3nt_contracts import new_ulid
from trid3nt_contracts.chart_contracts import ChartEmissionPayload

__all__ = [
    "ChartToolError",
    "build_chart_payload",
    "is_chart_emission_result",
    "build_budget_partition_chart",
    "build_hydrograph_overlay_chart",
]

logger = logging.getLogger("trid3nt_server.render.charts")



#: Maximum number of inline rows in a Vega-Lite spec's ``data.values``, so the
#: wire envelope (and the function_response that summarizes it) stays small.
#: Histograms and bars are pre-binned well under this; the cap is a hard rail.
_MAX_ROWS = 2000

#: Maximum number of raster cells sampled for a histogram. A full COG can be
#: tens of millions of cells; sampling caps the read cost while preserving the
#: distribution shape. Deterministic sampling (fixed RNG seed) so the chart is
#: stable across calls on the same layer.
_RASTER_SAMPLE_CAP = 500_000

#: Vega-Lite v5 schema URL - declaring it makes the spec pass the contract's
#: structural sanity check (``is_structurally_valid_vega_lite_spec``).
_VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"

#: Deterministic RNG seed for raster sampling.
_SAMPLE_SEED = 1730000000

_RASTER_EXTS = {".tif", ".tiff", ".img", ".vrt", ".nc"}

_VECTOR_EXTS = {".fgb", ".geojson", ".gpkg", ".shp", ".json", ".gml", ".kml"}



class ChartToolError(RuntimeError):
    """Raised when a chart-generation tool cannot produce a chart.

    ``error_code`` is a SCREAMING_SNAKE_CASE code the retry surface reads.
    """

    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable



def _download_uri_bytes(uri: str, storage_client: object | None = None) -> bytes:
    """Download bytes from an ``s3://`` URI or read a local path.

    ``storage_client`` is ignored: object-store reads route through boto3.
    """
    del storage_client
    # The shared boto3 reader, never s3fs: only boto3 picks up the instance role.
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        try:
            return read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise ChartToolError(
                "DOWNLOAD_FAILED",
                f"S3 download failed for {uri!r}: {exc}",
                retryable=True,
            ) from exc
    try:
        with open(uri, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ChartToolError(
            "DOWNLOAD_FAILED", f"Could not read local path {uri!r}: {exc}"
        ) from exc

def _materialize_uri(uri: str, tmpdir: str, label: str, storage_client: object | None = None) -> str:
    """Return a local file path for the given URI (downloads ``s3://`` to tmpdir)."""
    # s3:// URIs are staged via the shared reader.
    if uri.startswith("s3://"):
        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local_path = os.path.join(tmpdir, f"{label}_{name}")
        data = _download_uri_bytes(uri, storage_client)
        with open(local_path, "wb") as f:
            f.write(data)
        return local_path
    return uri

def _layer_type(uri: str) -> str:
    """Return ``"raster"`` or ``"vector"`` by extension, or by probing."""
    ext = os.path.splitext(uri.split("?")[0].rstrip("/"))[-1].lower()
    if ext in _RASTER_EXTS:
        return "raster"
    if ext in _VECTOR_EXTS:
        return "vector"
    try:
        import rasterio

        with rasterio.open(uri):
            return "raster"
    except Exception:  # noqa: BLE001
        return "vector"

def _read_geodataframe(local_path: str):  # type: ignore[return]
    """Read a vector file into a GeoDataFrame (typed error on failure)."""
    try:
        import geopandas as gpd  # type: ignore[import-not-found]

        return gpd.read_file(local_path)
    except Exception as exc:  # noqa: BLE001
        raise ChartToolError(
            "LAYER_OPEN_FAILED", f"Could not open vector layer {local_path!r}: {exc}"
        ) from exc

def _summarize_raster(local_path: str) -> dict[str, Any]:
    """Open a single-band raster and compute summary statistics + histogram.
    A masked read: GDAL's own validity decides which pixels count, and NaN is
    masked on top of it because a raster may carry NaN fill without tagging it.
    """
    try:
        import rasterio
    except ImportError as exc:
        raise ChartToolError("LAYER_OPEN_FAILED", "rasterio not available") from exc

    try:
        with rasterio.open(local_path) as src:
            band = src.read(1, masked=True).astype(np.float64)
            units = (
                src.tags().get("units")
                or (src.units[0] if src.units else None)
            )
    except Exception as exc:  # noqa: BLE001
        raise ChartToolError(
            "LAYER_OPEN_FAILED",
            f"Could not open raster {local_path!r}: {exc}",
        ) from exc

    pixels = np.ma.masked_invalid(band).compressed()
    count = int(pixels.size)

    if count == 0:
        return {
            "layer_type": "raster",
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "sum": None,
            "distribution": [],
            "units": units,
        }

    hist, bin_edges = np.histogram(pixels, bins=10)

    return {
        "layer_type": "raster",
        "count": count,
        "min": float(pixels.min()),
        "max": float(pixels.max()),
        "mean": float(pixels.mean()),
        "sum": float(pixels.sum()),
        "distribution": [
            {
                "bin_start": float(bin_edges[i]),
                "bin_end": float(bin_edges[i + 1]),
                "count": int(hist[i]),
            }
            for i in range(len(hist))
        ],
        "units": units,
    }

def _summarize_vector(local_path: str) -> dict[str, Any]:
    """Read a vector layer and compute per-attribute numeric summaries.

    A column with no non-null values reports zeros-and-Nones, not NaN aggregates.
    """
    gdf = _read_geodataframe(local_path)
    numeric = gdf.drop(columns="geometry", errors="ignore").select_dtypes("number")
    stats = (
        numeric.astype(np.float64)
        .agg(["count", "min", "max", "mean", "sum"])
        .to_dict()
        if len(numeric.columns)
        else {}
    )

    return {
        "layer_type": "vector",
        "feature_count": len(gdf),
        "attribute_summary": {
            col: (
                {"count": 0, "min": None, "max": None, "mean": None, "sum": None}
                if int(agg["count"]) == 0
                else {
                    "count": int(agg["count"]),
                    "min": float(agg["min"]),
                    "max": float(agg["max"]),
                    "mean": float(agg["mean"]),
                    "sum": float(agg["sum"]),
                }
            )
            for col, agg in stats.items()
        },
    }

def _validate_uri(uri: object, field: str) -> str:
    if not isinstance(uri, str) or not uri.strip():
        raise ChartToolError(
            "DOWNLOAD_FAILED", f"{field} must be a non-empty URI string; got {uri!r}"
        )
    return uri.strip()




def build_chart_payload(
    *,
    vega_lite_spec: dict[str, Any],
    title: str,
    caption: str | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any]:
    """Wrap a Vega-Lite spec in a validated ``ChartEmissionPayload`` dict.

    Forces the v5 ``$schema`` and caps inline ``data.values`` at ``_MAX_ROWS``.
    """
    spec = dict(vega_lite_spec)
    spec.setdefault("$schema", _VEGA_LITE_V5_SCHEMA)

    # Hard row cap on inline data (contract + wire-size safety).
    data = spec.get("data")
    if isinstance(data, dict) and isinstance(data.get("values"), list):
        values = data["values"]
        if len(values) > _MAX_ROWS:
            spec = {**spec, "data": {**data, "values": values[:_MAX_ROWS]}}
            logger.warning(
                "chart inline data clipped from %d to %d rows (title=%r)",
                len(values),
                _MAX_ROWS,
                title,
            )

    payload = ChartEmissionPayload(
        chart_id=new_ulid(),
        vega_lite_spec=spec,
        title=title,
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )
    return payload.model_dump(mode="json")

def is_chart_emission_result(result: Any) -> bool:
    """True iff ``result`` is a ChartEmissionPayload-shaped dict.

    The discriminator is ``envelope_type == "chart-emission"`` plus a dict spec.
    """
    return (
        isinstance(result, dict)
        and result.get("envelope_type") == "chart-emission"
        and isinstance(result.get("vega_lite_spec"), dict)
        and isinstance(result.get("chart_id"), str)
    )

# Engine-output chart builders (wire non-raster engine values).
#
# Every number is a real parsed engine output, never synthesized. When the
# required series is absent or empty each builder returns ``None``: the honesty
# floor is to emit NO chart rather than invent one.


def build_budget_partition_chart(
    *,
    budget_partition_m3_day: dict[str, float],
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Build a signed inflow/outflow bar chart from a MODFLOW CBC partition.
    Flows are m^3/day under the MF6 sign (+ = into the aquifer); ``None`` when
    the partition is empty.
    """
    if not budget_partition_m3_day:
        return None
    rows: list[dict[str, Any]] = []
    for term, value in budget_partition_m3_day.items():
        q = float(value)
        rows.append(
            {
                "term": str(term),
                "flow_m3_day": q,
                "direction": "inflow" if q >= 0 else "outflow",
            }
        )
    if not rows:
        return None
    # Order largest-inflow -> largest-outflow for a readable budget.
    rows.sort(key=lambda r: r["flow_m3_day"], reverse=True)

    spec = {
        "title": "Groundwater budget partition",
        "data": {"values": rows},
        "mark": {"type": "bar", "tooltip": True},
        "encoding": {
            "x": {
                "field": "term",
                "type": "nominal",
                "title": "budget term",
                "sort": [r["term"] for r in rows],
            },
            "y": {
                "field": "flow_m3_day",
                "type": "quantitative",
                "title": "flow (m^3/day, + = into aquifer)",
            },
            "color": {
                "field": "direction",
                "type": "nominal",
                "scale": {
                    "domain": ["inflow", "outflow"],
                    "range": ["#1d6fb8", "#c1121f"],
                },
                "title": "direction",
            },
        },
        "width": "container",
    }
    total_in = sum(r["flow_m3_day"] for r in rows if r["flow_m3_day"] >= 0)
    total_out = sum(-r["flow_m3_day"] for r in rows if r["flow_m3_day"] < 0)
    caption = (
        f"{len(rows)} budget terms · inflow {total_in:,.3g} m^3/day · "
        f"outflow {total_out:,.3g} m^3/day (+ = into the aquifer)"
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title="Groundwater budget partition",
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )


def build_hydrograph_overlay_chart(
    *,
    times: list[float] | list[str],
    computed: list[float | None],
    observed: list[float | None] | None = None,
    x_title: str = "elapsed hours",
    y_title: str = "discharge (m3/s)",
    title: str = "Computed vs observed hydrograph",
    computed_label: str = "computed",
    observed_label: str = "observed",
    nse: float | None = None,
    r2: float | None = None,
    source_layer_uri: str | None = None,
    created_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Overlay a computed hydrograph against an optional observed one.
    ``times`` is numeric (elapsed hours) or ISO8601 strings; ``nse``/``r2`` are
    captioned as supplied and never recomputed here.
    """
    n = min(len(times), len(computed))
    xs_raw = list(times)[:n]
    numeric_x = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in xs_raw)
    x_type = "quantitative" if numeric_x else "temporal"

    # Non-finite and mismatched-length samples are dropped, never interpolated.
    def _series_rows(values: list[Any] | None, label: str) -> list[dict[str, Any]]:
        if not values:
            return []
        rows: list[dict[str, Any]] = []
        for i in range(min(n, len(values))):
            v = values[i]
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fv):
                continue
            x = float(xs_raw[i]) if numeric_x else str(xs_raw[i])
            rows.append({"t": x, "discharge": fv, "series": label})
        return rows

    computed_rows = _series_rows(list(computed), computed_label)
    # Fewer than 2 finite computed points is not a hydrograph: emit no chart
    # rather than draw a single point as a series.
    if len(computed_rows) < 2:
        return None
    observed_rows = _series_rows(list(observed) if observed else None, observed_label)
    rows = computed_rows + observed_rows
    if len(rows) > _MAX_ROWS:
        # Uniform per-series stride so both series survive the wire-size cap.
        keep: list[dict[str, Any]] = []
        for label_rows in (computed_rows, observed_rows):
            if not label_rows:
                continue
            stride = max(1, len(label_rows) // (_MAX_ROWS // 2))
            keep.extend(label_rows[::stride])
        rows = keep

    spec = {
        "title": title,
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {"field": "t", "type": x_type, "title": x_title},
            "y": {"field": "discharge", "type": "quantitative", "title": y_title},
            "color": {"field": "series", "type": "nominal", "title": "series"},
        },
        "width": "container",
    }

    comp_peak = max(r["discharge"] for r in computed_rows)
    skill_txt = ""
    if nse is not None:
        skill_txt += f" · NSE {nse:.3f}"
    if r2 is not None:
        skill_txt += f" · R2 {r2:.3f}"
    if observed_rows:
        obs_peak = max(r["discharge"] for r in observed_rows)
        caption = (
            f"computed (peak {comp_peak:.3g}) vs observed (peak {obs_peak:.3g}) "
            f"{y_title}{skill_txt}"
        )
    else:
        caption = f"computed outlet hydrograph · peak {comp_peak:.3g} {y_title}{skill_txt}"

    return build_chart_payload(
        vega_lite_spec=spec,
        title=title,
        caption=caption,
        source_layer_uri=source_layer_uri,
        created_turn_id=created_turn_id,
    )



def _sample_raster_values(local_path: str) -> np.ndarray:
    """Return a 1-D array of valid (non-nodata, finite) cell values.

    Samples at most ``_RASTER_SAMPLE_CAP`` cells, deterministically seeded.
    """
    try:
        import rasterio
    except ImportError as exc:
        raise ChartToolError("LAYER_OPEN_FAILED", "rasterio not available") from exc

    try:
        with rasterio.open(local_path) as src:
            data = src.read(1).astype(np.float64)
            nodata = src.nodata
    except Exception as exc:  # noqa: BLE001
        raise ChartToolError(
            "LAYER_OPEN_FAILED", f"Could not open raster {local_path!r}: {exc}"
        ) from exc

    flat = data.ravel()
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        valid = flat[(flat != nodata) & np.isfinite(flat)]
    else:
        valid = flat[np.isfinite(flat)]

    if valid.size > _RASTER_SAMPLE_CAP:
        rng = np.random.default_rng(_SAMPLE_SEED)
        idx = rng.choice(valid.size, size=_RASTER_SAMPLE_CAP, replace=False)
        valid = valid[idx]
    return valid


