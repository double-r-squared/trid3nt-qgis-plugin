"""Tests for the which-farm-fields composer (ftw-affected-fields demo, S3).

Mirrors ``test_model_groundwater_contamination_scenario.py``:

  * AOI resolution: explicit bbox -> centroid; location_query -> geocode bbox.
  * Up-gradient placement: the auto-placed spill is WEST of the field centroid
    (the demo deck's west->east gradient); an explicit point overrides it.
  * Confirmation-before-consequence gate BLOCKS the MODFLOW run without a
    confirm (fail-closed: no hook + confirmed=False -> denied; a denying hook ->
    denied; no run_modflow_job dispatch).
  * Full chain with run_modflow_job + fetch_field_boundaries +
    analyze_affected_fields MOCKED through the registry -> AffectedFieldsResult
    carrying the ranked readout + both layers + the spill point.
  * FTW no-coverage surfaces honestly as the typed no-coverage error.
  * Registration presence (PRIMARY/SECONDARY category + TOOL_REGISTRY metadata).

No Gemini / Vertex / Batch / mf6: the solver + geocode + FTW fetch + analysis
are dependency-injected fakes through the registry seam.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grace2_contracts.execution import LayerURI
from grace2_contracts.modflow_contracts import MODFLOWRunArgs, PlumeLayerURI
from grace2_contracts.payload_warning import PayloadWarningEnvelopePayload

from grace2_agent.tools import RegisteredTool, TOOL_REGISTRY
from grace2_agent.workflows.model_contamination_affected_fields import (
    AffectedFieldsResult,
    ContaminationAffectedFieldsConfirmationDeniedError,
    ContaminationAffectedFieldsGeocodeError,
    ContaminationAffectedFieldsInputError,
    ContaminationAffectedFieldsNoCoverageError,
    DEFAULT_UPGRADIENT_OFFSET_KM,
    model_contamination_affected_fields,
    place_spill_up_gradient,
    resolve_aoi_bbox,
)


# --------------------------------------------------------------------------- #
# Registry fakes (DI through the registry seam — no Gemini)
# --------------------------------------------------------------------------- #

# Ames, Iowa cropland AOI (inside FTW US coverage).
_AMES_BBOX = [-93.70, 42.00, -93.60, 42.08]
_AMES_LAT = 42.04
_AMES_LON = -93.65


def _fake_geocode(query: str, **_: Any) -> dict[str, Any]:
    return {
        "name": query,
        "bbox": list(_AMES_BBOX),
        "latitude": _AMES_LAT,
        "longitude": _AMES_LON,
        "source": "fake",
    }


def _fake_plume(area: float = 1.5, max_conc: float = 15.0) -> PlumeLayerURI:
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


def _fake_fields_layer() -> LayerURI:
    return LayerURI(
        layer_id="ftw-fields-us_usda_cropland-TEST",
        name="Field Boundaries — US Cropland",
        layer_type="vector",
        uri="file:///tmp/fields.fgb",
        style_preset="field_boundaries",
        role="context",
        units=None,
    )


def _fake_affected(**_: Any) -> dict[str, Any]:
    return {
        "affected_fields": [
            {"field_id": 2, "crop_name": "corn", "max_concentration_mgl": 12.5,
             "mean_concentration_mgl": 6.0, "area_km2": 0.42},
            {"field_id": 5, "crop_name": "soybeans", "max_concentration_mgl": 3.1,
             "mean_concentration_mgl": 1.2, "area_km2": 0.30},
        ],
        "n_fields_total": 9,
        "n_fields_affected": 2,
        "affected_area_km2": 0.72,
        "threshold_mgl": 0.001,
        "rank_by": "peak",
        "worst_field": {"field_id": 2, "crop_name": "corn",
                        "max_concentration_mgl": 12.5, "area_km2": 0.42},
        "headline": "2 farm fields affected, 0.72 km2 of cropland over the "
                    "0.001 mg/L threshold; worst-hit field 2 (corn) at 12.5 mg/L.",
        "units": "mg/L",
        "computed_at": "2026-06-23T00:00:00+00:00",
    }


def _install_fake_tool(name: str, fn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = TOOL_REGISTRY.get(name)
    assert existing is not None, f"{name} must be registered to fake it"
    monkeypatch.setitem(
        TOOL_REGISTRY,
        name,
        RegisteredTool(metadata=existing.metadata, fn=fn, module="test"),
    )


def _install_happy_path(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)

    def _fake_modflow(**kwargs: Any) -> PlumeLayerURI:
        captured["modflow"] = kwargs
        MODFLOWRunArgs(
            spill_location_latlon=kwargs["spill_location_latlon"],
            contaminant=kwargs["contaminant"],
            release_rate_kg_s=kwargs["release_rate_kg_s"],
            duration_days=kwargs["duration_days"],
        )
        return _fake_plume()

    def _fake_fetch(**kwargs: Any) -> LayerURI:
        captured["fetch_fields"] = kwargs
        return _fake_fields_layer()

    def _fake_analyze(**kwargs: Any) -> dict[str, Any]:
        captured["analyze"] = kwargs
        return _fake_affected()

    _install_fake_tool("run_modflow_job", _fake_modflow, monkeypatch)
    _install_fake_tool("fetch_field_boundaries", _fake_fetch, monkeypatch)
    _install_fake_tool("analyze_affected_fields", _fake_analyze, monkeypatch)


# --------------------------------------------------------------------------- #
# AOI resolution + up-gradient placement (pure)
# --------------------------------------------------------------------------- #


def test_resolve_aoi_from_bbox() -> None:
    b, centroid = resolve_aoi_bbox(_AMES_BBOX, None)
    assert b == (-93.70, 42.00, -93.60, 42.08)
    # centroid = (mid_lat, mid_lon)
    assert centroid[0] == pytest.approx(42.04)
    assert centroid[1] == pytest.approx(-93.65)


def test_resolve_aoi_requires_a_source() -> None:
    with pytest.raises(ContaminationAffectedFieldsInputError):
        resolve_aoi_bbox(None, None)


def test_resolve_aoi_geocode(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tool("geocode_location", _fake_geocode, monkeypatch)
    b, centroid = resolve_aoi_bbox(None, "Ames, Iowa")
    assert b == (-93.70, 42.00, -93.60, 42.08)
    assert centroid == (_AMES_LAT, _AMES_LON)


def test_resolve_aoi_geocode_no_bbox_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_geocode(_q: str, **_: Any) -> dict[str, Any]:
        return {"name": "x", "bbox": None}

    _install_fake_tool("geocode_location", _bad_geocode, monkeypatch)
    with pytest.raises(ContaminationAffectedFieldsGeocodeError):
        resolve_aoi_bbox(None, "Nowhere")


def test_up_gradient_is_west_of_centroid() -> None:
    """The auto-placed spill is WEST (smaller lon), same lat, by ~offset km."""
    centroid = (42.04, -93.65)
    spill = place_spill_up_gradient(centroid, DEFAULT_UPGRADIENT_OFFSET_KM)
    assert spill[0] == pytest.approx(42.04)  # latitude unchanged
    assert spill[1] < -93.65  # WEST = smaller longitude
    # ~3 km west at 42N is ~0.036 deg lon.
    assert spill[1] == pytest.approx(-93.65 - 0.0363, abs=2e-3)


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def test_requires_an_aoi() -> None:
    async def _run() -> None:
        with pytest.raises(ContaminationAffectedFieldsInputError):
            await model_contamination_affected_fields(confirmed=True)

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# Confirmation gate (fail-closed)
# --------------------------------------------------------------------------- #


def test_confirmation_gate_blocks_without_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _install_happy_path(monkeypatch, captured)

    async def _run() -> None:
        with pytest.raises(ContaminationAffectedFieldsConfirmationDeniedError):
            await model_contamination_affected_fields(
                bbox=_AMES_BBOX, confirmed=False, confirmation_hook=None
            )

    asyncio.run(_run())
    assert "modflow" not in captured, "solver must NOT run when confirm denied"


def test_confirmation_hook_deny_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _install_happy_path(monkeypatch, captured)
    seen: dict[str, Any] = {}

    async def _deny(env: PayloadWarningEnvelopePayload) -> bool:
        seen["envelope"] = env
        return False

    async def _run() -> None:
        with pytest.raises(ContaminationAffectedFieldsConfirmationDeniedError):
            await model_contamination_affected_fields(
                bbox=_AMES_BBOX, confirmed=False, confirmation_hook=_deny
            )

    asyncio.run(_run())
    assert "modflow" not in captured
    env = seen["envelope"]
    assert isinstance(env, PayloadWarningEnvelopePayload)
    assert env.tool_name == "run_modflow_job"
    assert env.estimated_mb == 0.0
    assert env.threshold_mb == 0.0


# --------------------------------------------------------------------------- #
# Full chain (all sub-tools mocked)
# --------------------------------------------------------------------------- #


def test_full_chain_confirmed_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """confirmed=True drives the full chain -> AffectedFieldsResult."""
    captured: dict[str, Any] = {}
    _install_happy_path(monkeypatch, captured)

    result = asyncio.run(
        model_contamination_affected_fields(
            bbox=_AMES_BBOX,
            contaminant="trichloroethylene",
            release_rate_kg_s=0.05,
            duration_days=1.0,
            confirmed=True,
        )
    )
    assert isinstance(result, AffectedFieldsResult)
    # The plume + fields layers are carried through.
    assert isinstance(result.plume_layer, PlumeLayerURI)
    assert isinstance(result.fields_layer, LayerURI)
    # The ranked affected readout is the analysis output.
    assert result.affected["n_fields_affected"] == 2
    assert result.summary["n_fields_affected"] == 2
    assert result.summary["worst_field"]["crop_name"] == "corn"
    assert result.summary["plume_area_km2"] == 1.5
    assert result.summary["max_concentration_mgl"] == 15.0
    # The spill was placed up-gradient (WEST) of the AOI centroid.
    assert result.spill_location_latlon[1] < -93.65
    # The solver saw the up-gradient spill point + the contaminant.
    assert captured["modflow"]["contaminant"] == "trichloroethylene"
    assert captured["modflow"]["spill_location_latlon"][1] < -93.65
    # The FTW fetch saw the resolved AOI bbox.
    assert tuple(captured["fetch_fields"]["bbox"]) == (-93.70, 42.00, -93.60, 42.08)
    # The analysis saw the plume URI + the fields URI.
    assert captured["analyze"]["plume_layer_uri"] == "file:///tmp/plume.tif"
    assert captured["analyze"]["fields_layer_uri"] == "file:///tmp/fields.fgb"


def test_full_chain_proceeding_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proceeding hook authorizes the run (no confirmed bypass)."""
    captured: dict[str, Any] = {}
    _install_happy_path(monkeypatch, captured)

    async def _approve(_env: PayloadWarningEnvelopePayload) -> bool:
        return True

    result = asyncio.run(
        model_contamination_affected_fields(
            bbox=_AMES_BBOX, confirmed=False, confirmation_hook=_approve
        )
    )
    assert isinstance(result, AffectedFieldsResult)
    assert "modflow" in captured


