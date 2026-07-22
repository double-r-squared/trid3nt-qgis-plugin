"""Tests for the MODFLOW Wave-4 PRT capture-zone agent-side path.

Three layers, mirroring ``test_model_multi_species_scenario.py``:

  * ``CaptureZoneInputError`` honesty gate: a missing well raises the typed
    error (never fabricates a well location, Invariant 9).

  * The composer ``model_capture_zone_scenario`` full chain through a fake
    ``run_modflow_archetype_job`` (DI through the lazy import seam) returning a
    ``CaptureZoneLayerURI``, verifying the archetype + well threading and the
    typed result envelope.

  * The LLM-facing wrapper ``run_model_capture_zone_scenario`` and
    ``run_model_wellhead_protection_scenario`` map a missing well to a
    ``USER_INPUT_REQUIRED`` dict (Invariant 9 surfaced as a narrated error).

  * Tool registration: both wrappers appear in TOOL_REGISTRY with
    ``cacheable=False`` + ``ttl_class='live-no-cache'``.

No mf6 binary is needed: the solver chain is stubbed; the tests exercise the
composition, honesty gate, and registration seams only.
"""

from __future__ import annotations

from typing import Any

import pytest

from trid3nt_contracts.modflow_contracts import CaptureZoneLayerURI

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows import model_capture_zone_scenario as cz_mod
from trid3nt_server.workflows.model_capture_zone_scenario import (
    CAPTURE_ZONE_DEFAULT_TIERS,
    WELLHEAD_PROTECTION_DEFAULT_TIERS,
    CaptureZoneInputError,
    CaptureZoneResult,
    CaptureZoneScenarioError,
    model_capture_zone_scenario,
)


# --------------------------------------------------------------------------- #
# Shared fake helpers
# --------------------------------------------------------------------------- #


def _fake_cz_layer(*, area: float = 4.2, tiers: list[float] | None = None) -> CaptureZoneLayerURI:
    _tiers = tiers or [1.0, 5.0, 10.0]
    return CaptureZoneLayerURI(
        layer_id="capture-zone-TEST",
        name="Capture Zone (demo)",
        layer_type="vector",
        uri="file:///tmp/capture_zone_TEST.fgb",
        style_preset="capture_zone",
        role="primary",
        capture_zone_area_km2=area,
        travel_time_years=_tiers,
        isochrone_areas_km2={str(t): round(area * (i + 1) / len(_tiers), 4) for i, t in enumerate(_tiers)},
        particle_count=16,
    )


# --------------------------------------------------------------------------- #
# Honesty gate (Invariant 9): missing well raises CaptureZoneInputError
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_honesty_gate_missing_well_raises() -> None:
    """A call with well_location_latlon=None must raise CaptureZoneInputError
    immediately, before any geocode or solver step (Invariant 9)."""
    with pytest.raises(CaptureZoneInputError, match="well_location_latlon"):
        await model_capture_zone_scenario(
            aoi_latlon=(26.64, -81.87),
            well_location_latlon=None,
        )


@pytest.mark.asyncio
async def test_honesty_gate_message_never_invents() -> None:
    """The error message must explicitly state that the coordinates are never
    invented (Invariant 9 guard for the narration layer)."""
    with pytest.raises(CaptureZoneInputError) as exc_info:
        await model_capture_zone_scenario(
            aoi_latlon=(26.64, -81.87),
            well_location_latlon=None,
        )
    msg = str(exc_info.value).lower()
    # Must mention the prohibition on fabrication.
    assert "never" in msg or "never invented" in msg or "never fabricat" in msg


# --------------------------------------------------------------------------- #
# Default tier logic
# --------------------------------------------------------------------------- #


def test_capture_zone_default_tiers() -> None:
    assert CAPTURE_ZONE_DEFAULT_TIERS == [1.0, 5.0, 10.0]


def test_wellhead_protection_default_tiers() -> None:
    assert WELLHEAD_PROTECTION_DEFAULT_TIERS == [2.0, 5.0, 10.0]


