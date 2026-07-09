"""Fetch-resolution gate (NATE 2026-06-26) — the heavy-raster-fetcher half.

The #154 granularity gate widened to the two HEAVY raster FETCHERS (fetch_dem +
fetch_topobathy) so the user controls fetch resolution before a big download/merge
(memory: feedback_user_controlled_granularity). It REUSES ``_gate_on_solver_confirm``
and the SAME ``GranularitySuggestion`` card: ``engine`` in {dem, topobathy},
``resolution_param="resolution_m"``, ``compute_class="fetch"``. The override reuses
the existing ``tool-payload-confirmation`` ``narrow_scope`` path (no new envelope).

A fetch is NOT a solve, so ``FETCH_CONFIRM_TOOLS`` is kept SEPARATE from
``SOLVER_CONFIRM_TOOLS`` — the autostop solver-marker (``_is_solver_dispatch`` ->
``_solve_started``) keys off ``SOLVER_CONFIRM_TOOLS`` only and must NOT fire for a
fetch. The fetch branch never injects ``confirmed`` / ``enable_autoscale``.

Covers:
- the gate emits a ``tool-payload-warning`` carrying a ``granularity`` block with
  ``resolution_param="resolution_m"`` + ``engine`` in {dem, topobathy};
- ``proceed`` pins ``resolution_m=10`` (the coarse default) + injects NO confirmed;
- ``narrow_scope`` to a finer rung (1 / 3) on a small AOI is applied as-is;
- ``narrow_scope`` finer-than-finest_allowed on a LARGE AOI is clamped UP to
  finest_allowed_m (the px-grid bound);
- ``cancel`` / timeout fail-CLOSED (the fetch does not run);
- a build exception fails OPEN (proceed with original params);
- solver tools still gate as before (fetch_suggestion is None for them);
- fetch_naip / compute_ndvi are NOT in FETCH_CONFIRM_TOOLS (no finer knob).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_contracts import new_ulid
from grace2_contracts.ws import PayloadConfirmationEnvelopePayload


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _FakeState:
    def __init__(self) -> None:
        self.session_id = new_ulid()


# A small AOI (Twin Falls, ID) — ~2 km on a side, so even a 1 m rung is well under
# the MAX_FETCH_PX (8192) bound -> a fine narrow_scope is honoured as-is.
_SMALL_BBOX = [-114.48, 42.55, -114.46, 42.57]

# A large AOI (~a few degrees) — at a fine rung the px grid blows past 8192 on the
# long axis, so finest_allowed_m floors above the ladder floor and a finer
# narrow_scope is clamped UP.
_LARGE_BBOX = [-120.0, 35.0, -110.0, 45.0]


def _fetch_params(bbox=None, resolution_m=None) -> dict:
    p: dict = {"bbox": list(bbox if bbox is not None else _SMALL_BBOX)}
    if resolution_m is not None:
        p["resolution_m"] = resolution_m
    return p


async def _drive_decision(server, decision: str, revised_args=None) -> None:
    """Resolve the single pending gate future with ``decision`` once it appears."""
    for _ in range(400):
        if server._PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    wid = next(iter(server._PENDING_CONFIRMATIONS))
    server._PENDING_CONFIRMATIONS[wid][1].set_result(
        PayloadConfirmationEnvelopePayload(
            warning_id=wid, decision=decision, revised_args=revised_args
        )
    )


# --------------------------------------------------------------------------- #
# 1) Registration: the fetchers are in FETCH_CONFIRM_TOOLS, SEPARATE from the
#    solver set; the no-finer-knob fetchers are NOT in either.
# --------------------------------------------------------------------------- #
def test_fetch_tools_in_fetch_confirm_set() -> None:
    from grace2_agent import server

    assert "fetch_dem" in server.FETCH_CONFIRM_TOOLS
    assert "fetch_topobathy" in server.FETCH_CONFIRM_TOOLS
    # SEPARATE from the solver set so the autostop solver-marker never fires for
    # a fetch.
    assert not (server.FETCH_CONFIRM_TOOLS & server.SOLVER_CONFIRM_TOOLS)
    # The no-finer-knob fetchers are NOT gated.
    assert "fetch_naip" not in server.FETCH_CONFIRM_TOOLS
    assert "compute_ndvi" not in server.FETCH_CONFIRM_TOOLS
    assert "fetch_naip" not in server.SOLVER_CONFIRM_TOOLS
    assert "compute_ndvi" not in server.SOLVER_CONFIRM_TOOLS


# --------------------------------------------------------------------------- #
# 2) The gate emits a granularity block for each fetcher.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,engine",
    [("fetch_dem", "dem"), ("fetch_topobathy", "topobathy")],
)
async def test_gate_emits_fetch_granularity_block(tool_name: str, engine: str) -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    approver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, tool_name, _fetch_params()
    )
    await approver

    assert should_run is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    g = card["payload"]["granularity"]
    assert g is not None
    assert g["engine"] == engine
    assert g["resolution_param"] == "resolution_m"
    assert g["compute_class"] == "fetch"
    assert g["suggested_resolution_m"] > 0
    assert len(g["resolution_choices"]) >= 1
    assert all(r > 0 for r in g["resolution_choices"])
    assert g["estimated_active_cells"] >= 0
    assert g["estimated_solve_seconds"] == 0.0
    assert g["vcpus"] == 1
    assert g["coarsened"] is False
    assert g["spot_label"] is None
    assert g["cell_cap"] == server.MAX_FETCH_PX ** 2
    # narrow_scope must be offered so the user can override the rung.
    assert "narrow_scope" in card["payload"]["options"]


# --------------------------------------------------------------------------- #
# 3) proceed pins resolution_m=10 (the coarse default) + injects NO confirmed.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["fetch_dem", "fetch_topobathy"])
async def test_proceed_pins_default_resolution(tool_name: str) -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    approver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, tool_name, _fetch_params(resolution_m=10)
    )
    await approver

    assert should_run is True
    assert effective["resolution_m"] == 10
    # A fetch is NOT a solve: no confirmed / no autoscale flag.
    assert "confirmed" not in effective
    assert "enable_autoscale" not in effective


# --------------------------------------------------------------------------- #
# 4) narrow_scope to a finer rung on a SMALL AOI is applied as-is.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name,finer",
    [("fetch_dem", 1), ("fetch_dem", 3), ("fetch_topobathy", 3)],
)
async def test_narrow_scope_finer_applied_small_aoi(
    tool_name: str, finer: int
) -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    approver = asyncio.create_task(
        _drive_decision(server, "narrow_scope", {"resolution_m": finer})
    )
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, tool_name, _fetch_params(bbox=_SMALL_BBOX)
    )
    await approver

    assert should_run is True
    # Small AOI: the finer rung is well under the px bound -> applied as-is.
    assert effective["resolution_m"] == finer
    assert "confirmed" not in effective


# --------------------------------------------------------------------------- #
# 5) narrow_scope finer-than-finest_allowed on a LARGE AOI is CLAMPED UP.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_narrow_scope_finer_clamped_large_aoi() -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()

    # Compute the finest_allowed_m the gate will derive for this LARGE AOI so we
    # assert the clamp matched it (the px-grid bound, > the ladder floor here).
    _env, sugg = await server._build_fetch_resolution_envelope(  # type: ignore[attr-defined]
        "fetch_dem", _fetch_params(bbox=_LARGE_BBOX)
    )
    finest = sugg.finest_allowed_m
    assert finest > 1.0  # the large AOI floors the finest rung above 1 m

    approver = asyncio.create_task(
        _drive_decision(server, "narrow_scope", {"resolution_m": 1})
    )
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "fetch_dem", _fetch_params(bbox=_LARGE_BBOX)
    )
    await approver

    assert should_run is True
    # The over-fine 1 m request is floored UP to finest_allowed_m (int-coerced).
    assert effective["resolution_m"] == int(finest)
    assert effective["resolution_m"] >= int(finest)
    assert "confirmed" not in effective


# --------------------------------------------------------------------------- #
# 6) cancel / timeout fail-CLOSED (the fetch does not run).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_fails_closed() -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    canceller = asyncio.create_task(_drive_decision(server, "cancel"))
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "fetch_dem", _fetch_params()
    )
    await canceller
    assert should_run is False
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "USER_INPUT_CANCELLED"


@pytest.mark.asyncio
async def test_timeout_fails_closed(monkeypatch) -> None:
    from grace2_agent import server

    monkeypatch.setattr(server, "CODE_EXEC_CONFIRM_TIMEOUT_SECONDS", 0)
    ws, state = _FakeWS(), _FakeState()
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "fetch_topobathy", _fetch_params()
    )
    assert should_run is False
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "CONFIRMATION_TIMEOUT"
    # No stuck pending entry after the timeout (the finally pops it).
    assert not server._PENDING_CONFIRMATIONS


# --------------------------------------------------------------------------- #
# 7) A build exception fails OPEN (proceed with original params, unmodified).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_build_exception_fails_open() -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    # A missing/invalid bbox makes the envelope builder raise -> fail OPEN.
    params = {"resolution_m": 3}  # no bbox

    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "fetch_dem", params
    )
    # FAIL OPEN: the gate must NEVER block/orphan a fetch on its own error.
    assert should_run is True
    assert effective is params  # original params, unmodified
    assert "confirmed" not in effective
    # No half-built card emitted, no stuck pending entry.
    assert not any(e.get("type") == "tool-payload-warning" for e in ws.sent)
    assert not server._PENDING_CONFIRMATIONS


# --------------------------------------------------------------------------- #
# 8) Solver tools still gate as before (fetch_suggestion is None for them) and a
#    fetch is NOT marked as a solver dispatch.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_solver_branch_unchanged_no_fetch_suggestion(monkeypatch) -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    params = {"location_query": "Fort Myers, Florida", "return_period_yr": 100}

    approver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await approver

    # The solver gate still injects confirmed (its existing behavior is intact).
    assert should_run is True and effective["confirmed"] is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    # The solver card carries NO fetch resolution-param granularity.
    g = card["payload"].get("granularity")
    if g is not None:
        assert g["resolution_param"] != "resolution_m"
    # resolution_m is never written for a solver gate.
    assert "resolution_m" not in effective


def test_fetch_is_not_a_solver_dispatch_marker() -> None:
    """The autostop solver-marker keys off SOLVER_CONFIRM_TOOLS only; a fetcher
    being absent there is what keeps a fetch from skewing the in-flight solve
    count + the auto-stop coupling."""
    from grace2_agent import server

    for fetcher in server.FETCH_CONFIRM_TOOLS:
        assert fetcher not in server.SOLVER_CONFIRM_TOOLS


# --------------------------------------------------------------------------- #
# 9) _clamp_fetch_resolution unit: finer floored up, coarser honoured.
# --------------------------------------------------------------------------- #
def test_clamp_fetch_resolution_helper() -> None:
    from grace2_agent import server

    # Finer (smaller) than the bound -> floored UP to the bound.
    assert server._clamp_fetch_resolution(1.0, 5.0) == 5.0
    # Coarser (larger) than the bound -> honoured exactly.
    assert server._clamp_fetch_resolution(30.0, 5.0) == 30.0
    # Equal -> the bound.
    assert server._clamp_fetch_resolution(5.0, 5.0) == 5.0


# --------------------------------------------------------------------------- #
# 10) Local-cloud fingerprint seam (NATE 2026-07-08): the LOCAL build
#     (GRACE2_SOLVER_BACKEND=local-docker) must not surface the cloud
#     "fetch (1 vCPU)" compute label on the confirm card -- it renders the
#     "local" compute lane instead. The cloud lane (aws-batch / unset) keeps
#     the exact prior values byte-for-byte.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend,expected_compute_class",
    [
        ("local-docker", "local"),
        ("aws-batch", "fetch"),
        ("", "fetch"),  # unset/empty -> the cloud default, unchanged
    ],
)
async def test_fetch_gate_compute_label_deployment_aware(
    monkeypatch, backend: str, expected_compute_class: str
) -> None:
    from grace2_agent import server

    if backend:
        monkeypatch.setenv("GRACE2_SOLVER_BACKEND", backend)
    else:
        monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)

    ws, state = _FakeWS(), _FakeState()
    approver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "fetch_dem", _fetch_params()
    )
    await approver

    assert should_run is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    g = card["payload"]["granularity"]
    assert g["compute_class"] == expected_compute_class
    # Both lanes: vcpus stays 1 (contract requires > 0) and no Spot label.
    assert g["vcpus"] == 1
    assert g["spot_label"] is None
