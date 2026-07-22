"""Tests for the Case 2 groundwater-contamination composer (job-0228).

Coverage (kickoff acceptance):

  * Parameter extraction unit conversions:
      - gallons -> kg via contaminant density (TCE 1.46 kg/L).
      - hours -> days.
      - release_rate_kg_s = total_mass_kg / duration_seconds.
  * Plausibility clamps (release rate 1e-6..100 kg/s; duration 0.1..3650 d).
  * Confirmation-before-consequence gate BLOCKS the MODFLOW run without a
    confirm (fail-closed: no hook + confirmed=False -> ConfirmationDeniedError;
    a denying hook -> ConfirmationDeniedError; no run_modflow_job dispatch).
  * Full chain with run_modflow_job MOCKED — confirmed=True (or a proceeding
    hook) reaches the solver dispatch and returns a Case2Result with a non-zero
    plume summary.
  * Registration presence (PRIMARY_CATEGORY + TOOL_REGISTRY + FR-DC-6 metadata).

No Gemini/Vertex calls anywhere; the solver + geocode are dependency-injected
fakes through the registry seam.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from trid3nt_contracts.modflow_contracts import MODFLOWRunArgs, PlumeLayerURI
from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload

from trid3nt_server.tools import RegisteredTool, TOOL_REGISTRY
from trid3nt_server.tools.run_modflow_tool import _RUN_MODFLOW_JOB_METADATA
from trid3nt_server.workflows import model_groundwater_contamination_scenario as gw
from trid3nt_server.workflows.model_groundwater_contamination_scenario import (
    Case2Result,
    ConfirmationDeniedError,
    DURATION_MAX_DAYS,
    DURATION_MIN_DAYS,
    GroundwaterContaminationError,
    GroundwaterContaminationInputError,
    LITERS_PER_GALLON,
    ParameterExtractionError,
    RELEASE_RATE_MAX_KG_S,
    RELEASE_RATE_MIN_KG_S,
    extract_spill_parameters,
    model_groundwater_contamination_scenario,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "case2_news_article.txt"


# --------------------------------------------------------------------------- #
# Registry fakes (DI through the registry seam — no Gemini)
# --------------------------------------------------------------------------- #


def _fake_geocode(query: str, **_: Any) -> dict[str, Any]:
    """Return a Twin Falls, Idaho centroid for any 'Twin Falls' query."""
    return {
        "name": query,
        "bbox": [-114.55, 42.45, -114.35, 42.65],
        "latitude": 42.5630,
        "longitude": -114.4609,
        "source": "fake",
    }


def _install_fake_tool(name: str, fn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``TOOL_REGISTRY[name]`` with a fake whose ``.fn`` is ``fn``."""
    existing = TOOL_REGISTRY.get(name)
    metadata = existing.metadata if existing else _RUN_MODFLOW_JOB_METADATA
    monkeypatch.setitem(
        TOOL_REGISTRY,
        name,
        RegisteredTool(metadata=metadata, fn=fn, module="test"),
    )


def _fake_plume(area: float = 1.25, max_conc: float = 12.5) -> PlumeLayerURI:
    return PlumeLayerURI(
        layer_id="plume-concentration-TEST",
        name="Contaminant Plume (peak concentration)",
        layer_type="raster",
        uri="file:///tmp/plume.tif",
        style_preset="continuous_plume_concentration",
        role="primary",
        units="mg/L",
        max_concentration_mgl=max_conc,
        plume_area_km2=area,
    )


# --------------------------------------------------------------------------- #
# Unit conversions
# --------------------------------------------------------------------------- #


