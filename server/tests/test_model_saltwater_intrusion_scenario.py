"""Tests for the MODFLOW Wave-5 BUY saltwater-intrusion agent-side path.

Four layers, mirroring ``test_model_capture_zone_scenario.py``:

  * ``SaltwaterIntrusionInputError`` honesty gate: a missing coastal transect
    raises the typed error immediately (never fabricates a coastline,
    Invariant 9).

  * The composer ``model_saltwater_intrusion_scenario`` full chain through a
    fake ``run_modflow_archetype_job`` (DI through the lazy import seam)
    returning a ``SaltwaterWedgeLayerURI``, verifying the archetype + transect
    threading and the typed result envelope.

  * Chart payload threading: the fake layer carries a ``_chart_payload``
    runtime attribute; the test verifies ``emit_chart_payloads`` is awaited
    with it (the chart path, no second UCN read).

  * The LLM-facing wrapper ``run_model_saltwater_intrusion_scenario`` maps a
    missing transect to ``USER_INPUT_REQUIRED`` (Invariant 9 surfaced as a
    narrated error).

  * Tool registration: the wrapper appears in TOOL_REGISTRY with
    ``cacheable=False`` + ``ttl_class='live-no-cache'``.

No mf6 binary is needed: the solver chain is stubbed; the tests exercise the
composition, honesty gate, chart emission, and registration seams only.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from trid3nt_contracts.modflow_contracts import SaltwaterWedgeLayerURI

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows import model_saltwater_intrusion_scenario as si_mod
from trid3nt_server.workflows.model_saltwater_intrusion_scenario import (
    SaltwaterIntrusionInputError,
    SaltwaterIntrusionResult,
    SaltwaterIntrusionScenarioError,
    model_saltwater_intrusion_scenario,
)


# --------------------------------------------------------------------------- #
# Shared fake helpers
# --------------------------------------------------------------------------- #


def _fake_si_layer(
    *,
    intrusion_m: float = 320.5,
    seawater_ppt: float = 35.0,
    ep_a: tuple[float, float] = (25.78, -80.19),
    ep_b: tuple[float, float] = (25.78, -80.18),
    chart: dict[str, Any] | None = None,
) -> SaltwaterWedgeLayerURI:
    """Construct a minimal ``SaltwaterWedgeLayerURI`` for use in unit tests."""
    layer = SaltwaterWedgeLayerURI(
        layer_id="saltwater-intrusion-TEST",
        name="Saltwater Intrusion Wedge (demo)",
        layer_type="vector",
        uri="file:///tmp/saltwater_intrusion_TEST.fgb",
        style_preset="saltwater_intrusion",
        role="primary",
        intrusion_length_m=intrusion_m,
        toe_distance_m=intrusion_m,
        seaward_salinity_ppt=seawater_ppt,
        transect_endpoints=(ep_a, ep_b),
    )
    # Stash the chart payload as a runtime attribute (mirrors postprocess behaviour).
    object.__setattr__(layer, "_chart_payload", chart)
    return layer


# --------------------------------------------------------------------------- #
# Honesty gate (Invariant 9): missing transect raises SaltwaterIntrusionInputError
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_honesty_gate_missing_transect_raises() -> None:
    """A call with coastal_transect_latlon=None must raise
    SaltwaterIntrusionInputError immediately, before any geocode or solver step
    (Invariant 9)."""
    with pytest.raises(SaltwaterIntrusionInputError, match="coastal_transect_latlon"):
        await model_saltwater_intrusion_scenario(
            aoi_latlon=(25.78, -80.19),
            coastal_transect_latlon=None,
        )


@pytest.mark.asyncio
async def test_honesty_gate_message_never_invents() -> None:
    """The error message must explicitly state that the transect is never
    invented (Invariant 9 guard for the narration layer)."""
    with pytest.raises(SaltwaterIntrusionInputError) as exc_info:
        await model_saltwater_intrusion_scenario(
            aoi_latlon=(25.78, -80.19),
            coastal_transect_latlon=None,
        )
    msg = str(exc_info.value).lower()
    # Must mention the prohibition on fabrication.
    assert "never" in msg or "never invented" in msg or "never fabricat" in msg


@pytest.mark.asyncio
async def test_honesty_gate_invalid_n_layers() -> None:
    """n_vertical_layers outside [4, 80] raises SaltwaterIntrusionInputError."""
    with pytest.raises(SaltwaterIntrusionInputError):
        await model_saltwater_intrusion_scenario(
            aoi_latlon=(25.78, -80.19),
            coastal_transect_latlon=((25.78, -80.20), (25.78, -80.18)),
            n_vertical_layers=2,  # below minimum of 4
        )


# --------------------------------------------------------------------------- #
# Composer full chain (fake archetype-tool DI)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_composer_full_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake run tool returning a SaltwaterWedgeLayerURI -> SaltwaterIntrusionResult
    with all required fields (Invariant 1: no free-generated numbers)."""
    captured: dict[str, Any] = {}
    fake_layer = _fake_si_layer(intrusion_m=415.0, seawater_ppt=35.0)

    async def _fake_run(run_args: Any, **_kw: Any) -> SaltwaterWedgeLayerURI:
        captured["run_args"] = run_args
        return fake_layer

    import trid3nt_server.tools.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _fake_run)

    result = await model_saltwater_intrusion_scenario(
        aoi_latlon=(25.78, -80.19),
        coastal_transect_latlon=((25.78, -80.20), (25.78, -80.18)),
        seawater_salinity_ppt=35.0,
        n_vertical_layers=20,
    )

    assert isinstance(result, SaltwaterIntrusionResult)
    # Archetype and transect threaded correctly.
    ra = captured["run_args"]
    assert ra.archetype == "saltwater_intrusion"
    assert ra.coastal_transect_latlon is not None
    ep_a, ep_b = ra.coastal_transect_latlon
    assert abs(ep_a[0] - 25.78) < 0.001
    assert abs(ep_a[1] - (-80.20)) < 0.001
    assert abs(ep_b[1] - (-80.18)) < 0.001
    assert ra.seawater_salinity_ppt == pytest.approx(35.0)
    assert ra.n_vertical_layers == 20
    # Typed layer carried through.
    assert isinstance(result.intrusion_layer, SaltwaterWedgeLayerURI)
    assert result.intrusion_layer.intrusion_length_m == pytest.approx(415.0)
    # Summary mirrors typed fields.
    assert result.summary["intrusion_length_m"] == pytest.approx(415.0)
    assert "demo_aquifer_caveat" in result.summary


