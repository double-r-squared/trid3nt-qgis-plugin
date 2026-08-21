"""Offline coverage for the ADR 0296 completion pass: GeoClaw Manning's n
domain split extended to the two sibling sites the original ADR parked
(``geoclaw_amr_refinement_regions``, ``geoclaw_tsunami_gauge_timeseries`` --
full-coverage law: no partial coverage on a ruled paradigm).

Mirrors ``tests/test_geoclaw_manning_domain_split.py``'s idiom: ``resolve_
overland_manning`` (+ ``gate_input_review`` where the site has one) is stubbed
at each composer's module namespace so no real MRLC/NLCD network call happens
offline. The live derive-vs-0.025 A/B is the live acceptance run, not here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.geoclaw_contracts import GeoClawDepthLayerURI
from trid3nt_server.workflows.geoclaw.amr_regions import amr_regions as amr
from trid3nt_server.workflows.geoclaw.gauge_timeseries import gauge_timeseries as gts
from trid3nt_server.workflows.shared.roughness_resolve import ManningResolution

_AOI = (-124.24, 41.73, -124.16, 41.78)
_WIN = {
    "min_level": 3, "max_level": 3, "t_start_s": 0.0, "t_end_s": 900.0,
    "min_lon": -124.21, "max_lon": -124.18, "min_lat": 41.745, "max_lat": 41.770,
}


async def _fake_gate(*, tool_name, mode, entries, params):
    return SimpleNamespace(
        cancelled=False, cancel_reason=None, entries=entries, params=params,
    )


def _fake_peak(run_args) -> GeoClawDepthLayerURI:
    return GeoClawDepthLayerURI(
        uri="s3://runs/peak.tif", layer_id="x", name="Peak flood depth",
        layer_type="raster", style_preset="continuous_flood_depth",
        bbox=tuple(run_args.bbox), role="primary", max_depth_m=1.0,
        flooded_area_km2=1.0, max_inundation_m=1.0, scenario=run_args.scenario,
    )


# --------------------------------------------------------------------------- #
# geoclaw_amr_refinement_regions -- same split as geoclaw_inundation (ADR 0296).
# --------------------------------------------------------------------------- #
def _install_amr_stubs(monkeypatch):
    """Stub the gate (pass-through) + the solve dispatch (capture run_args)."""
    monkeypatch.setattr(amr, "gate_input_review", _fake_gate)
    captured: dict = {}

    async def _fake_model_run(run_args, **kwargs):
        captured["run_args"] = run_args
        return _fake_peak(run_args)

    monkeypatch.setattr(amr, "model_geoclaw_inundation", _fake_model_run)
    return captured


@pytest.mark.asyncio
async def test_amr_dam_break_derives_manning_from_nlcd(monkeypatch):
    captured = _install_amr_stubs(monkeypatch)
    resolve_calls: list = []

    async def _fake_resolve(bbox, user_manning, *, param_name, **_kw):
        resolve_calls.append((tuple(bbox), user_manning, param_name))
        return ManningResolution(
            manning_n=0.062,
            source="nlcd_area_weighted",
            entry=SyntheticInput(
                param=param_name, value=0.062, units="s/m^(1/3)",
                basis="derived", consequence="physics",
                real_source_if_any="fetch_landcover (NLCD area-weighted Manning's n)",
                note="test-stubbed NLCD derivation.",
            ),
        )

    monkeypatch.setattr(amr, "resolve_overland_manning", _fake_resolve)

    result = await amr.geoclaw_amr_refinement_regions(
        bbox=_AOI, amr_regions=[_WIN], scenario="dam_break",
    )
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    ra = captured["run_args"]
    assert ra.manning_n == pytest.approx(0.062)
    assert resolve_calls and resolve_calls[0][2] == "manning_n"
    assert resolve_calls[0][1] is None  # no user override supplied


@pytest.mark.asyncio
async def test_amr_surge_derives_manning_from_nlcd(monkeypatch):
    captured = _install_amr_stubs(monkeypatch)

    async def _fake_resolve(bbox, user_manning, *, param_name, **_kw):
        return ManningResolution(
            manning_n=0.045,
            source="nlcd_area_weighted",
            entry=SyntheticInput(
                param=param_name, value=0.045, units="s/m^(1/3)",
                basis="derived", consequence="physics",
                real_source_if_any="fetch_landcover (NLCD area-weighted Manning's n)",
                note="test-stubbed NLCD derivation.",
            ),
        )

    monkeypatch.setattr(amr, "resolve_overland_manning", _fake_resolve)

    result = await amr.geoclaw_amr_refinement_regions(
        bbox=_AOI, amr_regions=[_WIN], scenario="surge",
    )
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.045)


@pytest.mark.asyncio
async def test_amr_dam_break_user_supplied_manning_bypasses_nlcd(monkeypatch):
    """A caller-supplied manning_n rides the user rung -- resolve_overland_manning
    is still called (it owns the user->derive->refuse ladder) but never fetches."""
    captured = _install_amr_stubs(monkeypatch)

    async def _fake_resolve(bbox, user_manning, *, param_name, **_kw):
        assert user_manning == pytest.approx(0.05)
        return ManningResolution(
            manning_n=float(user_manning), source="user_supplied",
            entry=SyntheticInput(
                param=param_name, value=float(user_manning), basis="user",
                note="test-stubbed user passthrough.",
            ),
        )

    monkeypatch.setattr(amr, "resolve_overland_manning", _fake_resolve)

    result = await amr.geoclaw_amr_refinement_regions(
        bbox=_AOI, amr_regions=[_WIN], scenario="dam_break", manning_n=0.05,
    )
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_amr_dam_break_unresolved_manning_refuses(monkeypatch):
    """NLCD cannot serve (fetch fails / no coverage) and the user supplied
    nothing -> GEOCLAW_PHYSICS_INPUT_REQUIRED, never a silent invented run."""
    _install_amr_stubs(monkeypatch)

    async def _fake_resolve(bbox, user_manning, *, param_name, **_kw):
        return ManningResolution(
            manning_n=None, source="unresolved",
            entry=SyntheticInput(
                param=param_name, value=None, basis="default_demo",
                consequence="physics",
                note="overland Manning's n could not be resolved from NLCD.",
            ),
        )

    monkeypatch.setattr(amr, "resolve_overland_manning", _fake_resolve)

    result = await amr.geoclaw_amr_refinement_regions(
        bbox=_AOI, amr_regions=[_WIN], scenario="dam_break",
    )
    assert isinstance(result, dict) and result["status"] == "error"
    assert result["error_code"] == "GEOCLAW_PHYSICS_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_amr_tsunami_keeps_literature_0025_and_skips_nlcd(monkeypatch):
    captured = _install_amr_stubs(monkeypatch)

    def _must_not_be_called(*a, **k):
        raise AssertionError("tsunami (offshore) must not call resolve_overland_manning")

    monkeypatch.setattr(amr, "resolve_overland_manning", _must_not_be_called)

    result = await amr.geoclaw_amr_refinement_regions(
        bbox=_AOI, amr_regions=[_WIN], scenario="tsunami",
    )
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.025)
    entry = next(e for e in result.synthetic_inputs if e.param == "manning_n")
    assert entry.basis == "default_demo"
    assert entry.consequence == "numerical"


@pytest.mark.asyncio
async def test_amr_tsunami_user_manning_overrides_the_literature_default(monkeypatch):
    captured = _install_amr_stubs(monkeypatch)
    monkeypatch.setattr(
        amr, "resolve_overland_manning",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    result = await amr.geoclaw_amr_refinement_regions(
        bbox=_AOI, amr_regions=[_WIN], scenario="tsunami", manning_n=0.04,
    )
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_amr_derived_land_value_differs_from_offshore_literature_default(monkeypatch):
    """The A/B: a stubbed derived land value differs from the offshore literal
    in the SAME test run, proving the split is real, not two paths converging on
    the same number."""
    captured_land = _install_amr_stubs(monkeypatch)

    async def _fake_resolve(bbox, user_manning, *, param_name, **_kw):
        return ManningResolution(
            manning_n=0.086, source="nlcd_area_weighted",
            entry=SyntheticInput(
                param=param_name, value=0.086, basis="derived",
                consequence="physics",
                real_source_if_any="fetch_landcover (NLCD area-weighted Manning's n)",
            ),
        )

    monkeypatch.setattr(amr, "resolve_overland_manning", _fake_resolve)
    await amr.geoclaw_amr_refinement_regions(
        bbox=_AOI, amr_regions=[_WIN], scenario="dam_break",
    )
    land_n = captured_land["run_args"].manning_n

    captured_offshore = _install_amr_stubs(monkeypatch)
    monkeypatch.setattr(
        amr, "resolve_overland_manning",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    await amr.geoclaw_amr_refinement_regions(
        bbox=_AOI, amr_regions=[_WIN], scenario="tsunami",
    )
    offshore_n = captured_offshore["run_args"].manning_n

    assert land_n == pytest.approx(0.086)
    assert offshore_n == pytest.approx(0.025)
    assert land_n != offshore_n


# --------------------------------------------------------------------------- #
# geoclaw_tsunami_gauge_timeseries -- ALWAYS offshore; label-only pass (no
# derivation possible or needed -- it never varies by scenario).
# --------------------------------------------------------------------------- #
def _install_gts_stubs(monkeypatch):
    captured: dict = {}

    async def _fake_model_run(run_args, **kwargs):
        captured["run_args"] = run_args
        captured["kwargs"] = kwargs
        return _fake_peak(run_args)

    monkeypatch.setattr(gts, "model_geoclaw_inundation", _fake_model_run)
    return captured


@pytest.mark.asyncio
async def test_gauge_timeseries_default_manning_labeled_literature_0025(monkeypatch):
    captured = _install_gts_stubs(monkeypatch)

    result = await gts.geoclaw_tsunami_gauge_timeseries(bbox=_AOI)
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.025)
    si = captured["kwargs"]["synthetic_inputs"]
    entry = next(e for e in si if e.param == "manning_n")
    assert entry.basis == "default_demo"
    assert entry.consequence == "numerical"
    assert entry.value == pytest.approx(0.025)


@pytest.mark.asyncio
async def test_gauge_timeseries_user_manning_rides_user_basis(monkeypatch):
    captured = _install_gts_stubs(monkeypatch)

    result = await gts.geoclaw_tsunami_gauge_timeseries(bbox=_AOI, manning_n=0.04)
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.04)
    si = captured["kwargs"]["synthetic_inputs"]
    entry = next(e for e in si if e.param == "manning_n")
    assert entry.basis == "user"
    assert entry.value == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_gauge_timeseries_never_touches_nlcd():
    """Static proof this template has no resolve_overland_manning import to call
    -- it is ALWAYS offshore, so no land-dominated leg exists here."""
    assert not hasattr(gts, "resolve_overland_manning")
