"""``query_point_hazard`` atomic tool -- sample every case raster at one point.

Given a location (an explicit lon/lat pair OR a free-text place name geocoded
through the SAME Nominatim machinery ``geocode_location`` uses), sample every
RASTER layer currently loaded on the Case at that point and return the
per-layer values (layer name, sampled value, units when known).

Case layers come from the persisted ``CaseSummary.loaded_layer_summaries``
(``ProjectLayerSummary`` dicts) via the app-level ``Persistence`` singleton --
the exact enumeration path ``export_case_to_qgis`` uses. The Case defaults to
the turn's bound Case (``pipeline_emitter.current_turn_case``) so the LLM does
not need to thread a case_id through.

Honesty (data-source fallback norm)
===================================

- A Case with NO layers raises the typed ``NoCaseLayersError`` (the required
  clear "no case layers" signal); a Case whose layers are all vector raises
  the same typed error with an explicit no-raster message.
- A single unreadable layer is a PER-LAYER honest entry
  (``value=None`` + ``error``), never a hard fail and never a made-up value.
- A point outside a layer's extent, or on nodata, returns ``value=None`` with
  an explicit ``note`` -- "no data here" is stated, not zero-filled.

``cacheable=False`` (``ttl_class="live-no-cache"``): the result depends on the
LIVE Case layer list, which changes as the session runs.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "query_point_hazard",
    "PointHazardError",
    "PointHazardInputError",
    "NoCaseBoundError",
    "NoCaseLayersError",
    "PointHazardUpstreamError",
]

logger = logging.getLogger("grace2_agent.tools.query_point_hazard")


# ---------------------------------------------------------------------------
# Typed errors (FR-AS-11).
# ---------------------------------------------------------------------------


class PointHazardError(RuntimeError):
    """Base class for query_point_hazard failures."""

    error_code: str = "POINT_HAZARD_ERROR"
    retryable: bool = True


class PointHazardInputError(PointHazardError):
    """Bad inputs (no location, out-of-range lon/lat, geocode miss)."""

    error_code = "POINT_HAZARD_INPUT_INVALID"
    retryable = False


class NoCaseBoundError(PointHazardError):
    """No case_id was supplied and no Case is bound to the current turn."""

    error_code = "POINT_HAZARD_NO_CASE"
    retryable = False


class NoCaseLayersError(PointHazardError):
    """The Case has no layers (or no raster layers) to sample -- honest miss."""

    error_code = "POINT_HAZARD_NO_CASE_LAYERS"
    retryable = False


class PointHazardUpstreamError(PointHazardError):
    """Persistence lookup / staging infrastructure failed."""

    error_code = "POINT_HAZARD_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Metadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="query_point_hazard",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


# ---------------------------------------------------------------------------
# Location resolution (lon/lat pair or geocoded place).
# ---------------------------------------------------------------------------


def _geocode_place(place: str) -> dict[str, Any]:
    """Forward-geocode ``place`` via the geocode_location machinery.

    Module-level indirection so offline tests monkeypatch this seam.
    """
    from .data_fetch import geocode_location

    return geocode_location(query=place)


def resolve_point(
    lon: Any, lat: Any, place: Any, error_cls: type[Exception] = PointHazardInputError
) -> tuple[float, float, str]:
    """Resolve ``(lon, lat, label)`` from an explicit pair or a place name.

    Shared with ``extract_timeseries_at_point``. Raises ``error_cls`` on a
    missing/invalid location; geocode failures surface with their real reason.
    """
    if lon is not None or lat is not None:
        try:
            flon, flat = float(lon), float(lat)
        except (TypeError, ValueError) as exc:
            raise error_cls(
                f"lon/lat must both be numeric; got lon={lon!r} lat={lat!r}"
            ) from exc
        if not (math.isfinite(flon) and math.isfinite(flat)):
            raise error_cls(f"lon/lat must be finite; got lon={lon!r} lat={lat!r}")
        if not (-180.0 <= flon <= 180.0 and -90.0 <= flat <= 90.0):
            raise error_cls(
                f"lon/lat out of range (lon in [-180,180], lat in [-90,90]); "
                f"got lon={flon} lat={flat}"
            )
        return flon, flat, f"({flon:.5f}, {flat:.5f})"

    if place is not None and str(place).strip():
        try:
            geo = _geocode_place(str(place).strip())
        except Exception as exc:  # noqa: BLE001 -- surface the real reason
            raise error_cls(
                f"could not geocode place {place!r}: {exc}"
            ) from exc
        try:
            flon = float(geo["longitude"])
            flat = float(geo["latitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise error_cls(
                f"geocoder returned no usable centroid for {place!r}: {geo!r}"
            ) from exc
        label = str(geo.get("name") or place)
        return flon, flat, label

    raise error_cls(
        "provide a location: either lon AND lat, or a free-text place name."
    )


# ---------------------------------------------------------------------------
# Case-layer enumeration (the export_case_to_qgis persistence seam).
# ---------------------------------------------------------------------------


def resolve_case_id(case_id: Any, error_cls: type[Exception] = NoCaseBoundError) -> str:
    """``case_id`` param wins; else the turn's bound Case; else typed error."""
    if case_id is not None and str(case_id).strip():
        return str(case_id).strip()
    try:
        from ..pipeline_emitter import current_turn_case

        bound = current_turn_case()
    except Exception:  # noqa: BLE001
        bound = None
    if bound:
        return str(bound)
    raise error_cls(
        "no case_id was supplied and no Case is bound to the current turn; "
        "pass case_id explicitly."
    )


