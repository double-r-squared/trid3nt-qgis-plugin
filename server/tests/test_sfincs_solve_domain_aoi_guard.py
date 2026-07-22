"""#183 (NATE compute-domain guard): the SFINCS solve domain is EXACTLY the
active AOI bbox -- no padding, no un-required expansion.

NATE DIRECTIVE (verbatim intent): "The SFINCS runs should ONLY compute within the
bbox unless something requires it to expand, so we shouldn't need to clip the
publish COG." This file LOCKS that invariant in two places:

ENGINE side (the grid is built from the bbox with no padding):
  ``_generate_hydromt_yaml_config`` emits
  ``setup_grid_from_region: { region: { bbox: [...] }, res: <m> }`` directly from
  the AOI bbox. These tests assert the emitted grid bbox == the active AOI bbox
  BYTE-FOR-BYTE (no buffer / pad), for a normal PLUVIAL deck AND a COASTAL /
  archetype deck (a surge run), with autoscale ON -- proving the autoscaler
  coarsens ``res`` ONLY and never widens the extent. (job-0318's active-mask
  ELEVATION window is an orthogonal cell-mask concern and is NOT touched here.)

SERVER side (the residual #159 lineage -- the SOLVE was not always re-derived
from the CURRENT active AOI):
  ``_maybe_default_solver_bbox_to_pinned_aoi`` pins an expensive solver's bbox to
  the active Case AOI by the SAME conservative rule the fetch default uses --
  snap a drifted / wider same-area box DOWN to the active AOI, but HONOR an
  explicit WIDEN (encloses the pin) or a DIFFERENT place (disjoint). The first
  solve (no AOI pinned yet) and non-solver tools are no-ops, and an archetype /
  coastal run (selected by forcing FLAGS, never an enclosing-wider bbox) is left
  byte-identical.

Mirrors the harness in ``test_sfincs_builder_surge_forcing.py`` (engine) and
``test_aoi_pin_lane_c.py`` (server pure-rule).
"""

from __future__ import annotations

import yaml

from grace2_agent.server import _maybe_default_solver_bbox_to_pinned_aoi
from grace2_agent.workflows.sfincs_builder import (
    BuildOptions,
    ForcingSpec,
    WaterlevelForcing,
    WindForcing,
    _generate_hydromt_yaml_config,
)

# A normal inland pluvial AOI (the active AOI a non-archetype solve runs on).
_PLUVIAL_BBOX = (-85.32, 35.02, -85.24, 35.08)

# A coastal AOI near Mexico Beach, FL (the SFINCS North Star + archetype geography).
_COASTAL_BBOX = (-85.45, 29.92, -85.38, 29.98)

# Local paths so ``_stage_gcs_local`` is a no-op (no GCS/S3 in unit tests). The
# DEM path is intentionally unreadable -> the active-mask falls back to the wide
# window, which is irrelevant to the GRID EXTENT assertion under test.
_DEM = "/tmp/does-not-exist-dep.tif"
_LC = "/tmp/lc.tif"
_MAP = "/tmp/manning.csv"


def _emit(bbox, forcing: ForcingSpec, options: BuildOptions) -> dict:
    """Emit + parse the deck YAML for ``bbox`` / ``forcing`` / ``options``."""
    text = _generate_hydromt_yaml_config(
        bbox=bbox,
        options=options,
        dem_local_path=_DEM,
        landcover_local_path=_LC,
        river_local_path=None,
        forcing=forcing,
        mapping_csv_path=_MAP,
    )
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict), f"YAML did not parse to a dict:\n{text}"
    return parsed


def _grid_bbox(deck: dict) -> list:
    """The ``setup_grid_from_region`` region bbox from a parsed deck."""
    region = deck["setup_grid_from_region"]["region"]
    return list(region["bbox"])


# --------------------------------------------------------------------------- #
# ENGINE: the SFINCS grid extent == the active AOI bbox, byte-for-byte
# --------------------------------------------------------------------------- #


