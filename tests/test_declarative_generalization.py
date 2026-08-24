"""The generalization checkpoint: one SWMM and one MODFLOW declared template.

Covers the declarations themselves (plan validity, doors, law-9 refusals) and the
three library seams the checkpoint added: ``Derived`` evidence on a derived row,
the honest ``real_source`` rule, and ``RunResult.params`` - the sheet the run
actually ran on.

Offline: every fetch, geocode and solver the templates reach for is patched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tests.card_client import (  # noqa: F401 - card_client is a fixture
    answer_form_card,
    card_client,
)
from trid3nt_server.declarative import (
    Derived,
    Param,
    doors,
    resolve_params,
    validate_plan,
)
from trid3nt_server.workflows.modflow.regional_water_budget import (
    regional_water_budget as budget_mod,
)
from trid3nt_server.workflows.swmm.aquifer_baseflow import (
    aquifer_baseflow as swmm_mod,
)


# --------------------------------------------------------------------------- #
# The declarations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mod", [budget_mod, swmm_mod])
def test_plan_validates_against_its_own_declaration(mod: Any) -> None:
    """Every Ref and ParamRef in the plan names something the workflow declares."""
    sheet = _sheet(mod.PARAMS)
    validate_plan(mod.plan(sheet, None), mod.PARAMS, mod.DATA, sheet=sheet)


@pytest.mark.parametrize("mod", [budget_mod, swmm_mod])
def test_every_physics_param_has_a_real_source_or_a_gate(mod: Any) -> None:
    """Law 9, structurally: no physics param may rest on a labeled default."""
    offenders = [p.name for p in mod.PARAMS
                 if p.consequence == "physics"
                 and p.door in (doors.SCENARIO, doors.CONSTANT)]
    assert offenders == []


@pytest.mark.parametrize("tool", [budget_mod.modflow_regional_water_budget,
                                  swmm_mod.swmm_aquifer_baseflow_to_node])
def test_the_routing_view_is_rendered_from_the_declaration(tool: Any) -> None:
    """Import already enforces the 1000-char front budget; this states the promise."""
    assert "Returns:" in tool.routing_doc
    assert "Params:" in tool.__doc__
    assert "Do NOT use this for:" in tool.routing_doc


def _sheet(params: Any) -> Any:
    supplied = {p.name: _stub(p) for p in params}
    return asyncio.run(resolve_params(params, supplied))


def _stub(param: Param) -> Any:
    if param.bounds is not None:
        return float(param.bounds[0])
    if param.default is not None:
        return param.default
    if param.name.endswith("latlon"):
        return (40.0, -100.0)
    return "x"


# --------------------------------------------------------------------------- #
# The library seams the checkpoint added
# --------------------------------------------------------------------------- #


def _derived_with_evidence(_params: Any) -> Derived:
    return Derived(value=0.25, note="fitted from the sampled texture",
                   real_source="fetch_soilgrids")


@pytest.mark.asyncio
async def test_a_derivation_can_record_the_evidence_it_read() -> None:
    declared = (
        Param("k", door=doors.DERIVED,
              resolve=f"{__name__}._derived_with_evidence",
              bounds=(0.0, 1.0), consequence="physics", desc="a derived value"),
    )
    sheet = await resolve_params(declared, {})
    row = sheet.row("k")
    assert row.value == pytest.approx(0.25)
    assert row.note == "fitted from the sampled texture"
    assert row.real_source == "fetch_soilgrids"


@pytest.mark.asyncio
async def test_a_user_supplied_value_claims_no_real_source() -> None:
    """The declaration names where a DERIVED value comes from, not a typed one."""
    declared = (
        Param("k", door=doors.DERIVED,
              resolve=f"{__name__}._derived_with_evidence",
              bounds=(0.0, 1.0), consequence="physics",
              real_source="fetch_soilgrids", desc="a derived value"),
    )
    sheet = await resolve_params(declared, {"k": 0.5})
    row = sheet.row("k")
    assert row.basis == "user"
    assert row.real_source is None


@pytest.mark.asyncio
async def test_a_tiny_physics_value_survives_the_provenance_row() -> None:
    """Significant figures, not decimals: 9.3e-07 must not be reported as 0."""
    from trid3nt_server.declarative import provenance_entries

    declared = (
        Param("k", door=doors.SCENARIO, default=9.298175630928423e-07,
              bounds=(1e-9, 1.0), units="m/s", consequence="numerical",
              desc="conductivity"),
    )
    sheet = await resolve_params(declared, {})
    entry = provenance_entries(sheet, declared)[0]
    assert entry.value == pytest.approx(9.29818e-07, rel=1e-6)


@pytest.mark.asyncio
async def test_the_run_hands_back_the_sheet_it_actually_ran_on(card_client) -> None:
    """A form gate REVISES the sheet; the caller narrates from the revised one.

    The sheet the caller passed IN still holds the pre-review value - that is the
    whole defect: without ``RunResult.params`` the only sheet a narrator can reach
    reports the number the user REPLACED, while the solver ran on the approved one.
    """
    from trid3nt_server.declarative import FormGate, Workflow, interpret

    declared = (
        Param("q", door=doors.SCENARIO, default=1.0, bounds=(0.0, 100.0),
              consequence="scenario", desc="a reviewable value"),
    )
    sheet = await resolve_params(declared, {})
    plan = Workflow("t")[
        FormGate(title="review"),
        _echo_step(q=sheet.q),
    ]
    task = asyncio.ensure_future(
        interpret(plan, sheet, declared, (), input_mode="user_gated", resume=False))
    await answer_form_card(card_client, {"q": 7.5})
    result = await task

    assert sheet.get("q") == pytest.approx(1.0)
    assert result.params is not None
    assert result.params.get("q") == pytest.approx(7.5)
    assert result.params.row("q").basis == "user"
    # ... and the step downstream of the gate ran on the revised value.
    assert result.value == pytest.approx(7.5)


def _echo_step(**kwargs: Any) -> Any:
    from trid3nt_server.declarative import Step

    return Step(runner=f"{__name__}._echo", kwargs=kwargs)


async def _echo(*, q: float) -> float:
    return q


# --------------------------------------------------------------------------- #
# Law-9 refusals, per engine
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_budget_refuses_without_an_aoi() -> None:
    out = await budget_mod.modflow_regional_water_budget()
    assert out["error_code"] == "REGIONAL_WATER_BUDGET_INPUT_INVALID"


@pytest.mark.asyncio
async def test_aquifer_baseflow_refuses_with_no_site_and_no_column() -> None:
    out = await swmm_mod.swmm_aquifer_baseflow_to_node(input_mode="auto")
    assert out["status"] == "error"
    assert out["error_code"] == "SWMM_PHYSICS_INPUT_REQUIRED"
    assert "never invented" in out["error_message"]


# --------------------------------------------------------------------------- #
# The point memo caches FACTS, not failures
# --------------------------------------------------------------------------- #


class _Flaky:
    """An upstream that fails the first call and serves the second."""

    def __init__(self, fit: Any) -> None:
        self.fit, self.calls = fit, 0

    def __call__(self, lat: float, lon: float) -> tuple[Any, dict[str, Any]]:
        self.calls += 1
        if self.calls == 1:
            return None, {"reason": "HTTP 503 from SoilGrids"}
        return self.fit, {"sand_pct": 16.9, "clay_pct": 32.1, "depth": "5-15cm"}


@pytest.mark.asyncio
async def test_a_transient_soilgrids_failure_does_not_stick_to_the_modflow_aoi(
        monkeypatch) -> None:
    """Attempt 2 fetches AGAIN: a 503 is an upstream error, not a fact about soil."""
    import trid3nt_server.workflows.modflow.steps.aquifer as aquifer
    from trid3nt_server.workflows.modflow.steps.errors import (
        ModflowPhysicsInputRequired,
    )

    flaky = _Flaky(SimpleNamespace(k_m_s=9.298175630928423e-07, porosity=0.157))
    monkeypatch.setattr(aquifer, "derive_soil_k", flaky)
    aquifer._texture_fit.cache_clear()
    params = SimpleNamespace(aoi_latlon=(42.0176777, -93.6292127))

    with pytest.raises(ModflowPhysicsInputRequired, match="HTTP 503"):
        await aquifer.aquifer_k_ms(params)

    row = await aquifer.aquifer_k_ms(params)
    assert row.value == pytest.approx(9.298175630928423e-07)
    assert flaky.calls == 2
    # ... and the RESOLVED fit is remembered: the second param off the same point
    # costs no third fetch.
    assert (await aquifer.porosity(params)).value == pytest.approx(0.157)
    assert flaky.calls == 2
    aquifer._texture_fit.cache_clear()


@pytest.mark.asyncio
async def test_a_transient_soilgrids_failure_does_not_stick_to_the_swmm_site(
        monkeypatch) -> None:
    import trid3nt_server.workflows.swmm.steps.soil as soil
    from trid3nt_server.workflows.swmm.steps.errors import SwmmPhysicsInputRequired

    column = SimpleNamespace(porosity=0.4637, wilting_point=0.1963,
                             field_capacity=0.3568, conductivity_in_hr=0.1318)
    flaky = _Flaky(column)
    monkeypatch.setattr(soil, "derive_soil_column", flaky)
    monkeypatch.setattr(soil, "resolve_site", lambda params: (42.0176777, -93.6292127))
    soil._column.cache_clear()
    params = SimpleNamespace()

    with pytest.raises(SwmmPhysicsInputRequired, match="HTTP 503"):
        await soil.porosity(params)

    assert (await soil.porosity(params)).value == pytest.approx(0.4637)
    assert flaky.calls == 2
    assert (await soil.conductivity_in_hr(params)).value == pytest.approx(0.1318)
    assert flaky.calls == 2
    soil._column.cache_clear()


@pytest.mark.asyncio
async def test_aquifer_baseflow_refuses_when_soilgrids_cannot_serve(monkeypatch) -> None:
    import trid3nt_server.workflows.swmm.steps.soil as soil

    monkeypatch.setattr(soil, "_column",
                        lambda lat, lon: (None, {"reason": "off coverage"}))
    monkeypatch.setattr(soil, "resolve_site", lambda params: (40.0, -100.0))
    out = await swmm_mod.swmm_aquifer_baseflow_to_node(lat=40.0, lon=-100.0,
                                                       input_mode="auto")
    assert out["error_code"] == "SWMM_PHYSICS_INPUT_REQUIRED"
    assert "off coverage" in out["error_message"]
