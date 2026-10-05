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
         "input_mode": "auto"}, layers, ("proceed", None)))
    rows = _rows(emitter)
    assert list(rows) == ["extent"]
    assert "extent does not take 'depth raster'" in rows["extent"].note


def test_cancel_on_the_card_launches_nothing(layers) -> None:
    _emitter, launched = asyncio.run(_run({}, layers, ("cancel", None)))
    assert isinstance(launched, RunInputsDeclinedError)
