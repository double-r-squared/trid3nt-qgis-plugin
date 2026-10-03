"""The two session tools: each is a request on the plugin wire, answered by the
user's QGIS session, and the code one never runs without the approval card.

Offline: a fake emitter captures the ``processing-request`` envelope and a
scripted reply resolves it; no session, a wait that runs out and an error reply
are each a typed refusal, never a fabricated result."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from trid3nt_contracts import new_ulid
from trid3nt_contracts.processing_contracts import ProcessingResponsePayload

from trid3nt_server.render import pipeline_emitter as pe
from trid3nt_server.server import processing
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.run_pyqgis.run_pyqgis import (
    CodeExecConfirmationRequired,
    run_pyqgis,
)
from trid3nt_server.tools.derive.run_qgis_algorithm.run_qgis_algorithm import (
    run_qgis_algorithm,
)


class _FakeEmitter:
    def __init__(self) -> None:
        self.session_id = new_ulid()
        self.sent: list[tuple[str, object]] = []

    async def send_envelope(self, message_type: str, payload) -> None:
        self.sent.append((message_type, payload))


def _answer_when_asked(emitter: _FakeEmitter, **fields):
    async def _reply() -> None:
        for _ in range(200):
            if emitter.sent:
                break
            await asyncio.sleep(0.005)
        _kind, payload = emitter.sent[-1]
        processing._resolve_pending_processing(
            emitter.session_id,
            ProcessingResponsePayload(request_id=payload.request_id, **fields),
        )
    return asyncio.create_task(_reply())


def test_registered_with_no_cache_and_no_world() -> None:
    for name, fn in (("run_qgis_algorithm", run_qgis_algorithm), ("run_pyqgis", run_pyqgis)):
        entry = TOOL_REGISTRY[name]
        assert entry.fn is fn
        assert entry.metadata.cacheable is False
        assert entry.metadata.ttl_class == "live-no-cache"
        assert entry.metadata.open_world_hint is False
        assert entry.metadata.read_only_hint is False


def test_each_carries_a_corpus_and_no_algorithm_catalog() -> None:
    for name in ("run_qgis_algorithm", "run_pyqgis"):
        folder = Path(TOOL_REGISTRY[name].fn.__code__.co_filename).parent
        corpus = yaml.safe_load((folder / "corpus.yaml").read_text())
        assert len(corpus[name]) >= 5
        assert not (folder / "algorithms.yaml").exists()


def test_run_pyqgis_refuses_without_the_card() -> None:
    with pytest.raises(CodeExecConfirmationRequired) as exc:
        asyncio.run(run_pyqgis("result = 2 + 2"))
    assert exc.value.error_code == "CODE_EXEC_CONFIRMATION_REQUIRED"
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_algorithm_request_rides_the_wire_and_returns_the_summary(monkeypatch) -> None:
    emitter = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: emitter)
    reply = _answer_when_asked(
        emitter, status="ok",
        result={"layer_name": "Slope", "kind": "raster", "band_count": 1},
    )
    out = await run_qgis_algorithm("native:slope", {"INPUT": "DEM", "Z_FACTOR": 1.0})
    await reply
    kind, payload = emitter.sent[0]
    assert kind == "processing-request"
    assert payload.kind == "algorithm" and payload.algorithm == "native:slope"
    assert payload.params == {"INPUT": "DEM", "Z_FACTOR": 1.0} and payload.code is None
    assert out == {"status": "ok", "algorithm": "native:slope", "layer_name": "Slope",
                   "kind": "raster", "band_count": 1}
    assert not processing._PENDING_PROCESSING


@pytest.mark.asyncio
async def test_confirmed_code_request_rides_the_wire(monkeypatch) -> None:
    emitter = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: emitter)
    reply = _answer_when_asked(emitter, status="ok", result={"value": 4}, stdout="hi\n")
    cx = new_ulid()
    out = await run_pyqgis("result = 2 + 2", confirmed=True, code_exec_id=cx)
    await reply
    _kind, payload = emitter.sent[0]
    assert payload.kind == "code" and payload.code == "result = 2 + 2"
    assert payload.code_exec_id == cx
    assert out == {"status": "ok", "result": 4, "stdout": "hi\n", "code_exec_id": cx}


@pytest.mark.asyncio
async def test_an_error_reply_is_the_sessions_own_message(monkeypatch) -> None:
    emitter = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: emitter)
    reply = _answer_when_asked(emitter, status="error", error="NameError: name 'x' is not defined")
    with pytest.raises(processing.SessionProcessingFailedError, match="NameError"):
        await run_pyqgis("result = x", confirmed=True)
    await reply
    assert not processing._PENDING_PROCESSING


@pytest.mark.asyncio
async def test_no_session_is_a_typed_refusal(monkeypatch) -> None:
    monkeypatch.setattr(pe, "current_emitter", lambda: None)
    with pytest.raises(processing.SessionUnavailableError) as exc:
        await run_qgis_algorithm("native:slope", {})
    assert exc.value.retryable is False


@pytest.mark.asyncio
async def test_a_wait_that_runs_out_is_typed_and_cleans_up(monkeypatch) -> None:
    emitter = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: emitter)
    monkeypatch.setenv("TRID3NT_SESSION_PROCESSING_TIMEOUT_S", "0.05")
    with pytest.raises(processing.SessionProcessingTimeoutError):
        await run_qgis_algorithm("native:slope", {})
    assert not processing._PENDING_PROCESSING


def test_a_cross_session_reply_is_refused() -> None:
    fut = asyncio.new_event_loop().create_future()
    rid = new_ulid()
    processing._register_pending_processing("owner", rid, fut)
    try:
        assert not processing._resolve_pending_processing(
            "intruder", ProcessingResponsePayload(request_id=rid, status="ok"))
        assert not fut.done()
        assert processing._resolve_pending_processing(
            "owner", ProcessingResponsePayload(request_id=rid, status="ok"))
        assert fut.done()
    finally:
        processing._pop_pending_processing(rid)


def test_bad_arguments_refuse_before_the_wire(monkeypatch) -> None:
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    with pytest.raises(processing.SessionProcessingFailedError):
        asyncio.run(run_qgis_algorithm("", {}))
    with pytest.raises(processing.SessionProcessingFailedError):
        asyncio.run(run_qgis_algorithm("native:slope", ["INPUT"]))  # type: ignore[arg-type]
    with pytest.raises(processing.SessionProcessingFailedError):
        asyncio.run(run_pyqgis("   ", confirmed=True))


_WITHIN = {"type": "Polygon", "coordinates": [[
    [-82.5, 42.99], [-82.49, 42.99], [-82.49, 43.0], [-82.5, 43.0], [-82.5, 42.99]]]}


async def _a_qgis_output(monkeypatch, tmp_path, store, session: str):
    """One algorithm run over a fetched case layer, the QGIS side stubbed with
    the file it would have written -> (registry, the tool's return)."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    from trid3nt_server import storage
    from trid3nt_server.render.uri_registry import (
        activate_registry, deactivate_registry, get_uri_registry)

    values = np.full((10, 10), 2.0, dtype="float32")
    values[4:6, 4:6] = np.nan
    path = tmp_path / "OUTPUT.tif"
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=np.nan,
                       transform=from_origin(-82.5, 43.0, 0.001, 0.001)) as dst:
        dst.write(values, 1)
    monkeypatch.setattr(storage, "_CLIENT", store)
    registry = get_uri_registry(session)
    registry.register_tool_result("fetch_dem", {
        "layer_id": "dem-1", "uri": "s3://bucket/dem.tif",
        "vertical_datum": "NAVD88", "quantity": "elevation",
        "datum_offset_m": 176.0, "datum_offset_frame": "IGLD85"})
    emitter = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: emitter)
    reply = _answer_when_asked(emitter, status="ok", result={
        "layer_name": "Reprojected", "layer_id": "Reprojected_7f3a", "kind": "raster",
        "crs": "EPSG:4326", "extent": [-82.5, 42.99, -82.49, 43.0],
        "source": str(path)})
    token = activate_registry(registry)
    try:
        out = await run_qgis_algorithm(
            "native:reprojectlayer", {"INPUT": "dem-1", "TARGET_CRS": "EPSG:4326"})
    finally:
        deactivate_registry(token)
    await reply
    registry.register_tool_result("run_qgis_algorithm", out)
    return registry, out


