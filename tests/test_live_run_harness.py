"""The live-run driver harness, offline: gate answering, evidence, assertions.

The harness is product code (drivers are), so its wire shapes and its refusals
are pinned here without a daemon. A fake socket replays a recorded turn; the
assertions the harness offers are exercised on the evidence it builds.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from trid3nt_server.testing.live_run import (
    GateAnswers,
    LiveRun,
    LiveRunError,
    RunEvidence,
    _answer_draw,
    _answer_warning,
    _check_declared_cards,
    _feature_collection,
    _pump,
    _read_run_products,
)


class _FakeWS:
    """Replays a scripted server turn and records what the client sent back."""

    def __init__(self, inbound: list[dict]) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        if not self._inbound:
            await asyncio.sleep(3600)  # nothing more is coming
        return json.dumps(self._inbound.pop(0))


def _env(**kw) -> RunEvidence:
    return RunEvidence(tool="t", args={}, session_id="S", **kw)


def _msg(kind: str, payload: dict) -> dict:
    return {"type": kind, "id": "1", "ts": "", "session_id": "S",
            "case_id": None, "payload": payload}


# --- the DRAW card ----------------------------------------------------------- #
def test_a_point_answer_rides_the_stock_pick_coordinates():
    ws, ev = _FakeWS([]), _env()
    asyncio.run(_answer_draw(ws, "S", _msg("spatial-input-request", {
        "request_id": "R", "mode": "point", "title": "Draw release"}),
        GateAnswers(draw=[-124.1, 40.5]), ev))
    reply = ws.sent[0]["payload"]
    assert reply["request_id"] == "R" and reply["cancelled"] is False
    assert reply["coordinates"] == [-124.1, 40.5] and reply["features"] is None
    assert ev.draw_card["answered_with"] == [-124.1, 40.5]
    assert ev.draw_card["declined"] is False


def test_a_shape_answer_rides_the_vertex_capture_feature_collection():
    ws, ev = _FakeWS([]), _env()
    ring = [[-124.1, 40.5], [-124.0, 40.5], [-124.0, 40.6]]
    asyncio.run(_answer_draw(ws, "S", _msg("spatial-input-request", {
        "request_id": "R", "mode": "vector_draw", "purpose": "aoi"}),
        GateAnswers(draw=ring, draw_geometry="polygon"), ev))
    reply = ws.sent[0]["payload"]
    assert reply["coordinates"] is None
    geom = reply["features"]["features"][0]["geometry"]
    assert geom["type"] == "Polygon" and geom["coordinates"][0] == ring


def test_a_declined_draw_cancels_rather_than_inventing_a_geometry():
    ws, ev = _FakeWS([]), _env()
    asyncio.run(_answer_draw(ws, "S", _msg("spatial-input-request",
                                           {"request_id": "R"}),
                             GateAnswers(draw=None), ev))
    assert ws.sent[0]["payload"]["cancelled"] is True
    assert ws.sent[0]["payload"]["coordinates"] is None
    assert ev.draw_card["declined"] is True


def test_a_polyline_answer_is_a_linestring():
    assert _feature_collection("polyline", [[0.0, 1.0], [2.0, 3.0]]
                               )["features"][0]["geometry"]["type"] == "LineString"


# --- the FORM card ----------------------------------------------------------- #
_SHEET = {"workflow": "w", "title": "Review", "rows": [
    {"name": "dye_concentration_mgl", "value": 100.0, "units": "mg/L",
     "door": "scenario", "basis": "default_demo", "source_badge": "labeled default",
     "bounds": [0.0, 1000000.0], "advanced": False, "note": ""},
    {"name": "sim_duration_s", "value": 3600.0, "units": "s", "door": "constant",
     "basis": "default_demo", "source_badge": "labeled default",
     "bounds": [60.0, 864000.0], "advanced": True, "note": ""},
]}


def test_an_edited_sheet_submits_as_a_revision_and_proceeds():
    """SUBMIT IS THE APPROVAL: the whole sheet was on screen, so an edited submit
    goes forward instead of re-presenting the table the user just filled in."""
    ws, ev = _FakeWS([]), _env()
    asyncio.run(_answer_warning(ws, "S", _msg("tool-payload-warning", {
        "warning_id": "W", "tool_name": "t", "param_sheet": _SHEET}),
        GateAnswers(form_edits={"dye_concentration_mgl": 250.0}), ev))
    reply = ws.sent[0]["payload"]
    assert reply["decision"] == "narrow_scope"
    assert reply["revised_args"] == {"dye_concentration_mgl": 250.0}
    assert [r["name"] for r in ev.form_card["rows"]] == [
        "dye_concentration_mgl", "sim_duration_s"]
    assert ev.form_card["rows"][1]["advanced"] is True   # the constants fold


def test_an_unedited_sheet_submits_as_a_plain_proceed():
    ws, ev = _FakeWS([]), _env()
    asyncio.run(_answer_warning(ws, "S", _msg("tool-payload-warning", {
        "warning_id": "W", "param_sheet": _SHEET}), GateAnswers(), ev))
    assert ws.sent[0]["payload"]["decision"] == "proceed"
    assert ws.sent[0]["payload"]["revised_args"] is None


def test_an_edit_the_card_does_not_carry_refuses_before_it_is_submitted():
    """A misspelled edit would be submitted, ignored by the re-seat, and then
    asserted about - a green run that changed nothing."""
    ws, ev = _FakeWS([]), _env()
    with pytest.raises(LiveRunError, match="which the card does not carry"):
        asyncio.run(_answer_warning(ws, "S", _msg("tool-payload-warning", {
            "warning_id": "W", "param_sheet": _SHEET}),
            GateAnswers(form_edits={"dye_concentration": 250.0}), ev))
    assert ws.sent == []


def test_a_sheetless_warning_takes_the_back_compatible_path():
    """A client that knows nothing about param sheets still answers the review."""
    ws, ev = _FakeWS([]), _env()
    asyncio.run(_answer_warning(ws, "S", _msg("tool-payload-warning", {
        "warning_id": "W", "tool_name": "telemac_do_sag"}), GateAnswers(), ev))
    assert ws.sent[0]["payload"]["decision"] == "proceed"
    assert ev.form_card is None and ev.plain_warnings == ["telemac_do_sag"]


# --- a declared card that never fired is a FAILURE, not a silent pass -------- #
def test_a_declared_card_that_never_fired_refuses():
    with pytest.raises(LiveRunError, match="no draw card fired"):
        _check_declared_cards(GateAnswers(draw=[0.0, 0.0], require_draw=True), _env())
    with pytest.raises(LiveRunError, match="no param sheet fired"):
        _check_declared_cards(
            GateAnswers(form_edits={"a": 1}, require_form=True), _env())
    _check_declared_cards(GateAnswers(), _env())  # nothing declared, nothing to miss


# --- the turn pump ----------------------------------------------------------- #
def test_the_pump_collects_status_layers_and_charts():
    layers = [{"name": "Peak dye", "layer_type": "raster",
               "uri": "s3://runs/RID/telemac_dye_peak.tif", "role": "primary"}]
    ws = _FakeWS([
        _msg("tool-payload-warning", {"warning_id": "W", "param_sheet": _SHEET}),
        _msg("chart-emission", {}),
        _msg("tool-io", {"function_response": json.dumps({"status": "ok"}),
                         "is_error": False}),
        _msg("session-state", {"loaded_layers": layers}),
        _msg("turn-complete", {}),
    ])
    ev = _env()
    run = LiveRun(tool="t", args={}, case_title="c", timeout_s=5,
                  answers=GateAnswers(form_edits={"dye_concentration_mgl": 250.0}))
    asyncio.run(_pump(ws, "S", run, ev))
    assert ev.turn_complete is True and ev.charts == 1
    assert ev.tool_status == "ok" and ev.is_error is False and ev.dispatched is True
    assert ev.layers == layers
    ev.require_ok()
    assert ev.require_layer(layer_type="raster")["name"] == "Peak dye"


def test_the_pump_reports_a_blocking_event_it_cannot_answer():
    ws = _FakeWS([_msg("clarification-request", {"question": "which river?"})])
    ev = _env()
    asyncio.run(_pump(ws, "S", LiveRun(tool="t", args={}, case_title="c",
                                       timeout_s=5), ev))
    assert "BLOCKED by clarification-request" in ev.detail
    assert ev.turn_complete is False


# --- the assertions ---------------------------------------------------------- #
def test_a_typed_result_carries_no_status_and_that_IS_success():
    """A tool returning a LayerURI has no ``status`` field; requiring the literal
    "ok" would only ever pass for tools that answer with a status dict."""
    _env(dispatched=True, tool_status=None, turn_complete=True).require_ok()
    _env(dispatched=True, tool_status="ok", turn_complete=True).require_ok()
    with pytest.raises(LiveRunError, match="never dispatched"):
        _env(tool_status=None).require_ok()


def test_a_turn_that_never_completed_is_not_an_ok_run():
    """A run stopped by an unanswered blocking event delivered a tool-io frame and
    then went nowhere; reading that as success is how a half-run passes."""
    ws = _FakeWS([
        _msg("tool-io", {"function_response": json.dumps({"status": "ok"}),
                         "is_error": False}),
        _msg("recovery-choice", {"question": "retry?"}),
    ])
    ev = _env()
    asyncio.run(_pump(ws, "S", LiveRun(tool="t", args={}, case_title="c",
                                       timeout_s=5), ev))
    assert ev.dispatched is True and ev.turn_complete is False
    with pytest.raises(LiveRunError, match="never completed"):
        ev.require_ok()


def test_the_assertions_refuse_a_run_that_did_not_deliver():
    with pytest.raises(LiveRunError, match="failed"):
        _env(dispatched=True, tool_status="error", detail="boom").require_ok()
    with pytest.raises(LiveRunError, match="failed"):
        _env(dispatched=True, is_error=True, detail="boom").require_ok()
    with pytest.raises(LiveRunError, match="no layer matching"):
        _env().require_layer(name_contains="release")
    with pytest.raises(LiveRunError, match="no run prefix"):
        _env().require_run_products()
    with pytest.raises(LiveRunError, match="absent from the run prefix"):
        _env(run_id="RID", metrics={"a": 1}).require_run_products()
    with pytest.raises(LiveRunError, match="no metrics.json"):
        _env().metric("dye_cmax_mgl")
    with pytest.raises(LiveRunError, match="has no"):
        _env(metrics={"a": 1}).metric("dye_cmax_mgl")


# --- locating the run's OWN prefix ------------------------------------------- #
_CONTEXT_RASTER = {"layer_type": "raster", "role": "context",
                   "uri": "s3://trid3nt-cache/soilgrids/sand_5_15.tif"}
_PRIMARY_RASTER = {"layer_type": "raster", "role": "primary",
                   "uri": "s3://trid3nt-runs/01RUNULID/budget.tif"}


class _FakeS3:
    """Serves the two run products off whatever prefix it is asked for."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803 - boto3 kwargs
        self.asked.append((Bucket, Key))
        body = json.dumps({"key": Key}).encode("utf-8")
        return {"Body": io.BytesIO(body)}


