"""The FORM and DRAW cards, driven end to end by a scripted client.

Offline: no daemon, no network. The client is REAL though - it reads the envelope
the gate actually put on the sink (serialized through ``Envelope``, parsed back
from JSON) and replies with a contract-validated payload into the same pending
registry the WS loop resolves. So what is proven here is the wire round trip, not
a patched-out gate: the card goes out, an answer comes back, and the run uses it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.payload_warning import (
    ParamSheet,
    PayloadConfirmationEnvelopePayload,
    PayloadWarningEnvelopePayload,
)
from trid3nt_contracts.ws import (
    Envelope,
    SpatialInputRequestPayload,
    SpatialInputResponsePayload,
)
from trid3nt_server.declarative import (
    DrawGate,
    FormGate,
    Param,
    Step,
    Workflow,
    doors,
    interpret,
    resolve_params,
)
from trid3nt_server.emission import pipeline_emitter as pe
from trid3nt_server.gates import pending
from trid3nt_server.server import spatial as server_spatial

_HERE = "tests.test_declarative_cards"

pytestmark = pytest.mark.asyncio


# --- the runner the plans below name, and what it saw ----------------------- #
SEEN: dict = {}


def stub_solve(**kwargs):
    SEEN.clear()
    SEEN.update(kwargs)
    return {"ok": True}


@pytest.fixture(autouse=True)
def _clean():
    SEEN.clear()
    yield
    SEEN.clear()


# --- the scripted client ---------------------------------------------------- #
class _Client:
    """A test client on the emitter's sink: it reads envelopes off the wire.

    Envelopes arrive as the JSON the daemon would send, and are parsed back into
    their contract models - so a payload this client cannot read is a payload the
    plugin could not read either.
    """

    def __init__(self) -> None:
        self.session_id = new_ulid()
        self.envelopes: list[dict] = []

    def begin_substeps(self, total):
        return None

    @contextlib.asynccontextmanager
    async def substep(self, raw_name):
        yield "child"

    async def send_envelope(self, message_type: str, payload) -> None:
        wire = Envelope(type=message_type, session_id=self.session_id,
                        payload=payload).model_dump_json()
        self.envelopes.append(json.loads(wire))

    async def next_envelope(self, message_type: str, timeout: float = 5.0) -> dict:
        for _ in range(int(timeout / 0.005)):
            for env in self.envelopes:
                if env["type"] == message_type:
                    self.envelopes.remove(env)
                    return env
            await asyncio.sleep(0.005)
        raise AssertionError(f"no {message_type} envelope arrived")


@pytest.fixture
def client(monkeypatch):
    from trid3nt_server.declarative import interpreter as _interp

    c = _Client()
    monkeypatch.setattr(pe, "current_emitter", lambda: c)
    monkeypatch.setattr(_interp, "current_emitter", lambda: c)
    return c


def _params():
    return [
        Param("water_temp_c", desc="Water temperature", door=doors.SCENARIO,
              default=20.0, bounds=(0.0, 40.0), units="C"),
        Param("outfall", desc="Where the discharge enters the water",
              door=doors.USER, optional=False),
        Param("sim_seconds", desc="Simulated time", door=doors.CONSTANT,
              default=3600.0, bounds=(60.0, 86400.0), units="s"),
    ]


async def _drive_form(client, revised: dict | None, decision: str = "narrow_scope"):
    """Read the form card, answer it exactly as the plugin would."""
    env = await client.next_envelope("tool-payload-warning")
    warning = PayloadWarningEnvelopePayload.model_validate(env["payload"])
    reply = PayloadConfirmationEnvelopePayload(
        warning_id=warning.warning_id, decision=decision, revised_args=revised)
    assert pending._resolve_pending_confirmation(client.session_id, reply)
    return warning


async def _drive_draw(client, response_kwargs: dict):
    """Read the draw card, answer it exactly as the plugin would."""
    env = await client.next_envelope("spatial-input-request")
    request = SpatialInputRequestPayload.model_validate(env["payload"])
    reply = SpatialInputResponsePayload(request_id=request.request_id,
                                        **response_kwargs)
    assert server_spatial._resolve_pending_spatial_input(client.session_id, reply)
    return request


# --------------------------------------------------------------------------- #
# The FORM card
# --------------------------------------------------------------------------- #
async def test_the_form_card_carries_the_declared_sheet(client):
    decl = _params()
    p = await resolve_params(decl, {"outfall": [-124.1, 40.5]})
    plan = Workflow("form_w")[
        FormGate(title="Review the DO-sag inputs"),
        Step(runner=f"{_HERE}.stub_solve", kwargs={"t": p.water_temp_c},
             consequential=True).named("solve"),
    ]
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    warning = await _drive_form(client, {"water_temp_c": 24.0})
    await task

    sheet: ParamSheet = warning.param_sheet
    assert sheet is not None and sheet.workflow == "form_w"
    assert sheet.title == "Review the DO-sag inputs"
    rows = {r.name: r for r in sheet.rows}
    # Question-bearing first, the constant folded away at the end.
    assert [r.name for r in sheet.rows][0] == "outfall"
    assert sheet.rows[-1].name == "sim_seconds" and sheet.rows[-1].advanced
    temp = rows["water_temp_c"]
    assert temp.units == "C" and temp.bounds == (0.0, 40.0)
    assert temp.desc == "Water temperature"
    assert temp.source_badge == "labeled default"
    assert temp.editable and not temp.advanced
    assert rows["outfall"].source_badge == "you supplied this"


async def test_an_edit_submitted_at_the_form_card_is_what_runs(client):
    """The round trip that matters: the card goes out, an edit comes back, and the
    step downstream of the gate reads the edited value - not the resolved one."""
    decl = _params()
    p = await resolve_params(decl, {"outfall": [-124.1, 40.5]})
    plan = Workflow("form_edit")[
        FormGate(),
        Step(runner=f"{_HERE}.stub_solve", kwargs={"t": p.water_temp_c},
             consequential=True).named("solve"),
    ]
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_form(client, {"water_temp_c": 24.0})
    out = await task

    assert SEEN["t"] == 24.0
    row = next(e for e in out.entries if e.param == "water_temp_c")
    assert row.value == 24.0 and row.basis == "user"
    assert "revised at input review" in (row.note or "")


async def test_a_submitted_edit_still_obeys_the_declared_bounds(client):
    decl = _params()
    p = await resolve_params(decl, {"outfall": [-124.1, 40.5]})
    plan = Workflow("form_clamp")[
        FormGate(),
        Step(runner=f"{_HERE}.stub_solve", kwargs={"t": p.water_temp_c},
             consequential=True).named("solve"),
    ]
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_form(client, {"water_temp_c": 900.0})
    out = await task

    assert SEEN["t"] == 40.0
    assert "CLAMPED" in (next(e for e in out.entries
                              if e.param == "water_temp_c").note or "")


async def test_submitting_the_form_proceeds_rather_than_re_presenting(client):
    """The whole sheet was on screen, so the submit IS the approval. A second card
    would ask the user to confirm the table they just filled in."""
    decl = _params()
    p = await resolve_params(decl, {"outfall": [-124.1, 40.5]})
    plan = Workflow("form_once")[
        FormGate(),
        Step(runner=f"{_HERE}.stub_solve", consequential=True).named("solve"),
    ]
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_form(client, {"water_temp_c": 21.0})
    await task
    assert not [e for e in client.envelopes if e["type"] == "tool-payload-warning"]


async def test_cancelling_the_form_card_refuses_typed(client):
    decl = _params()
    p = await resolve_params(decl, {"outfall": [-124.1, 40.5]})
    plan = Workflow("form_cancel")[
        FormGate(),
        Step(runner=f"{_HERE}.stub_solve", consequential=True).named("solve"),
    ]
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_form(client, None, decision="cancel")
    with pytest.raises(Exception) as exc:
        await task
    assert exc.value.error_code == "INPUT_REVIEW_CANCELLED"
    assert SEEN == {}


# --------------------------------------------------------------------------- #
# The DRAW card
# --------------------------------------------------------------------------- #
async def _draw_plan(name, geometry, *, optional=False):
    decl = [Param("outfall", desc="Where the discharge enters the water",
                  door=doors.USER, optional=optional,
                  **({"derived_when_absent": "the reach seed stands in"}
                     if optional else {}))]
    p = await resolve_params(decl, {})
    plan = Workflow(name)[
        DrawGate(param="outfall", geometry=geometry,
                 prompt="Click where the discharge enters the river"),
        Step(runner=f"{_HERE}.stub_solve", kwargs={"pt": p.outfall},
             consequential=True).named("solve"),
    ]
    return plan, p, decl


async def test_a_drawn_point_reaches_the_run_stamped_user(client):
    plan, p, decl = await _draw_plan("draw_point", "point")
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    request = await _drive_draw(
        client, {"geometry_type": "point", "coordinates": [-124.1, 40.5]})
    out = await task

    assert request.mode == "point"
    assert "discharge enters the river" in request.description
    assert SEEN["pt"] == (-124.1, 40.5)
    row = next(e for e in out.entries if e.param == "outfall")
    assert row.basis == "user" and "drawn on the canvas" in (row.note or "")


@pytest.mark.parametrize("geometry,mode,purpose", [
    ("point", "point", "barrier"),
    ("rectangle", "bbox", "barrier"),
    ("polygon", "vector_draw", "aoi"),
    ("polyline", "vector_draw", "line"),
])
async def test_every_draw_kind_asks_for_its_own_affordance(client, geometry, mode,
                                                           purpose):
    plan, p, decl = await _draw_plan(f"draw_{geometry}", geometry)
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    request = await _drive_draw(client, {"cancelled": True})
    with pytest.raises(Exception):
        await task
    assert (request.mode, request.purpose) == (mode, purpose)


async def test_a_drawn_polygon_arrives_as_its_ring(client):
    plan, p, decl = await _draw_plan("draw_poly", "polygon")
    ring = [[-124.2, 40.4], [-124.0, 40.4], [-124.0, 40.6], [-124.2, 40.4]]
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_draw(client, {
        "geometry_type": "vector_draw",
        "features": {"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {"role": "aoi"},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}]},
    })
    await task
    assert SEEN["pt"] == ring


async def test_a_drawn_polyline_arrives_as_its_vertices(client):
    plan, p, decl = await _draw_plan("draw_line", "polyline")
    line = [[-124.2, 40.4], [-124.0, 40.6]]
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_draw(client, {
        "geometry_type": "vector_draw",
        "features": {"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {"role": "line"},
            "geometry": {"type": "LineString", "coordinates": line}}]},
    })
    await task
    assert SEEN["pt"] == line


async def test_a_declined_draw_card_refuses_typed_naming_the_param(client):
    """The v1 hybrid rule: a decline is a refusal that names the unmet gate, never
    a fallback geometry."""
    plan, p, decl = await _draw_plan("draw_decline", "point")
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_draw(client, {"cancelled": True})
    with pytest.raises(Exception) as exc:
        await task
    assert exc.value.error_code == "GATE_INPUT_REQUIRED"
    assert "outfall" in str(exc.value) and "cancelled" in str(exc.value)
    assert SEEN == {}


async def test_a_draw_gate_leaves_no_pending_request_behind(client):
    plan, p, decl = await _draw_plan("draw_clean", "point")
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_draw(client, {"geometry_type": "point",
                               "coordinates": [-124.1, 40.5]})
    await task
    assert server_spatial._PENDING_SPATIAL_INPUTS == {}


async def test_an_optional_param_is_still_ASKED_in_user_gated_mode(client):
    """Declaring the gate IS the request to ask. Auto stays silent about an
    optional param, because its declared absence describes itself; a user_gated
    run exists so the user can answer, and they cannot answer a card nobody
    showed."""
    plan, p, decl = await _draw_plan("draw_opt", "point", optional=True)
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_draw(client, {"geometry_type": "point",
                               "coordinates": [-124.1, 40.5]})
    await task
    assert SEEN["pt"] == (-124.1, 40.5)


async def test_declining_an_optional_draw_lets_the_declared_absence_stand(client):
    plan, p, decl = await _draw_plan("draw_opt_no", "point", optional=True)
    task = asyncio.ensure_future(
        interpret(plan, p, decl, input_mode="user_gated", resume=False))
    await _drive_draw(client, {"cancelled": True})
    await task
    assert SEEN["pt"] is None


async def test_auto_mode_never_shows_a_card_for_an_optional_param(client):
    plan, p, decl = await _draw_plan("draw_opt_auto", "point", optional=True)
    await interpret(plan, p, decl, input_mode="auto", resume=False)
    assert SEEN["pt"] is None
    assert not [e for e in client.envelopes if e["type"] == "spatial-input-request"]
