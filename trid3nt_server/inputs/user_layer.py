"""A user's OWN file as a layer: the one ingestion for bytes the user pushed in.

The bytes must ALREADY be in object storage when the layer is registered, and
``upload_layer_file`` is the half that puts them there. What lands is minted the
way a fetch result is - a ``LayerURI`` through the emission seam, registered on
the case as the same row a turn would have written - carrying origin ``user``.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI

logger = logging.getLogger("trid3nt_server.inputs.user_layer")

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
]

#: Size cap for a pushed layer (raw upload OR the object being ingested). 200 MB
#: comfortably covers a desktop-drawn AOI polygon or a modest DEM tile while
#: keeping a single HTTP round trip + in-memory read bounded.
MAX_INGEST_BYTES: int = 200 * 1024 * 1024

#: Staging prefix the plugin's raw-bytes upload lands under. Content-addressed
#: cache TTL eviction does NOT apply -- this is a plain object, not a
#: ``cache/<ttl-class>/...`` key -- but the artifact is copied OUT to the durable
#: runs bucket before it becomes a case layer.
USER_UPLOAD_PREFIX = "user-uploads"

_VECTOR_KIND = "vector"
_RASTER_KIND = "raster"
_KINDS = (_VECTOR_KIND, _RASTER_KIND)




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


# S3 helpers. boto3 honors AWS_ENDPOINT_URL, so MinIO works unchanged.


def _split_s3_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    return bucket, key


def _s3_client():
    """The ONE object-store client (bound or lazily built), never a second one."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    return _get_s3_client()


