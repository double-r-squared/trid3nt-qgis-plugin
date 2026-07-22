"""Unit tests for ``compute_impact_envelope`` workflow (Wave 4.11 P3).

Coverage maps to the kickoff's 8-test minimum:

1. ``test_registered_in_tool_registry`` — composer appears in TOOL_REGISTRY
   with the workflow_dispatch metadata shape.
2. ``test_signature_matches_kickoff`` — function signature exposes the four
   irreducible inputs (flood_layer_uri, bbox|location_query,
   structure_inventory_source, fragility_set) + **_extra_ignored.
3. ``test_chains_geocode_then_inventory_then_pelicun_then_postprocess`` — happy
   path with all sub-tools mocked; assert each is called once with the
   expected args.
4. ``test_handles_postprocess_pelicun_error_propagates`` — when
   ``postprocess_pelicun`` raises ``PelicunPostprocessError``, the composer
   surfaces ``ComputeImpactEnvelopePostprocessError`` with
   ``error_code="POSTPROCESS_FAILED"``.
5. ``test_narrative_string_contains_count_and_dollar`` — snapshot check on
   the headline-metric chat narration: count, ``$``-prefixed loss amount,
   and population-at-high-risk segment.
6. ``test_metadata_category_damage_assessment`` — composer metadata
   declares the ``damage_assessment`` category (or its workflow_dispatch
   equivalent) and the read-only / open-world / destructive / idempotent
   hints requested by the kickoff.
7. ``test_extra_kwargs_swallowed`` — Gemini hallucination guard: arbitrary
   extra kwargs do not raise TypeError.
8. ``test_input_error_on_missing_flood_layer_uri`` — empty / None /
   non-string ``flood_layer_uri`` raises
   ``ComputeImpactEnvelopeInputError``.

Plus two ancillary tests:

9. ``test_input_error_when_neither_bbox_nor_location_query`` — both bbox
   and location_query omitted → ``ComputeImpactEnvelopeInputError``.
10. ``test_ms_buildings_path_routes_through_run_pelicun_with_buildings`` —
    ``structure_inventory_source="MS_BUILDINGS"`` bypasses ``fetch_usace_nsi``
    and uses ``run_pelicun_with_buildings`` directly.
11. ``test_nsi_fetch_failure_raises_typed_error`` — fetcher exception → typed
    ``ComputeImpactEnvelopeNSIFetchError`` with ``error_code="NSI_FETCH_FAILED"``.
12. ``test_pelicun_failure_raises_typed_error`` — Pelicun exception → typed
    ``ComputeImpactEnvelopePelicunError`` with ``error_code="PELICUN_UPSTREAM_FAILED"``.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.postprocess_pelicun import (
    PelicunPostprocessEmptyError,
)
from grace2_agent.workflows.compute_impact_envelope import (
    ComputeImpactEnvelopeError,
    ComputeImpactEnvelopeGeocodeError,
    ComputeImpactEnvelopeInputError,
    ComputeImpactEnvelopeNSIFetchError,
    ComputeImpactEnvelopePelicunError,
    ComputeImpactEnvelopePostprocessError,
    compute_impact_envelope,
)


# --------------------------------------------------------------------------- #
# Fixtures.
# --------------------------------------------------------------------------- #


_FLOOD_URI = "s3://trid3nt-runs/01TEST/flood_depth_peak.tif"
_NSI_URI = (
    "s3://trid3nt-cache/cache/static-30d/usace_nsi/abc123.fgb"
)
_DAMAGE_URI = (
    "s3://trid3nt-cache/cache/static-30d/pelicun_damage/def456.fgb"
)
_FT_MYERS_BBOX = (-81.92, 26.55, -81.80, 26.68)


def _mock_envelope_dict(
    *,
    n_total: int = 100,
    n_damaged: int = 42,
    n_destroyed: int = 3,
    expected_loss_usd: float = 5_678_900.0,
    p95_loss_usd: float = 8_456_300.0,
    pop_total: int | None = 1_234,
    pop_displaced: int | None = 250,
    pop_high_risk: int | None = 567,
    impact_area_km2: float = 12.4,
    inventory_source: str = "USACE_NSI",
) -> dict[str, Any]:
    """Build a fake ``ImpactEnvelope.model_dump(mode='json')`` dict."""
    return {
        "schema_version": "v1",
        "n_structures_total": n_total,
        "n_structures_damaged": n_damaged,
        "n_structures_destroyed": n_destroyed,
        "damage_state_distribution": {
            "DS0_none": n_total - n_damaged,
            "DS1_slight": max(n_damaged - n_destroyed - 5, 0),
            "DS2_moderate": 3,
            "DS3_extensive": 2,
            "DS4_complete": n_destroyed,
        },
        "total_replacement_value_usd": 25_000_000.0,
        "damaged_replacement_value_usd": 10_500_000.0,
        "expected_loss_usd": expected_loss_usd,
        "loss_percentile_95_usd": p95_loss_usd,
        "population_total": pop_total,
        "population_displaced": pop_displaced,
        "population_at_high_risk": pop_high_risk,
        "impact_area_km2": impact_area_km2,
        "bbox": list(_FT_MYERS_BBOX),
        "by_occupancy_class": {
            "RES1": {
                "n_structures": n_total,
                "n_damaged": n_damaged,
                "n_destroyed": n_destroyed,
                "expected_loss_usd": expected_loss_usd,
                "loss_percentile_95_usd": p95_loss_usd,
                "population": pop_total,
                "population_displaced": pop_displaced,
            }
        },
        "pelicun_run_id": "01HTESTPELICUNRUNID0000000000",
        "damage_layer_uri": _DAMAGE_URI,
        "structure_inventory_source": inventory_source,
        "flood_layer_uri": _FLOOD_URI,
        "fragility_set": "hazus_flood_v6",
        "realization_count": 100,
        "generated_at": "2026-06-09T12:00:00+00:00",
    }


def _mock_layer_uri(uri: str) -> MagicMock:
    """Build a ``LayerURI``-like duck-type with a ``.uri`` attribute."""
    m = MagicMock()
    m.uri = uri
    return m


# --------------------------------------------------------------------------- #
# Test 1 — registration
# --------------------------------------------------------------------------- #


def test_registered_in_tool_registry() -> None:
    """The composer is registered in TOOL_REGISTRY with workflow_dispatch metadata."""
    assert "compute_impact_envelope" in TOOL_REGISTRY, (
        f"compute_impact_envelope not in TOOL_REGISTRY; keys={sorted(TOOL_REGISTRY)}"
    )
    entry = TOOL_REGISTRY["compute_impact_envelope"]
    assert entry.metadata.name == "compute_impact_envelope"
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.metadata.supports_global_query is False
    assert entry.fn is compute_impact_envelope


# --------------------------------------------------------------------------- #
# Test 2 — signature
# --------------------------------------------------------------------------- #


def test_signature_matches_kickoff() -> None:
    """Function signature exposes the four irreducible inputs + **_extra_ignored."""
    sig = inspect.signature(compute_impact_envelope)
    params = sig.parameters

    assert "flood_layer_uri" in params
    assert "bbox" in params
    assert "location_query" in params
    assert "structure_inventory_source" in params
    assert "fragility_set" in params

    # flood_layer_uri is the only required positional (no default).
    assert params["flood_layer_uri"].default is inspect.Parameter.empty
    # All others carry defaults.
    assert params["bbox"].default is None
    assert params["location_query"].default is None
    assert params["structure_inventory_source"].default == "USACE_NSI"
    assert params["fragility_set"].default is None

    # **_extra_ignored absorbs Gemini-invented kwargs.
    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    assert has_var_keyword, "expected **_extra_ignored on the signature"


# --------------------------------------------------------------------------- #
# Test 3 — happy-path chaining
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_chains_geocode_then_inventory_then_pelicun_then_postprocess() -> None:
    """Mocked sub-tools fire in order: geocode → nsi → pelicun → postprocess."""
    geocode_result = {
        "name": "Fort Myers, FL, USA",
        "bbox": list(_FT_MYERS_BBOX),
        "latitude": 26.62,
        "longitude": -81.87,
        "source": "nominatim",
        "osm_type": "relation",
        "osm_id": 12345,
        "place_id": 67890,
    }
    nsi_layer = _mock_layer_uri(_NSI_URI)
    damage_layer = _mock_layer_uri(_DAMAGE_URI)
    envelope_dict = _mock_envelope_dict()

    nsi_mock = MagicMock(return_value=nsi_layer)
    pelicun_mock = MagicMock(return_value=damage_layer)
    postprocess_mock = AsyncMock(return_value=envelope_dict)

    # Tweak TOOL_REGISTRY entries' .fn for the duration of the test.
    nsi_orig = TOOL_REGISTRY["fetch_usace_nsi"]
    pelicun_orig = TOOL_REGISTRY["run_pelicun_damage_assessment"]

    fake_nsi = type(nsi_orig)(metadata=nsi_orig.metadata, fn=nsi_mock, module=nsi_orig.module)
    fake_pelicun = type(pelicun_orig)(
        metadata=pelicun_orig.metadata, fn=pelicun_mock, module=pelicun_orig.module
    )

    with (
        patch.dict(
            TOOL_REGISTRY,
            {
                "fetch_usace_nsi": fake_nsi,
                "run_pelicun_damage_assessment": fake_pelicun,
            },
        ),
        patch(
            "grace2_agent.workflows.compute_impact_envelope.geocode_location",
            return_value=geocode_result,
        ) as geocode_mock,
        patch(
            "grace2_agent.workflows.compute_impact_envelope.postprocess_pelicun",
            new=postprocess_mock,
        ),
    ):
        result = await compute_impact_envelope(
            flood_layer_uri=_FLOOD_URI,
            location_query="Fort Myers, FL",
        )

    # All four steps were called once.
    geocode_mock.assert_called_once_with("Fort Myers, FL")
    nsi_mock.assert_called_once()
    nsi_call_kwargs = nsi_mock.call_args.kwargs
    assert nsi_call_kwargs["bbox"] == _FT_MYERS_BBOX

    pelicun_mock.assert_called_once()
    pelicun_kwargs = pelicun_mock.call_args.kwargs
    assert pelicun_kwargs["hazard_raster_uri"] == _FLOOD_URI
    assert pelicun_kwargs["assets_uri"] == _NSI_URI
    assert pelicun_kwargs["fragility_set"] == "hazus_flood_v6"

    postprocess_mock.assert_awaited_once()
    pp_kwargs = postprocess_mock.await_args.kwargs
    assert pp_kwargs["damage_layer_uri"] == _DAMAGE_URI
    assert pp_kwargs["flood_layer_uri"] == _FLOOD_URI
    # M5.5 provenance threading: the composer forwards the fragility set it
    # actually ran upstream so the envelope provenance is not a constant.
    assert pp_kwargs["fragility_set"] == "hazus_flood_v6"

    # Result shape.
    assert set(result.keys()) == {
        "envelope_summary",
        "raw_envelope",
        "narrative",
        "provenance",
    }
    summary = result["envelope_summary"]
    assert summary["n_structures_total"] == 100
    assert summary["n_structures_damaged"] == 42
    assert summary["expected_loss_usd"] == 5_678_900.0
    assert summary["population_at_high_risk"] == 567

    prov = result["provenance"]
    assert prov["flood_layer_uri"] == _FLOOD_URI
    assert prov["assets_uri"] == _NSI_URI
    assert prov["damage_layer_uri"] == _DAMAGE_URI
    assert prov["structure_inventory_source"] == "USACE_NSI"
    assert prov["fragility_set"] == "hazus_flood_v6"
    assert prov["bbox"] == list(_FT_MYERS_BBOX)
    assert prov["location_query"] == "Fort Myers, FL"
    assert "generated_at" in prov

    assert result["raw_envelope"] is envelope_dict


# --------------------------------------------------------------------------- #
# Test 3b — custom fragility set threads through to postprocess (M5.5).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_custom_fragility_set_threads_to_postprocess() -> None:
    """A non-default fragility_set reaches Pelicun AND postprocess provenance."""
    nsi_layer = _mock_layer_uri(_NSI_URI)
    damage_layer = _mock_layer_uri(_DAMAGE_URI)
    envelope_dict = _mock_envelope_dict()

    nsi_mock = MagicMock(return_value=nsi_layer)
    pelicun_mock = MagicMock(return_value=damage_layer)
    postprocess_mock = AsyncMock(return_value=envelope_dict)

    nsi_orig = TOOL_REGISTRY["fetch_usace_nsi"]
    pelicun_orig = TOOL_REGISTRY["run_pelicun_damage_assessment"]
    fake_nsi = type(nsi_orig)(metadata=nsi_orig.metadata, fn=nsi_mock, module=nsi_orig.module)
    fake_pelicun = type(pelicun_orig)(
        metadata=pelicun_orig.metadata, fn=pelicun_mock, module=pelicun_orig.module
    )

    with (
        patch.dict(
            TOOL_REGISTRY,
            {
                "fetch_usace_nsi": fake_nsi,
                "run_pelicun_damage_assessment": fake_pelicun,
            },
        ),
        patch(
            "grace2_agent.workflows.compute_impact_envelope.postprocess_pelicun",
            new=postprocess_mock,
        ),
    ):
        await compute_impact_envelope(
            flood_layer_uri=_FLOOD_URI,
            bbox=_FT_MYERS_BBOX,
            fragility_set="hazus_flood_v7_custom",
        )

    # Pelicun ran with the custom set.
    assert pelicun_mock.call_args.kwargs["fragility_set"] == "hazus_flood_v7_custom"
    # postprocess received the same set for provenance.
    assert (
        postprocess_mock.await_args.kwargs["fragility_set"]
        == "hazus_flood_v7_custom"
    )


# --------------------------------------------------------------------------- #
# Test 4 — postprocess error propagation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_handles_postprocess_pelicun_error_propagates() -> None:
    """``PelicunPostprocessError`` from postprocess → typed composer error."""
    nsi_layer = _mock_layer_uri(_NSI_URI)
    damage_layer = _mock_layer_uri(_DAMAGE_URI)

    nsi_mock = MagicMock(return_value=nsi_layer)
    pelicun_mock = MagicMock(return_value=damage_layer)
    pp_err = PelicunPostprocessEmptyError("zero features")
    postprocess_mock = AsyncMock(side_effect=pp_err)

    nsi_orig = TOOL_REGISTRY["fetch_usace_nsi"]
    pelicun_orig = TOOL_REGISTRY["run_pelicun_damage_assessment"]

    fake_nsi = type(nsi_orig)(metadata=nsi_orig.metadata, fn=nsi_mock, module=nsi_orig.module)
    fake_pelicun = type(pelicun_orig)(
        metadata=pelicun_orig.metadata, fn=pelicun_mock, module=pelicun_orig.module
    )

    with (
        patch.dict(
            TOOL_REGISTRY,
            {
                "fetch_usace_nsi": fake_nsi,
                "run_pelicun_damage_assessment": fake_pelicun,
            },
        ),
        patch(
            "grace2_agent.workflows.compute_impact_envelope.postprocess_pelicun",
            new=postprocess_mock,
        ),
    ):
        with pytest.raises(ComputeImpactEnvelopePostprocessError) as excinfo:
            await compute_impact_envelope(
                flood_layer_uri=_FLOOD_URI,
                bbox=_FT_MYERS_BBOX,
            )
    assert excinfo.value.error_code == "POSTPROCESS_FAILED"
    assert excinfo.value.retryable is False
    # The wrapped cause is the PelicunPostprocessError.
    assert isinstance(excinfo.value.__cause__, PelicunPostprocessEmptyError)


# --------------------------------------------------------------------------- #
# Test 5 — narrative string snapshot
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_narrative_string_contains_count_and_dollar() -> None:
    """``narrative`` carries the count, a ``$``-prefixed loss, and (if NSI) pop."""
    nsi_layer = _mock_layer_uri(_NSI_URI)
    damage_layer = _mock_layer_uri(_DAMAGE_URI)
    envelope_dict = _mock_envelope_dict(
        n_damaged=1_234,
        expected_loss_usd=5_678_900.0,
        pop_high_risk=567,
    )

    nsi_mock = MagicMock(return_value=nsi_layer)
    pelicun_mock = MagicMock(return_value=damage_layer)
    postprocess_mock = AsyncMock(return_value=envelope_dict)

    nsi_orig = TOOL_REGISTRY["fetch_usace_nsi"]
    pelicun_orig = TOOL_REGISTRY["run_pelicun_damage_assessment"]

    fake_nsi = type(nsi_orig)(metadata=nsi_orig.metadata, fn=nsi_mock, module=nsi_orig.module)
    fake_pelicun = type(pelicun_orig)(
        metadata=pelicun_orig.metadata, fn=pelicun_mock, module=pelicun_orig.module
    )

    with (
        patch.dict(
            TOOL_REGISTRY,
            {
                "fetch_usace_nsi": fake_nsi,
                "run_pelicun_damage_assessment": fake_pelicun,
            },
        ),
        patch(
            "grace2_agent.workflows.compute_impact_envelope.postprocess_pelicun",
            new=postprocess_mock,
        ),
    ):
        result = await compute_impact_envelope(
            flood_layer_uri=_FLOOD_URI,
            bbox=_FT_MYERS_BBOX,
        )

    narrative = result["narrative"]
    assert "1,234" in narrative, narrative
    assert "structures impacted" in narrative
    assert "$" in narrative
    assert "5,678,900" in narrative
    assert "567" in narrative
    assert "population at high risk" in narrative


@pytest.mark.asyncio
async def test_narrative_omits_population_when_ms_buildings() -> None:
    """MS_BUILDINGS path has no population → narrative drops that segment."""
    damage_layer = _mock_layer_uri(_DAMAGE_URI)
    envelope_dict = _mock_envelope_dict(
        n_damaged=42,
        expected_loss_usd=100_000.0,
        pop_total=None,
        pop_displaced=None,
        pop_high_risk=None,
        inventory_source="MS_BUILDINGS",
    )

    ms_mock = AsyncMock(return_value=damage_layer)
    postprocess_mock = AsyncMock(return_value=envelope_dict)

    ms_orig = TOOL_REGISTRY["run_pelicun_with_buildings"]
    fake_ms = type(ms_orig)(metadata=ms_orig.metadata, fn=ms_mock, module=ms_orig.module)

    with (
        patch.dict(TOOL_REGISTRY, {"run_pelicun_with_buildings": fake_ms}),
        patch(
            "grace2_agent.workflows.compute_impact_envelope.postprocess_pelicun",
            new=postprocess_mock,
        ),
    ):
        result = await compute_impact_envelope(
            flood_layer_uri=_FLOOD_URI,
            bbox=_FT_MYERS_BBOX,
            structure_inventory_source="MS_BUILDINGS",
        )

    narrative = result["narrative"]
    assert "42" in narrative
    assert "$" in narrative
    assert "population" not in narrative


# --------------------------------------------------------------------------- #
# Test 6 — metadata sanity (category / hints).
# --------------------------------------------------------------------------- #


def test_metadata_category_damage_assessment() -> None:
    """Composer metadata declares the read-only / non-destructive / idempotent
    hints the kickoff specified for the damage_assessment category.

    Note: ``AtomicToolMetadata`` carries no ``category`` field
    (``tool_category`` is convention-only and surfaces on the
    ``tool-call-start`` WebSocket envelope rather than the registry model).
    We assert the MCP-annotation shape the kickoff explicitly required.
    """
    entry = TOOL_REGISTRY["compute_impact_envelope"]
    meta = entry.metadata
    assert meta.read_only_hint is True
    assert meta.open_world_hint is False
    assert meta.destructive_hint is False
    assert meta.idempotent_hint is True
    # Composer surface is workflow_dispatch (FR-DC-6); the underlying step
    # caches deliver the static-30d cacheable behavior end-to-end.
    assert meta.source_class == "workflow_dispatch"


# --------------------------------------------------------------------------- #
# Test 7 — extra kwargs swallowed (Gemini hallucination guard).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extra_kwargs_swallowed() -> None:
    """Arbitrary extra kwargs do not raise TypeError (Gemini guard)."""
    nsi_layer = _mock_layer_uri(_NSI_URI)
    damage_layer = _mock_layer_uri(_DAMAGE_URI)
    envelope_dict = _mock_envelope_dict()

    nsi_mock = MagicMock(return_value=nsi_layer)
    pelicun_mock = MagicMock(return_value=damage_layer)
    postprocess_mock = AsyncMock(return_value=envelope_dict)

    nsi_orig = TOOL_REGISTRY["fetch_usace_nsi"]
    pelicun_orig = TOOL_REGISTRY["run_pelicun_damage_assessment"]
    fake_nsi = type(nsi_orig)(metadata=nsi_orig.metadata, fn=nsi_mock, module=nsi_orig.module)
    fake_pelicun = type(pelicun_orig)(
        metadata=pelicun_orig.metadata, fn=pelicun_mock, module=pelicun_orig.module
    )

    with (
        patch.dict(
            TOOL_REGISTRY,
            {
                "fetch_usace_nsi": fake_nsi,
                "run_pelicun_damage_assessment": fake_pelicun,
            },
        ),
        patch(
            "grace2_agent.workflows.compute_impact_envelope.postprocess_pelicun",
            new=postprocess_mock,
        ),
    ):
        # Pass an extra invented kwarg — the workflow must absorb it.
        result = await compute_impact_envelope(
            flood_layer_uri=_FLOOD_URI,
            bbox=_FT_MYERS_BBOX,
            invented_kwarg="ignored",
            another="also_ignored",
        )
    assert result["envelope_summary"]["n_structures_damaged"] == 42


# --------------------------------------------------------------------------- #
# Test 8 — input error on missing flood_layer_uri.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_input_error_on_missing_flood_layer_uri() -> None:
    """Empty / None / non-string flood_layer_uri raises typed input error."""
    with pytest.raises(ComputeImpactEnvelopeInputError):
        await compute_impact_envelope(
            flood_layer_uri=None,  # type: ignore[arg-type]
            bbox=_FT_MYERS_BBOX,
        )

    with pytest.raises(ComputeImpactEnvelopeInputError):
        await compute_impact_envelope(
            flood_layer_uri="",
            bbox=_FT_MYERS_BBOX,
        )

    with pytest.raises(ComputeImpactEnvelopeInputError):
        await compute_impact_envelope(
            flood_layer_uri="   ",
            bbox=_FT_MYERS_BBOX,
        )

    with pytest.raises(ComputeImpactEnvelopeInputError):
        await compute_impact_envelope(
            flood_layer_uri=42,  # type: ignore[arg-type]
            bbox=_FT_MYERS_BBOX,
        )


# --------------------------------------------------------------------------- #
# Test 9 — bbox AND location_query both missing.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_input_error_when_neither_bbox_nor_location_query() -> None:
    """Both bbox + location_query None → typed input error."""
    with pytest.raises(ComputeImpactEnvelopeInputError):
        await compute_impact_envelope(flood_layer_uri=_FLOOD_URI)


# --------------------------------------------------------------------------- #
# Test 10 — MS_BUILDINGS path routes through run_pelicun_with_buildings.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ms_buildings_path_routes_through_run_pelicun_with_buildings() -> None:
    """MS_BUILDINGS path uses the composer ``run_pelicun_with_buildings``."""
    damage_layer = _mock_layer_uri(_DAMAGE_URI)
    envelope_dict = _mock_envelope_dict(
        inventory_source="MS_BUILDINGS",
        pop_total=None,
        pop_displaced=None,
        pop_high_risk=None,
    )

    ms_mock = AsyncMock(return_value=damage_layer)
    postprocess_mock = AsyncMock(return_value=envelope_dict)

    ms_orig = TOOL_REGISTRY["run_pelicun_with_buildings"]
    fake_ms = type(ms_orig)(metadata=ms_orig.metadata, fn=ms_mock, module=ms_orig.module)

    with (
        patch.dict(TOOL_REGISTRY, {"run_pelicun_with_buildings": fake_ms}),
        patch(
            "grace2_agent.workflows.compute_impact_envelope.postprocess_pelicun",
            new=postprocess_mock,
        ),
    ):
        result = await compute_impact_envelope(
            flood_layer_uri=_FLOOD_URI,
            bbox=_FT_MYERS_BBOX,
            structure_inventory_source="MS_BUILDINGS",
        )

    ms_mock.assert_awaited_once()
    ms_kwargs = ms_mock.await_args.kwargs
    assert ms_kwargs["hazard_raster_uri"] == _FLOOD_URI
    assert ms_kwargs["bbox"] == _FT_MYERS_BBOX
    assert ms_kwargs["fragility_set"] == "hazus_flood_v6"

    postprocess_mock.assert_awaited_once()

    prov = result["provenance"]
    assert prov["structure_inventory_source"] == "MS_BUILDINGS"
    assert prov["assets_uri"] == "<ms_buildings:intermediate>"


# --------------------------------------------------------------------------- #
# Test 11 — NSI fetch failure → typed error.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_nsi_fetch_failure_raises_typed_error() -> None:
    """``fetch_usace_nsi`` exception → ``COMPUTE_IMPACT_ENVELOPE`` NSI error."""

    def _raise_nsi(**kwargs: Any) -> Any:
        raise RuntimeError("NSI cluster 5xx")

    nsi_mock = MagicMock(side_effect=_raise_nsi)
    nsi_orig = TOOL_REGISTRY["fetch_usace_nsi"]
    fake_nsi = type(nsi_orig)(
        metadata=nsi_orig.metadata, fn=nsi_mock, module=nsi_orig.module
    )

    with patch.dict(TOOL_REGISTRY, {"fetch_usace_nsi": fake_nsi}):
        with pytest.raises(ComputeImpactEnvelopeNSIFetchError) as excinfo:
            await compute_impact_envelope(
                flood_layer_uri=_FLOOD_URI,
                bbox=_FT_MYERS_BBOX,
            )
    assert excinfo.value.error_code == "NSI_FETCH_FAILED"
    assert excinfo.value.retryable is True


# --------------------------------------------------------------------------- #
# Test 12 — Pelicun failure → typed error.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pelicun_failure_raises_typed_error() -> None:
    """run_pelicun_damage_assessment exception → PELICUN_UPSTREAM_FAILED."""
    nsi_layer = _mock_layer_uri(_NSI_URI)
    nsi_mock = MagicMock(return_value=nsi_layer)

    def _raise_pelicun(**kwargs: Any) -> Any:
        raise RuntimeError("pelicun crashed")

    pelicun_mock = MagicMock(side_effect=_raise_pelicun)

    nsi_orig = TOOL_REGISTRY["fetch_usace_nsi"]
    pelicun_orig = TOOL_REGISTRY["run_pelicun_damage_assessment"]
    fake_nsi = type(nsi_orig)(metadata=nsi_orig.metadata, fn=nsi_mock, module=nsi_orig.module)
    fake_pelicun = type(pelicun_orig)(
        metadata=pelicun_orig.metadata, fn=pelicun_mock, module=pelicun_orig.module
    )

    with patch.dict(
        TOOL_REGISTRY,
        {
            "fetch_usace_nsi": fake_nsi,
            "run_pelicun_damage_assessment": fake_pelicun,
        },
    ):
        with pytest.raises(ComputeImpactEnvelopePelicunError) as excinfo:
            await compute_impact_envelope(
                flood_layer_uri=_FLOOD_URI,
                bbox=_FT_MYERS_BBOX,
            )
    assert excinfo.value.error_code == "PELICUN_UPSTREAM_FAILED"
    assert excinfo.value.retryable is True
