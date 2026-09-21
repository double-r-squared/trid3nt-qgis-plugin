"""qgis-provider executor: a source the session's QGIS opens through its OWN data
provider, borrowed over the wire the way Processing already is.

The row states the provider, the datasource uri it takes and what to do with the
layer. ``mode: open`` leaves an overlay on the user's map and returns a record, so
nothing enters the store and no packet row is published; ``mode: materialise``
has the session export and upload the layer, and the bytes come back through the
read-through under the row-shaped cache key like any other fetch. A row may name
a ``credential``: the session attaches its own stored config to the uri, so a
keyed provider's key never reaches the daemon at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from trid3nt_contracts.ws import LayerRequestPayload, LayerResponsePayload
from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_input_error, router_upstream_error
from .vector_fgb import build_where, resolve_endpoints

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.qgis_provider"
)

__all__ = ["ACCESS", "build_uri", "row_key", "resolve_pending_layer", "execute"]

#: The ``ingest.access`` value that routes a row here.
ACCESS = "qgis_provider"

#: A WHERE that selects everything is the absence of one, not a clause to send.
_NO_WHERE = "1=1"

# key -> (owner_session_id, future). The key is the row's own, so a response can
# only answer the request that asked and a sibling connection of the same session
# may carry it; a cross-session answer is refused.
_PENDING_LAYER: dict[str, tuple[str, asyncio.Future]] = {}


def _block(spec: SourceSpec) -> dict[str, Any]:
    """The row's ``ingest.qgis_provider`` block: provider, uri, mode, and the
    optional credential name the session resolves in its own auth store."""
    block = (spec.ingest or {}).get("qgis_provider") or {}
    missing = [k for k in ("provider", "uri", "mode") if not block.get(k)]
    if missing:
        raise router_input_error(
            spec.error_code_prefix,
            f"a qgis_provider row states {', '.join(missing)} in its "
            "ingest.qgis_provider block",
            spec.input_error_suffix,
        )
    if block["mode"] not in ("open", "materialise"):
        raise router_input_error(
            spec.error_code_prefix,
            f"qgis_provider mode {block['mode']!r} is neither open nor materialise",
            spec.input_error_suffix,
        )
    if block["mode"] == "open" and spec.output.layer_type != "record":
        raise router_input_error(
            spec.error_code_prefix,
            "a qgis_provider row in mode open is context on the user's map, so it "
            "declares shape: record - a layer_type that publishes a packet row "
            "would claim an input nothing fetched",
            spec.input_error_suffix,
        )
    return dict(block)


def build_uri(spec: SourceSpec, params: dict[str, Any]) -> str:
    """The provider's datasource string for this call: the row's template over the
    resolved endpoint, the bbox and the asked params, with the row's declared WHERE
    appended as the provider's subset clause."""
    endpoint = resolve_endpoints(spec, params)[0]
    url = endpoint.url or endpoint.url_template or ""
    bbox = params.get("bbox")
    fields: dict[str, Any] = dict(params)
    fields["url"] = url.format(**params) if "{" in url else url
    fields["bbox"] = ",".join(str(v) for v in bbox) if bbox else ""
    try:
        uri = str(_block(spec)["uri"]).format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise router_input_error(
            spec.error_code_prefix,
            f"the qgis_provider uri template names {exc} which this call has no "
            "value for",
            spec.input_error_suffix,
        )
    where = build_where(spec, params)
    return f"{uri} sql={where}" if where and where != _NO_WHERE else uri


def row_key(spec: SourceSpec, params: dict[str, Any]) -> str:
    """The row-shaped cache key this call lands under: it names the uploaded object
    and correlates the request with its answer."""
    from ..router import synthesize_metadata
    from ..spec import record_shape
    from trid3nt_server.tools.cache import cache_key_for

    return cache_key_for(
        synthesize_metadata(spec), params, record_shape=record_shape(spec)
    )


def resolve_pending_layer(session_id: str, response: LayerResponsePayload) -> bool:
    """Complete the pending future for ``response.key``; False for an unknown,
    already-resolved or cross-session answer."""
    entry = _PENDING_LAYER.get(response.key)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "layer-response REFUSED: session=%s is not the owner (owner=%s) for "
            "key=%s", session_id, owner_session, response.key,
        )
        return False
    _PENDING_LAYER.pop(response.key, None)
    if fut.done():
        return False
    fut.set_result(response)
    return True


async def _ask(emitter: Any, payload: LayerRequestPayload, timeout_s: float):
    """Emit the request on the session and wait for its answer."""
    from trid3nt_server.server.processing import SessionProcessingTimeoutError

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _PENDING_LAYER[payload.key] = (emitter.session_id, fut)
    try:
        await emitter.send_envelope("layer-request", payload)
        logger.info(
            "layer-request emitted session=%s key=%s provider=%s mode=%s",
            emitter.session_id, payload.key, payload.provider, payload.mode,
        )
        return await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError:
        raise SessionProcessingTimeoutError(
            f"the QGIS session did not answer layer request {payload.key!r} within "
            f"{timeout_s:.0f}s; nothing was opened"
        ) from None
    finally:
        _PENDING_LAYER.pop(payload.key, None)