def test_pluvial_grid_bbox_equals_active_aoi_no_padding() -> None:
    """A normal pluvial deck builds the grid from the EXACT AOI bbox (no pad)."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=8.0,
        duration_hours=24.0,
        return_period_years=100,
    )
    # autoscale ON -- proves the autoscaler only changes ``res``, not the extent.
    deck = _emit(
        _PLUVIAL_BBOX,
        forcing,
        BuildOptions(grid_resolution_m=30.0, autoscale_grid=True),
    )
    assert _grid_bbox(deck) == [
        _PLUVIAL_BBOX[0],
        _PLUVIAL_BBOX[1],
        _PLUVIAL_BBOX[2],
        _PLUVIAL_BBOX[3],
    ], "pluvial SFINCS grid extent must equal the active AOI bbox with NO padding"


def test_coastal_archetype_grid_bbox_equals_active_aoi_no_padding() -> None:
    """A COASTAL / surge (archetype) deck also builds the grid from the EXACT
    AOI bbox -- archetypes differ by FORCING, never by a widened extent."""
    forcing = ForcingSpec(
        forcing_type="storm_surge",
        waterlevel=WaterlevelForcing(
            timeseries_uri="/tmp/wl.csv",
            locations_uri="/tmp/bnd.fgb",
            offset=0.15,
        ),
        wind=WindForcing(magnitude=45.0, direction=170.0),
    )
    deck = _emit(
        _COASTAL_BBOX,
        forcing,
        # Coastal cadence on + autoscale on; neither widens the extent.
        BuildOptions(
            grid_resolution_m=50.0,
            autoscale_grid=True,
            output_interval_min=5.0,
        ),
    )
    # The surge forcing blocks ARE present (archetype is intact) ...
    assert "setup_waterlevel_forcing" in deck
    assert "setup_wind_forcing" in deck
    # ... and the grid extent is still EXACTLY the AOI bbox.
    assert _grid_bbox(deck) == [
        _COASTAL_BBOX[0],
        _COASTAL_BBOX[1],
        _COASTAL_BBOX[2],
        _COASTAL_BBOX[3],
    ], "coastal SFINCS grid extent must equal the active AOI bbox with NO padding"


def test_grid_bbox_invariant_to_resolution_choice() -> None:
    """Changing the resolution (the autoscale lever) NEVER changes the extent."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=6.0,
        duration_hours=12.0,
        return_period_years=50,
    )
    fine = _emit(_PLUVIAL_BBOX, forcing, BuildOptions(grid_resolution_m=30.0))
    coarse = _emit(_PLUVIAL_BBOX, forcing, BuildOptions(grid_resolution_m=120.0))
    # res differs ...
    assert (
        coarse["setup_grid_from_region"]["res"]
        != fine["setup_grid_from_region"]["res"]
    )
    # ... but the grid bbox is identical (and == the AOI).
    assert _grid_bbox(fine) == _grid_bbox(coarse) == list(_PLUVIAL_BBOX)


# --------------------------------------------------------------------------- #
# SERVER: the SOLVER bbox is pinned to the active AOI (the #159 lineage)
# --------------------------------------------------------------------------- #

_SOLVE_DOMAIN = (-97.755, 30.26, -97.725, 30.285)


def test_solver_bare_bbox_defaults_to_active_aoi() -> None:
    """A follow-up solve with NO bbox runs on the active AOI."""
    pin = list(_SOLVE_DOMAIN)
    out = _maybe_default_solver_bbox_to_pinned_aoi(
        "run_model_flood_scenario", {}, pin
    )
    assert out["bbox"] == pin


