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

from trid3nt_server.emission import pipeline_emitter as pe
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
