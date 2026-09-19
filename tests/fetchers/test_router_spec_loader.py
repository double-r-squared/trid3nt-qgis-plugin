"""Offline tests for the router's source-spec loader.

A well-formed spec loads for each pilot shape, and a malformed one - a missing
required key, a shape and output mismatch, a join over a non-vector shape -
raises the typed load error. A spec that omits its phrasings picks them up from
the sibling corpus, and a tree compose keys by name and SKIPS a malformed member."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.tools.fetchers._router.spec import (
    SpecLoadError,
    compose_specs_from_tree,
    load_spec,
    load_spec_from_path,
)




def raster_spec() -> dict:
    return {
        "name": "fetch_demo_raster",
        "source_class": "demo_raster",
        "shape": "raster-cog",
        "endpoints": {"data": {"url": "http://example.test/data.nc"}},
        "params": {
            "bbox": {"type": "bbox", "required": True, "quantize": "round_6dp"},
            "variable": {"type": "enum", "values": ["fm100", "pr"], "required": True},
        },
        "gates": {"conus_only": True},
        "ingest": {"access": "opendap"},
        "normalize": {"crs": "EPSG:4326", "units": "Percent", "orientation": "north_up"},
        "output": {"layer_type": "raster", "ext": "tif", "style": {"kind": "continuous"}},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "bbox_area", "mb_per_sq_deg": 0.01},
    }


def vector_spec() -> dict:
    return {
        "name": "fetch_demo_vector",
        "source_class": "demo_vector",
        "shape": "vector-fgb",
        "endpoints": {"data": {"url": "http://example.test/FeatureServer/0/query"}},
        "params": {"bbox": {"type": "bbox", "required": True}},
        "gates": {"max_features": 5},
        "output": {"layer_type": "vector", "ext": "fgb", "style": {"kind": "reference"}},
        "cache": {"ttl_class": "semi-static-7d"},
        "payload_estimate": {"model": "per_feature", "kb_per_feature": 1.0},
    }


def join_spec() -> dict:
    return {
        "name": "fetch_demo_join",
        "source_class": "demo_join",
        "shape": "vector-fgb",
        "endpoints": {
            "geometry": {"url": "http://example.test/tracts/query"},
            "values": {"url": "http://example.test/acs"},
        },
        "params": {"bbox": {"type": "bbox", "required": True}},
        "join": {
            "geometry": {"endpoint": "geometry", "key_field": "GEOID", "keep": ["NAME"]},
            "values": {
                "endpoint": "values",
                "scope_by": ["STATE", "COUNTY"],
                "null_sentinel_below": -666666000.0,
                "variables": {
                    "median_income": {"code": "B19013_001E", "kind": "value", "units": "usd"},
                },
            },
        },
        "output": {"layer_type": "vector", "ext": "fgb", "style": {"kind": "reference"}},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "per_feature", "kb_per_feature": 2.0},
    }




@pytest.mark.parametrize("factory", [raster_spec, vector_spec, join_spec])
def test_load_spec_accepts_wellformed(factory):
    spec = load_spec(factory())
    assert isinstance(spec, SourceSpec)
    assert spec.name == factory()["name"]


def test_load_spec_defaults_applied():
    spec = load_spec(vector_spec())
    assert spec.auth.mode == "none"
    assert spec.normalize.crs == "EPSG:4326"
    assert spec.output.role == "primary"
    assert spec.supports_global_query is False




def test_load_spec_rejects_missing_required_key():
    bad = raster_spec()
    del bad["output"]  # required
    with pytest.raises(SpecLoadError):
        load_spec(bad)


def test_load_spec_rejects_unknown_top_level_key():
    bad = raster_spec()
    bad["bogus_key"] = 1  # extra="forbid"
    with pytest.raises(SpecLoadError):
        load_spec(bad)


def test_load_spec_rejects_shape_output_mismatch():
    bad = raster_spec()
    bad["output"]["layer_type"] = "vector"  # raster-cog must emit raster
    with pytest.raises(SpecLoadError):
        load_spec(bad)


def test_load_spec_rejects_join_over_non_vector_shape():
    bad = raster_spec()
    bad["join"] = {"geometry": {}, "values": {}}
    with pytest.raises(SpecLoadError):
        load_spec(bad)


def test_load_spec_rejects_non_mapping():
    with pytest.raises(SpecLoadError):
        load_spec(["not", "a", "mapping"])  # type: ignore[arg-type]


# the declared style row: the preset family is closed at registration
#
# A row naming a shape nothing can draw must fail HERE, where a spec is loaded,
# and not at paint time - a layer that reaches the canvas with no renderer is a
# blank the reader has to diagnose.


def test_a_style_row_naming_a_kind_outside_the_family_is_refused_at_registration():
    bad = raster_spec()
    bad["output"]["style"] = {"kind": "heatmap", "ramp": "viridis"}
    with pytest.raises(SpecLoadError):
        load_spec(bad)


def test_a_style_row_naming_a_geometry_outside_the_family_is_refused():
    bad = vector_spec()
    bad["output"]["style"] = {"kind": "reference", "geometry": "blob"}
    with pytest.raises(SpecLoadError):
        load_spec(bad)


def test_a_per_param_row_is_held_to_the_same_family():
    """The overlay is a style row too, and a typo hides well inside a map."""
    bad = raster_spec()
    bad["output"]["style"] = {
        "kind": "continuous",
        "by_param": {"param": "variable", "map": {"pr": {"kind": "chloropleth"}}},
    }
    with pytest.raises(SpecLoadError):
        load_spec(bad)


def test_a_declared_row_survives_the_load_as_the_spec_wrote_it():
    spec = load_spec(raster_spec())
    assert spec.output.style == {"kind": "continuous"}
    # An absent row is the kind's bare default, not a missing style.
    bare = raster_spec()
    del bare["output"]["style"]
    assert load_spec(bare).output.style is None




def _write_source_yaml(dir_path: Path, spec_dict: dict) -> Path:
    import yaml

    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / "source.yaml"
    p.write_text(yaml.safe_dump(spec_dict))
    return p


def test_load_from_path_picks_up_sibling_corpus(tmp_path):
    src_dir = tmp_path / "fetch_demo_vector"
    sy = _write_source_yaml(src_dir, vector_spec())  # no inline corpus
    (src_dir / "corpus.yaml").write_text(
        textwrap.dedent(
            """
            fetch_demo_vector:
              - "where are the demo vectors near me"
              - "list demo point features in this bbox"
            """
        )
    )
    spec = load_spec_from_path(sy)
    assert spec.corpus == [
        "where are the demo vectors near me",
        "list demo point features in this bbox",
    ]


def test_inline_corpus_not_overridden_by_sibling(tmp_path):
    src_dir = tmp_path / "fetch_demo_vector"
    sd = vector_spec()
    sd["corpus"] = ["inline phrasing wins"]
    sy = _write_source_yaml(src_dir, sd)
    (src_dir / "corpus.yaml").write_text('fetch_demo_vector:\n  - "sibling phrasing"\n')
    spec = load_spec_from_path(sy)
    assert spec.corpus == ["inline phrasing wins"]


def test_compose_specs_from_tree_walks_and_keys_by_name(tmp_path):
    _write_source_yaml(tmp_path / "a" / "fetch_demo_raster", raster_spec())
    _write_source_yaml(tmp_path / "b" / "fetch_demo_vector", vector_spec())
    composed = compose_specs_from_tree(tmp_path)
    assert set(composed) == {"fetch_demo_raster", "fetch_demo_vector"}
    assert all(isinstance(s, SourceSpec) for s in composed.values())


def test_compose_specs_from_tree_skips_malformed(tmp_path):
    _write_source_yaml(tmp_path / "good" / "fetch_demo_vector", vector_spec())
    bad_dir = tmp_path / "bad" / "fetch_broken"
    bad_dir.mkdir(parents=True)
    (bad_dir / "source.yaml").write_text("name: broken\nshape: not-a-shape\n")
    composed = compose_specs_from_tree(tmp_path)
    # The malformed spec is skipped; the good one survives.
    assert "fetch_demo_vector" in composed
    assert "broken" not in composed


def _coverage(**over) -> dict:
    row = {
        "data_class": "terrain",
        "kind": "measured",
        "extent": {"kind": "surface",
                   "rings": [[[-125.0, 24.0], [-66.0, 24.0], [-66.0, 50.0],
                              [-125.0, 50.0]]]},
        "window": {"series": False},
        "resolution_m": 10.0,
        "units": {"elevation": "m"},
        "value_column": "elevation",
    }
    row.update(over)
    return row


def test_a_coverage_row_loads_onto_the_spec():
    spec = load_spec({**raster_spec(), "coverage": [_coverage()]})
    assert len(spec.coverage) == 1
    row = spec.coverage[0]
    assert row.data_class == "terrain"
    assert row.value_column == "elevation"
    assert row.extent.covers(-122.6, 45.5)
    assert not row.extent.covers(2.3, 48.9)


def test_a_source_serving_two_classes_carries_one_row_each():
    spec = load_spec({**raster_spec(),
                      "coverage": [_coverage(),
                                   _coverage(data_class="bathymetry")]})
    assert [row.data_class for row in spec.coverage] == ["terrain", "bathymetry"]


def test_two_rows_of_one_class_are_refused():
    with pytest.raises(SpecLoadError):
        load_spec({**raster_spec(), "coverage": [_coverage(), _coverage()]})


def test_a_column_the_row_states_no_unit_for_is_refused():
    with pytest.raises(SpecLoadError):
        load_spec({**raster_spec(),
                   "coverage": [_coverage(value_column="depth")]})


def test_a_coverage_row_naming_a_class_outside_the_vocabulary_is_refused():
    with pytest.raises(SpecLoadError):
        load_spec({**raster_spec(), "coverage": [_coverage(data_class="lidar")]})


def test_a_coverage_extent_with_no_ring_is_refused():
    with pytest.raises(SpecLoadError):
        load_spec({**raster_spec(),
                   "coverage": [_coverage(extent={"kind": "surface",
                                                  "rings": []})]})


def test_a_service_extent_carrying_rings_is_refused():
    with pytest.raises(SpecLoadError):
        load_spec({**raster_spec(),
                   "coverage": [_coverage(extent={
                       "kind": "service",
                       "rings": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0],
                                  [0.0, 1.0]]]})]})


def test_a_station_set_with_no_reach_is_refused():
    with pytest.raises(SpecLoadError):
        load_spec({**raster_spec(),
                   "coverage": [_coverage(extent={
                       "kind": "stations",
                       "rings": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0],
                                  [0.0, 1.0]]]})]})


def test_a_surface_stating_a_reach_is_refused():
    with pytest.raises(SpecLoadError):
        load_spec({**raster_spec(), "coverage": [_coverage(reach_km=25.0)]})


def test_a_spec_states_no_coverage_by_default():
    assert load_spec(raster_spec()).coverage == []


def test_a_source_takes_its_layer_s_datum_from_the_coverage_row_that_states_one():
    spec = load_spec({**raster_spec(),
                      "coverage": [_coverage(datum="NAVD88 (metres, positive up)")]})
    assert spec.vertical_datum == "NAVD88 (metres, positive up)"


def test_a_datum_stated_on_the_source_row_is_never_overwritten():
    spec = load_spec({**raster_spec(), "vertical_datum": "EGM2008",
                      "coverage": [_coverage(datum="NAVD88")]})
    assert spec.vertical_datum == "EGM2008"