@pytest.mark.asyncio
async def test_composer_assembles_correct_run_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Freshwater inflow and custom salinity are threaded into run_args."""
    captured: dict[str, Any] = {}
    fake_layer = _fake_si_layer(seawater_ppt=15.0, intrusion_m=200.0)

    async def _fake_run(run_args: Any, **_kw: Any) -> SaltwaterWedgeLayerURI:
        captured["run_args"] = run_args
        return fake_layer

    import trid3nt_server.tools.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _fake_run)

    await model_saltwater_intrusion_scenario(
        aoi_latlon=(34.0, -118.0),
        coastal_transect_latlon=((34.0, -118.1), (34.0, -117.9)),
        seawater_salinity_ppt=15.0,
        freshwater_inflow_m3_day=500.0,
        n_vertical_layers=10,
    )

    ra = captured["run_args"]
    assert ra.seawater_salinity_ppt == pytest.approx(15.0)
    assert ra.freshwater_inflow_m3_day == pytest.approx(500.0)
    assert ra.n_vertical_layers == 10
    assert ra.archetype == "saltwater_intrusion"


@pytest.mark.asyncio
async def test_composer_emits_chart_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the layer carries a non-None ``_chart_payload``, the composer calls
    ``emit_chart_payloads`` with it (no second UCN read needed)."""
    fake_chart: dict[str, Any] = {"$schema": "test", "vega_lite_spec": {"mark": "rect"}}
    fake_layer = _fake_si_layer(intrusion_m=300.0, chart=fake_chart)

    async def _fake_run(run_args: Any, **_kw: Any) -> SaltwaterWedgeLayerURI:
        return fake_layer

    import trid3nt_server.tools.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _fake_run)

    emitted: list[Any] = []

    async def _fake_emit_chart_payloads(payload: Any) -> None:
        emitted.append(payload)

    monkeypatch.setattr(
        si_mod,
        "emit_chart_payloads",
        _fake_emit_chart_payloads,
    )

    await model_saltwater_intrusion_scenario(
        aoi_latlon=(25.78, -80.19),
        coastal_transect_latlon=((25.78, -80.20), (25.78, -80.18)),
    )

    assert len(emitted) == 1
    assert emitted[0] is fake_chart


