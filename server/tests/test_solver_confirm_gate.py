"""job-0241: solver confirm gate — the dispatch-path test that was missing.

The Stage 3 live gate (job-0235) proved the Case 2 composer dispatched MODFLOW
with ZERO user confirmation: the registered wrapper hardcoded
``confirmed=True`` and the server dispatch path had no solver gate. The
programmatic tests all drove the INNER composer, never the registered wrapper
through the server's dispatch seam — this file closes that exact gap.

Covers:
- the gate builds the confirm card from the PURE extraction and emits it as a
  ``tool-payload-warning`` (the card the web client already renders);
- approve → ``confirmed=True`` injected; cancel/timeout → fail-closed with a
  typed error envelope and NO dispatch;
- the LLM cannot self-approve: ``confirmed`` supplied in params is STRIPPED
  before gating;
- the registered wrapper defaults ``confirmed=False`` (no hardcoded bypass);
- extraction failure falls through (gate must not mask parameter errors).
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.ws import PayloadConfirmationEnvelopePayload

ARTICLE = """\
TWIN FALLS, IDAHO — A tanker overturned south of Twin Falls, Idaho, releasing
an estimated 12,000 gallons of trichloroethylene (TCE) into roadside soil over
roughly six hours before containment. Officials warned the solvent sits above
the Eastern Snake River Plain aquifer and asked for plume modeling.
"""


@pytest.fixture(autouse=True)
def _cap_gate_waits(monkeypatch):
    """LANE C: cap every user-decision gate wait so a headless run never hangs
    on the F6 24h local-lane lift (``_gate_wait_timeout``). Production leaves
    ``TRID3NT_GATE_WAIT_CAP_S`` unset -> byte-identical behavior. Happy-path
    approver tasks answer within milliseconds; the dedicated timeout test
    tightens the cap so it hits the honest fail-closed path fast."""
    monkeypatch.setenv("TRID3NT_GATE_WAIT_CAP_S", "5")


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _FakeState:
    def __init__(self) -> None:
        self.session_id = new_ulid()
        # fix (bbox-gate-retry-loop, 2026-07-09): the turn-memory dict the
        # ``_gate_with_turn_memory`` wrapper reads/writes. Real
        # ``SessionState`` carries this too (reset at the start of every
        # user-message dispatch); tests construct it here since ``_FakeState``
        # is a minimal stand-in.
        self.gate_decisions_this_turn: dict = {}


def _patch_extraction(monkeypatch):
    """Avoid live geocoding: patch extract_spill_parameters with a fixed derived dict."""
    from trid3nt_server import server

    derived = {
        "spill_location_latlon": (42.556, -114.470),
        "contaminant": "trichloroethylene",
        "release_rate_kg_s": 3.07,
        "duration_days": 0.25,
        "location_name": "Twin Falls, Idaho",
        "total_mass_kg": 66320.4,
        "scale_value": 12000,
        "scale_unit": "gallons",
        "clamps_applied": [],
        "extraction_notes": [],
    }
    import trid3nt_server.workflows.model_groundwater_contamination_scenario as m

    monkeypatch.setattr(m, "extract_spill_parameters", lambda text, geocode=True: derived)
    return derived


@pytest.mark.asyncio
async def test_gate_emits_card_and_approve_injects_confirmed(monkeypatch) -> None:
    from trid3nt_server import server

    _patch_extraction(monkeypatch)
    ws = _FakeWS()
    state = _FakeState()
    params = {"article_text": ARTICLE}

    async def _approve_soon() -> None:
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        wid = next(iter(server._PENDING_CONFIRMATIONS))
        server._PENDING_CONFIRMATIONS[wid][1].set_result(
            PayloadConfirmationEnvelopePayload(warning_id=wid, decision="proceed")
        )

    approver = asyncio.create_task(_approve_soon())
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_groundwater_contamination_scenario", params
    )
    await approver

    assert should_run is True
    assert effective["confirmed"] is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    # The user confirms the ACTUAL derived forcing, not a generic banner.
    assert card["payload"]["tool_args"]["contaminant"] == "trichloroethylene"
    assert card["payload"]["tool_args"]["location_name"] == "Twin Falls, Idaho"
    assert card["payload"]["options"] == ["proceed", "cancel"]


@pytest.mark.asyncio
async def test_gate_cancel_fails_closed(monkeypatch) -> None:
    from trid3nt_server import server

    _patch_extraction(monkeypatch)
    ws = _FakeWS()
    state = _FakeState()

    async def _cancel_soon() -> None:
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        wid = next(iter(server._PENDING_CONFIRMATIONS))
        server._PENDING_CONFIRMATIONS[wid][1].set_result(
            PayloadConfirmationEnvelopePayload(warning_id=wid, decision="cancel")
        )

    canceller = asyncio.create_task(_cancel_soon())
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_groundwater_contamination_scenario", {"article_text": ARTICLE}
    )
    await canceller

    assert should_run is False
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "USER_INPUT_CANCELLED"


@pytest.mark.asyncio
async def test_gate_timeout_fails_closed(monkeypatch) -> None:
    from trid3nt_server import server

    _patch_extraction(monkeypatch)
    monkeypatch.setattr(server, "CODE_EXEC_CONFIRM_TIMEOUT_SECONDS", 0)
    # No client answers the card: the F6 local-lane gate would wait 24h, so the
    # LANE C cap forces the honest timeout path quickly (a tight override of the
    # autouse 5s net keeps this pure-timeout assertion fast).
    monkeypatch.setenv("TRID3NT_GATE_WAIT_CAP_S", "0.05")
    ws = _FakeWS()
    state = _FakeState()

    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_groundwater_contamination_scenario", {"article_text": ARTICLE}
    )
    assert should_run is False
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "CONFIRMATION_TIMEOUT"


@pytest.mark.asyncio
async def test_extraction_failure_falls_through_to_composer(monkeypatch) -> None:
    """The gate must not mask parameter problems — composer raises its own error."""
    from trid3nt_server import server
    import trid3nt_server.workflows.model_groundwater_contamination_scenario as m

    def _boom(text, geocode=True):
        raise ValueError("no spill scale found")

    monkeypatch.setattr(m, "extract_spill_parameters", _boom)
    ws = _FakeWS()
    state = _FakeState()
    should_run, params = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_groundwater_contamination_scenario", {"article_text": ARTICLE}
    )
    assert should_run is True  # fall through; composer will raise typed error
    assert "confirmed" not in params
    assert not ws.sent  # no confusing half-card emitted


def test_solver_tool_registered_in_confirm_set() -> None:
    from trid3nt_server import server

    assert (
        "run_model_groundwater_contamination_scenario" in server.SOLVER_CONFIRM_TOOLS
    )


def test_wrapper_defaults_fail_closed() -> None:
    """The registered wrapper must NOT hardcode confirmed=True (the job-0235 bug)."""
    from trid3nt_server.workflows.model_groundwater_contamination_scenario import (
        run_model_groundwater_contamination_scenario as wrapper,
    )

    sig = inspect.signature(wrapper)
    assert "confirmed" in sig.parameters
    assert sig.parameters["confirmed"].default is False


@pytest.mark.asyncio
async def test_dispatch_path_strips_llm_supplied_confirmed(monkeypatch) -> None:
    """Gemini cannot self-approve: params['confirmed']=True is stripped before
    gating. We verify the strip+gate wiring at the dispatch site by checking
    the gate still runs (emits the card) even when the LLM supplied
    confirmed=True."""
    from trid3nt_server import server

    _patch_extraction(monkeypatch)
    ws = _FakeWS()
    state = _FakeState()
    params = {"article_text": ARTICLE, "confirmed": True}

    # Simulate the dispatch-site wiring exactly as _invoke_tool_via_emitter does.
    tool_name = "run_model_groundwater_contamination_scenario"
    assert tool_name in server.SOLVER_CONFIRM_TOOLS
    params.pop("confirmed", None)

    async def _approve_soon() -> None:
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        wid = next(iter(server._PENDING_CONFIRMATIONS))
        server._PENDING_CONFIRMATIONS[wid][1].set_result(
            PayloadConfirmationEnvelopePayload(warning_id=wid, decision="proceed")
        )

    approver = asyncio.create_task(_approve_soon())
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, tool_name, params
    )
    await approver

    assert should_run is True
    # The gate ran (card emitted) — the LLM-supplied confirmed did not bypass it.
    assert any(e.get("type") == "tool-payload-warning" for e in ws.sent)
    assert effective["confirmed"] is True


def test_dispatch_source_contains_strip_and_gate() -> None:
    """Belt-and-braces: the dispatch site strips LLM-supplied ``confirmed`` for
    SOLVER_CONFIRM_TOOLS before gating (source-level assertion so a refactor
    that drops the strip line fails loudly)."""
    import trid3nt_server.server as server_mod

    src = inspect.getsource(server_mod._invoke_tool_via_emitter)
    assert "SOLVER_CONFIRM_TOOLS" in src
    assert 'params.pop("confirmed", None)' in src
    assert "SolverConfirmationCancelledError" in src


def test_code_exec_request_in_hot_set() -> None:
    """job-0247 (OQ-0247-CODE-EXEC-NOT-IN-HOT-SET): code_exec_request must be
    hot-set-reachable — round-4 live showed the validator rejecting Gemini's
    CORRECT first-turn call, producing a false 'cannot run Python' narration."""
    from trid3nt_server.categories import HOT_SET_TOOLS

    assert "code_exec_request" in HOT_SET_TOOLS


@pytest.mark.asyncio
async def test_flood_gate_emits_args_card_and_approve(monkeypatch) -> None:
    """job-0256: run_model_flood_scenario is gated; the card carries the call
    args (no extraction) and approve injects confirmed=True."""
    from trid3nt_server import server

    ws = _FakeWS()
    state = _FakeState()
    params = {"location_query": "Fort Myers, Florida", "return_period_yr": 100}

    async def _approve_soon() -> None:
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        wid = next(iter(server._PENDING_CONFIRMATIONS))
        server._PENDING_CONFIRMATIONS[wid][1].set_result(
            PayloadConfirmationEnvelopePayload(warning_id=wid, decision="proceed")
        )

    approver = asyncio.create_task(_approve_soon())
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await approver
    assert should_run is True and effective["confirmed"] is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    assert card["payload"]["tool_args"]["location"] == "Fort Myers, Florida"
    assert "SFINCS" in card["payload"]["recommendation"]


def test_flood_solvers_in_confirm_set() -> None:
    from trid3nt_server import server

    assert "run_model_flood_scenario" in server.SOLVER_CONFIRM_TOOLS
    assert "run_model_flood_habitat_scenario" in server.SOLVER_CONFIRM_TOOLS


# NATE 2026-06-26: the OpenQuake classical-PSHA solver is gated like the others.
@pytest.mark.asyncio
async def test_psha_gate_emits_card_and_approve(monkeypatch) -> None:
    """run_seismic_hazard_psha is gated; the card is a simple proceed/cancel
    confirm summarizing the PSHA (AOI area, IMT, PoE -> return period) and
    approve injects confirmed=True (no granularity picker)."""
    from trid3nt_server import server

    ws = _FakeWS()
    state = _FakeState()
    # San Francisco Bay-ish AOI; 10% in 50 yr -> ~475-year return period.
    params = {
        "bbox": [-122.6, 37.5, -122.2, 37.9],
        "imt": "PGA",
        "poe": 0.10,
        "investigation_time_years": 50.0,
    }

    async def _approve_soon() -> None:
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        wid = next(iter(server._PENDING_CONFIRMATIONS))
        server._PENDING_CONFIRMATIONS[wid][1].set_result(
            PayloadConfirmationEnvelopePayload(warning_id=wid, decision="proceed")
        )

    approver = asyncio.create_task(_approve_soon())
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_seismic_hazard_psha", params
    )
    await approver
    assert should_run is True and effective["confirmed"] is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    assert card["payload"]["tool_name"] == "run_seismic_hazard_psha"
    assert card["payload"]["options"] == ["proceed", "cancel"]
    assert card["payload"]["tool_args"]["imt"] == "PGA"
    # 10% in 50 yr -> ~475-year return period (rounded).
    assert card["payload"]["tool_args"]["return_period_years"] == 475
    assert "PSHA" in card["payload"]["recommendation"]


@pytest.mark.asyncio
async def test_psha_gate_cancel_fails_closed() -> None:
    """A cancel decision fails closed (no dispatch) like the other solvers."""
    from trid3nt_server import server

    ws = _FakeWS()
    state = _FakeState()
    params = {"bbox": [-122.6, 37.5, -122.2, 37.9], "imt": "PGA", "poe": 0.10}

    async def _cancel_soon() -> None:
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        wid = next(iter(server._PENDING_CONFIRMATIONS))
        server._PENDING_CONFIRMATIONS[wid][1].set_result(
            PayloadConfirmationEnvelopePayload(warning_id=wid, decision="cancel")
        )

    canceller = asyncio.create_task(_cancel_soon())
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_seismic_hazard_psha", params
    )
    await canceller
    assert should_run is False


def test_psha_solver_in_confirm_set() -> None:
    from trid3nt_server import server

    assert "run_seismic_hazard_psha" in server.SOLVER_CONFIRM_TOOLS


# --------------------------------------------------------------------------- #
# Local-cloud fingerprint seam (NATE 2026-07-08): confirm-card prose is
# deployment-aware. The LOCAL build (TRID3NT_SOLVER_BACKEND=local-docker)
# never says "cloud solve" / "AWS Batch"; the cloud lane (aws-batch / unset)
# keeps the exact prior wording byte-for-byte.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,expected,forbidden",
    [
        # AWS Batch arm removed (local-only slim) -> solver_backend() is always
        # local-docker, so only the local-solve recommendation prose is reachable.
        ("local-docker", "(local solve).", "cloud solve"),
    ],
)
async def test_flood_gate_recommendation_deployment_aware(
    monkeypatch, backend: str, expected: str, forbidden: str
) -> None:
    from trid3nt_server import server

    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", backend)
    ws = _FakeWS()
    state = _FakeState()
    # bbox included so the card also carries a granularity block.
    params = {
        "location_query": "Fort Myers, Florida",
        "bbox": [-81.98, 26.55, -81.90, 26.63],
        "return_period_yr": 100,
    }

    async def _approve_soon() -> None:
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        wid = next(iter(server._PENDING_CONFIRMATIONS))
        server._PENDING_CONFIRMATIONS[wid][1].set_result(
            PayloadConfirmationEnvelopePayload(warning_id=wid, decision="proceed")
        )

    approver = asyncio.create_task(_approve_soon())
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await approver
    assert should_run is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    rec = card["payload"]["recommendation"]
    assert expected in rec
    assert forbidden not in rec
    g = card["payload"]["granularity"]
    assert g is not None
    if backend == "local-docker":
        # The local lane renders the local compute descriptors...
        assert g["compute_class"] == "local"
        assert g["spot_label"] is None
    else:
        # ...and the cloud lane keeps the prior default label unchanged.
        assert g["compute_class"] == "standard"
    # The dispatch args are NEVER localized -- only the card wording is.
    assert card["payload"]["tool_args"]["compute_class"] == "standard"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,expected,forbidden",
    [
        # AWS Batch arm removed (local-only slim) -> solver_backend() is always
        # local-docker, so only the local-solve recommendation prose is reachable.
        (
            "local-docker",
            "This runs the OpenQuake engine locally (typically several "
            "minutes).",
            "AWS Batch",
        ),
    ],
)
async def test_psha_gate_recommendation_deployment_aware(
    monkeypatch, backend: str, expected: str, forbidden: str
) -> None:
    from trid3nt_server import server

    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", backend)
    ws = _FakeWS()
    state = _FakeState()
    params = {
        "bbox": [-122.6, 37.5, -122.2, 37.9],
        "imt": "PGA",
        "poe": 0.10,
        "investigation_time_years": 50.0,
    }

    async def _approve_soon() -> None:
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        wid = next(iter(server._PENDING_CONFIRMATIONS))
        server._PENDING_CONFIRMATIONS[wid][1].set_result(
            PayloadConfirmationEnvelopePayload(warning_id=wid, decision="proceed")
        )

    approver = asyncio.create_task(_approve_soon())
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_seismic_hazard_psha", params
    )
    await approver
    assert should_run is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    rec = card["payload"]["recommendation"]
    assert expected in rec
    assert forbidden not in rec


# --------------------------------------------------------------------------- #
# Turn-memory fix (bbox-gate-retry-loop, 2026-07-09) - live drive found a
# model retrying ``fetch_landcover`` with corrected NON-bbox args after typed
# errors (dataset='nlcd' -> 'nlcd_' -> 'nlcd_2021'); each valid-bbox retry
# re-emitted a NEW confirm gate for the SAME tool + SAME bbox, and the second
# (unanswered) gate hung the turn forever (local gates have no timeout by
# design). ``_gate_with_turn_memory`` wraps ``_gate_on_solver_confirm`` with a
# per-turn ``state.gate_decisions_this_turn`` memory keyed by
# ``_gate_memory_key`` (tool + bbox rounded to ~6 decimals).
#
# fetch_landcover skips the card entirely for a small AOI (no coarsening
# needed) -- these tests use a Washington-state-scale bbox (mirrors the live
# bug report) so the gate is the REAL thing, not the small-AOI skip path.
# --------------------------------------------------------------------------- #

_WA_BBOX = [-124.8, 45.5, -116.9, 49.0]
_CA_BBOX = [-124.4, 32.5, -114.1, 42.0]  # a DIFFERENT state-scale bbox


async def _approve_next_pending(server, decision: str = "proceed") -> None:
    """Resolve the next pending gate future with ``decision`` once it appears."""
    for _ in range(400):
        if server._PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    wid = next(iter(server._PENDING_CONFIRMATIONS))
    server._PENDING_CONFIRMATIONS[wid][1].set_result(
        PayloadConfirmationEnvelopePayload(warning_id=wid, decision=decision)
    )


@pytest.mark.asyncio
async def test_gate_turn_memory_auto_applies_same_tool_same_bbox() -> None:
    """A second call this turn with the SAME tool + bbox (but a corrected,
    non-bbox arg -- e.g. a fixed `dataset`) must NOT gate again; the earlier
    proceed decision is replayed and the remembered resolution_m override
    carries onto the new call's params."""
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()

    approver = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_1, approved_1 = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd"}
    )
    await approver
    assert should_run_1 is True
    assert "resolution_m" in approved_1  # the gate pinned the suggested rung
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1

    # Retry with a CORRECTED dataset (the fix-1 alias would already accept
    # 'nlcd', but this exercises the exact live failure shape where the
    # model retried with a different, still-typed-error-prone value before
    # landing on 'nlcd_2021').
    should_run_2, approved_2 = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    assert should_run_2 is True
    # NO second gate was emitted.
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1
    # The remembered resolution_m override carried onto the retry...
    assert approved_2["resolution_m"] == approved_1["resolution_m"]
    # ...while the retry's OWN corrected dataset arg is preserved (not
    # clobbered by the memoized decision).
    assert approved_2["dataset"] == "nlcd_2021"