@pytest.mark.asyncio
async def test_an_output_is_a_case_layer_carrying_what_its_input_carried(
        monkeypatch, tmp_path, fake_s3) -> None:
    """The reprojected surface is published the way a fetch output is - in the
    store, a LayerURI through the emission seam - and named by ITS id it brings
    the zero, the shift and the quantity of the layer it was computed from."""
    from trid3nt_contracts.execution import LayerURI

    from trid3nt_server.render.layer_uri_emit import emit_layer_uri
    from trid3nt_server.render.uri_registry import (
        activate_registry, deactivate_registry, lookup_layer_for_handle)

    registry, out = await _a_qgis_output(monkeypatch, tmp_path, fake_s3, "qgis-pub")
    token = activate_registry(registry)
    try:
        named = lookup_layer_for_handle(out.layer_id)
    finally:
        deactivate_registry(token)
    assert isinstance(out, LayerURI) and emit_layer_uri(out) is not None
    assert out.layer_id.startswith("qgis-") and out.layer_id != "Reprojected_7f3a"
    assert out.uri.startswith("s3://") and fake_s3.store[out.uri.split("/", 3)[3]]
    assert (out.name, out.layer_type, out.crs) == ("Reprojected", "raster", "EPSG:4326")
    assert named["uri"] == out.uri
    for layer in (out.model_dump(), named):
        assert layer["vertical_datum"] == "NAVD88" and layer["quantity"] == "elevation"
        assert (layer["datum_offset_m"], layer["datum_offset_frame"]) == (176.0, "IGLD85")


