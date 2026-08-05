"""Offline unit gates for the hecras_flood_2d template (ADR 0140 promotion).

Pure-logic gates only (NO docker / NO fetch / NO solve): the bbox coercion, the
granularity-gated resolution autoscaler + cell-count estimate, the archetype
literal, and the template registration/metadata. The live author->compose->solve
chain is proven by the direct-call acceptances (ADR 0140), not the offline suite.
"""
from __future__ import annotations

import pytest

from trid3nt_contracts.hecras_contracts import HECRAS_ARCHETYPES, HECRASRunArgs


def test_fresh_aoi_archetype_present():
    assert "fresh_aoi_flood_2d" in HECRAS_ARCHETYPES
    a = HECRASRunArgs(archetype="fresh_aoi_flood_2d")
    assert a.archetype == "fresh_aoi_flood_2d"


def test_template_registered_engine_tier():
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    assert "hecras_flood_2d" in TOOL_REGISTRY
    m = TOOL_REGISTRY["hecras_flood_2d"].metadata
    assert m.engine == "hecras" and m.tier == "template"
    assert m.cacheable is False


def test_solver_name_registered():
    from trid3nt_server.agent.workflows.hecras.run_hecras import HECRAS_SOLVER_NAMES

    assert "hecras_flood_2d" in HECRAS_SOLVER_NAMES


def test_bbox_coercion():
    from trid3nt_server.agent.workflows.hecras.flood_2d.flood_2d import _coerce_bbox

    assert _coerce_bbox([-88.0, 38.0, -87.9, 38.1]) == [-88.0, 38.0, -87.9, 38.1]
    assert _coerce_bbox(None) is None
    assert _coerce_bbox([1, 2, 3]) is None  # wrong arity
    assert _coerce_bbox([-87.9, 38.1, -88.0, 38.0]) is None  # inverted -> rejected


def test_resolution_autoscale_respects_soft_cap():
    from trid3nt_server.agent.workflows.hecras.flood_2d.flood_2d import (
        _autoscale_resolution,
        _estimate_cells,
        _SOFT_CELL_CAP,
        _MAX_RES_M,
    )

    # a large AOI at a fine resolution must be coarsened under the soft cap
    big = [-88.0, 38.0, -87.7, 38.3]  # ~30 km square
    res = _autoscale_resolution(big, 20.0)
    assert res >= 20.0
    assert _estimate_cells(big, res) <= _SOFT_CELL_CAP or res == _MAX_RES_M
    # a tiny AOI keeps its resolution (already under the cap)
    tiny = [-87.95, 38.11, -87.90, 38.15]
    assert _autoscale_resolution(tiny, 60.0) == 60.0


@pytest.mark.asyncio
async def test_bad_bbox_returns_typed_error():
    from trid3nt_server.agent.workflows.hecras.flood_2d.flood_2d import hecras_flood_2d
    from trid3nt_contracts.hecras_contracts import HECRAS_INPUT_INVALID

    out = await hecras_flood_2d(bbox=None, location=None)
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == HECRAS_INPUT_INVALID
