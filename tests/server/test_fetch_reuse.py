"""A repeat fetch hands back the layer already on the Case, by cache key.

A fetched layer's uri IS the address its bytes are cached under, so the registry
records that key when the layer lands and a fetch that would compute the same key
is the same data. The Case-state note reads the same address for the dataset it
tags a layer with."""

from __future__ import annotations

from types import SimpleNamespace

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.adapters.adapter import build_layers_present_note
from trid3nt_server.render.uri_registry import (
    get_uri_registry,
    reset_uri_registries_for_tests,
)
from trid3nt_server.server.dispatch.emitter import _reusable_fetch_layer
from trid3nt_server.tools.cache import cache_path, parse_cache_path
from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers._router.router import prospective_cache_key

# Small enough for the buildings source's own area cap.
AOI = [-81.90, 26.60, -81.80, 26.70]
OTHER_AOI = [-81.70, 26.40, -81.60, 26.50]


def _fetch_uri(tool_name: str, params: dict) -> tuple[str, str]:
    """The uri a fetch of ``tool_name`` would land, and the key it carries."""
    spec = get_spec(tool_name)
    key = prospective_cache_key(spec, params)
    path = cache_path(spec.source_class, spec.cache.ttl_class, key, spec.output.ext)
    return f"s3://trid3nt-cache/{path}", key


def _landed(tool_name: str, params: dict, layer_id: str = "L-buildings") -> LayerURI:
    uri, _key = _fetch_uri(tool_name, params)
    return LayerURI(
        layer_id=layer_id,
        name="Building footprints (OSM)",
        layer_type="vector",
        uri=uri,
        role="context",
        bbox=tuple(params["bbox"]),
    )


def _state(session_id: str, layers: list) -> SimpleNamespace:
    """A session holding the case ROWS the emitter carries, which is what a
    reuse has to rebuild a layer from."""
    from trid3nt_server.render.pipeline_emitter import summary_of

    rows = [summary_of(layer) for layer in layers]
    return SimpleNamespace(
        session_id=session_id, emitter=SimpleNamespace(loaded_layers=rows)
    )


def _register(session_id: str, layer: LayerURI, tool_name: str) -> None:
    get_uri_registry(session_id).record(
        layer.layer_id, uri=layer.uri, tool_name=tool_name
    )


def test_the_predicted_key_is_the_one_the_source_would_land_under() -> None:
    """The prediction runs the source's own pre-cache-key resolve, so two
    spellings of one request - the default stated and the default left out -
    land on one key, the key the fetch itself computes."""
    spec = get_spec("fetch_landcover")
    stated = prospective_cache_key(spec, {"dataset": "NLCD", "bbox": AOI})
    omitted = prospective_cache_key(spec, {"bbox": AOI})
    assert stated is not None and stated == omitted
    # A source whose key depends on a resolve round trip does not predict, and
    # neither does a frames list - one key per frame, no single address.
    assert prospective_cache_key(get_spec("fetch_mrms_qpe"), {"bbox": AOI}) is None
    assert prospective_cache_key(get_spec("fetch_satellite_imagery"),
                                 {"bbox": AOI}) is None


def test_a_cached_artifacts_uri_reads_back_as_its_dataset_and_key() -> None:
    uri, key = _fetch_uri("fetch_buildings", {"bbox": AOI})
    assert parse_cache_path(uri) == ("buildings", key)
    # A run's own product is not a fetched artifact and carries no key.
    assert parse_cache_path("s3://trid3nt-runs/r7/depth_peak.tif") is None
    assert parse_cache_path(None) is None


def test_the_registry_records_the_key_a_landed_fetch_was_addressed_by() -> None:
    reset_uri_registries_for_tests()
    layer = _landed("fetch_buildings", {"bbox": AOI})
    _register("s1", layer, "fetch_buildings")
    _, key = _fetch_uri("fetch_buildings", {"bbox": AOI})
    reg = get_uri_registry("s1")
    assert reg.handle_for_cache_key(key) == "L-buildings"
    assert reg._records["L-buildings"].dataset == "buildings"
    assert reg.handle_for_cache_key("0" * 32) is None
    assert reg.handle_for_cache_key(None) is None