@pytest.mark.asyncio
async def test_a_second_derive_takes_a_qgis_output_by_its_id(
        monkeypatch, tmp_path, fake_s3) -> None:
    from trid3nt_server.render.uri_registry import activate_registry, deactivate_registry
    from trid3nt_server.tools.derive.fill_nodata.fill_nodata import fill_nodata

    registry, out = await _a_qgis_output(monkeypatch, tmp_path, fake_s3, "qgis-chain")
    token = activate_registry(registry)
    try:
        filled = await fill_nodata(layer=out.layer_id, within=_WITHIN, seed=1.0)
    finally:
        deactivate_registry(token)
    assert filled.name == "Reprojected (filled)"
    assert dict(filled.coverage)["filled"] > 0.0
    assert filled.vertical_datum == "NAVD88" and filled.quantity == "elevation"


@pytest.mark.asyncio
async def test_the_bed_slot_takes_a_qgis_output_by_its_id(
        monkeypatch, tmp_path, fake_s3) -> None:
    from trid3nt_server.inputs.bed import RASTER, bed
    from trid3nt_server.render.uri_registry import activate_registry, deactivate_registry

    registry, out = await _a_qgis_output(monkeypatch, tmp_path, fake_s3, "qgis-bed")
    token = activate_registry(registry)
    try:
        held = bed(out.layer_id, frame="NAVD88")
    finally:
        deactivate_registry(token)
    assert held.kind == RASTER and held.source["uri"] == out.uri


@pytest.mark.asyncio
async def test_an_output_no_file_names_is_refused_never_left_unpublished(
        monkeypatch) -> None:
    emitter = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: emitter)
    reply = _answer_when_asked(emitter, status="ok", result={
        "layer_name": "Buffered", "kind": "vector", "source": "memory?geometry=Polygon"})
    with pytest.raises(processing.SessionProcessingFailedError, match="cannot become a case layer"):
        await run_qgis_algorithm("native:buffer", {"INPUT": "roads", "DISTANCE": 50})
    await reply
