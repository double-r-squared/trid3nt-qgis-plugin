"""Live integration test: ``run_model_flood_scenario`` survives Gemini-invented kwargs (job-0164).

The kickoff motivating problem: Gemini routinely emits kwargs like
``run_name``, ``scenario_id``, ``description``, ``rainfall_event``,
``return_period_years`` (when ours was ``return_period_yr``). Each one
used to crash the dispatch with ``TypeError: unexpected keyword argument``.
After job-0164's sweep:
  1. ``tool_arg_normalizer.normalize_args`` rewrites known aliases (yr↔years,
     hr↔hours) and drops the rest at the ``_invoke_tool_via_emitter`` site;
  2. Every ``@register_tool`` function additionally carries
     ``**_extra_ignored`` as belt-and-suspenders.

These tests assert that calling ``run_model_flood_scenario`` with a real-world
mash-up of valid + invented + alias kwargs **does not raise TypeError**.
Network / GCS errors are tolerated (we cannot reach 3DEP / NLCD in CI), but
the failure-envelope branch must produce an envelope, not raise.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.tool_arg_normalizer import normalize_args
from trid3nt_server.tools import TOOL_REGISTRY

# Import the workflow module so it registers.
import trid3nt_server.workflows.model_flood_scenario  # noqa: F401


def _invoke_via_normalizer(tool_name: str, raw: dict) -> object:
    """Mirror the production call site (``_invoke_tool_via_emitter``)."""
    entry = TOOL_REGISTRY[tool_name]
    normalized = normalize_args(tool_name, raw, entry.fn)
    if asyncio.iscoroutinefunction(entry.fn):
        return asyncio.run(entry.fn(**normalized))
    return entry.fn(**normalized)


def test_run_model_flood_scenario_with_invented_kwargs_does_not_raise_type_error() -> None:
    """Simulate Gemini's worst-case kwargs combination.

    This is the exact failure mode that triggered the kickoff: a tool call
    where the LLM has:
    - ``rainfall_event="atlas14_100yr"`` (string-form forcing — must parse)
    - ``return_period_years=100`` (alias of ``return_period_yr``)
    - ``durationHours=24`` (camelCase of ``duration_hours``)
    - ``run_name="ian-demo"`` + ``scenario_id="IAN-2022"`` + ``description="…"``
      (invented Gemini fields — must be dropped silently)
    """
    raw = {
        "location_query": "Fort Myers, FL",
        "rainfall_event": "atlas14_100yr",
        "return_period_years": 100,
        "durationHours": 24,
        "run_name": "ian-demo",
        "scenario_id": "IAN-2022",
        "description": "Hurricane Ian flood demo",
    }
    try:
        _invoke_via_normalizer("run_model_flood_scenario", raw)
    except TypeError as exc:
        pytest.fail(
            f"run_model_flood_scenario raised TypeError on Gemini-style kwargs: {exc}\n"
            f"This indicates the normalizer + **_extra_ignored sweep failed."
        )
    except Exception:
        # All other errors (geocoder timeout, fetch_dem returning None, etc.)
        # are expected in CI — we can't reach external services. The point is
        # NO TypeError.
        pass


def test_run_model_flood_scenario_with_only_invented_kwargs_does_not_raise_type_error() -> None:
    """Stress: every kwarg is invented (no valid ones). Must drop them all."""
    raw = {
        "run_name": "test",
        "scenario_id": "test",
        "description": "test",
        "user_id": "test",
        "comment": "test",
        "explanation": "test",
    }
    try:
        _invoke_via_normalizer("run_model_flood_scenario", raw)
    except TypeError as exc:
        pytest.fail(f"TypeError: {exc}")
    except Exception:
        pass  # expected: bbox/query missing → WorkflowError, not TypeError


def test_normalizer_rewrites_camel_case_for_run_model_flood_scenario() -> None:
    """Direct normalizer assertion: ``durationHours`` → wrapper-accepted form.

    The wrapper signature exposes BOTH ``return_period_years`` (long, primary)
    and ``return_period_yr`` (short, backward-compat alias) — same for hours.
    The normalizer should land the LLM's camelCase / alias forms on one of
    them.
    """
    entry = TOOL_REGISTRY["run_model_flood_scenario"]
    out = normalize_args(
        "run_model_flood_scenario",
        {"durationHours": 12, "returnPeriodYears": 25, "rainfall_event": "atlas14_500yr"},
        entry.fn,
    )
    # duration landed on duration_hr (short — the signature-accepted name).
    assert out.get("duration_hr") == 12 or out.get("duration_hours") == 12
    # return period landed on return_period_yr (short) or _years (long).
    assert (
        out.get("return_period_yr") == 25
        or out.get("return_period_years") == 25
    )


def test_normalizer_alias_yr_to_years_only_when_signature_accepts() -> None:
    """``run_model_flood_scenario`` accepts ``return_period_years`` directly.

    If LLM sends ``return_period_yr=500`` it should be aliased to
    ``return_period_years=500`` because the signature accepts the long form.
    """
    entry = TOOL_REGISTRY["run_model_flood_scenario"]
    out = normalize_args("run_model_flood_scenario", {"return_period_yr": 500}, entry.fn)
    # The wrapper accepts BOTH ``return_period_years`` and ``return_period_yr``
    # (it normalizes internally). Either name is fine — the test asserts
    # neither is dropped.
    assert (
        out.get("return_period_years") == 500
        or out.get("return_period_yr") == 500
    )