def test_solver_wider_same_area_box_snaps_to_active_aoi() -> None:
    """A drifted / wider same-area solve box is snapped DOWN to the active AOI.

    This IS the #183 bug: the displayed AOI snapped to the pin while the LLM
    handed the solver a box poking outside it, so the SFINCS grid (built straight
    from the bbox, no padding) computed OUTSIDE the displayed AOI.
    """
    pin = list(_SOLVE_DOMAIN)  # (-97.755, 30.26, -97.725, 30.285)
    # A DRIFTED same-area box: it pokes OUTSIDE the pin on the east + north edges
    # (-97.70 > -97.725 ; 30.30 > 30.285) yet CLIPS it on the west + south edges
    # (-97.74 > -97.755 ; 30.27 > 30.26). It overlaps the pin but does NOT enclose
    # it -> the snap fires (this is the #183 compute-outside-AOI case).
    drifted_same_area = {"bbox": [-97.74, 30.27, -97.70, 30.30]}
    out = _maybe_default_solver_bbox_to_pinned_aoi(
        "run_model_flood_scenario", drifted_same_area, pin
    )
    assert out["bbox"] == pin, "solve domain must be snapped to the active AOI"


def test_solver_explicit_widen_is_honored() -> None:
    """An explicit WIDEN (a box ENCLOSING the active AOI) is REQUIRED expansion
    the user asked for -> honored verbatim (NATE: 'unless something requires it
    to expand')."""
    pin = list(_SOLVE_DOMAIN)
    widen = {"bbox": [-97.80, 30.20, -97.68, 30.34]}  # encloses the pin
    out = _maybe_default_solver_bbox_to_pinned_aoi(
        "run_model_flood_scenario", widen, pin
    )
    assert out == widen, "an explicit enclosing widen must be honored"


def test_solver_different_place_is_honored() -> None:
    """A genuinely DIFFERENT place (disjoint bbox) is honored, not dragged back."""
    pin = list(_SOLVE_DOMAIN)
    elsewhere = {"bbox": [-100.0, 40.0, -99.9, 40.1]}
    out = _maybe_default_solver_bbox_to_pinned_aoi(
        "run_model_flood_scenario", elsewhere, pin
    )
    assert out == elsewhere


def test_solver_first_solve_no_pin_is_noop() -> None:
    """The FIRST solve (no AOI pinned yet) DEFINES the domain -> untouched."""
    supplied = {"bbox": [-97.80, 30.20, -97.68, 30.34]}
    out = _maybe_default_solver_bbox_to_pinned_aoi(
        "run_model_flood_scenario", supplied, None
    )
    assert out == supplied


def test_solver_guard_ignores_non_solver_tools() -> None:
    """A non-solver tool is never touched by the SOLVER guard (the fetch default
    owns fetchers; this guard owns expensive solvers only)."""
    pin = list(_SOLVE_DOMAIN)
    # A fetcher box drifting outside the pin: the SOLVER guard leaves it alone.
    drifted = {"bbox": [-97.755, 30.26, -97.70, 30.30]}
    out = _maybe_default_solver_bbox_to_pinned_aoi("fetch_buildings", drifted, pin)
    assert out == drifted


def test_solver_equivalent_box_is_passthrough() -> None:
    """A box essentially equal to the pin is returned unchanged (no copy churn)."""
    pin = list(_SOLVE_DOMAIN)
    same = {"bbox": list(_SOLVE_DOMAIN)}
    out = _maybe_default_solver_bbox_to_pinned_aoi(
        "run_swmm_urban_flood", same, pin
    )
    assert out == same


def test_solver_guard_never_injects_bbox_into_point_driven_modflow() -> None:
    """POINT-driven groundwater solvers (MODFLOW plume) take NO bbox -- the AOI
    guard must NOT inject one even when a Case AOI is pinned. Their domain is a
    well / source point, not a rectangle; injecting a flood-AOI bbox would be a
    spurious key today and latent wrong-extent debt tomorrow."""
    pin = list(_SOLVE_DOMAIN)
    for tool in ("run_modflow_job", "run_model_groundwater_contamination_scenario"):
        params = {"upgradient_offset_km": 1.0, "duration_days": 30}
        out = _maybe_default_solver_bbox_to_pinned_aoi(tool, params, pin)
        # Same object, no bbox injected -- byte-for-byte passthrough.
        assert out is params
        assert "bbox" not in out
