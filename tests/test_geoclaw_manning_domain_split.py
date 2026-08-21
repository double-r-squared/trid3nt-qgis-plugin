"""Offline coverage for ADR 0296: GeoClaw Manning's n split by domain character.

``geoclaw_inundation`` resolves bottom-friction Manning's n differently by
scenario: dam_break / surge (land-dominated / mixed-coastal) DERIVE from NLCD
via the shared ``roughness_resolve`` seam (the storm_surge precedent); tsunami
(offshore -- ``GEOCLAW_OFFSHORE_SCENARIOS``) keeps the published Chow (1959)
0.025 open-water standard, now loudly labeled instead of silent.

Mirrors the swmm urban_flood offline idiom (``test_urban_flood_publish_
offloop.py::_stub_overland_manning_resolution``): ``resolve_overland_manning``
is stubbed at the composer's module namespace so these tests never hit the
real MRLC fetch_landcover network call. The live derive-vs-0.025 A/B is the
LIVE acceptance run (ADR 0296), not here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.geoclaw_contracts import GeoClawDepthLayerURI
from trid3nt_server.workflows.geoclaw.inundation import inundation as comp
from trid3nt_server.workflows.shared.roughness_resolve import ManningResolution

_AOI = (-85.75, 29.55, -85.25, 30.20)


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


def _install_common_stubs(monkeypatch):
    """Stub the gate (pass-through) + the solve dispatch (capture run_args)."""
    monkeypatch.setattr(comp, "gate_input_review", _fake_gate)
    captured: dict = {}

    async def _fake_model_run(run_args, **kwargs):
        captured["run_args"] = run_args
        return _fake_peak(run_args)

    monkeypatch.setattr(comp, "model_geoclaw_inundation", _fake_model_run)
    return captured


# --------------------------------------------------------------------------- #
# Land-dominated (dam_break / surge): DERIVES from NLCD via resolve_overland_
# manning -- never invents a literal 0.025.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_dam_break_derives_manning_from_nlcd(monkeypatch):
    captured = _install_common_stubs(monkeypatch)
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

    monkeypatch.setattr(comp, "resolve_overland_manning", _fake_resolve)

    result = await comp.geoclaw_inundation(
        bbox=_AOI, scenario="dam_break",
        source_lonlat=(-85.5, 29.9), dam_break_depth_m=8.0,
    )
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    ra = captured["run_args"]
    assert ra.manning_n == pytest.approx(0.062)
    assert resolve_calls and resolve_calls[0][2] == "manning_n"
    assert resolve_calls[0][1] is None  # no user override supplied


@pytest.mark.asyncio
async def test_surge_derives_manning_from_nlcd(monkeypatch):
    captured = _install_common_stubs(monkeypatch)

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

    monkeypatch.setattr(comp, "resolve_overland_manning", _fake_resolve)

    result = await comp.geoclaw_inundation(bbox=_AOI, scenario="surge")
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.045)


@pytest.mark.asyncio
async def test_dam_break_user_supplied_manning_bypasses_nlcd(monkeypatch):
    """A caller-supplied manning_n rides the user rung -- resolve_overland_manning
    is still called (it owns the user->derive->refuse ladder) but never fetches."""
    captured = _install_common_stubs(monkeypatch)

    async def _fake_resolve(bbox, user_manning, *, param_name, **_kw):
        assert user_manning == pytest.approx(0.05)
        return ManningResolution(
            manning_n=float(user_manning), source="user_supplied",
            entry=SyntheticInput(
                param=param_name, value=float(user_manning), basis="user",
                note="test-stubbed user passthrough.",
            ),
        )

    monkeypatch.setattr(comp, "resolve_overland_manning", _fake_resolve)

    result = await comp.geoclaw_inundation(
        bbox=_AOI, scenario="dam_break",
        source_lonlat=(-85.5, 29.9), dam_break_depth_m=8.0, manning_n=0.05,
    )
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_dam_break_unresolved_manning_refuses(monkeypatch):
    """NLCD cannot serve (fetch fails / no coverage) and the user supplied
    nothing -> GEOCLAW_PHYSICS_INPUT_REQUIRED, never a silent invented run."""
    _install_common_stubs(monkeypatch)

    async def _fake_resolve(bbox, user_manning, *, param_name, **_kw):
        return ManningResolution(
            manning_n=None, source="unresolved",
            entry=SyntheticInput(
                param=param_name, value=None, basis="default_demo",
                consequence="physics",
                note="overland Manning's n could not be resolved from NLCD.",
            ),
        )

    monkeypatch.setattr(comp, "resolve_overland_manning", _fake_resolve)

    result = await comp.geoclaw_inundation(
        bbox=_AOI, scenario="dam_break",
        source_lonlat=(-85.5, 29.9), dam_break_depth_m=8.0,
    )
    assert isinstance(result, dict) and result["status"] == "error"
    assert result["error_code"] == "GEOCLAW_PHYSICS_INPUT_REQUIRED"


# --------------------------------------------------------------------------- #
# Offshore (tsunami): keeps the published 0.025, never touches NLCD.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tsunami_keeps_literature_0025_and_skips_nlcd(monkeypatch):
    captured = _install_common_stubs(monkeypatch)

    def _must_not_be_called(*a, **k):
        raise AssertionError("tsunami (offshore) must not call resolve_overland_manning")

    monkeypatch.setattr(comp, "resolve_overland_manning", _must_not_be_called)

    result = await comp.geoclaw_inundation(bbox=_AOI, scenario="tsunami")
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.025)


@pytest.mark.asyncio
async def test_tsunami_user_manning_overrides_the_literature_default(monkeypatch):
    captured = _install_common_stubs(monkeypatch)
    monkeypatch.setattr(
        comp, "resolve_overland_manning",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    result = await comp.geoclaw_inundation(bbox=_AOI, scenario="tsunami", manning_n=0.04)
    assert not isinstance(result, dict), f"unexpected error envelope: {result}"
    assert captured["run_args"].manning_n == pytest.approx(0.04)


# --------------------------------------------------------------------------- #
# The A/B: derived land-dominated value is REAL and differs from the offshore
# literature constant (never the same invented 0.025 riding both legs).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_derived_land_value_differs_from_offshore_literature_default(monkeypatch):
    captured_land = _install_common_stubs(monkeypatch)

    async def _fake_resolve(bbox, user_manning, *, param_name, **_kw):
        return ManningResolution(
            manning_n=0.086, source="nlcd_area_weighted",
            entry=SyntheticInput(
                param=param_name, value=0.086, basis="derived",
                consequence="physics",
                real_source_if_any="fetch_landcover (NLCD area-weighted Manning's n)",
            ),
        )

    monkeypatch.setattr(comp, "resolve_overland_manning", _fake_resolve)
    await comp.geoclaw_inundation(
        bbox=_AOI, scenario="dam_break",
        source_lonlat=(-85.5, 29.9), dam_break_depth_m=8.0,
    )
    land_n = captured_land["run_args"].manning_n

    captured_offshore = _install_common_stubs(monkeypatch)
    monkeypatch.setattr(
        comp, "resolve_overland_manning",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    await comp.geoclaw_inundation(bbox=_AOI, scenario="tsunami")
    offshore_n = captured_offshore["run_args"].manning_n

    assert land_n == pytest.approx(0.086)
    assert offshore_n == pytest.approx(0.025)
    assert land_n != offshore_n
