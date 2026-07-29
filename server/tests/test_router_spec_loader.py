"""Offline tests for the router source-spec loader (B1 -- router-core).

Coverage:
- ``load_spec`` accepts a well-formed spec for each pilot shape.
- ``load_spec`` rejects malformed specs (missing required key, shape/output
  mismatch, join over a non-vector shape) with ``SpecLoadError``.
- ``load_spec_from_path`` lifts corpus phrasings from the sibling ``corpus.yaml``
  when the spec omits them (co-located corpus pickup).
- ``compose_specs_from_tree`` rglobs ``source.yaml`` and keys by name; a
  malformed spec in the tree is skipped, not fatal.

No network. Synthetic YAML written to a tmp dir.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.agent.tools.fetchers._router.spec import (
    SpecLoadError,
    compose_specs_from_tree,
    load_spec,
    load_spec_from_path,
)


# --------------------------------------------------------------------------- #
# Minimal well-formed spec dicts (one per shape + the two transforms).
# --------------------------------------------------------------------------- #


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
        "output": {"layer_type": "raster", "ext": "tif", "style_preset": "demo_{variable}"},
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
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "demo_vec"},
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
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "acs"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "per_feature", "kb_per_feature": 2.0},
    }


# --------------------------------------------------------------------------- #
# load_spec: good specs
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# load_spec: bad specs
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# co-located corpus pickup + tree walk
# --------------------------------------------------------------------------- #


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
