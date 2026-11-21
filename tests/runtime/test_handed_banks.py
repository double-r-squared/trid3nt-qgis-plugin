"""A river run may hand the water surface its reach is cut from; the run's own fetch of it is then not made.

Offline, on recorded real answers: the NHD water surface around the Missouri calibration release (7.5 km), the
footprint polygons of the two CENWO eHydro packages over that reach, and NLDI's main stem walked 7 km down."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import shape

from trid3nt.inputs.domain import domain
from trid3nt.inputs.slots import DOMAIN
from trid3nt.tools import TOOL_REGISTRY
from trid3nt.workflows.runtime import fill
from trid3nt.workflows.runtime.workflow import BANKS

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_WATER = _FIXTURES / "nhd_water_surface" / "missouri_release_7_5km.fgb"
_FOOTPRINT = _FIXTURES / "ehydro" / "missouri_omaha_survey_footprints.fgb"
_LINE = _FIXTURES / "nhdplus_nldi" / "missouri_release_dm_7km.fgb"
_RELEASE = [-95.921233757, 41.257659140]


def _area_ha(geometry) -> float:
    return float(gpd.GeoSeries([shape(geometry)], crs=4326).to_crs(32615).area.iloc[0]) / 1e4


@pytest.fixture
def clipped(tmp_path) -> Path:
    water = gpd.read_file(_WATER).to_crs(32615)
    footprint = gpd.read_file(_FOOTPRINT).to_crs(32615)[["geometry"]]
    out = tmp_path / "banks_clipped.fgb"
    gpd.overlay(water, footprint, how="intersection").to_crs(4326).to_file(out, driver="FlatGeobuf")
    return out


def _cut(banks):
    return domain(str(_LINE), banks=str(banks), near=tuple(_RELEASE), span_km=7.0, above_m=60.0)


def test_the_cut_over_handed_banks_drops_the_clipped_water_and_keeps_the_reach(clipped) -> None:
    whole, kept = _cut(_WATER), _cut(clipped)
    assert _area_ha(kept.geometry) < _area_ha(whole.geometry) - 10.0
    assert [r.feature["properties"]["type"] for r in kept.runs] == ["inflow", "outflow"]
    assert kept.companions["centerline"] == whole.companions["centerline"]


def test_a_river_template_lists_the_banks_and_a_domain_of_its_own_kind_lists_none() -> None:
    rows = {r.name: r for r in TOOL_REGISTRY["telemac_dye_release"].fn.workflow.inputs}
    assert rows["banks"].stated_only and rows["banks"].geometry == "polygon" and rows["banks"].desc == BANKS.desc
    assert all(r.name != "banks" for r in TOOL_REGISTRY["modflow_water_table"].fn.workflow.inputs)


def test_handed_banks_are_the_ones_the_domain_is_cut_from_and_the_fetch_is_not_made(clipped, monkeypatch) -> None:
    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    env = fill._Env(data={r.name: r for r in workflow.inputs if r.is_row}, run={"release": _RELEASE},
                    supplied={"banks": str(clipped)})
    row = next(r for r in workflow.inputs if r.slot is DOMAIN)

    async def _no_fetch(*_a, **_k):
        raise AssertionError("the water surface was fetched beside handed banks")

    monkeypatch.setattr(fill, "_probe", _no_fetch)
    held = asyncio.run(fill._beside(env, dataclasses.replace(row, kind="flowline")))
    assert held == {"banks": str(clipped)}
