"""``query_point_hazard`` - sample every case raster at one point.

A Case with no raster layers is a typed error; a single unreadable layer, or a
point off an extent or on nodata, is None with a note, never zero-filled.
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "query_point_hazard",
    "PointHazardError",
    "PointHazardInputError",
    "NoCaseBoundError",
    "NoCaseLayersError",
    "PointHazardUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.derive.query_point_hazard.query_point_hazard")




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



_METADATA = AtomicToolMetadata(
    name="query_point_hazard",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)




def resolve_point(
    lon: Any, lat: Any, error_cls: type[Exception] = PointHazardInputError
) -> tuple[float, float, str]:
    """``(lon, lat, label)`` from an explicit pair; a missing or invalid location
    raises ``error_cls``. A place name is geocoded by the caller first."""
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

    raise error_cls(
        "provide a location as lon AND lat (geocode a place name first with "
        "geocode_location)."
    )




def resolve_case_id(case_id: Any, error_cls: type[Exception] = NoCaseBoundError) -> str:
    """``case_id`` param wins; else the turn's bound Case; else typed error."""
    if case_id is not None and str(case_id).strip():
        return str(case_id).strip()
    try:
        from trid3nt_server.emission.pipeline_emitter import current_turn_case

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
    """``(layer dicts, case bbox, case title, case doc)`` for ``case_id``, read
    from the Case doc's persisted ``loaded_layer_summaries``.
    """
    from trid3nt_server.telemetry import get_persistence

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




def stage_layer_local(uri: str, tmpdir: str, label: str) -> str:
    """Materialize an ``s3://`` or local layer uri to a local path; a failure
    raises, for the caller to record as a per-layer entry.
    """
    from trid3nt_server.tools._uri_util import _strip_query

    resolved = uri
    if resolved.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

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
    """``(value, note, units)`` for band 1 at an EPSG:4326 point; a point off the
    extent or on nodata is None with a note, and a read failure raises.
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




@register_tool(
    _METADATA,
    # Reads case state + layer artifacts; nothing external.
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def query_point_hazard(
    lon: float | None = None,
    lat: float | None = None,
    case_id: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Sample every raster layer in the current Case at one point.

    Use this when: "what's the flood depth at my house / 123 Main St /
    this point?" or "what do all the layers say at Fort Myers pier?" --
    one call reads the whole loaded raster stack at a point instead of N
    zonal calls. Takes ``lon``/``lat``; geocode a place name first. Do NOT
    use for: a time series over frames (``extract_timeseries_at_point``);
    vector layers (skipped here; use vector query tools).

    Params:
        lon/lat: explicit EPSG:4326 coords, both required.
        case_id: Case to sample; default the turn's bound Case.

    Returns the location, the case, and one result per raster layer carrying its
    value, units and any note. A per-layer failure is an honest entry, never a
    fabricated number; no layers at all is a typed refusal.
    """
    q_lon, q_lat, label = resolve_point(lon, lat)
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
    with tempfile.TemporaryDirectory(prefix="trid3nt_point_hazard_") as tmpdir:
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