@pytest.fixture()
def fake_s3(monkeypatch) -> _FakeS3:
    import boto3

    s3 = _FakeS3()
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: s3)
    return s3


def test_the_run_prefix_comes_from_the_primary_raster(fake_s3):
    """Emit-on-fetch puts CONTEXT rasters on the canvas ahead of the result."""
    ev = _env()
    ev.layers = [_CONTEXT_RASTER, _PRIMARY_RASTER]
    _read_run_products(ev)
    assert ev.run_id == "01RUNULID"
    assert ev.product_uris["metrics"] == "s3://trid3nt-runs/01RUNULID/metrics.json"
    assert {b for b, _ in fake_s3.asked} == {"trid3nt-runs"}
    ev.require_run_products()


def test_context_rasters_alone_locate_no_run_prefix(fake_s3):
    """The cache bucket is not a run prefix; `run_id="cache"` was a fabrication."""
    ev = _env()
    ev.layers = [_CONTEXT_RASTER]
    _read_run_products(ev)
    assert ev.run_id is None and fake_s3.asked == []
    assert "no published PRIMARY raster" in ev.product_errors["run_id"]
    assert "1 context raster" in ev.product_errors["run_id"]
    with pytest.raises(LiveRunError, match="no run prefix"):
        ev.require_run_products()


def test_a_metric_comparison_is_relative_and_typed():
    ev = _env(metrics={"dye_cmax_mgl": 14.6854887})
    ev.require_metric_close("dye_cmax_mgl", 14.6854887)
    ev.require_metric_close("dye_cmax_mgl", 14.6855, rel=1e-4)
    with pytest.raises(LiveRunError, match="not within"):
        ev.require_metric_close("dye_cmax_mgl", 20.0, rel=1e-3)