def test_extract_gallons_to_kg_via_density() -> None:
    """12,000 gallons of TCE (1.46 kg/L) -> ~66,320 kg, hours -> days."""
    text = (
        "A tanker spilled about 12,000 gallons of trichloroethylene (TCE) near "
        "Twin Falls, Idaho over roughly six hours on March 14, 2026."
    )
    d = extract_spill_parameters(text, geocode=False)
    assert d["contaminant"] == "trichloroethylene"
    assert d["contaminant_density_kg_l"] == pytest.approx(1.46)
    # 12000 gal * 3.785411784 L/gal * 1.46 kg/L
    expected_mass = 12000.0 * LITERS_PER_GALLON * 1.46
    assert d["total_mass_kg"] == pytest.approx(expected_mass, rel=1e-9)
    # six hours -> 0.25 d
    assert d["duration_days"] == pytest.approx(6.0 / 24.0)
    # rate = mass / (0.25 d * 86400 s/d)
    expected_rate = expected_mass / (0.25 * 86400.0)
    assert d["release_rate_kg_s"] == pytest.approx(expected_rate, rel=1e-9)
    assert d["clamps_applied"] == []


def test_extract_numeric_hours_and_days() -> None:
    """Numeric 'over 12 hours' and '3 days' both convert to days correctly."""
    t_hours = (
        "2,000 gallons of benzene leaked near Reno, Nevada over 12 hours "
        "on June 1, 2026."
    )
    d_hours = extract_spill_parameters(t_hours, geocode=False)
    assert d_hours["duration_days"] == pytest.approx(0.5)

    t_days = (
        "5,000 liters of toluene leaked near Boise, Idaho over 3 days "
        "on June 1, 2026."
    )
    d_days = extract_spill_parameters(t_days, geocode=False)
    assert d_days["duration_days"] == pytest.approx(3.0)
    # liters: 5000 L * 0.867 kg/L (toluene)
    assert d_days["total_mass_kg"] == pytest.approx(5000.0 * 0.867, rel=1e-9)


def test_barrels_conversion() -> None:
    """Barrels convert via 42-gallon barrels * density."""
    text = (
        "300 barrels of crude oil spilled near Casper, Wyoming over 4 hours "
        "on May 2, 2026."
    )
    d = extract_spill_parameters(text, geocode=False)
    # 300 bbl * 42 gal/bbl * 3.785411784 L/gal * 0.87 kg/L (crude oil)
    expected = 300.0 * 42.0 * LITERS_PER_GALLON * 0.87
    assert d["total_mass_kg"] == pytest.approx(expected, rel=1e-9)


# --------------------------------------------------------------------------- #
# Clamps
# --------------------------------------------------------------------------- #


def test_release_rate_clamp_high() -> None:
    """A huge, near-instant release clamps the rate to the 100 kg/s ceiling."""
    # 5 million gallons of TCE over 1 hour -> a rate far above 100 kg/s.
    text = (
        "5 million gallons of trichloroethylene spilled near Twin Falls, Idaho "
        "over 1 hour on March 14, 2026."
    )
    d = extract_spill_parameters(text, geocode=False)
    assert d["release_rate_kg_s"] == pytest.approx(RELEASE_RATE_MAX_KG_S)
    assert "release_rate" in d["clamps_applied"]
    # The raw (pre-clamp) value is preserved and is above the ceiling.
    assert d["release_rate_kg_s_raw"] > RELEASE_RATE_MAX_KG_S


def test_release_rate_clamp_low() -> None:
    """A tiny release over a long period clamps the rate up to the floor."""
    # 1 gallon over 365 days -> a rate far below 1e-6 kg/s.
    text = (
        "About 1 gallon of benzene seeped near Twin Falls, Idaho over 360 days "
        "starting January 1, 2026."
    )
    d = extract_spill_parameters(text, geocode=False)
    assert d["release_rate_kg_s"] == pytest.approx(RELEASE_RATE_MIN_KG_S)
    assert "release_rate" in d["clamps_applied"]
    assert d["release_rate_kg_s_raw"] < RELEASE_RATE_MIN_KG_S


