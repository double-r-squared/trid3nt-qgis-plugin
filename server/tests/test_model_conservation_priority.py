"""Unit tests for the ``model_conservation_priority`` composer (conservation
micro-North-Star).

Coverage:
- The LLM-facing wrapper ``run_model_conservation_priority`` is registered with
  ``workflow_dispatch`` / ``live-no-cache`` / cacheable=False metadata.
- AOI resolution: explicit bbox passes through; a location_query is geocoded;
  neither raises ConservationPriorityInputError.
- Happy fan-out: all five sources produce layers -> status="ok", every layer is
  carried in order (aerial base first), summary names the chips.
- Partial: one source raises -> status="partial", the failure is recorded, the
  OTHER layers still come through (best-effort, independent steps).
- Honesty floor: EVERY source fails -> status="error" (never "ok") with zero
  layers and all failures recorded.
- Determinism (Invariant 1): the summary is built from typed fields (no LLM).
- The wrapper returns a JSON-able dict via model_dump.

The composer's atomic-tool callables are resolved through ``_registry_fn``; we
patch that to inject fakes so no network / STAC is touched.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows import model_conservation_priority as cp_mod
from trid3nt_server.workflows.model_conservation_priority import (
    ConservationPriorityInputError,
    ConservationPriorityResult,
    model_conservation_priority,
    run_model_conservation_priority,
)
from trid3nt_contracts.execution import LayerURI


_BBOX = (-80.05, 32.75, -79.95, 32.82)


def _layer(layer_id: str, role: str = "primary", lt: str = "raster") -> LayerURI:
    return LayerURI(
        layer_id=layer_id,
        name=layer_id,
        layer_type=lt,
        uri=f"s3://bucket/{layer_id}.tif",
        style_preset="x",
        role=role,
    )


def _fake_registry(mapping):
    """Return a _registry_fn replacement that serves callables from ``mapping``
    and raises for anything not provided."""
    def _fn(name):
        if name in mapping:
            return mapping[name]
        raise cp_mod.ConservationPriorityError(f"unexpected tool {name!r}")
    return _fn


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_wrapper_is_registered() -> None:
    assert "run_model_conservation_priority" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["run_model_conservation_priority"].metadata
    assert meta.source_class == "workflow_dispatch"
    assert meta.ttl_class == "live-no-cache"
    assert meta.cacheable is False


# ---------------------------------------------------------------------------
# AOI resolution.
# ---------------------------------------------------------------------------


def test_requires_bbox_or_location() -> None:
    with pytest.raises(ConservationPriorityInputError):
        asyncio.run(model_conservation_priority())


def test_geocodes_location_query() -> None:
    geocode_calls = {}

    def fake_geocode(q):
        geocode_calls["q"] = q
        return {"name": "Charleston, SC", "bbox": list(_BBOX)}

    def fake_naip(bbox):
        # confirm the geocoded bbox flowed through
        assert tuple(bbox) == _BBOX
        return _layer("naip", role="context")

    mapping = {"geocode_location": fake_geocode, "fetch_naip": fake_naip}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cp_mod, "_registry_fn", _fake_registry(mapping))
        res = asyncio.run(
            model_conservation_priority(location_query="Charleston SC")
        )
    assert geocode_calls["q"] == "Charleston SC"
    assert res.location_name == "Charleston, SC"
    assert res.aerial_layer is not None


# ---------------------------------------------------------------------------
# Happy fan-out -> status ok.
# ---------------------------------------------------------------------------


def test_full_stack_status_ok_and_ordered() -> None:
    mapping = {
        "fetch_naip": lambda bbox: _layer("naip", role="context"),
        "compute_ndvi": lambda bbox, start_date, end_date: _layer("ndvi"),
        "fetch_mobi": lambda bbox, layer: _layer("mobi"),
        "fetch_gbif_occurrences": lambda bbox, species_key: _layer(
            f"gbif-{species_key}", lt="vector"
        ),
        "fetch_iucn_red_list_range": lambda species_name: _layer(
            f"iucn-{species_name}", lt="vector"
        ),
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cp_mod, "_registry_fn", _fake_registry(mapping))
        res = asyncio.run(
            model_conservation_priority(
                bbox=_BBOX,
                species_keys=[2435099],
                species_names=["Puma concolor coryi"],
            )
        )

    assert isinstance(res, ConservationPriorityResult)
    assert res.status == "ok"
    assert res.failures == {}
    assert res.aerial_layer is not None
    assert res.ndvi_layer is not None
    assert res.biodiversity_layer is not None
    assert len(res.species_layers) == 1
    assert len(res.range_layers) == 1
    # Aerial base first in stack order.
    layers = res.all_layers()
    assert layers[0].layer_id == "naip"
    assert len(layers) == 5
    # Summary is deterministic + names the chips (Invariant 1).
    assert "NAIP aerial base" in res.summary
    assert "NDVI vegetation" in res.summary
    assert "MoBI biodiversity importance" in res.summary
    assert "status=ok" in res.summary


# ---------------------------------------------------------------------------
# Partial -> some layers, some failures.
# ---------------------------------------------------------------------------


def test_partial_when_one_source_fails() -> None:
    def boom_mobi(bbox, layer):
        raise RuntimeError("MoBI outage")

    mapping = {
        "fetch_naip": lambda bbox: _layer("naip", role="context"),
        "compute_ndvi": lambda bbox, start_date, end_date: _layer("ndvi"),
        "fetch_mobi": boom_mobi,
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cp_mod, "_registry_fn", _fake_registry(mapping))
        res = asyncio.run(model_conservation_priority(bbox=_BBOX))

    assert res.status == "partial"
    assert res.aerial_layer is not None
    assert res.ndvi_layer is not None
    assert res.biodiversity_layer is None
    assert "fetch_mobi" in res.failures
    assert "MoBI outage" in res.failures["fetch_mobi"]
    assert "unavailable" in res.summary


# ---------------------------------------------------------------------------
# Honesty floor -> zero layers must NOT report ok.
# ---------------------------------------------------------------------------


def test_zero_layers_status_error_not_ok() -> None:
    def boom(*a, **k):
        raise RuntimeError("source down")

    mapping = {
        "fetch_naip": boom,
        "compute_ndvi": boom,
        "fetch_mobi": boom,
        "fetch_gbif_occurrences": boom,
        "fetch_iucn_red_list_range": boom,
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cp_mod, "_registry_fn", _fake_registry(mapping))
        res = asyncio.run(
            model_conservation_priority(
                bbox=_BBOX, species_keys=[1], species_names=["X"]
            )
        )

    assert res.status == "error"
    assert res.status != "ok"
    assert res.all_layers() == []
    # Every attempted source recorded a failure.
    assert "fetch_naip" in res.failures
    assert "compute_ndvi" in res.failures
    assert "fetch_mobi" in res.failures
    assert any(k.startswith("fetch_gbif_occurrences") for k in res.failures)
    assert any(k.startswith("fetch_iucn_red_list_range") for k in res.failures)


# ---------------------------------------------------------------------------
# Wrapper returns JSON-able dict.
# ---------------------------------------------------------------------------


def test_wrapper_returns_json_dict() -> None:
    mapping = {"fetch_naip": lambda bbox: _layer("naip", role="context")}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cp_mod, "_registry_fn", _fake_registry(mapping))
        out = asyncio.run(run_model_conservation_priority(bbox=_BBOX))

    assert isinstance(out, dict)
    assert out["status"] == "partial"  # only NAIP came through, others "failed"
    assert out["schema_version"] == "v1"
    assert out["aerial_layer"]["layer_id"] == "naip"
    assert isinstance(out["bbox"], list) and len(out["bbox"]) == 4