@pytest.mark.asyncio
async def test_composer_no_chart_emit_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_chart_payload`` is None, ``emit_chart_payloads`` is NOT called."""
    fake_layer = _fake_si_layer(intrusion_m=300.0, chart=None)

    async def _fake_run(run_args: Any, **_kw: Any) -> SaltwaterWedgeLayerURI:
        return fake_layer

    import trid3nt_server.tools.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _fake_run)

    emit_called = False

    async def _fake_emit_chart_payloads(payload: Any) -> None:
        nonlocal emit_called
        emit_called = True

    monkeypatch.setattr(
        si_mod,
        "emit_chart_payloads",
        _fake_emit_chart_payloads,
    )

    await model_saltwater_intrusion_scenario(
        aoi_latlon=(25.78, -80.19),
        coastal_transect_latlon=((25.78, -80.20), (25.78, -80.18)),
    )

    assert not emit_called


@pytest.mark.asyncio
async def test_composer_surfaces_run_error_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed run dict from the archetype tool re-raises as a typed scenario
    error (the honesty floor: a failed run never reads as a successful layer)."""

    async def _err_run(run_args: Any, **_kw: Any) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": "SALTWATER_INTRUSION_RUN_FAILED",
            "error_message": "BUY variable-density solve failed",
        }

    import trid3nt_server.tools.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _err_run)

    with pytest.raises(SaltwaterIntrusionScenarioError, match="RUN_FAILED"):
        await model_saltwater_intrusion_scenario(
            aoi_latlon=(25.78, -80.19),
            coastal_transect_latlon=((25.78, -80.20), (25.78, -80.18)),
        )


# --------------------------------------------------------------------------- #
# LLM-facing wrapper: USER_INPUT_REQUIRED on missing transect
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wrapper_missing_transect_returns_user_input_required() -> None:
    """The LLM-facing wrapper maps a missing transect to USER_INPUT_REQUIRED
    (Invariant 9: the tool never fabricates a coastal transect)."""
    out = await si_mod.run_model_saltwater_intrusion_scenario(
        aoi_latlon=[25.78, -80.19],
        coastal_transect_latlon=None,
    )
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"
    # The error message must mention the transect requirement.
    assert "transect" in out["error_message"].lower() or "coastline" in out["error_message"].lower()


@pytest.mark.asyncio
async def test_wrapper_invalid_transect_shape_returns_user_input_required() -> None:
    """A malformed transect (wrong shape) returns USER_INPUT_REQUIRED, not a
    crash (the wrapper coerces + validates before calling the composer)."""
    out = await si_mod.run_model_saltwater_intrusion_scenario(
        aoi_latlon=[25.78, -80.19],
        coastal_transect_latlon=[[25.78]],  # too few coordinates
    )
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"


# --------------------------------------------------------------------------- #
# Registration: tool in TOOL_REGISTRY with correct metadata
# --------------------------------------------------------------------------- #


def test_saltwater_intrusion_registered_uncacheable() -> None:
    import trid3nt_server.tools  # noqa: F401 - fires registration side-effects

    entry = TOOL_REGISTRY.get("run_model_saltwater_intrusion_scenario")
    assert entry is not None, (
        "run_model_saltwater_intrusion_scenario not in TOOL_REGISTRY"
    )
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"