def test_duration_clamp_floor_keeps_rate_in_band() -> None:
    """A duration below the 0.1 d floor clamps up; clamp is recorded."""
    # "over 1 hour" = 0.0417 d < 0.1 d floor.
    text = (
        "200 gallons of benzene spilled near Twin Falls, Idaho over 1 hour "
        "on March 14, 2026."
    )
    d = extract_spill_parameters(text, geocode=False)
    assert d["duration_days"] == pytest.approx(DURATION_MIN_DAYS)
    assert "duration" in d["clamps_applied"]
    assert DURATION_MIN_DAYS <= d["duration_days"] <= DURATION_MAX_DAYS


# --------------------------------------------------------------------------- #
# Required-field extraction failures
# --------------------------------------------------------------------------- #


def test_missing_contaminant_raises() -> None:
    text = "Something leaked near Twin Falls, Idaho over 6 hours on March 14, 2026."
    with pytest.raises(ParameterExtractionError):
        extract_spill_parameters(text, geocode=False)


def test_missing_duration_raises() -> None:
    text = "12,000 gallons of TCE spilled near Twin Falls, Idaho on March 14, 2026."
    with pytest.raises(ParameterExtractionError):
        extract_spill_parameters(text, geocode=False)


def test_missing_scale_raises() -> None:
    text = "TCE leaked near Twin Falls, Idaho over 6 hours on March 14, 2026."
    with pytest.raises(ParameterExtractionError):
        extract_spill_parameters(text, geocode=False)


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def test_requires_exactly_one_source() -> None:
    async def _run() -> None:
        with pytest.raises(GroundwaterContaminationInputError):
            await model_groundwater_contamination_scenario(confirmed=True)
        with pytest.raises(GroundwaterContaminationInputError):
            await model_groundwater_contamination_scenario(
                article_text="x", source_url="http://e", confirmed=True
            )

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# Confirmation gate (fail-closed)
# --------------------------------------------------------------------------- #


def test_confirmation_gate_blocks_without_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hook + confirmed=False -> ConfirmationDeniedError; solver NOT called."""
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)

    called = {"modflow": 0}

    def _spy_modflow(**_: Any) -> PlumeLayerURI:
        called["modflow"] += 1
        return _fake_plume()

    _install_fake_tool("run_modflow_job", _spy_modflow, monkeypatch)
    text = _FIXTURE.read_text()

    async def _run() -> None:
        with pytest.raises(ConfirmationDeniedError):
            await model_groundwater_contamination_scenario(
                article_text=text, confirmed=False, confirmation_hook=None
            )

    asyncio.run(_run())
    assert called["modflow"] == 0, "solver must NOT run when confirmation is denied"


def test_confirmation_hook_deny_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denying confirmation hook blocks the run (fail-closed)."""
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)
    called = {"modflow": 0}

    def _spy_modflow(**_: Any) -> PlumeLayerURI:
        called["modflow"] += 1
        return _fake_plume()

    _install_fake_tool("run_modflow_job", _spy_modflow, monkeypatch)
    text = _FIXTURE.read_text()

    seen: dict[str, Any] = {}

    async def _deny(env: PayloadWarningEnvelopePayload) -> bool:
        seen["envelope"] = env
        return False

    async def _run() -> None:
        with pytest.raises(ConfirmationDeniedError):
            await model_groundwater_contamination_scenario(
                article_text=text, confirmed=False, confirmation_hook=_deny
            )

    asyncio.run(_run())
    assert called["modflow"] == 0
    # The confirmation envelope was emitted to the hook and carries the params.
    env = seen["envelope"]
    assert isinstance(env, PayloadWarningEnvelopePayload)
    assert env.tool_name == "run_modflow_job"
    assert env.tool_args["contaminant"] == "trichloroethylene"
    assert env.tool_args["location_name"] == "Twin Falls, Idaho"
    # No cost theater: the only numbers are the structured forcing fields.
    assert env.estimated_mb == 0.0
    assert env.threshold_mb == 0.0


# --------------------------------------------------------------------------- #
# Full chain (solver mocked)
# --------------------------------------------------------------------------- #