def test_explicit_spill_overrides_placement(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit spill point overrides the auto up-gradient placement."""
    captured: dict[str, Any] = {}
    _install_happy_path(monkeypatch, captured)

    result = asyncio.run(
        model_contamination_affected_fields(
            bbox=_AMES_BBOX,
            spill_location_latlon=(42.10, -93.80),
            confirmed=True,
        )
    )
    assert result.spill_location_latlon == [42.10, -93.80]
    assert captured["modflow"]["spill_location_latlon"] == (42.10, -93.80)


def test_no_coverage_surfaces_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    """An FTW FIELDS_NO_COVERAGE error becomes the typed no-coverage error."""
    captured: dict[str, Any] = {}
    _install_happy_path(monkeypatch, captured)

    class _NoCoverage(RuntimeError):
        error_code = "FIELDS_NO_COVERAGE"

    def _no_coverage(**_: Any) -> LayerURI:
        raise _NoCoverage("no FTW coverage here")

    _install_fake_tool("fetch_field_boundaries", _no_coverage, monkeypatch)

    async def _run() -> None:
        with pytest.raises(ContaminationAffectedFieldsNoCoverageError):
            await model_contamination_affected_fields(
                bbox=[10.0, 0.0, 10.1, 0.1], confirmed=True
            )

    asyncio.run(_run())
    # The plume ran but no fields were fabricated.
    assert "modflow" in captured
    assert "analyze" not in captured


def test_solver_error_dict_surfaces_as_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _install_happy_path(monkeypatch, captured)

    def _err_modflow(**_: Any) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": "MODFLOW_SOLVER_DIVERGED",
            "error_message": "convergence failure",
        }

    _install_fake_tool("run_modflow_job", _err_modflow, monkeypatch)

    async def _run() -> None:
        from grace2_agent.workflows.model_contamination_affected_fields import (
            ContaminationAffectedFieldsError,
        )

        with pytest.raises(ContaminationAffectedFieldsError) as ei:
            await model_contamination_affected_fields(
                bbox=_AMES_BBOX, confirmed=True
            )
        assert "MODFLOW_SOLVER_DIVERGED" in str(ei.value)

    asyncio.run(_run())


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_composer_registered_with_fr_dc6_metadata() -> None:
    import grace2_agent.workflows  # noqa: F401 — fire registration

    entry = TOOL_REGISTRY.get("run_model_contamination_affected_fields")
    assert entry is not None
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"


def test_composer_in_categories() -> None:
    from grace2_agent.categories import PRIMARY_CATEGORY, SECONDARY_CATEGORIES

    assert (
        PRIMARY_CATEGORY["run_model_contamination_affected_fields"]
        == "hazard_modeling"
    )
    secondary = SECONDARY_CATEGORIES["run_model_contamination_affected_fields"]
    assert "damage_assessment" in secondary
    assert "land_cover_development" in secondary


def test_composer_in_solver_confirm_set() -> None:
    from grace2_agent.server import SOLVER_CONFIRM_TOOLS

    assert "run_model_contamination_affected_fields" in SOLVER_CONFIRM_TOOLS
