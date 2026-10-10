"""qgis-provider executor: a source the session's QGIS opens through its OWN data provider.

``mode: open`` leaves an overlay on the user's map and returns a record (nothing enters the
store); ``mode: materialise`` has the session export and upload the layer, read back under the
row-shaped cache key. A named ``credential`` is attached by the session, never reaching the daemon."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from trid3nt_contracts.ws import LayerRequestPayload, LayerResponsePayload
from trid3nt_contracts.source_spec import SourceSpec

from trid3nt_server.inputs.gate.pending import _PENDING_LAYER

from ..errors import router_empty_error, router_input_error, router_upstream_error
from .vector_fgb import build_where, resolve_endpoints

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.qgis_provider"
)

__all__ = ["ACCESS", "build_uri", "row_key", "execute"]

#: The ``ingest.access`` value that routes a row here.
ACCESS = "qgis_provider"

#: A WHERE that selects everything is the absence of one, not a clause to send.
_NO_WHERE = "1=1"



def _block(spec: SourceSpec) -> dict[str, Any]:
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
    """The provider's datasource string: the row's template over the endpoint, bbox and params, plus the declared WHERE."""
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
    """The row-shaped cache key this call lands under; it names the uploaded object."""
    from ..router import synthesize_metadata
    from ..spec import record_shape
    from trid3nt_server.tools.cache import cache_key_for

    return cache_key_for(
        synthesize_metadata(spec), params, record_shape=record_shape(spec)
    )


async def _ask(emitter: Any, payload: LayerRequestPayload, timeout_s: float):
    from trid3nt_server.server.processing import SessionProcessingTimeoutError

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _PENDING_LAYER.register(emitter.session_id, payload.key, fut)
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

    The body runs off-loop, so the coroutine is driven onto the emitter's bound loop; on the
    loop thread itself a blocking wait would deadlock, so that refuses."""
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
            "as every sync tool body does"
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
    from trid3nt_server.store import objects as storage

    _scheme, bucket, key = storage.split_object_uri(uri)
    return storage.client().get_object(Bucket=bucket, Key=key)["Body"].read()


def _within_pixel_budget(
    spec: SourceSpec, block: dict[str, Any], bbox: Any, resolution_m: Any
) -> None:
    """A service stating ``max_px`` serves at most that many pixels per axis; past it the row REFUSES, naming the spacing that fits."""
    from ..._fetch_common import enforce_pixel_budget

    budget = block.get("max_px")
    if budget is None or bbox is None or resolution_m is None:
        return
    enforce_pixel_budget(
        tuple(float(v) for v in bbox),
        float(resolution_m),
        budget_px=int(budget),
        source=spec.name,
    )


def _exported_raster_to_cog(spec: SourceSpec, raw: bytes) -> bytes:
    """The session's GeoTIFF as the COG the publish seam reads: values at or below ``nodata_sentinel``
    are no-data, ``serialize`` sets nodata and dtype, and an all-no-data export is no-coverage."""
    import numpy as np
    import rasterio

    from .raster_cog import array_to_cog_bytes

    ingest = spec.ingest or {}
    sentinel = (ingest.get("qgis_provider") or {}).get("nodata_sentinel")
    serialize = ingest.get("serialize") or {}
    nodata = serialize.get("nodata")
    with rasterio.MemoryFile(raw) as memfile:
        with memfile.open() as src:
            array = src.read(1)
            transform, crs = src.transform, src.crs
    if sentinel is not None:
        fill = float("nan") if nodata is None else float(nodata)
        array = np.where(array <= float(sentinel), fill, array)
        if not bool((array != fill).any()):
            raise router_empty_error(
                spec.error_code_prefix,
                "the exported window is entirely the dataset's own no-data "
                f"(at or below {sentinel}); no valid pixels",
                spec.empty_error_suffix,
            )
    if nodata is None:
        return array_to_cog_bytes(array, transform, crs)
    dtype = str(serialize.get("dtype", "float32"))
    return array_to_cog_bytes(
        np.where(np.isfinite(array), array, nodata).astype(dtype),
        transform,
        crs,
        nodata=float(nodata),
        dtype=dtype,
    )


def _exported_to_output(spec: SourceSpec, params: dict[str, Any], raw: bytes) -> bytes:
    if spec.output.layer_type == "raster":
        return _exported_raster_to_cog(spec, raw)

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
    """Ask the session for the provider layer (the ``fetch_fn`` body); ``open`` returns the
    on-map record, ``materialise`` the exported bytes landed under the row-shaped cache key."""
    from trid3nt_server.workflows.runtime.journal import journal_note

    block = _block(spec)
    uri = build_uri(spec, params)
    bbox = params.get("bbox")
    resolution_m = params.get("resolution_m")
    _within_pixel_budget(spec, block, bbox, resolution_m)
    payload = LayerRequestPayload(
        key=row_key(spec, params),
        provider=str(block["provider"]),
        uri=uri,
        name=spec.name.removeprefix("fetch_"),
        bbox=bbox,
        resolution_m=None if resolution_m is None else float(resolution_m),
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
                "bbox": None if payload.bbox is None else list(payload.bbox),
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
