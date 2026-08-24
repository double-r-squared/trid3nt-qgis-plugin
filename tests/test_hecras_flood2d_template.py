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
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "hecras_flood_2d" in TOOL_REGISTRY
    m = TOOL_REGISTRY["hecras_flood_2d"].metadata
    assert m.engine == "hecras" and m.tier == "template"
    assert m.cacheable is False


def test_solver_name_registered():
    from trid3nt_server.workflows.hecras.run_hecras import HECRAS_SOLVER_NAMES

    assert "hecras_flood_2d" in HECRAS_SOLVER_NAMES


def test_bbox_coercion():
    from trid3nt_server.workflows.hecras.flood_2d.flood_2d import _coerce_bbox

    assert _coerce_bbox([-88.0, 38.0, -87.9, 38.1]) == [-88.0, 38.0, -87.9, 38.1]
    assert _coerce_bbox(None) is None
    assert _coerce_bbox([1, 2, 3]) is None  # wrong arity
    assert _coerce_bbox([-87.9, 38.1, -88.0, 38.0]) is None  # inverted -> rejected


def test_resolution_autoscale_respects_soft_cap():
    from trid3nt_server.workflows.hecras.flood_2d.flood_2d import (
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


def test_resolution_out_of_range_is_quoted_back_not_clamped():
    """ADR 0225 (upgrades ADR 0223's labeled snap): an out-of-[20, 200] m request is
    QUOTED BACK as a typed ResolutionOutOfRangeError (the range + native hint), never
    silently clamped to an undeclared value. An in-range value stays user-basis; an
    in-range value the AOI autoscale coarsens gets a labeled derived note (within the
    declared range)."""
    from trid3nt_server.workflows.hecras.flood_2d.flood_2d import (
        _autoscale_resolution,
        _RES_SPEC,
    )
    from trid3nt_server.tools.resolution_declared import (
        ResolutionOutOfRangeError,
        resolve_resolution,
    )

    tiny = [-87.95, 38.11, -87.90, 38.15]

    def _resolve(res_m):
        # ADR 0232: flood_2d resolves through the shared resolve_resolution seam.
        return resolve_resolution(
            res_m, spec=_RES_SPEC, autoscale=lambda x: _autoscale_resolution(tiny, x)
        )

    # (a) a 5 m request is OUT OF RANGE -> quoted back (typed error), NOT clamped.
    with pytest.raises(ResolutionOutOfRangeError) as ei:
        _resolve(5.0)
    assert "20-200 m" in str(ei.value) and "5 m requested" in str(ei.value)

    # (b) an in-range request on a small AOI is user-basis with no note.
    r2 = _resolve(60.0)
    assert r2.value == 60.0
    assert r2.basis == "user"
    assert r2.note is None

    # (c) a coarser-than-200 m request is ALSO out of range -> quoted back, not clamped.
    with pytest.raises(ResolutionOutOfRangeError):
        _resolve(500.0)


def test_equation_set_map_covers_choices():
    from trid3nt_server.workflows.hecras.flood_2d.flood_2d import (
        _EQUATION_SET_MAP,
        _DEFAULT_EQUATION_SET,
    )

    # the validated default maps to the plan-HDF Diffusion Wave string
    assert _DEFAULT_EQUATION_SET == "diffusion_wave"
    assert _EQUATION_SET_MAP["diffusion_wave"] == "Diffusion Wave"
    # the advanced full-momentum choice maps to a shallow-water solver string
    assert _EQUATION_SET_MAP["full_swe"] == "SWE-ELM"


def test_computation_interval_regex_accepts_hec_tokens():
    # ADR 0188: the stability-knob validator accepts int+SEC/MIN/HOUR, rejects prose.
    from trid3nt_server.workflows.hecras.flood_2d.flood_2d import (
        _COMPUTATION_INTERVAL_RE,
    )

    for good in ("30SEC", "1MIN", "5MIN", "2HOUR"):
        assert _COMPUTATION_INTERVAL_RE.match(good), good
    for bad in ("fast", "2 MIN", "MIN", "0.5MIN", "10sec"):
        assert not _COMPUTATION_INTERVAL_RE.match(bad), bad


@pytest.mark.asyncio
async def test_bad_bbox_returns_typed_error():
    from trid3nt_server.workflows.hecras.flood_2d.flood_2d import hecras_flood_2d
    from trid3nt_contracts.hecras_contracts import HECRAS_INPUT_INVALID

    out = await hecras_flood_2d(bbox=None, location=None)
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == HECRAS_INPUT_INVALID