async def layers_from_case(
    case_id: str,
    not_found_cls: type[Exception] = PointHazardUpstreamError,
) -> tuple[list[dict[str, Any]], list[float] | None, str, Any]:
    """Resolve ``(layer dicts, case bbox, case title, case doc)`` for ``case_id``.

    Layers come from the Case doc's persisted ``loaded_layer_summaries``
    (``ProjectLayerSummary`` dicts) via ``telemetry.get_persistence`` -- the
    exact seam ``export_case_to_qgis._layers_from_case`` uses (and the same
    monkeypatch point for tests). Shared with ``extract_timeseries_at_point``
    and ``compose_case_report``.
    """
    from ..telemetry import get_persistence

    try:
        persistence = get_persistence()
    except Exception:  # noqa: BLE001
        persistence = None
    if persistence is None:
        raise not_found_cls(
            f"cannot look up case {case_id!r}: the persistence backend is not "
            "available from this process."
        )
    case = await persistence.get_case(case_id)
    if case is None:
        raise not_found_cls(f"case {case_id!r} not found.")
    layers = [dict(entry) for entry in (case.loaded_layer_summaries or [])]
    bbox = list(case.bbox) if getattr(case, "bbox", None) else None
    return layers, bbox, getattr(case, "title", None) or case_id, case


# ---------------------------------------------------------------------------
# Raster staging + point sampling.
# ---------------------------------------------------------------------------


def stage_layer_local(uri: str, tmpdir: str, label: str) -> str:
    """Materialize a layer uri locally (s3:// via boto3, else a local path).

    TiTiler display tile templates are unwrapped to the underlying COG first
    (the export_case_to_qgis convention). Raises on failure -- callers convert
    to a per-layer honest entry.
    """
    from .export_case_to_qgis import _strip_query, _unwrap_tile_template

    resolved = _unwrap_tile_template(uri)
    if resolved.startswith("s3://"):
        from .cache import read_object_bytes_s3

        name = resolved.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        with open(local, "wb") as f:
            f.write(read_object_bytes_s3(resolved))
        return local
    if resolved.startswith(("gs://", "http://", "https://")):
        raise ValueError(
            f"layer uri scheme not supported for point sampling: {resolved!r}"
        )
    probe = _strip_query(resolved)
    if not os.path.exists(probe):
        raise FileNotFoundError(f"layer uri is not a readable local file: {uri!r}")
    return probe