# --------------------------------------------------------------------------- #
# Composer full chain (fake archetype-tool DI)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_composer_full_chain_capture_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake run tool returning a CaptureZoneLayerURI -> CaptureZoneResult with
    all required fields (Invariant 1: no free-generated numbers)."""
    captured: dict[str, Any] = {}
    fake_layer = _fake_cz_layer(area=3.7, tiers=[1.0, 5.0, 10.0])

    async def _fake_run(run_args: Any, **_kw: Any) -> CaptureZoneLayerURI:
        captured["run_args"] = run_args
        return fake_layer

    import trid3nt_server.tools.simulation.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _fake_run)

    result = await model_capture_zone_scenario(
        aoi_latlon=(26.64, -81.87),
        well_location_latlon=(26.62, -81.88),
        travel_time_years=[1.0, 5.0, 10.0],
        n_particles=16,
        archetype="capture_zone",
    )

    assert isinstance(result, CaptureZoneResult)
    # The run_args threaded the right archetype and well.
    assert captured["run_args"].archetype == "capture_zone"
    wlat, wlon = captured["run_args"].well_location_latlon
    assert abs(wlat - 26.62) < 0.001
    assert abs(wlon - (-81.88)) < 0.001
    # The typed layer carries typed scalars (Invariant 1: no fabricated numbers).
    assert isinstance(result.capture_zone_layer, CaptureZoneLayerURI)
    assert result.capture_zone_layer.capture_zone_area_km2 == pytest.approx(3.7)
    assert result.capture_zone_layer.travel_time_years == [1.0, 5.0, 10.0]
    assert result.capture_zone_layer.particle_count == 16
    # The summary mirrors the typed layer fields.
    assert result.summary["capture_zone_area_km2"] == pytest.approx(3.7)
    assert result.summary["travel_time_years"] == [1.0, 5.0, 10.0]
    assert "demo_aquifer_caveat" in result.summary


@pytest.mark.asyncio
async def test_composer_full_chain_wellhead_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wellhead_protection archetype threads the EPA default tiers when none
    are supplied, and uses the 'wellhead_protection' archetype string."""
    captured: dict[str, Any] = {}
    fake_layer = _fake_cz_layer(area=6.1, tiers=[2.0, 5.0, 10.0])

    async def _fake_run(run_args: Any, **_kw: Any) -> CaptureZoneLayerURI:
        captured["run_args"] = run_args
        return fake_layer

    import trid3nt_server.tools.simulation.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _fake_run)

    result = await model_capture_zone_scenario(
        aoi_latlon=(26.64, -81.87),
        well_location_latlon=(26.62, -81.88),
        travel_time_years=None,  # should default to [2, 5, 10]
        archetype="wellhead_protection",
    )

    assert captured["run_args"].archetype == "wellhead_protection"
    # Default tiers for wellhead_protection are [2, 5, 10].
    assert captured["run_args"].capture_zone_travel_time_years == [2.0, 5.0, 10.0]
    assert isinstance(result.capture_zone_layer, CaptureZoneLayerURI)


@pytest.mark.asyncio
async def test_composer_surfaces_run_error_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed run dict from the archetype tool re-raises as a typed scenario
    error (the honesty floor: a failed run never reads as a successful layer)."""
    async def _err_run(run_args: Any, **_kw: Any) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": "CAPTURE_ZONE_RUN_FAILED",
            "error_message": "PRT backward tracking produced no pathlines",
        }

    import trid3nt_server.tools.simulation.run_modflow_archetype_tool as _tool

    monkeypatch.setattr(_tool, "run_modflow_archetype_job", _err_run)

    with pytest.raises(CaptureZoneScenarioError, match="RUN_FAILED"):
        await model_capture_zone_scenario(
            aoi_latlon=(26.64, -81.87),
            well_location_latlon=(26.62, -81.88),
        )


@pytest.mark.asyncio
async def test_composer_rejects_invalid_archetype() -> None:
    """An unrecognised archetype string raises CaptureZoneInputError immediately
    (before any network or solver call)."""
    with pytest.raises(CaptureZoneInputError, match="archetype"):
        await model_capture_zone_scenario(
            aoi_latlon=(26.64, -81.87),
            well_location_latlon=(26.62, -81.88),
            archetype="plume",  # not a valid capture-zone archetype
        )


# --------------------------------------------------------------------------- #
# LLM-facing wrappers: USER_INPUT_REQUIRED on missing well
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wrapper_missing_well_returns_user_input_required() -> None:
    """The LLM-facing wrapper maps a missing well to USER_INPUT_REQUIRED
    (Invariant 9: the tool never fabricates a well location)."""
    out = await cz_mod.run_model_capture_zone_scenario(
        aoi_latlon=[26.64, -81.87],
        well_location_latlon=None,
    )
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"
    assert "well" in out["error_message"].lower()


@pytest.mark.asyncio
async def test_whpa_wrapper_missing_well_returns_user_input_required() -> None:
    """The wellhead_protection LLM wrapper also maps a missing well to
    USER_INPUT_REQUIRED."""
    out = await cz_mod.run_model_wellhead_protection_scenario(
        aoi_latlon=[26.64, -81.87],
        well_location_latlon=None,
    )
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"


# --------------------------------------------------------------------------- #
# Registration: both tools in TOOL_REGISTRY with correct metadata
# --------------------------------------------------------------------------- #


def test_capture_zone_registered_uncacheable() -> None:
    import trid3nt_server.tools  # noqa: F401 - fires registration side-effects

    entry = TOOL_REGISTRY.get("run_model_capture_zone_scenario")
    assert entry is not None, "run_model_capture_zone_scenario not in TOOL_REGISTRY"
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"


def test_wellhead_protection_registered_uncacheable() -> None:
    import trid3nt_server.tools  # noqa: F401

    entry = TOOL_REGISTRY.get("run_model_wellhead_protection_scenario")
    assert entry is not None, "run_model_wellhead_protection_scenario not in TOOL_REGISTRY"
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"
