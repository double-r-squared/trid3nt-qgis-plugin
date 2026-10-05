"""The card a direct run opens: which inputs it lists, what each dropdown
offers, and a pick going through the input's own accept rule."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import pytest

from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload
from trid3nt_server.inputs.gate import pending
from trid3nt_server.inputs.gate.cards.run_inputs import (
    RunInputsDeclinedError, review_run_inputs)


def _tool(mesher: Literal["om2d", "reg_grid"] = "reg_grid", extent: Any = None,
          resolution_m: float | None = None, input_mode: str | None = None) -> None:
    """A tool with an enumerated input, a slot-named input and a typed one."""


class _Emitter:
    session_id = "sess-run-card"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_envelope(self, message_type: str, payload: Any) -> None:
        self.sent.append(payload)


@pytest.fixture
def layers(tmp_path) -> list[dict]:
    polygon = tmp_path / "harbour.geojson"
    polygon.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[-82.5, 42.9], [-82.4, 42.9],
                                                [-82.4, 43.0], [-82.5, 42.9]]]}}]}))
    junk = tmp_path / "depth.tif"
    junk.write_text("not a geometry")
    return [{"layer_id": "L-poly", "name": "harbour outline", "uri": str(polygon)},
            {"layer_id": "L-junk", "name": "depth raster", "uri": str(junk)}]


async def _answer(decision: str, revised: dict | None = None,
                  seen: set | None = None) -> None:
    seen = seen if seen is not None else set()
    for _ in range(2000):
        fresh = [(wid, fut) for wid, (_s, fut)
                 in pending._PENDING_CONFIRMATIONS.items()
                 if wid not in seen and not fut.done()]
        if fresh:
            wid, fut = fresh[0]
            seen.add(wid)
            fut.set_result(PayloadConfirmationEnvelopePayload(
                warning_id=wid, decision=decision, revised_args=revised))
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no card was opened")


def _rows(emitter: _Emitter, at: int = -1) -> dict:
    return {row.name: row for row in emitter.sent[at].param_sheet.rows}


async def _run(args: dict, layers: list[dict], *answers) -> tuple[_Emitter, Any]:
    emitter = _Emitter()
    seen: set = set()

    async def drive() -> None:
        for decision, revised in answers:
            await _answer(decision, revised, seen)

    task = asyncio.ensure_future(drive())
    try:
        launched = await asyncio.wait_for(review_run_inputs(
            "tool", _tool, args, layers=layers, emitter=emitter), 10)
    except Exception as exc:  # noqa: BLE001 - the test reads the refusal
        launched = exc
    await task
    return emitter, launched


def test_a_bare_run_opens_a_blank_card_listing_every_input(layers) -> None:
    emitter, launched = asyncio.run(_run({}, layers, ("proceed", None)))
    rows = _rows(emitter)
    assert list(rows) == ["mesher", "extent", "resolution_m"]
    assert all(row.value is None for row in rows.values())
    assert launched == {}


def test_a_partial_run_lists_only_the_missing(layers) -> None:
    emitter, launched = asyncio.run(_run({"mesher": "om2d"}, layers,
                                         ("proceed", None)))
    assert list(_rows(emitter)) == ["extent", "resolution_m"]
    assert launched == {"mesher": "om2d"}


def test_a_complete_run_opens_no_card(layers) -> None:
    emitter, launched = asyncio.run(_run(
        {"mesher": "om2d", "extent": "L-poly", "resolution_m": 50.0}, layers))
    assert emitter.sent == []
    assert launched["extent"] == layers[0]["uri"]


def test_a_dropdown_lists_exactly_what_the_accept_rule_takes(layers) -> None:
    emitter, _ = asyncio.run(_run({}, layers, ("proceed", None)))
    rows = _rows(emitter)
    offered = [(o.value, o.label) for o in rows["extent"].options]
    assert offered == [("L-poly", "harbour outline")]
    assert [o.value for o in rows["mesher"].options] == ["om2d", "reg_grid"]
    assert rows["resolution_m"].options is None


def test_a_pick_goes_through_the_accept_rule(layers) -> None:
    emitter, launched = asyncio.run(_run(
        {}, layers,
        ("narrow_scope", {"mesher": "tri", "extent": "L-poly",
                          "resolution_m": "25"}),
        ("narrow_scope", {"mesher": "om2d"}),
        ("proceed", None)))
    redrawn = _rows(emitter, 1)
    assert "mesher takes one of ['om2d', 'reg_grid']" in redrawn["mesher"].note
    assert redrawn["extent"].note is None
    assert launched == {"mesher": "om2d", "extent": layers[0]["uri"],
                        "resolution_m": 25.0}


def test_a_stated_value_the_rule_refuses_opens_the_card(layers) -> None:
    emitter, _ = asyncio.run(_run(
        {"mesher": "om2d", "extent": "L-junk", "resolution_m": 1.0,
         "input_mode": "auto"}, layers, ("cancel", None)))
    rows = _rows(emitter)
    assert list(rows) == ["extent"]
    assert "extent does not take 'depth raster'" in rows["extent"].note


def test_run_while_an_input_is_refused_launches_nothing_and_names_it(layers) -> None:
    emitter, launched = asyncio.run(_run(
        {"mesher": "om2d", "extent": "L-junk", "resolution_m": 1.0}, layers,
        ("proceed", None), ("cancel", None)))
    assert isinstance(launched, RunInputsDeclinedError)
    assert len(emitter.sent) == 2, "the Run click did not redraw the card"
    assert emitter.sent[-1].param_sheet.title.endswith(
        "Run waits on extent (refused)")


def test_run_after_the_refused_input_is_fixed_launches(layers) -> None:
    emitter, launched = asyncio.run(_run(
        {"mesher": "om2d", "extent": "L-junk", "resolution_m": 1.0}, layers,
        ("proceed", None), ("narrow_scope", {"extent": "L-poly"}),
        ("proceed", None)))
    assert launched["extent"] == layers[0]["uri"]
    assert "waits on" not in emitter.sent[-1].param_sheet.title


def _needs_extent(extent: Any, mesher: Literal["om2d", "reg_grid"] = "reg_grid"
                  ) -> None:
    """A tool whose extent has no default: a run without one is not ready."""


def test_run_with_a_required_input_missing_launches_nothing(layers) -> None:
    async def run() -> tuple[_Emitter, Any]:
        emitter, seen = _Emitter(), set()

        async def drive() -> None:
            for decision in ("proceed", "cancel"):
                await _answer(decision, None, seen)

        task = asyncio.ensure_future(drive())
        try:
            launched = await asyncio.wait_for(review_run_inputs(
                "tool", _needs_extent, {}, layers=layers, emitter=emitter), 10)
        except Exception as exc:  # noqa: BLE001 - the test reads the refusal
            launched = exc
        await task
        return emitter, launched

    emitter, launched = asyncio.run(run())
    assert isinstance(launched, RunInputsDeclinedError)
    assert emitter.sent[-1].param_sheet.title.endswith(
        "Run waits on extent (required)")


def test_cancel_on_the_card_launches_nothing(layers) -> None:
    _emitter, launched = asyncio.run(_run({}, layers, ("cancel", None)))
    assert isinstance(launched, RunInputsDeclinedError)


def _bang_run(monkeypatch, layers, *answers) -> tuple[list, list]:
    """``!run probe`` through the server's dispatch: the calls the tool saw and
    the frames the socket carried."""
    from trid3nt_contracts.common import new_ulid
    from trid3nt_contracts.tool_registry import AtomicToolMetadata
    from trid3nt_server.server import SessionState, _dispatch_tool_and_persist
    from trid3nt_server.server.dispatch.emitter import _ensure_emitter
    from trid3nt_server.tools import TOOL_REGISTRY, RegisteredTool

    calls: list = []

    def probe(mesher: Literal["om2d", "reg_grid"] = "reg_grid") -> dict:
        calls.append({"mesher": mesher})
        return {"mesher": mesher}

    monkeypatch.setitem(TOOL_REGISTRY, "run_card_probe", RegisteredTool(
        metadata=AtomicToolMetadata(name="run_card_probe",
                                    ttl_class="live-no-cache", cacheable=False),
        fn=probe, module=__name__))

    class _Socket:
        def __init__(self) -> None:
            self.sent: list = []

        async def send(self, text: str) -> None:
            self.sent.append(json.loads(text))

    async def drive() -> list:
        socket, state = _Socket(), SessionState(session_id=new_ulid())
        _ensure_emitter(socket, state)
        seen: set = set()

        async def answer() -> None:
            for decision, revised in answers:
                await _answer(decision, revised, seen)

        task = asyncio.ensure_future(answer())
        await asyncio.wait_for(_dispatch_tool_and_persist(
            socket, state, "run_card_probe", {}, "!run run_card_probe"), 10)
        await task
        return socket.sent

    return calls, asyncio.run(drive())


def test_a_bang_run_launches_only_through_the_card(monkeypatch, layers) -> None:
    calls, _sent = _bang_run(monkeypatch, layers,
                             ("narrow_scope", {"mesher": "om2d"}),
                             ("proceed", None))
    assert calls == [{"mesher": "om2d"}]
    calls, sent = _bang_run(monkeypatch, layers, ("cancel", None))
    assert calls == []
    assert [m["payload"]["error_code"] for m in sent
            if m.get("type") == "error"] == ["USER_INPUT_CANCELLED"]


def test_build_mesh_lists_the_registered_meshers() -> None:
    """The mesher dropdown is the mesher registry: every registered one, and a
    name it does not hold is refused by the same rule."""
    from trid3nt_server.inputs.accept import InputRefused, accept, offers
    from trid3nt_server.tools.mesh.meshers import registered_meshers
    from trid3nt_server.tools.mesh.tool import build_mesh

    fn = getattr(build_mesh, "__wrapped__", build_mesh)
    roster = registered_meshers()
    assert len(roster) >= 2
    assert [value for value, _label in offers(fn, "mesher", [])] == list(roster)
    with pytest.raises(InputRefused):
        accept(fn, "mesher", "tri")