@pytest.mark.asyncio
async def test_gate_turn_memory_different_bbox_gates_again() -> None:
    """A different bbox in the same turn is a memory MISS -- gates fresh."""
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()

    approver = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_1, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver
    assert should_run_1 is True
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1

    approver_2 = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_2, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_CA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver_2
    assert should_run_2 is True
    # A SECOND gate WAS emitted for the different bbox.
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 2


@pytest.mark.asyncio
async def test_gate_turn_memory_cancel_not_memoized_gates_again() -> None:
    """A 'cancel' decision must NOT be memoized -- an identical retry gates
    again (the user might reconsider on a corrected retry)."""
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()

    canceller = asyncio.create_task(_approve_next_pending(server, "cancel"))
    should_run_1, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await canceller
    assert should_run_1 is False
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1

    # Identical retry (same tool, same bbox) -- must gate AGAIN, not hang or
    # silently auto-cancel from memory.
    approver = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_2, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver
    assert should_run_2 is True
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 2


@pytest.mark.asyncio
async def test_gate_turn_memory_new_turn_gates_again() -> None:
    """A fresh turn (state.gate_decisions_this_turn reset, mirroring the
    real per-turn reset in server.py's dispatch entrypoint) gates again even
    for the SAME tool + bbox that was approved last turn."""
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()

    approver = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_1, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver
    assert should_run_1 is True
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1

    # Simulate the new-turn reset (same site as credential_prompted_tools).
    state.gate_decisions_this_turn = {}

    approver_2 = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_2, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver_2
    assert should_run_2 is True
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 2


def test_gate_decisions_this_turn_reset_at_new_dispatch() -> None:
    """Source-level assertion: the real per-turn reset site (the same one
    that clears ``credential_prompted_tools``) also clears
    ``gate_decisions_this_turn`` -- a regression here would silently bring
    back the cross-turn leak this fix closes."""
    import inspect

    import trid3nt_server.server as server_mod

    src = inspect.getsource(server_mod)
    assert "state.gate_decisions_this_turn = {}" in src


def test_gate_memory_key_bboxless_tool_uses_full_args() -> None:
    """A gated tool with no bbox arg (e.g. the groundwater composers) keys
    on the full normalized args dict, so ANY arg change gates fresh."""
    from trid3nt_server import server

    k1 = server._gate_memory_key(
        "run_model_groundwater_contamination_scenario", {"article_text": ARTICLE}
    )
    k2 = server._gate_memory_key(
        "run_model_groundwater_contamination_scenario",
        {"article_text": ARTICLE + " "},
    )
    assert k1 != k2
