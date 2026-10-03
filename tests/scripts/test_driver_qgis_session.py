"""A turn that borrows QGIS is answered on the driver's own session.

The QGIS side is a stub process speaking the headless session's answer protocol,
so the routing, the reply envelope and the case the before-steps land in are
pinned without PyQGIS or a daemon.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if not (REPO / "dev").is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)
sys.path.insert(0, str(REPO))

from dev.testing.live_run import (  # noqa: E402
    Before, LiveRun, QgisSession, RunEvidence, _pump, placed, run_before)

_STUB = '''
import json, sys
seen = []
for line in sys.stdin:
    env = json.loads(line)
    if env["type"] == "session-state":
        seen += [l["layer_id"] for l in env["payload"].get("loaded_layers") or []]
        print("null", flush=True)
    elif env["type"] == "processing-request":
        p = env["payload"]
        print(json.dumps({"request_id": p["request_id"], "status": "ok",
                          "result": {"layer_id": "qgis-" + p["params"]["INPUT"],
                                     "seen": seen},
                          "error": None, "stdout": ""}), flush=True)
    else:
        print(json.dumps({"key": env["payload"]["key"], "uri": "s3://b/k",
                          "error": None}), flush=True)
'''


class _WS:
    """Replays each invoke's scripted turn and records what the client sent."""

    def __init__(self, turns: dict[str, list[dict]]) -> None:
        self.turns, self.sent, self._queue = turns, [], []

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["type"] == "dev-tool-invoke":
            self._queue += self.turns[msg["payload"]["name"]]

    async def recv(self) -> str:
        if not self._queue:
            await asyncio.sleep(3600)
        return json.dumps(self._queue.pop(0))


def _msg(kind: str, payload: dict) -> dict:
    return {"type": kind, "id": "1", "ts": "", "session_id": "S",
            "case_id": "C", "payload": payload}


def _session(tmp_path: Path) -> QgisSession:
    stub = tmp_path / "stub_qgis.py"
    stub.write_text(_STUB)
    return QgisSession([sys.executable, str(stub)])


def _qgis_turn(request_id: str) -> list[dict]:
    return [_msg("processing-request", {"request_id": request_id,
                                        "kind": "algorithm",
                                        "algorithm": "native:reprojectlayer",
                                        "params": {"INPUT": "L-soundings"}}),
            _msg("tool-io", {"function_response": json.dumps(
                {"status": "ok", "layer_id": "qgis-L-soundings"})}),
            _msg("turn-complete", {})]


def test_a_processing_request_is_answered_on_the_driving_session(tmp_path):
    qgis = _session(tmp_path)
    ws = _WS({"run_qgis_algorithm": _qgis_turn("R1")})
    ev = RunEvidence(tool="run_qgis_algorithm", args={}, session_id="S",
                     case_id="C")
    run = LiveRun(tool="run_qgis_algorithm", args={}, case_title="t",
                  timeout_s=20)
    try:
        asyncio.run(ws.send(json.dumps({"type": "dev-tool-invoke", "payload": {
            "name": "run_qgis_algorithm"}})))
        asyncio.run(_pump(ws, "S", run, ev, qgis))
    finally:
        qgis.close()
    reply = next(m for m in ws.sent if m["type"] == "processing-response")
    assert reply["session_id"] == "S" and reply["case_id"] == "C"
    assert reply["payload"]["request_id"] == "R1"
    assert reply["payload"]["status"] == "ok"
    assert "BLOCKED" not in ev.detail and ev.turn_complete
    assert ev.session_answers == [{"request": "processing-request", "id": "R1",
                                   "status": "ok", "error": None}]


def test_a_run_that_borrows_nothing_never_starts_qgis(tmp_path):
    qgis = _session(tmp_path)
    ws = _WS({"fetch_dem": [
        _msg("session-state", {"loaded_layers": [{"layer_id": "L1"}]}),
        _msg("tool-io", {"function_response": "{}"}),
        _msg("turn-complete", {})]})
    ev = RunEvidence(tool="fetch_dem", args={}, session_id="S", case_id="C")
    asyncio.run(ws.send(json.dumps({"type": "dev-tool-invoke",
                                    "payload": {"name": "fetch_dem"}})))
    asyncio.run(_pump(ws, "S", LiveRun(tool="fetch_dem", args={},
                                       case_title="t", timeout_s=20), ev, qgis))
    assert qgis.proc is None and ev.turn_complete and not ev.session_answers


def test_without_a_qgis_session_the_request_still_blocks_by_name():
    ws = _WS({"run_qgis_algorithm": _qgis_turn("R1")})
    ev = RunEvidence(tool="run_qgis_algorithm", args={}, session_id="S")
    asyncio.run(ws.send(json.dumps({"type": "dev-tool-invoke", "payload": {
        "name": "run_qgis_algorithm"}})))
    asyncio.run(_pump(ws, "S", LiveRun(tool="run_qgis_algorithm", args={},
                                       case_title="t", timeout_s=20), ev))
    assert "BLOCKED by processing-request" in ev.detail


def test_the_before_steps_layer_lands_in_the_templates_case(tmp_path):
    qgis = _session(tmp_path)
    ws = _WS({
        "fetch_ehydro_surveys": [
            _msg("tool-io", {"function_response": "{}"}),
            _msg("session-state", {"loaded_layers": [
                {"layer_id": "L-soundings", "name": "soundings"}]}),
            _msg("turn-complete", {})],
        "run_qgis_algorithm": _qgis_turn("R2")})
    run = LiveRun(tool="telemac_ice_cover", args={"bed": {"layer": "$bed"}},
                  case_title="t", timeout_s=20, before=(
                      Before("soundings", "fetch_ehydro_surveys", {}),
                      Before("bed", "run_qgis_algorithm", {
                          "algorithm": "native:reprojectlayer",
                          "params": {"INPUT": "$soundings"}})))
    try:
        made = asyncio.run(run_before(ws, "S", run, "C", qgis))
    finally:
        qgis.close()
    assert made == {"soundings": "L-soundings", "bed": "qgis-L-soundings"}
    invokes = [m for m in ws.sent if m["type"] == "dev-tool-invoke"
               and "case_id" in m["payload"]]
    assert {m["payload"]["case_id"] for m in invokes} == {"C"}
    assert invokes[1]["payload"]["args"]["params"]["INPUT"] == "L-soundings"
    assert placed(run.args, made) == {"bed": {"layer": "qgis-L-soundings"}}


def test_every_ehydro_matched_canary_states_a_chain_that_binds():
    from dev.testing.canaries import CANARIES, SURVEYED_BEDS
    from dev.testing.live_run import _refuse_unread_args

    assert "telemac_ice_cover_port_huron" not in SURVEYED_BEDS
    for name in SURVEYED_BEDS:
        run = CANARIES[name]
        assert [s.tool for s in run.before] == [
            "fetch_ehydro_surveys", "run_qgis_algorithm", "run_qgis_algorithm"]
        _refuse_unread_args(run.tool, run.args)
        made: dict = {}
        for step in run.before:
            _refuse_unread_args(step.tool, step.args)
            placed(dict(step.args), made)
            made[step.name] = f"id-{step.name}"
        assert placed(run.args, made)["bed"] == {"layer": "id-bed"}
        cell = float(run.args["mesh_resolution_m"])
        assert f"-tr {cell:g} {cell:g}" in run.before[-1].args["params"]["EXTRA"]