def sample_raster_at_point(
    local_path: str, lon: float, lat: float
) -> tuple[float | None, str | None, str | None]:
    """Sample band 1 of ``local_path`` at an EPSG:4326 point.

    Returns ``(value, note, units)``: ``value=None`` with an explicit note
    when the point is outside the raster extent or lands on nodata. Raises on
    open/read failure (callers convert to a per-layer honest entry).
    """
    import rasterio
    from rasterio.warp import transform as warp_transform
    from rasterio.windows import Window

    with rasterio.open(local_path) as src:
        units = (
            src.tags().get("units")
            or (src.units[0] if src.units and src.units[0] else None)
        )
        x, y = lon, lat
        if src.crs is not None and str(src.crs).upper() != "EPSG:4326":
            xs, ys = warp_transform("EPSG:4326", src.crs, [lon], [lat])
            x, y = float(xs[0]), float(ys[0])
        row, col = src.index(x, y)
        if not (0 <= row < src.height and 0 <= col < src.width):
            return None, "point outside the layer extent", units
        value = float(
            src.read(1, window=Window(col, row, 1, 1)).astype("float64")[0, 0]
        )
        nodata = src.nodata
    if not math.isfinite(value) or (
        nodata is not None
        and math.isfinite(float(nodata))
        and value == float(nodata)
    ):
        return None, "nodata at this point", units
    return value, None, units


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Reads case state + layer artifacts; may geocode via Nominatim.
    read_only_hint=True,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
)
async def query_point_hazard(
    lon: float | None = None,
    lat: float | None = None,
    place: str | None = None,
    case_id: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Sample every raster layer in the current Case at one point.

    **What it does:** Resolves a location (an explicit ``lon``/``lat`` pair,
    or a free-text ``place`` geocoded through the same Nominatim machinery as
    ``geocode_location``), then samples EVERY raster layer loaded on the Case
    at that point and returns the per-layer values with units when known.

    **When to use:**
    - "What's the flood depth at my house / at 123 Main St / at this point?"
    - "What do all the layers say at Fort Myers pier?" -- one call reads the
      whole loaded stack at a point instead of N zonal calls.

    **When NOT to use:**
    - Area statistics (use ``compute_zonal_statistics`` /
      ``summarize_layer_statistics``).
    - A time series over animation frames at a point -- use
      ``extract_timeseries_at_point``.
    - Vector layers (building footprints, boundaries): they are listed as
      skipped here; use vector query tools instead.

    **Parameters:**
    - ``lon`` / ``lat``: explicit EPSG:4326 coordinates (both required when
      used; wins over ``place``).
    - ``place``: free-text place name to geocode (e.g. "Mexico Beach, FL").
    - ``case_id``: the Case whose layers to sample. Default: the Case bound
      to the current turn.

    **Returns:** dict with ``location`` ({lon, lat, label}), ``case_id``,
    ``case_title``, ``results`` (one entry per raster layer:
    ``{layer_id, name, value, units, note?, error?}`` -- ``value=None``
    with a ``note`` for outside-extent/nodata, ``value=None`` with an
    ``error`` for an unreadable layer), ``skipped_vector_layers`` (names),
    ``sampled_count``, ``computed_at``. Per-layer failures are honest
    entries, never fabricated values.

    **Errors (FR-AS-11):** ``PointHazardInputError`` (no/invalid location or
    geocode miss), ``NoCaseBoundError`` (no case_id and no turn Case),
    ``NoCaseLayersError`` (the Case has no layers, or none of them is a
    raster), ``PointHazardUpstreamError`` (persistence unavailable / case
    not found).
    """
    q_lon, q_lat, label = resolve_point(lon, lat, place)
    resolved_case = resolve_case_id(case_id)
    layers, _case_bbox, case_title, _case = await layers_from_case(resolved_case)

    if not layers:
        raise NoCaseLayersError(
            f"case {resolved_case!r} has no loaded layers -- nothing to sample. "
            "Load or compute a layer first."
        )

    raster_layers = [l for l in layers if l.get("layer_type") == "raster"]
    skipped_vectors = [
        str(l.get("name") or l.get("layer_id") or "?")
        for l in layers
        if l.get("layer_type") != "raster"
    ]
    if not raster_layers:
        raise NoCaseLayersError(
            f"case {resolved_case!r} has {len(layers)} layer(s) but none is a "
            "raster -- point sampling needs a raster layer "
            f"(vector layers present: {', '.join(skipped_vectors) or 'none'})."
        )

    results: list[dict[str, Any]] = []
    sampled = 0
    with tempfile.TemporaryDirectory(prefix="grace2_point_hazard_") as tmpdir:
        for idx, layer in enumerate(raster_layers):
            name = str(layer.get("name") or layer.get("layer_id") or f"layer_{idx + 1}")
            entry: dict[str, Any] = {
                "layer_id": str(layer.get("layer_id") or ""),
                "name": name,
                "value": None,
                "units": layer.get("units"),
            }
            uri = str(layer.get("uri") or "")
            try:
                if not uri:
                    raise ValueError("layer has no uri")
                local = stage_layer_local(uri, tmpdir, f"layer{idx}")
                value, note, tag_units = sample_raster_at_point(local, q_lon, q_lat)
                entry["value"] = value
                if entry["units"] is None and tag_units:
                    entry["units"] = tag_units
                if note:
                    entry["note"] = note
                if value is not None:
                    sampled += 1
            except Exception as exc:  # noqa: BLE001 -- per-layer honest entry
                entry["error"] = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "query_point_hazard: layer %r unreadable at point: %s",
                    name,
                    exc,
                )
            results.append(entry)

    logger.info(
        "query_point_hazard: case=%s point=(%.5f, %.5f) rasters=%d sampled=%d",
        resolved_case,
        q_lon,
        q_lat,
        len(raster_layers),
        sampled,
    )
    return {
        "location": {"lon": q_lon, "lat": q_lat, "label": label},
        "case_id": resolved_case,
        "case_title": case_title,
        "results": results,
        "skipped_vector_layers": skipped_vectors,
        "sampled_count": sampled,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
