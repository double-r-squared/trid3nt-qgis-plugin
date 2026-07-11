"""Atomic tool ``import_user_layer`` -- bidirectional layer push (QGIS -> case).

Every existing layer seam flows agent -> QGIS (``publish_layer``,
``export_case_to_qgis``). This module is the REVERSE seam: the TRID3NT QGIS
plugin's user has an ACTIVE layer in their desktop project (vector or raster)
they want to bring INTO the current case as a first-class input layer.

Two entry points share ONE core (``ingest_user_layer``):

1. ``POST /api/ingest-layer`` on the catalog HTTP listener
   (``tool_catalog_http.py``) -- the plugin's "Push layer" button drives this
   directly, cold (no WS session required), mirroring the ``/api/export-qgis``
   + ``/api/case-list`` route conventions (local-single-user gated, typed
   errors -> honest 4xx bodies).
2. The LLM-visible tool ``import_user_layer`` -- so a conversational request
   ("use the file I uploaded as the AOI") can drive the SAME core once the
   file already lives in object storage.

Both entry points assume the artifact bytes are ALREADY in object storage at
``s3_uri`` (bucket = ``GRACE2_CACHE_BUCKET``, prefix ``user-uploads/<ulid>/
<filename>`` -- the plugin uploads there first via
``POST /api/ingest-layer-file`` since the QGIS Python runtime has no boto3).
This module never accepts raw bytes directly -- see ``upload_layer_file``
below for the staging upload half.

**Vector path:** the uploaded artifact (GeoJSON / FlatGeobuf / GeoPackage) is
read via geopandas/pyogrio, reprojected to EPSG:4326, and written out as a
FlatGeobuf -- the SAME durable-vector-data-face format every other vector
case layer uses (see ``publish_layer`` module docstring: "Vectors are
produced as FlatGeobuf"). The canonical FGB lands at
``s3://<runs_bucket>/case-data/<case_id>/<layer_id>.fgb`` (DATA face); the
existing ``publish_layer._write_durable_vector_geojson`` helper is reused
UNCHANGED to materialize the browser-readable GeoJSON DISPLAY face at the
SAME #165 Phase-0 key (``durable_vector_geojson_key``).

**Raster path:** the uploaded GeoTIFF is validated readable via rasterio,
then handed to the existing ``publish_layer`` atomic tool VERBATIM -- it
already owns COG-overview validation/auto-translate (F33,
``_ensure_raster_has_overviews``), style-preset resolution, and TiTiler tile
-template minting for an ``s3://`` raster. No COG logic is duplicated here.

**Persistence is the contract.** The ingested layer is merged into the
Case's durable ``loaded_layer_summaries`` (the SAME field
``_persist_case_loaded_layers`` writes, using the identical
append/replace-by-layer_id merge policy) so a Case reopen -- cold OR live --
always shows the pushed layer. A best-effort nudge additionally refreshes any
LIVE WebSocket session with this Case open (see ``_notify_live_sessions``):
it pushes the existing ``case-list`` envelope (the same side-channel every
other case mutation uses), NOT a fabricated ``session-state`` -- this cold
entry point has no live ``PipelineEmitter`` to source a truthful
``chat_history``/``pipeline_history`` from, and inventing one risks the
documented D1-class "chat blanks on case reopen" failure mode. The client
repaints the pushed layer on its next Case reopen/reconnect regardless.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from grace2_contracts import new_ulid, now_utc
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

logger = logging.getLogger("grace2_agent.tools.import_user_layer")

__all__ = [
    "ImportLayerError",
    "ImportLayerInputError",
    "CaseNotFoundError",
    "ObjectNotFoundError",
    "ObjectTooLargeError",
    "UnreadableLayerError",
    "MAX_INGEST_BYTES",
    "USER_UPLOAD_PREFIX",
    "ingest_user_layer",
    "upload_layer_file",
    "import_user_layer",
]

#: Size cap for a pushed layer (raw upload OR the object being ingested).
#: 200 MB comfortably covers a desktop-drawn AOI polygon or a modest DEM tile
#: while keeping a single HTTP round trip + in-memory read bounded.
MAX_INGEST_BYTES: int = 200 * 1024 * 1024

#: Staging prefix the plugin's raw-bytes upload lands under (bucket =
#: ``GRACE2_CACHE_BUCKET``). Content-addressed-cache TTL eviction rules do
#: NOT apply here (this is a plain object, not a ``cache/<ttl-class>/...``
#: key), but the artifact is still copied OUT to the durable runs bucket
#: before it becomes a case layer -- see the module docstring.
USER_UPLOAD_PREFIX = "user-uploads"

_VECTOR_KIND = "vector"
_RASTER_KIND = "raster"
_KINDS = (_VECTOR_KIND, _RASTER_KIND)


# --------------------------------------------------------------------------- #
# Typed errors (mirrors export_case_to_qgis.ExportCaseError shape)
# --------------------------------------------------------------------------- #


class ImportLayerError(RuntimeError):
    """Base typed error for layer ingestion. ``error_code`` is
    SCREAMING_SNAKE_CASE and surfaced in the function_response."""

    error_code: str = "IMPORT_LAYER_FAILED"

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class ImportLayerInputError(ImportLayerError):
    """Malformed request: missing/invalid case_id, name, kind, or s3_uri."""

    error_code = "INVALID_INPUT"


class CaseNotFoundError(ImportLayerError):
    """The target case does not exist (or Persistence is unbound)."""

    error_code = "CASE_NOT_FOUND"


class ObjectNotFoundError(ImportLayerError):
    """``s3_uri`` does not resolve to an existing object."""

    error_code = "OBJECT_NOT_FOUND"


class ObjectTooLargeError(ImportLayerError):
    """The object (or upload body) exceeds ``MAX_INGEST_BYTES``."""

    error_code = "OBJECT_TOO_LARGE"


class UnreadableLayerError(ImportLayerError):
    """The object exists and is within the size cap but is not a valid
    vector/raster artifact of the declared ``kind``."""

    error_code = "UNREADABLE_LAYER"


# --------------------------------------------------------------------------- #
# S3 helpers (boto3; honors AWS_ENDPOINT_URL so MinIO works -- same posture
# as every other s3:// read/write in this package, see cache.py / publish_layer.py)
# --------------------------------------------------------------------------- #


def _split_s3_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    return bucket, key


def _s3_client():
    import boto3

    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _head_object_size(s3_uri: str) -> int:
    """Return the object's byte size. Raises ``ObjectNotFoundError`` if it
    does not exist. SYNC (boto3); callers wrap in ``asyncio.to_thread``."""
    from botocore.exceptions import ClientError

    bucket, key = _split_s3_uri(s3_uri)
    s3 = _s3_client()
    try:
        resp = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise ObjectNotFoundError(f"no such object: {s3_uri}") from exc
        raise ImportLayerError(
            f"could not inspect {s3_uri}: {exc}", error_code="OBJECT_HEAD_FAILED"
        ) from exc
    return int(resp.get("ContentLength") or 0)


def _get_object_bytes(s3_uri: str) -> bytes:
    """Read an object fully into memory. Caller must have already validated
    it exists and is within ``MAX_INGEST_BYTES``. SYNC; wrap in
    ``asyncio.to_thread``."""
    bucket, key = _split_s3_uri(s3_uri)
    s3 = _s3_client()
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()


def _put_object_bytes(
    s3_uri: str, data: bytes, *, content_type: str = "application/octet-stream"
) -> None:
    bucket, key = _split_s3_uri(s3_uri)
    s3 = _s3_client()
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)


def _sanitize_filename(filename: str) -> str:
    """Strip any path components + control chars; keep the extension.

    A path-traversal-shaped filename (``../../etc/passwd``) is collapsed to
    its basename -- the object key is minted server-side under a fresh ULID
    prefix regardless, so this is defense-in-depth, not the sole guard.
    """
    base = os.path.basename((filename or "").strip().replace("\\", "/"))
    base = base.strip().strip(".") or "layer"
    # Keep it to a conservative safe charset; anything else becomes "_".
    import re as _re

    return _re.sub(r"[^A-Za-z0-9_.-]+", "_", base)[:200] or "layer"


def upload_layer_file(filename: str, data: bytes) -> str:
    """Stage raw bytes uploaded by the plugin to
    ``s3://<cache_bucket>/user-uploads/<ulid>/<filename>``.

    SYNC (boto3); the caller (the HTTP route) wraps this in
    ``asyncio.to_thread``. This is the server-side half of the QGIS plugin's
    upload -- the plugin has no boto3 (stdlib-only QGIS Python runtime), so it
    streams the exported file's bytes to the agent over plain HTTP and the
    agent does the actual object-store PUT.

    Raises ``ObjectTooLargeError`` when ``data`` exceeds ``MAX_INGEST_BYTES``.
    """
    if len(data) > MAX_INGEST_BYTES:
        raise ObjectTooLargeError(
            f"upload is {len(data)} bytes, exceeds the {MAX_INGEST_BYTES}-byte cap"
        )
    if not data:
        raise ImportLayerInputError("upload body is empty")
    bucket = os.environ.get("GRACE2_CACHE_BUCKET") or _default_cache_bucket()
    safe_name = _sanitize_filename(filename)
    key = f"{USER_UPLOAD_PREFIX}/{new_ulid()}/{safe_name}"
    s3_uri = f"s3://{bucket}/{key}"
    _put_object_bytes(s3_uri, data)
    logger.info(
        "import_user_layer: staged upload filename=%s bytes=%d -> %s",
        filename,
        len(data),
        s3_uri,
    )
    return s3_uri


def _default_cache_bucket() -> str:
    from .cache import CACHE_BUCKET

    return CACHE_BUCKET


# --------------------------------------------------------------------------- #
# Vector ingestion
# --------------------------------------------------------------------------- #

_VECTOR_READ_EXTS = (".geojson", ".json", ".fgb", ".gpkg", ".shp")


def _vector_ext(s3_uri: str) -> str:
    ext = Path(s3_uri.split("?")[0]).suffix.lower()
    return ext


def _read_uploaded_vector_to_gdf(raw_bytes: bytes, ext: str, crs_authid: str | None):
    """Bytes + extension -> a EPSG:4326 GeoDataFrame. Raises
    ``UnreadableLayerError`` on any parse failure. SYNC; wrap in
    ``asyncio.to_thread``."""
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover -- hard dep in prod
        raise ImportLayerError(
            f"geopandas not available: {exc}", error_code="DEPENDENCY_MISSING"
        ) from exc

    if ext not in _VECTOR_READ_EXTS:
        raise UnreadableLayerError(
            f"unsupported vector extension {ext!r}; expected one of "
            f"{_VECTOR_READ_EXTS}"
        )

    suffix = ext if ext else ".geojson"
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix, delete=False, prefix="grace2_ingest_"
        ) as f:
            f.write(raw_bytes)
            tmp_path = f.name
        gdf = gpd.read_file(tmp_path, engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise UnreadableLayerError(
            f"could not read uploaded vector as {ext}: {exc}"
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if gdf is None or len(gdf) == 0:
        raise UnreadableLayerError("uploaded vector has zero features")
    try:
        gdf = gdf[gdf.geometry.notna()]
    except Exception:  # noqa: BLE001 -- best-effort geometry filter
        pass
    if len(gdf) == 0:
        raise UnreadableLayerError("uploaded vector has no valid geometries")

    try:
        if gdf.crs is None:
            gdf = gdf.set_crs(crs_authid or "EPSG:4326")
        elif str(gdf.crs).upper() not in {"EPSG:4326", "WGS84", "WGS 84"}:
            gdf = gdf.to_crs("EPSG:4326")
    except Exception as exc:  # noqa: BLE001
        raise UnreadableLayerError(f"could not reproject to EPSG:4326: {exc}") from exc
    return gdf


def _write_fgb_bytes(gdf) -> bytes:
    """GeoDataFrame -> FlatGeobuf bytes (the DATA-face format every other
    vector case layer uses; matches ``compute_contours``/``clip_vector_to_polygon``
    etc's ``to_file(..., driver="FlatGeobuf", engine="pyogrio")`` convention)."""
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_ingest_out_"
        ) as f:
            tmp_path = f.name
        gdf.to_file(tmp_path, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def _ingest_vector(
    *,
    case_id: str,
    layer_id: str,
    name: str,
    s3_uri: str,
    raw_bytes: bytes,
    crs_authid: str | None,
) -> dict[str, Any]:
    import asyncio

    ext = _vector_ext(s3_uri)
    gdf = await asyncio.to_thread(
        _read_uploaded_vector_to_gdf, raw_bytes, ext, crs_authid
    )
    bounds = [float(v) for v in gdf.total_bounds]  # [minx, miny, maxx, maxy]
    feature_count = int(len(gdf))

    fgb_bytes = await asyncio.to_thread(_write_fgb_bytes, gdf)

    from .solver import _get_runs_bucket

    runs_bucket = _get_runs_bucket()
    fgb_key = f"case-data/{case_id}/{layer_id}.fgb"
    fgb_uri = f"s3://{runs_bucket}/{fgb_key}"
    await asyncio.to_thread(
        _put_object_bytes, fgb_uri, fgb_bytes, content_type="application/octet-stream"
    )

    # Reuse the #165 Phase-0 durable-vector-GeoJSON writer UNCHANGED -- it
    # re-reads the fgb we just wrote and materializes the browser-readable
    # DISPLAY face at the frozen key. Fail-open (None) is honored: the layer
    # still registers, just without a cold-view display asset (the live
    # inline-GeoJSON path still works while the agent box is awake).
    from .publish_layer import _write_durable_vector_geojson

    geojson_uri = await asyncio.to_thread(
        _write_durable_vector_geojson, fgb_uri, layer_id, case_id
    )

    summary = {
        "layer_id": layer_id,
        "name": name,
        "layer_type": "vector",
        "uri": fgb_uri,
        "wms_url": geojson_uri,
        "style_preset": "",
        "visible": True,
        "role": "input",
        "temporal": False,
    }
    return {
        "summary": summary,
        "bbox": bounds,
        "feature_count": feature_count,
        "display_uri": geojson_uri or fgb_uri,
    }


# --------------------------------------------------------------------------- #
# Raster ingestion
# --------------------------------------------------------------------------- #


def _validate_raster_and_bounds(
    raw_bytes: bytes, crs_authid: str | None
) -> tuple[float, float, float, float]:
    """Validate the bytes are a GDAL-readable raster and return its EPSG:4326
    bounds. Raises ``UnreadableLayerError`` on any failure. SYNC; wrap in
    ``asyncio.to_thread``."""
    try:
        import rasterio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover -- hard dep in prod
        raise ImportLayerError(
            f"rasterio not available: {exc}", error_code="DEPENDENCY_MISSING"
        ) from exc

    try:
        from rasterio.io import MemoryFile

        with MemoryFile(raw_bytes) as mem, mem.open() as ds:
            if ds.width <= 0 or ds.height <= 0 or ds.count < 1:
                raise UnreadableLayerError("raster has no readable bands/extent")
            b = ds.bounds
            crs = ds.crs
            if crs is None and crs_authid:
                crs = crs_authid
            if crs is not None and str(crs).upper() not in (
                "EPSG:4326",
                "WGS 84",
                "WGS84",
            ):
                from rasterio.warp import transform_bounds

                left, bottom, right, top = transform_bounds(
                    crs, "EPSG:4326", b.left, b.bottom, b.right, b.top
                )
            else:
                left, bottom, right, top = b.left, b.bottom, b.right, b.top
    except UnreadableLayerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UnreadableLayerError(f"not a readable GeoTIFF: {exc}") from exc
    return (float(left), float(bottom), float(right), float(top))


async def _ingest_raster(
    *,
    case_id: str,
    layer_id: str,
    name: str,
    s3_uri: str,
    raw_bytes: bytes,
    crs_authid: str | None,
) -> dict[str, Any]:
    import asyncio

    bounds = await asyncio.to_thread(_validate_raster_and_bounds, raw_bytes, crs_authid)

    # Reuse publish_layer VERBATIM -- it owns F33 COG-overview validation,
    # style resolution, and TiTiler tile-template minting for an s3:// raster.
    # It is a blocking (sync) call (boto3 + rasterio internally); run it off
    # the event loop.
    from .publish_layer import PublishLayerError, publish_layer

    try:
        tile_template = await asyncio.to_thread(
            publish_layer, layer_uri=s3_uri, layer_id=layer_id, name=name
        )
    except PublishLayerError as exc:
        raise ImportLayerError(
            f"could not publish uploaded raster: {exc}",
            error_code=getattr(exc, "error_code", "RASTER_PUBLISH_FAILED"),
        ) from exc

    from ..server import _resolve_publish_wrap_style_preset
    from .publish_layer import derive_readable_layer_name

    resolved_style = _resolve_publish_wrap_style_preset(
        style_preset=None, layer_uri=tile_template, layer_id=layer_id
    )
    layer_name = derive_readable_layer_name(name, layer_id, resolved_style, tile_template)

    summary = {
        "layer_id": layer_id,
        "name": layer_name,
        "layer_type": "raster",
        "uri": tile_template,
        "style_preset": resolved_style,
        "visible": True,
        "role": "input",
        "temporal": False,
    }
    return {
        "summary": summary,
        "bbox": list(bounds),
        "feature_count": None,
        "display_uri": tile_template,
    }


# --------------------------------------------------------------------------- #
# Persistence merge (mirrors server._persist_case_loaded_layers's
# append/replace-by-layer_id policy, without requiring a live SessionState /
# PipelineEmitter -- this entry point is COLD by design).
# --------------------------------------------------------------------------- #


async def _merge_layer_into_case(
    case_id: str, summary: dict[str, Any], *, bbox: list[float] | None, make_aoi: bool
) -> bool:
    """Persist ``summary`` onto the Case's ``loaded_layer_summaries`` +
    ``layer_summary`` (append, or replace-in-place on a ``layer_id``
    collision). When ``make_aoi``, also pins ``Case.bbox`` from ``bbox``
    (mirrors the F32 ``_pin_case_aoi_from_*`` write shape in ``server.py``:
    ``case.model_copy(update={...})`` + ``upsert_case``).

    Returns True. Raises ``CaseNotFoundError`` when the case does not exist
    or Persistence is unbound.
    """
    from ..server import get_persistence

    p = get_persistence()
    if p is None:
        raise CaseNotFoundError(
            "persistence unavailable -- cannot register the layer on a case"
        )
    case = await p.get_case(case_id)
    if case is None:
        raise CaseNotFoundError(f"case {case_id!r} not found")

    merged = [dict(d) for d in case.loaded_layer_summaries if isinstance(d, dict)]
    index_by_layer_id = {
        d.get("layer_id"): i for i, d in enumerate(merged) if d.get("layer_id")
    }
    pos = index_by_layer_id.get(summary["layer_id"])
    if pos is None:
        merged.append(summary)
    else:
        merged[pos] = summary
    layer_ids = [d.get("layer_id") for d in merged if isinstance(d.get("layer_id"), str)]

    update: dict[str, Any] = {
        "loaded_layer_summaries": merged,
        "layer_summary": layer_ids,
        "updated_at": now_utc(),
    }
    if make_aoi and bbox is not None and len(bbox) == 4:
        update["bbox"] = list(bbox)

    updated = case.model_copy(update=update)
    await p.upsert_case(updated)
    return True


async def _notify_live_sessions(case_id: str) -> None:
    """Best-effort: nudge any LIVE session with ``case_id`` open.

    Pushes the EXISTING ``case-list`` envelope (the same side-channel every
    other case-mutating flow uses, e.g. ``_emit_case_list`` after create /
    rename / archive / delete) rather than a fabricated ``session-state`` --
    this cold entry point has no live ``PipelineEmitter`` to source a
    truthful ``chat_history`` from. NEVER raises; a missing/unreachable
    session, an unbound Persistence, or any send failure is silently
    swallowed -- durable persistence (``_merge_layer_into_case``) is the real
    contract; this is a nice-to-have nudge on top of it.
    """
    try:
        from .. import server as _server
        from ..auth_handshake import LOCAL_SINGLE_USER_ID
        from grace2_contracts.case import CaseListEnvelopePayload

        session_ids = [
            sid
            for sid, cid in _server._SESSION_ACTIVE_CASE.items()
            if cid == case_id
        ]
        if not session_ids:
            return
        p = _server.get_persistence()
        if p is None:
            return
        cases = await p.list_cases_for_user(LOCAL_SINGLE_USER_ID)
        payload = CaseListEnvelopePayload(cases=cases)
        for sid in session_ids:
            sockets = list(_server._SESSION_WS_CONNECTIONS.get(sid, ()) or ())
            for ws in sockets:
                try:
                    await ws.send(_server._new_envelope("case-list", sid, payload))
                except Exception:  # noqa: BLE001 -- one dead socket must not
                    continue  # block notifying the rest
    except Exception:  # noqa: BLE001 -- best-effort, never break the ingest
        logger.debug("import_user_layer: live-session nudge skipped", exc_info=True)


# --------------------------------------------------------------------------- #
# Shared core
# --------------------------------------------------------------------------- #


async def ingest_user_layer(
    *,
    case_id: str,
    name: str,
    kind: str,
    s3_uri: str,
    crs_authid: str | None = None,
    make_aoi: bool = False,
) -> dict[str, Any]:
    """Validate + register an already-uploaded vector/raster as a Case input
    layer. The shared core behind both ``POST /api/ingest-layer`` and the
    ``import_user_layer`` LLM tool -- see the module docstring for the full
    contract.

    Raises: ``ImportLayerInputError`` (bad kind / empty case_id or s3_uri),
    ``ObjectNotFoundError``/``ObjectTooLargeError`` (the s3_uri fails the
    existence/size gate), ``UnreadableLayerError`` (exists + in-cap but not a
    valid artifact of ``kind``), ``CaseNotFoundError`` (no such case /
    Persistence unbound). Never a bare traceback -- every failure is one of
    the above typed subclasses of ``ImportLayerError``.
    """
    import asyncio

    if kind not in _KINDS:
        raise ImportLayerInputError(f"kind must be one of {_KINDS}, got {kind!r}")
    if not case_id or not case_id.strip():
        raise ImportLayerInputError("missing or empty `case_id`")
    if not s3_uri or not s3_uri.startswith("s3://"):
        raise ImportLayerInputError(f"`s3_uri` must be an s3:// object, got {s3_uri!r}")
    clean_name = (name or "").strip() or "Pushed layer"

    size = await asyncio.to_thread(_head_object_size, s3_uri)
    if size > MAX_INGEST_BYTES:
        raise ObjectTooLargeError(
            f"{s3_uri} is {size} bytes, exceeds the {MAX_INGEST_BYTES}-byte cap"
        )
    if size <= 0:
        raise ObjectNotFoundError(f"{s3_uri} is empty or unreadable")

    raw_bytes = await asyncio.to_thread(_get_object_bytes, s3_uri)

    layer_id = f"user-{new_ulid()}"
    if kind == _VECTOR_KIND:
        ingested = await _ingest_vector(
            case_id=case_id,
            layer_id=layer_id,
            name=clean_name,
            s3_uri=s3_uri,
            raw_bytes=raw_bytes,
            crs_authid=crs_authid,
        )
    else:
        ingested = await _ingest_raster(
            case_id=case_id,
            layer_id=layer_id,
            name=clean_name,
            s3_uri=s3_uri,
            raw_bytes=raw_bytes,
            crs_authid=crs_authid,
        )

    await _merge_layer_into_case(
        case_id, ingested["summary"], bbox=ingested["bbox"], make_aoi=make_aoi
    )
    await _notify_live_sessions(case_id)

    logger.info(
        "import_user_layer: ingested case=%s layer_id=%s kind=%s make_aoi=%s",
        case_id,
        layer_id,
        kind,
        make_aoi,
    )
    return {
        "status": "ok",
        "layer_id": layer_id,
        "name": ingested["summary"]["name"],
        "layer_type": kind,
        "uri": ingested["display_uri"],
        "bbox": ingested["bbox"],
        "aoi_pinned": bool(make_aoi and ingested["bbox"] is not None),
        "feature_count": ingested["feature_count"],
    }


# --------------------------------------------------------------------------- #
# LLM tool registration -- thin wrapper over the shared core (design point 2:
# "use the file I uploaded as the AOI" works conversationally once the file
# already lives in object storage).
# --------------------------------------------------------------------------- #

_IMPORT_USER_LAYER_METADATA = AtomicToolMetadata(
    name="import_user_layer",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


@register_tool(
    _IMPORT_USER_LAYER_METADATA,
    # Writes a new object to the runs bucket + mutates the Case document --
    # not read-only. Reaches object storage (open world). Not destructive
    # (never overwrites another layer's data; a same-layer_id collision is
    # never possible -- layer_id is minted fresh per call). Idempotent in the
    # sense that re-running with the SAME s3_uri produces an equivalent new
    # layer (not a no-op -- each call mints its own layer_id), so
    # idempotent_hint=False.
    read_only_hint=False,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=False,
)
async def import_user_layer(
    s3_uri: str,
    name: str,
    kind: str,
    case_id: str | None = None,
    make_aoi: bool = False,
    crs_authid: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Register an already-uploaded vector or raster artifact as a Case input
    layer (the QGIS plugin's "Push layer" reverse seam, also reachable
    conversationally).

    USE THIS when the user says something like "use the file I just
    uploaded/pushed from QGIS" or "make the layer I pushed the AOI" -- the
    artifact must ALREADY be in object storage (the QGIS plugin uploads it
    via its own HTTP route before this tool is ever reachable; there is no
    way to hand this tool raw bytes).

    Params:
        s3_uri: the ``s3://`` object holding the uploaded artifact.
        name: human-readable display name for the layer panel.
        kind: ``"vector"`` or ``"raster"``.
        case_id: the case to register onto. REQUIRED in practice -- when
            omitted the server-side dispatch wrapper substitutes the turn's
            active case (mirrors ``publish_layer``'s ``case_id`` transport
            convention); a genuinely case-less call raises CASE_NOT_FOUND.
        make_aoi: when True, also pins the Case's AOI (bounding box) to the
            pushed layer's extent.
        crs_authid: optional CRS hint (e.g. ``"EPSG:2263"``) used ONLY when
            the uploaded artifact carries no embedded CRS.

    Returns:
        {"status": "ok", "layer_id": str, "name": str,
         "layer_type": "vector"|"raster", "uri": str,
         "bbox": [minLon, minLat, maxLon, maxLat] | None,
         "aoi_pinned": bool, "feature_count": int | None}

    Raises:
        ImportLayerError subclasses (see ``ingest_user_layer``) -- every
        failure is typed and honest, never a bare traceback.
    """
    if not case_id:
        raise CaseNotFoundError(
            "no active case -- import_user_layer requires a case_id "
            "(open or create a case first)"
        )
    return await ingest_user_layer(
        case_id=case_id,
        name=name,
        kind=kind,
        s3_uri=s3_uri,
        crs_authid=crs_authid,
        make_aoi=bool(make_aoi),
    )
