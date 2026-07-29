"""Offline tests for the router engine + surfacing toggle (B1 -- router-core).

Coverage:
- ``validate_params``: required-missing / bad-enum / bad-bbox / conus gate /
  date-range ceiling / bbox quantize -- all typed ``RouterInputError``, no network.
- ``synthesize_metadata`` mirrors the twin's AtomicToolMetadata.
- ``synthesize_payload_estimator`` per model (bbox_area / per_feature /
  per_station / tiled).
- ``route`` end-to-end with a monkeypatched executor + the in-memory S3 double:
  correct cache path + LayerURI, second call is a cache HIT.
- Fold-arm toggle (contract sec 3): default pool UNCHANGED when the env is unset;
  the spec-driven virtual entry swaps in under the twin's name when set.

No network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.agent.tools import (
    RegisteredTool,
    TOOL_REGISTRY,
    clear_registry_for_tests,
)
from trid3nt_server.agent.tools.fetchers._router import registration, router
from trid3nt_server.agent.tools.fetchers._router.errors import RouterInputError
from trid3nt_server.agent.tools.fetchers._router.executors import raster_cog

_PINNED_NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)


def _raster_spec(**over) -> SourceSpec:
    base = {
        "name": "fetch_demo_raster",
        "source_class": "demo_raster",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": "http://example.test/data.nc"}},
        "params": {
            "bbox": {"type": "bbox", "required": True, "quantize": "round_6dp"},
            "variable": {"type": "enum", "values": ["fm100", "pr"], "required": True},
            "start_date": {"type": "iso_date", "required": True},
            "end_date": {"type": "iso_date", "required": True, "max_range_days": 366},
        },
        "gates": {"conus_only": True},
        "ingest": {"access": "opendap"},
        "normalize": {"crs": "EPSG:4326", "units": "Percent"},
        "output": {"layer_type": "raster", "ext": "tif", "role": "primary", "style_preset": "demo_{variable}"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 0.01, "floor_mb": 0.01},
    }
    base.update(over)
    return SourceSpec.model_validate(base)


# --------------------------------------------------------------------------- #
# param validation + gates
# --------------------------------------------------------------------------- #


def test_validate_required_missing_raises_typed():
    spec = _raster_spec()
    with pytest.raises(RouterInputError) as ei:
        router.validate_params(spec, {"variable": "fm100", "start_date": "2026-07-01", "end_date": "2026-07-03"})
    assert ei.value.error_code == "DEMO_RASTER_INPUT_ERROR"
    assert ei.value.retryable is False


def test_validate_bad_enum_raises():
    spec = _raster_spec()
    with pytest.raises(RouterInputError):
        router.validate_params(spec, {
            "bbox": [-117.5, 33.5, -116.5, 34.5], "variable": "NOPE",
            "start_date": "2026-07-01", "end_date": "2026-07-03",
        })


def test_validate_degenerate_bbox_raises():
    spec = _raster_spec()
    with pytest.raises(RouterInputError):
        router.validate_params(spec, {
            "bbox": [-116.5, 34.5, -117.5, 33.5], "variable": "fm100",  # min>max
            "start_date": "2026-07-01", "end_date": "2026-07-03",
        })


def test_conus_gate_rejects_offshore_bbox():
    spec = _raster_spec()
    with pytest.raises(RouterInputError) as ei:
        router.validate_params(spec, {
            "bbox": [10.0, 40.0, 11.0, 41.0], "variable": "fm100",  # Europe
            "start_date": "2026-07-01", "end_date": "2026-07-03",
        })
    assert "CONUS" in str(ei.value)


def test_date_range_ceiling_enforced():
    spec = _raster_spec()
    with pytest.raises(RouterInputError):
        router.validate_params(spec, {
            "bbox": [-117.5, 33.5, -116.5, 34.5], "variable": "fm100",
            "start_date": "2024-01-01", "end_date": "2026-07-03",  # > 366 days
        })


def test_validate_quantizes_and_defaults():
    spec = _raster_spec(params={
        "bbox": {"type": "bbox", "required": True, "quantize": "round_6dp"},
        "variable": {"type": "enum", "values": ["fm100", "pr"], "default": "fm100"},
    })
    out = router.validate_params(spec, {"bbox": [-117.5000004, 33.5, -116.5, 34.5000009]})
    assert out["variable"] == "fm100"  # default applied
    assert out["bbox"][0] == pytest.approx(-117.5, abs=1e-6)  # quantized to 6dp
    assert out["bbox"] == [round(v, 6) for v in out["bbox"]]


# --------------------------------------------------------------------------- #
# metadata + payload estimator synthesis
# --------------------------------------------------------------------------- #


def test_synthesize_metadata_mirrors_twin():
    spec = _raster_spec()
    md = router.synthesize_metadata(spec)
    assert isinstance(md, AtomicToolMetadata)
    assert md.name == "fetch_demo_raster"
    assert md.ttl_class == "static-30d"
    assert md.source_class == "demo_raster"
    assert md.cacheable is True
    assert md.supports_global_query is False
    assert md.payload_mb_estimator_name == "estimate_payload_mb"
    assert md.open_world_hint is True


def test_payload_estimator_bbox_area():
    spec = _raster_spec()
    est = router.synthesize_payload_estimator(spec)
    mb = est(bbox=[-117.5, 33.5, -116.5, 34.5])  # 1 deg^2
    assert mb == pytest.approx(0.01, abs=1e-6)
    assert est(bbox=[-100.0, 40.0, -99.9, 40.0]) >= 0.01  # floor holds


def test_payload_estimator_per_station_scales_with_days():
    spec = SourceSpec.model_validate({
        "name": "s", "source_class": "s", "shape": "station-timeseries-fgb",
        "endpoints": {"data": {"url": "http://x"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "p"},
        "cache": {"ttl_class": "dynamic-1h"},
        "payload_estimate": {"model": "per_station", "kb_per_station_per_day": 2.0,
                             "stations_per_sq_deg": 2.0, "overhead_kb": 0.5},
    })
    est = router.synthesize_payload_estimator(spec)
    one_day = est(bbox=[-82.0, 26.0, -81.0, 27.0], start_date="2026-07-01", end_date="2026-07-01")
    ten_day = est(bbox=[-82.0, 26.0, -81.0, 27.0], start_date="2026-07-01", end_date="2026-07-10")
    assert ten_day > one_day


def test_payload_estimator_tiled_counts_tiles():
    spec = SourceSpec.model_validate({
        "name": "m", "source_class": "m", "shape": "raster-cog",
        "endpoints": {"data": {"url": "http://x"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "ingest": {"tile_deg2": 0.5, "mosaic": {}},
        "output": {"layer_type": "raster", "ext": "tif", "style_preset": "p"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "tiled", "mb_per_tile": 0.05, "tile_deg2": 0.5},
    })
    est = router.synthesize_payload_estimator(spec)
    small = est(bbox=[-100.0, 40.0, -99.8, 40.2])  # < 1 tile
    big = est(bbox=[-100.0, 40.0, -98.0, 42.0])    # 4 deg^2 -> 8 tiles
    assert big > small


# --------------------------------------------------------------------------- #
# route end-to-end (cache + LayerURI)
# --------------------------------------------------------------------------- #


def test_route_end_to_end_writes_cache_and_emits_layeruri(fake_s3, monkeypatch):
    import numpy as np
    import rasterio.transform as rtransform

    spec = _raster_spec()

    def _synthetic(s, p):
        n = 32
        arr = np.ones((n, n), dtype="float32")
        tf = rtransform.from_bounds(*p["bbox"], n, n)
        return arr, tf, "EPSG:4326"

    calls = {"n": 0}

    def _counting(s, p):
        calls["n"] += 1
        return _synthetic(s, p)

    monkeypatch.setattr(raster_cog, "fetch_source_array", _counting)

    args = {"bbox": [-117.5, 33.5, -116.5, 34.5], "variable": "fm100",
            "start_date": "2026-07-01", "end_date": "2026-07-03"}
    layer = router.route(spec, dict(args))
    assert isinstance(layer, LayerURI)
    assert layer.layer_type == "raster"
    assert layer.style_preset == "demo_fm100"  # templated on variable
    assert layer.units == "Percent"
    assert layer.uri.startswith("s3://")
    assert "cache/static-30d/demo_raster/" in layer.uri
    assert layer.uri.endswith(".tif")
    assert calls["n"] == 1

    # Second identical call is a cache HIT -- no second fetch.
    layer2 = router.route(spec, dict(args))
    assert layer2.uri == layer.uri
    assert calls["n"] == 1  # executor NOT called again


def test_route_vector_source_class_in_uri(fake_s3, monkeypatch):
    from trid3nt_server.agent.tools.fetchers._router.executors import vector_fgb

    spec = SourceSpec.model_validate({
        "name": "fetch_demo_vector", "source_class": "demo_vector", "shape": "vector-fgb",
        "endpoints": {"data": {"url": "http://x/query"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "vec"},
        "cache": {"ttl_class": "semi-static-7d"},
        "payload_estimate": {"model": "per_feature", "kb_per_feature": 1.0},
    })
    monkeypatch.setattr(vector_fgb, "fetch_features", lambda s, p: [])  # honest-empty
    layer = router.route(spec, {"bbox": [-100.0, 40.0, -99.0, 41.0]})
    assert layer.layer_type == "vector"
    assert "cache/semi-static-7d/demo_vector/" in layer.uri
    assert layer.uri.endswith(".fgb")


# --------------------------------------------------------------------------- #
# Fold-arm surfacing toggle (contract sec 3): default pool unchanged when OFF
# --------------------------------------------------------------------------- #


@pytest.fixture()
def registered_spec():
    """Register a spec-driven virtual tool; restore registry + spec map after."""
    saved = dict(TOOL_REGISTRY)
    saved_specs = dict(registration._SPEC_REGISTRY)
    clear_registry_for_tests()
    TOOL_REGISTRY.update(saved)
    registration.clear_specs_for_tests()
    spec = _raster_spec()
    alias = registration.register_spec(spec)
    try:
        yield spec, alias
    finally:
        clear_registry_for_tests()
        TOOL_REGISTRY.update(saved)
        registration.clear_specs_for_tests()
        registration._SPEC_REGISTRY.update(saved_specs)


def test_virtual_tool_registered_as_template(registered_spec):
    spec, alias = registered_spec
    assert alias == "fetch_demo_raster__spec"
    assert alias in TOOL_REGISTRY
    assert TOOL_REGISTRY[alias].metadata.tier == "template"
    assert registration.substitution_map() == {"fetch_demo_raster": "fetch_demo_raster__spec"}


def test_substitution_is_noop_when_fold_arm_off(registered_spec, monkeypatch):
    spec, alias = registered_spec
    monkeypatch.delenv(registration.FOLD_ARM_ENV, raising=False)
    assert registration.fold_arm_enabled() is False
    snapshot = dict(TOOL_REGISTRY)
    out = registration.apply_fold_substitution_registry(snapshot)
    assert out is snapshot  # SAME object -- provably unchanged when off


def test_default_pool_excludes_virtual_when_off(registered_spec, monkeypatch):
    """The pool producer (_build_index) excludes the tier=template virtual tool."""
    from trid3nt_server.agent.tools.search.search_tools import search_tools as st

    spec, alias = registered_spec
    monkeypatch.delenv(registration.FOLD_ARM_ENV, raising=False)
    snapshot = {alias: TOOL_REGISTRY[alias]}
    index = st._build_index(registry_snapshot=snapshot)
    assert alias not in index.tool_names  # template excluded from default pool
    assert "fetch_demo_raster" not in index.tool_names  # no twin in this snapshot


def test_fold_arm_on_surfaces_virtual_under_twin_name(registered_spec, monkeypatch):
    from trid3nt_server.agent.tools.search.search_tools import search_tools as st

    spec, alias = registered_spec
    monkeypatch.setenv(registration.FOLD_ARM_ENV, "1")
    assert registration.fold_arm_enabled() is True

    snapshot = {alias: TOOL_REGISTRY[alias]}
    swapped = registration.apply_fold_substitution_registry(snapshot)
    assert "fetch_demo_raster" in swapped        # spec surfaces UNDER twin name
    assert alias not in swapped                  # alias never leaks
    assert swapped["fetch_demo_raster"].metadata.tier == "general"

    # And the pool producer now includes it under the twin's name.
    index = st._build_index(registry_snapshot={alias: TOOL_REGISTRY[alias]})
    assert "fetch_demo_raster" in index.tool_names
    assert alias not in index.tool_names
