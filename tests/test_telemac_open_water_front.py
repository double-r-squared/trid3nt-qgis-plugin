"""The shared open-water front's contracts: idealized solves, sizing rows, origins.

Three defects these pin, all of the same family - the code said something the run
did not do:

  - an IDEALIZED domain reports no UTM zone by construction, and the dispatch
    refused it as ungeoreferenceable, so the readers' complete local-frame
    branches were unreachable;
  - a 3 cm disagreement between an asked 33.33 m and a reported 33.3 m raised a
    provenance row claiming the value had been RAISED, in the direction it was
    not moved;
  - the local mesh origin was reconstructed from the UNROUNDED AOI while the
    worker meshed the rounded one, which offsets the whole field.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.workflows.telemac.postprocess_telemac import (
    PostprocessTelemacError,
    _local_mesh_origin,
)
from trid3nt_server.workflows.telemac.steps.agitation import write_agitation_deck
from trid3nt_server.workflows.telemac.steps.coastal import write_coastal_deck
from trid3nt_server.workflows.telemac.steps.open_water import (
    OpenWaterError,
    mesh_sizing_provenance,
    solved_domain_bbox,
)
from trid3nt_server.workflows.telemac.steps.stratified import write_stratified_deck
from trid3nt_server.workflows.telemac.steps.wave import write_wave_deck

_MARQUETTE = {"name": "Marquette Lower Harbor", "slug": "marquette",
              "lon": -87.380, "lat": 46.539,
              "bbox": [-87.39234, 46.52812, -87.36788, 46.55021]}
_APALACH = {"name": "Apalachicola Bay", "slug": "apalachicola",
            "lon": -84.96, "lat": 29.745,
            "bbox": [-85.02341, 29.69107, -84.90118, 29.80442]}


# --------------------------------------------------------------------------- #
# requires_utm: an idealized domain is not a georeference failure.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("wave_mode,expected", [
    ("resonance", False), ("shoal", False), ("diffraction", True)])
def test_agitation_deck_requires_utm_only_on_the_real_harbour(wave_mode, expected):
    deck = asyncio.run(write_agitation_deck(
        aoi=_MARQUETTE, wave_mode=wave_mode, bathy_source="noaa_greatlakes",
        mesh_resolution_m=12.0))
    assert deck["requires_utm"] is expected
    assert deck["real_bathymetry"] is expected


def test_agitation_idealized_bathy_source_never_requires_utm():
    deck = asyncio.run(write_agitation_deck(
        aoi=_MARQUETTE, wave_mode="diffraction", bathy_source="idealized",
        mesh_resolution_m=12.0))
    assert deck["requires_utm"] is False


@pytest.mark.parametrize("flow_mode,expected", [
    ("salt_wedge", False), ("stratification", True)])
def test_stratified_deck_requires_utm_only_on_the_real_lake(flow_mode, expected):
    deck = asyncio.run(write_stratified_deck(
        aoi=_MARQUETTE, flow_mode=flow_mode, bathy_source="noaa_greatlakes"))
    assert deck["requires_utm"] is expected


def test_coastal_and_wave_decks_always_require_utm():
    coastal = asyncio.run(write_coastal_deck(
        aoi=_APALACH, bathy_source="synthetic", mesh_resolution_m=250.0))
    wave = asyncio.run(write_wave_deck(
        aoi=_MARQUETTE, bathy_source="noaa_greatlakes", mesh_resolution_m=3000.0))
    assert coastal["requires_utm"] is True
    assert wave["requires_utm"] is True


def test_salt_wedge_deck_authors_an_idealized_lock_exchange_offline():
    """The salt-wedge deck constructs and authors with no network and no zone."""
    deck = asyncio.run(write_stratified_deck(
        aoi=_MARQUETTE, flow_mode="salt_wedge", bathy_source="noaa_greatlakes",
        mesh_resolution_m=250.0))
    assert deck["config"]["flow_mode"] == "salt_wedge"
    assert deck["config"]["bathy_source"] == "idealized"
    assert "bbox" not in deck["config"]
    assert deck["requires_utm"] is False
    assert "lock-exchange" in deck["bathy_label"]


# --------------------------------------------------------------------------- #
# mesh_sizing_provenance: direction, and the report's own precision.
# --------------------------------------------------------------------------- #

def test_no_row_when_the_ask_was_honoured():
    assert mesh_sizing_provenance(40.0, {"dx_m": 40.0}) == []


def test_no_row_for_a_dx_reporting_rounding_artifact():
    """33.33 asked, 33.3 reported: the worker rounds dx_m to 0.1 m, that is all."""
    assert mesh_sizing_provenance(33.33, {"dx_m": 33.3}) == []
    assert mesh_sizing_provenance(8.34, {"dx_m": 8.3}) == []


def test_a_raised_spacing_says_raised_and_names_the_grid_floor():
    (row,) = mesh_sizing_provenance(5.0, {"dx_m": 40.0, "coarsened": False})
    assert row.param == "target_resolution_m"
    assert row.value == 5.0
    assert "RAISED to 40 m" in row.note
    assert "grid floor" in row.note
    assert "LOWERED" not in row.note


def test_a_budget_coarsened_spacing_names_the_node_budget():
    (row,) = mesh_sizing_provenance(500.0, {"dx_m": 2300.0, "coarsened": True})
    assert "RAISED to 2300 m" in row.note
    assert "node budget" in row.note


def test_a_lowered_spacing_says_lowered_and_blames_neither():
    """The old note claimed RAISED unconditionally - the opposite of what happened."""
    (row,) = mesh_sizing_provenance(100.0, {"dx_m": 30.0, "coarsened": False})
    assert "LOWERED to 30 m" in row.note
    assert "RAISED" not in row.note
    assert "grid floor" not in row.note
    assert "node budget" not in row.note


def test_no_row_without_an_ask_or_without_a_built_spacing():
    assert mesh_sizing_provenance(None, {"dx_m": 40.0}) == []
    assert mesh_sizing_provenance(40.0, {}) == []


# --------------------------------------------------------------------------- #
# The mesh origin: the corner the WORKER built from, not the one the user typed.
# --------------------------------------------------------------------------- #

def test_solved_domain_bbox_prefers_the_worker_echo():
    deck = {"config": {"bbox": [-85.0234, 29.6911, -84.9012, 29.8044]}}
    echoed = [-85.02341, 29.69107, -84.90118, 29.80442]
    assert solved_domain_bbox(deck, {"bbox": echoed}) == tuple(echoed)


def test_solved_domain_bbox_falls_back_to_the_staged_rounded_bbox():
    """The manifest's bbox is what the worker was handed - never the raw AOI."""
    staged = [-85.0234, 29.6911, -84.9012, 29.8044]
    deck = {"config": {"bbox": staged}}
    assert solved_domain_bbox(deck, {}) == tuple(staged)