def _head_object_size(s3_uri: str) -> int:
    """Return the object's byte size. Raises ``ObjectNotFoundError`` if it does
    not exist. SYNC (boto3); callers wrap in ``asyncio.to_thread``."""
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
    """Read an object fully into memory. Caller must have already validated it
    exists and is within ``MAX_INGEST_BYTES``. SYNC; wrap in
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
    """Strip any path components and control chars; keep the extension.

    Defense in depth: the key is minted server-side under a fresh ULID anyway."""
    base = os.path.basename((filename or "").strip().replace("\\", "/"))
    base = base.strip().strip(".") or "layer"
    # Keep it to a conservative safe charset; anything else becomes "_".
    import re as _re

    return _re.sub(r"[^A-Za-z0-9_.-]+", "_", base)[:200] or "layer"


def upload_layer_file(filename: str, data: bytes) -> str:
    """Stage raw bytes uploaded by the plugin under the staging prefix.

    SYNC; the caller wraps it. Past ``MAX_INGEST_BYTES`` this raises."""
    # The server-side half of the plugin's upload: the QGIS Python runtime is
    # stdlib-only and has no boto3, so the plugin streams the exported file's
    # bytes over plain HTTP and the object-store PUT happens here.
    if len(data) > MAX_INGEST_BYTES:
        raise ObjectTooLargeError(
            f"upload is {len(data)} bytes, exceeds the {MAX_INGEST_BYTES}-byte cap"
        )
    if not data:
        raise ImportLayerInputError("upload body is empty")
    bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or _default_cache_bucket()
    safe_name = _sanitize_filename(filename)
    key = f"{USER_UPLOAD_PREFIX}/{new_ulid()}/{safe_name}"
    s3_uri = f"s3://{bucket}/{key}"
    _put_object_bytes(s3_uri, data)
    logger.info(
        "user_layer: staged upload filename=%s bytes=%d -> %s",
        filename,
        len(data),
        s3_uri,
    )
    return s3_uri


def _default_cache_bucket() -> str:
    from trid3nt_server.tools.cache import CACHE_BUCKET

    return CACHE_BUCKET



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
            suffix=suffix, delete=False, prefix="trid3nt_ingest_"
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
    """GeoDataFrame -> FlatGeobuf bytes, the format every vector case layer uses."""
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_ingest_out_"
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

    from trid3nt_server.workflows.solver.solver import _get_runs_bucket

    runs_bucket = _get_runs_bucket()
    fgb_key = f"case-data/{case_id}/{layer_id}.fgb"
    fgb_uri = f"s3://{runs_bucket}/{fgb_key}"
    await asyncio.to_thread(
        _put_object_bytes, fgb_uri, fgb_bytes, content_type="application/octet-stream"
    )

    layer = LayerURI(layer_id=layer_id, name=name, layer_type="vector",
                     uri=fgb_uri, role="input", origin="user")
    return {"layer": layer, "bbox": bounds, "feature_count": feature_count}




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

    # Reuse publish_layer VERBATIM -- it owns COG-overview enforcement, style
    # resolution and registration for an s3:// raster. It is a blocking (sync)
    # call (boto3 + rasterio internally); run it off the event loop.
    from trid3nt_server.emission.publish import PublishLayerError, publish_layer

    try:
        published_uri = await asyncio.to_thread(
            publish_layer, layer_uri=s3_uri, layer_id=layer_id, name=name
        )
    except PublishLayerError as exc:
        raise ImportLayerError(
            f"could not publish uploaded raster: {exc}",
            error_code=getattr(exc, "error_code", "RASTER_PUBLISH_FAILED"),
        ) from exc

    from trid3nt_server.emission.publish import derive_readable_layer_name

    # A user upload declares no quantity: the bytes are a raster of unknown
    # physical meaning, and its filename is not a measurement. It publishes on
    # the continuous kind's bare default - the field's own range under a single
    # ramp - never on a physical band inferred from what the file is called.
    layer_name = derive_readable_layer_name(name, layer_id, None, published_uri)

    layer = LayerURI(layer_id=layer_id, name=layer_name, layer_type="raster",
                     uri=published_uri, role="input", origin="user")
    return {"layer": layer, "bbox": list(bounds), "feature_count": None}


# Durable persistence is the CONTRACT of an ingest: the layer is registered on
# the Case itself, so a reopen - cold OR live - always shows the pushed file.
# This entry point is COLD by design and has no session or emitter of its own,
# which is why it registers through persistence rather than through a turn.


async def _register_on_case(
    case_id: str, layer: LayerURI, *, bbox: list[float] | None, make_aoi: bool
) -> None:
    """Mint ``layer`` as the Case's own row, pinning the AOI when asked.

    A missing case or unbound persistence raises ``CaseNotFoundError``."""
    from trid3nt_server.emission.layer_uri_emit import emit_layer_uri
    from trid3nt_server.emission.pipeline_emitter import summary_of
    from trid3nt_server.server import get_persistence

    p = get_persistence()
    if p is None:
        raise CaseNotFoundError(
            "persistence unavailable -- cannot register the layer on a case"
        )
    safe = emit_layer_uri(layer)
    if safe is None:
        raise UnreadableLayerError(
            f"{layer.uri} is not a uri the map can open; nothing was registered"
        )
    found = await p.merge_case_layers(
        case_id,
        [summary_of(safe).model_dump(mode="json")],
        bbox=bbox if (make_aoi and bbox) else None,
    )
    if not found:
        raise CaseNotFoundError(f"case {case_id!r} not found")


async def _require_case_exists(case_id: str) -> None:
    """Fail fast before any ingest work for a case that does not exist.

    A cheap early exit: the merge re-reads the case right before its own write."""
    from trid3nt_server.server import get_persistence

    p = get_persistence()
    if p is None:
        raise CaseNotFoundError(
            "persistence unavailable -- cannot register the layer on a case"
        )
    case = await p.get_case(case_id)
    if case is None:
        raise CaseNotFoundError(f"case {case_id!r} not found")


async def _notify_live_sessions(case_id: str) -> None:
    """Best-effort nudge to any LIVE session with ``case_id`` open.

    NEVER raises: durable persistence is the contract, and this only makes the
    repaint sooner than the next reopen would."""
    try:
        from trid3nt_server import server as _server
        from trid3nt_server.credentials.auth_handshake import LOCAL_SINGLE_USER_ID
        from trid3nt_contracts.case import CaseListEnvelopePayload

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
        # The EXISTING ``case-list`` envelope, the side-channel every other
        # case-mutating flow uses, and never a fabricated ``session-state``:
        # this cold entry point has no live emitter to source a truthful chat
        # history from, and inventing one blanks the chat on the next reopen.
        payload = CaseListEnvelopePayload(cases=cases)
        for sid in session_ids:
            sockets = list(_server._SESSION_WS_CONNECTIONS.get(sid, ()) or ())
            for ws in sockets:
                try:
                    await ws.send(_server._new_envelope("case-list", sid, payload))
                except Exception:  # noqa: BLE001 -- one dead socket must not
                    continue  # block notifying the rest
    except Exception:  # noqa: BLE001 -- best-effort, never break the ingest
        logger.debug("user_layer: live-session nudge skipped", exc_info=True)




async def ingest_user_layer(
    *,
    case_id: str,
    name: str,
    kind: str,
    s3_uri: str,
    crs_authid: str | None = None,
    make_aoi: bool = False,
) -> dict[str, Any]:
    """Validate and register an already-uploaded artifact as a Case input layer.

    Every failure is a typed ``ImportLayerError`` subclass, never a traceback."""
    import asyncio

    if kind not in _KINDS:
        raise ImportLayerInputError(f"kind must be one of {_KINDS}, got {kind!r}")
    if not case_id or not case_id.strip():
        raise ImportLayerInputError("missing or empty `case_id`")
    if not s3_uri or not s3_uri.startswith("s3://"):
        raise ImportLayerInputError(f"`s3_uri` must be an s3:// object, got {s3_uri!r}")
    clean_name = (name or "").strip() or "Pushed layer"

    # Fail fast on a doomed case BEFORE any S3 read, geopandas or rasterio
    # conversion, or publish work.
    await _require_case_exists(case_id.strip())

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

    layer: LayerURI = ingested["layer"]
    await _register_on_case(
        case_id, layer, bbox=ingested["bbox"], make_aoi=make_aoi
    )
    await _notify_live_sessions(case_id)

    logger.info(
        "user_layer: ingested case=%s layer_id=%s kind=%s make_aoi=%s",
        case_id,
        layer_id,
        kind,
        make_aoi,
    )
    return {
        "status": "ok",
        "layer_id": layer_id,
        "name": layer.name,
        "layer_type": kind,
        "uri": layer.uri,
        "bbox": ingested["bbox"],
        "aoi_pinned": bool(make_aoi and ingested["bbox"] is not None),
        "feature_count": ingested["feature_count"],
    }