def ask_session_for_layer(payload: LayerRequestPayload) -> LayerResponsePayload:
    """Ask the bound session to open one provider layer and WAIT for its answer.

    The executor body is sync and off-loaded, so the coroutine is driven onto the
    emitter's bound loop from the worker thread; on the loop thread itself a
    blocking wait would deadlock, so that refuses instead of hanging."""
    from trid3nt_server.render.pipeline_emitter import current_emitter
    from trid3nt_server.server.processing import (
        SessionUnavailableError,
        _timeout_s,
    )

    emitter = current_emitter()
    loop = getattr(emitter, "_bound_loop", None) if emitter is not None else None
    if emitter is None or loop is None or not loop.is_running():
        raise SessionUnavailableError(
            f"{payload.name} is published through the QGIS {payload.provider} "
            "provider, so a live QGIS session opens it; no session is bound to this "
            "turn"
        )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise SessionUnavailableError(
            f"{payload.name} needs an answer from the QGIS session, which this tool "
            "cannot wait for on the event loop; it runs off-loop "
            "(TRID3NT_SYNC_TOOL_OFFLOAD)"
        )
    if payload.key in _PENDING_LAYER:
        raise SessionUnavailableError(
            f"a layer request for {payload.name} is already in flight on this "
            "session; the same row is asked once at a time"
        )
    timeout_s = _timeout_s()
    fut = asyncio.run_coroutine_threadsafe(_ask(emitter, payload, timeout_s), loop)
    return fut.result(timeout=timeout_s + 30)


def _store_bytes(uri: str) -> bytes:
    """The uploaded object's bytes, read back from the store the session staged
    them in."""
    from trid3nt_server import storage

    _scheme, bucket, key = storage.split_object_uri(uri)
    return storage.client().get_object(Bucket=bucket, Key=key)["Body"].read()


def _exported_to_output(spec: SourceSpec, params: dict[str, Any], raw: bytes) -> bytes:
    """The session's export in the row's own output format: a GeoPackage read back
    through the row's declared ingest transforms into FlatGeobuf, a GeoTIFF
    reserialized as the COG the publish seam reads."""
    if spec.output.layer_type == "raster":
        import rasterio

        from .raster_cog import array_to_cog_bytes

        with rasterio.MemoryFile(raw) as memfile:
            with memfile.open() as src:
                array = src.read(1)
                return array_to_cog_bytes(array, src.transform, src.crs)

    import os
    import tempfile

    import pyogrio

    from .vector_fgb import features_to_fgb_bytes

    fd, path = tempfile.mkstemp(suffix=".gpkg", prefix="trid3nt_layer_")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        frame = pyogrio.read_dataframe(path)
    finally:
        os.unlink(path)
    if frame.crs is not None and frame.crs.to_epsg() != 4326:
        frame = frame.to_crs("EPSG:4326")
    features = [
        {"type": "Feature", "geometry": geom, "properties": props}
        for geom, props in zip(
            (None if g is None else g.__geo_interface__ for g in frame.geometry),
            frame.drop(columns=[frame.geometry.name]).to_dict(orient="records"),
        )
    ]
    return features_to_fgb_bytes(features, spec, params)


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Ask the session for the provider layer (the ``fetch_fn`` body).

    Mode open returns the record of what was put on the map; mode materialise
    returns the exported bytes, which the read-through lands under the row-shaped
    cache key - so a later call is a cache hit that never asks QGIS again."""
    from trid3nt_server.workflows.runtime.journal import journal_note

    block = _block(spec)
    uri = build_uri(spec, params)
    payload = LayerRequestPayload(
        key=row_key(spec, params),
        provider=str(block["provider"]),
        uri=uri,
        name=spec.name.removeprefix("fetch_"),
        bbox=params["bbox"],
        mode=str(block["mode"]),
        credential=block.get("credential") or None,
    )
    answer = ask_session_for_layer(payload)
    if answer.error:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"the QGIS {payload.provider} provider could not open {payload.name}: "
            f"{answer.error}",
        )
    if payload.mode == "open":
        journal_note(
            f"{payload.name} opened on the map as a {payload.provider} overlay "
            f"({uri})"
        )
        return json.dumps(
            {
                "status": "ok",
                "provider": payload.provider,
                "uri": uri,
                "name": payload.name,
                "bbox": list(payload.bbox),
                "opened_in_session": True,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    if not answer.uri:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"the QGIS session answered {payload.name} with neither an uploaded "
            "object nor an error",
        )
    return _exported_to_output(spec, params, _store_bytes(answer.uri))