def test_full_chain_confirmed_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """confirmed=True reaches the (mocked) solver and returns a Case2Result."""
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)
    captured: dict[str, Any] = {}

    def _fake_modflow(**kwargs: Any) -> PlumeLayerURI:
        captured.update(kwargs)
        # Validate the composer assembled a real MODFLOWRunArgs-shaped call.
        MODFLOWRunArgs(
            spill_location_latlon=kwargs["spill_location_latlon"],
            contaminant=kwargs["contaminant"],
            release_rate_kg_s=kwargs["release_rate_kg_s"],
            duration_days=kwargs["duration_days"],
        )
        return _fake_plume(area=2.5, max_conc=18.0)

    _install_fake_tool("run_modflow_job", _fake_modflow, monkeypatch)
    text = _FIXTURE.read_text()

    result = asyncio.run(
        model_groundwater_contamination_scenario(article_text=text, confirmed=True)
    )
    assert isinstance(result, Case2Result)
    # Non-zero plume summary (kickoff acceptance).
    assert result.summary["plume_area_km2"] == 2.5
    assert result.summary["max_concentration_mgl"] == 18.0
    assert result.summary["location_name"] == "Twin Falls, Idaho"
    assert result.summary["contaminant"] == "trichloroethylene"
    # The solver was called with the contaminant + a CONUS-plausible spill point.
    lat, lon = captured["spill_location_latlon"]
    assert 41.0 <= lat <= 49.0  # Idaho latitude band
    assert -117.5 <= lon <= -111.0  # Idaho longitude band
    # Release rate landed in the clamp band.
    assert RELEASE_RATE_MIN_KG_S <= captured["release_rate_kg_s"] <= RELEASE_RATE_MAX_KG_S
    # Plume layer is the typed numeric carrier (Invariant 1).
    assert isinstance(result.plume_layer, PlumeLayerURI)
    assert result.plume_layer.plume_area_km2 == 2.5


def test_full_chain_proceeding_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proceeding confirmation hook authorizes the run (no confirmed bypass)."""
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)
    _install_fake_tool("run_modflow_job", lambda **_: _fake_plume(), monkeypatch)
    text = _FIXTURE.read_text()

    async def _approve(_env: PayloadWarningEnvelopePayload) -> bool:
        return True

    result = asyncio.run(
        model_groundwater_contamination_scenario(
            article_text=text, confirmed=False, confirmation_hook=_approve
        )
    )
    assert isinstance(result, Case2Result)
    assert result.summary["plume_area_km2"] > 0


def test_solver_error_dict_surfaces_as_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_modflow_job returning an error dict surfaces a typed composer error."""
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)

    def _err_modflow(**_: Any) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": "MODFLOW_SOLVER_DIVERGED",
            "error_message": "list file reports a convergence failure",
        }

    _install_fake_tool("run_modflow_job", _err_modflow, monkeypatch)
    text = _FIXTURE.read_text()

    async def _run() -> None:
        with pytest.raises(GroundwaterContaminationError) as ei:
            await model_groundwater_contamination_scenario(
                article_text=text, confirmed=True
            )
        assert "MODFLOW_SOLVER_DIVERGED" in str(ei.value)

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_composer_registered_with_fr_dc6_metadata() -> None:
    """The LLM-facing wrapper is registered with workflow_dispatch metadata."""
    import trid3nt_server.workflows  # noqa: F401 — fire registration

    entry = TOOL_REGISTRY.get("run_model_groundwater_contamination_scenario")
    assert entry is not None
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"


def test_composer_in_hazard_modeling_category() -> None:
    from trid3nt_server.categories import PRIMARY_CATEGORY, SECONDARY_CATEGORIES

    assert (
        PRIMARY_CATEGORY["run_model_groundwater_contamination_scenario"]
        == "hazard_modeling"
    )
    assert "news_events" in SECONDARY_CATEGORIES[
        "run_model_groundwater_contamination_scenario"
    ]