def test_solved_domain_bbox_is_none_for_a_geography_free_domain():
    assert solved_domain_bbox({"config": {}}, {}) is None


def test_solved_domain_bbox_refuses_a_malformed_bbox():
    with pytest.raises(OpenWaterError):
        solved_domain_bbox({"config": {"bbox": [1.0, 2.0, 3.0]}}, {})
    with pytest.raises(OpenWaterError):
        solved_domain_bbox({"config": {"bbox": ["west", 2.0, 3.0, 4.0]}}, {})


def test_the_coastal_deck_no_longer_carries_a_second_unrounded_bbox():
    """One bbox travels: the staged one. Two was how the origin drifted."""
    deck = asyncio.run(write_coastal_deck(
        aoi=_APALACH, bathy_source="synthetic", mesh_resolution_m=250.0))
    assert "domain_bbox" not in deck
    assert deck["config"]["bbox"] == [round(v, 4) for v in _APALACH["bbox"]]


def test_local_mesh_origin_absent_bbox_is_the_local_frame():
    assert _local_mesh_origin(None, 32616) == (0.0, 0.0)


def test_local_mesh_origin_refuses_when_the_reader_needs_one():
    with pytest.raises(PostprocessTelemacError) as excinfo:
        _local_mesh_origin(None, 32616, required=True, context="postprocess_coastal")
    assert excinfo.value.error_code == "TELEMAC_PARAMS_INVALID"


@pytest.mark.parametrize("bad", [[1.0, 2.0, 3.0], ["a", "b", "c", "d"], (1, 2)])
def test_local_mesh_origin_refuses_a_malformed_bbox_present_or_required(bad):
    """Present-but-malformed is never read as absent: that lands at the false origin."""
    with pytest.raises(PostprocessTelemacError):
        _local_mesh_origin(bad, 32616)


def test_local_mesh_origin_is_the_sw_corner():
    x, y = _local_mesh_origin([-85.02, 29.69, -84.90, 29.80], 32616)
    from pyproj import Transformer

    fwd = Transformer.from_crs(4326, 32616, always_xy=True)
    assert (x, y) == pytest.approx(fwd.transform(-85.02, 29.69), abs=1e-6)


def test_a_rounded_corner_moves_the_origin_by_metres():
    """Why the fallback is the STAGED bbox: 4-decimal rounding is metres on the ground."""
    raw = _local_mesh_origin([-85.02341, 29.69107, -84.90118, 29.80442], 32616)
    staged = _local_mesh_origin([-85.0234, 29.6911, -84.9012, 29.8044], 32616)
    offset = max(abs(raw[0] - staged[0]), abs(raw[1] - staged[1]))
    assert 0.05 < offset < 100.0
