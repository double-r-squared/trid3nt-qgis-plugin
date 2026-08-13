"""Tests for the server-side mesh sizing component (ADR 0099, mesh M2)."""
from __future__ import annotations

import pytest

from trid3nt_server.agent.mesh.refine_regions import (
    MeshSizingSpec,
    mesh_sizing_from_refine_regions,
    refine_level_for,
)


def _entry(target, bbox=(0.0, 0.0, 1.0, 1.0)):
    ring = [
        [bbox[0], bbox[1]],
        [bbox[2], bbox[1]],
        [bbox[2], bbox[3]],
        [bbox[0], bbox[3]],
        [bbox[0], bbox[1]],
    ]
    return {
        "polygon": {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {},
        },
        "target_size_m": target,
        "bbox": bbox,
    }


def test_refine_level_ladder() -> None:
    assert refine_level_for(100.0, 100.0) == 0
    assert refine_level_for(100.0, 50.0) == 1
    assert refine_level_for(100.0, 24.0) == 3  # ceil(log2(100/24)) = 3
    assert refine_level_for(100.0, 300.0) == 0


def test_empty_is_uniform() -> None:
    spec = mesh_sizing_from_refine_regions([], 100.0)
    assert isinstance(spec, MeshSizingSpec)
    assert spec.is_uniform
    assert spec.max_refine_level == 0


def test_explicit_target_resolved() -> None:
    spec = mesh_sizing_from_refine_regions([_entry(25.0)], 100.0)
    assert len(spec.regions) == 1
    r = spec.regions[0]
    assert r.target_size_m == 25.0
    assert r.refine_level == 2
    assert spec.max_refine_level == 2
    assert not spec.is_uniform


def test_missing_target_defaults_half_base() -> None:
    spec = mesh_sizing_from_refine_regions([_entry(None)], 100.0)
    assert spec.regions[0].target_size_m == 50.0
    assert spec.regions[0].refine_level == 1


def test_custom_default_target() -> None:
    spec = mesh_sizing_from_refine_regions(
        [_entry(None)], 100.0, default_target_size_m=10.0
    )
    assert spec.regions[0].target_size_m == 10.0


def test_bad_base_size_raises() -> None:
    with pytest.raises(ValueError):
        mesh_sizing_from_refine_regions([_entry(25.0)], 0.0)