def test_a_repeat_fetch_of_the_same_area_reuses_the_landed_layer() -> None:
    reset_uri_registries_for_tests()
    layer = _landed("fetch_buildings", {"bbox": AOI})
    _register("s2", layer, "fetch_buildings")
    state = _state("s2", [layer])
    reused = _reusable_fetch_layer(state, "fetch_buildings", {"bbox": AOI})
    assert reused is not None
    assert (reused.layer_id, reused.uri) == (layer.layer_id, layer.uri)


def test_a_different_area_is_different_data_and_re_fetches() -> None:
    reset_uri_registries_for_tests()
    layer = _landed("fetch_buildings", {"bbox": AOI})
    _register("s3", layer, "fetch_buildings")
    state = _state("s3", [layer])
    assert _reusable_fetch_layer(state, "fetch_buildings", {"bbox": OTHER_AOI}) is None


def test_a_different_source_never_answers_for_another() -> None:
    reset_uri_registries_for_tests()
    layer = _landed("fetch_buildings", {"bbox": AOI})
    _register("s4", layer, "fetch_buildings")
    state = _state("s4", [layer])
    assert _reusable_fetch_layer(state, "fetch_landcover", {"bbox": AOI}) is None


def test_a_tool_that_is_not_a_declared_source_never_reuses() -> None:
    reset_uri_registries_for_tests()
    layer = _landed("fetch_buildings", {"bbox": AOI})
    _register("s5", layer, "fetch_buildings")
    state = _state("s5", [layer])
    assert _reusable_fetch_layer(state, "compute_layer_bounds", {"bbox": AOI}) is None


def test_a_layer_no_longer_on_the_case_does_not_answer_a_fetch() -> None:
    reset_uri_registries_for_tests()
    layer = _landed("fetch_buildings", {"bbox": AOI})
    _register("s6", layer, "fetch_buildings")
    assert _reusable_fetch_layer(_state("s6", []), "fetch_buildings", {"bbox": AOI}) is None


def test_a_request_that_cannot_validate_has_no_key_and_never_reuses() -> None:
    reset_uri_registries_for_tests()
    layer = _landed("fetch_buildings", {"bbox": AOI})
    _register("s7", layer, "fetch_buildings")
    state = _state("s7", [layer])
    assert _reusable_fetch_layer(state, "fetch_buildings", {"bbox": "nonsense"}) is None
    assert _reusable_fetch_layer(state, "fetch_buildings", {}) is None


def test_the_case_state_note_tags_a_fetched_layer_with_its_dataset() -> None:
    layer = _landed("fetch_buildings", {"bbox": AOI})
    note = build_layers_present_note([layer.model_dump(mode="json")], case_bbox=AOI)
    assert note is not None
    assert "INPUT[buildings]" in note
    assert "handle=L-buildings" in note
    assert "compute_layer_bounds" in note
    assert "FETCHED LAYER REUSE" in note


def test_a_layer_that_was_not_fetched_is_a_plain_input_or_a_result() -> None:
    produced = {
        "layer_id": "L-mystery",
        "name": "Mystery",
        "layer_type": "vector",
        "uri": "s3://trid3nt-runs/r7/mystery.fgb",
        "role": "context",
    }
    note = build_layers_present_note([produced], case_bbox=None)
    assert note is not None
    line = next(ln for ln in note.splitlines() if ln.startswith("- Mystery"))
    assert ", INPUT," in line
    assert "INPUT[" not in line

    solved = {**produced, "name": "Peak depth", "role": "primary"}
    note = build_layers_present_note([solved], case_bbox=None)
    line = next(ln for ln in note.splitlines() if ln.startswith("- Peak depth"))
    assert ", RESULT," in line
