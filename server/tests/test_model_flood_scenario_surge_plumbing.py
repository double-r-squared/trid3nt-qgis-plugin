"""Unit tests for the COASTAL SFINCS surge / obstacle PLUMBING in
``model_flood_scenario`` (AGENT A — the workflow-side glue that turns the
``surge_forcing`` / ``building_obstacles`` args into typed ``ForcingSpec`` +
``BuildOptions`` members handed to ``build_sfincs_model``).

These tests exercise the pure translation helpers (no network, no solver):

- ``_build_surge_forcing_members`` maps the nested ``surge_forcing`` dict into
  the four typed ``ForcingSpec`` members; partial / absent sub-dicts yield
  ``None`` (no block emitted).
- ``_resolve_building_obstacle_uri`` handles the three obstacle forms
  (``False`` → None; ``str`` → verbatim; ``True`` → best-effort OSM fetch,
  degrading to None on failure without aborting the flood).
"""

from __future__ import annotations

import asyncio

import grace2_agent.workflows.model_flood_scenario as mfs
import grace2_agent.workflows.sfincs_forcing_adapter as _sfa
from grace2_agent.workflows.model_flood_scenario import (
    _build_surge_forcing_members,
    _resolve_building_obstacle_uri,
    _resolve_surge_forcing_from_fetchers,
)
from grace2_agent.workflows.sfincs_builder import (
    DischargeForcing,
    PressureForcing,
    WaterlevelForcing,
    WindForcing,
)


def test_surge_members_full_dict() -> None:
    """A fully-populated surge_forcing dict maps to all four typed members."""
    wl, dq, wind, press = _build_surge_forcing_members(
        {
            "waterlevel": {
                "timeseries_uri": "/tmp/wl.csv",
                "locations_uri": "/tmp/bnd.fgb",
                "offset": 0.2,
                "buffer_m": 4000.0,
            },
            "discharge": {
                "timeseries_uri": "/tmp/dis.csv",
                "rivers_uri": "/tmp/riv.fgb",
                "river_upa_km2": 12.0,
            },
            "wind": {"magnitude": 40.0, "direction": 200.0},
            "pressure": {"grid_uri": "/tmp/p.nc", "fill_value": 101000.0},
        }
    )
    assert isinstance(wl, WaterlevelForcing)
    assert wl.timeseries_uri == "/tmp/wl.csv"
    assert wl.offset == 0.2 and wl.buffer_m == 4000.0
    assert isinstance(dq, DischargeForcing)
    assert dq.rivers_uri == "/tmp/riv.fgb" and dq.river_upa_km2 == 12.0
    assert isinstance(wind, WindForcing)
    assert wind.magnitude == 40.0 and wind.direction == 200.0
    assert isinstance(press, PressureForcing)
    assert press.grid_uri == "/tmp/p.nc" and press.fill_value == 101000.0


def test_surge_members_none_and_empty() -> None:
    """None / empty surge_forcing → all four members None (pure-pluvial deck)."""
    assert _build_surge_forcing_members(None) == (None, None, None, None)
    assert _build_surge_forcing_members({}) == (None, None, None, None)


def test_surge_members_partial_dict() -> None:
    """Only the present sub-dicts produce members; the rest stay None."""
    wl, dq, wind, press = _build_surge_forcing_members(
        {"waterlevel": {"geodataset_uri": "/tmp/wl.nc"}}
    )
    assert isinstance(wl, WaterlevelForcing) and wl.geodataset_uri == "/tmp/wl.nc"
    assert dq is None and wind is None and press is None


def test_surge_members_incomplete_subdicts_are_dropped() -> None:
    """A sub-dict missing its required URI/values yields None (no half-built block).

    e.g. a wind dict with only ``magnitude`` (no ``direction``) is not a valid
    uniform-wind forcing, so it must NOT produce a WindForcing.
    """
    wl, dq, wind, press = _build_surge_forcing_members(
        {
            "waterlevel": {"offset": 0.1},  # no timeseries / geodataset
            "discharge": {"locations_uri": "/tmp/x.fgb"},  # no series / rivers / hydro
            "wind": {"magnitude": 30.0},  # missing direction
            "pressure": {"fill_value": 101325.0},  # missing grid_uri
        }
    )
    assert wl is None
    assert dq is None
    assert wind is None
    assert press is None


def test_resolve_building_obstacle_false_and_str() -> None:
    """``False`` → None; a string is used verbatim as the obstacle geofile URI."""
    assert _resolve_building_obstacle_uri(False, (0.0, 0.0, 1.0, 1.0), []) is None
    assert (
        _resolve_building_obstacle_uri("/tmp/b.fgb", (0.0, 0.0, 1.0, 1.0), [])
        == "/tmp/b.fgb"
    )


def test_resolve_building_obstacle_true_degrades_on_fetch_failure(monkeypatch) -> None:
    """``True`` with a failing fetch_buildings degrades to None (never aborts).

    Same degrade policy as river geometry (job-0307): a footprint-fetch failure
    must NOT kill the flood — the deck just omits obstacles.
    """
    import grace2_agent.tools.data_fetch as data_fetch

    def _boom(*_a, **_k):
        raise RuntimeError("overpass down")

    monkeypatch.setattr(data_fetch, "fetch_buildings", _boom)
    ds: list = []
    assert _resolve_building_obstacle_uri(True, (-85.4, 29.9, -85.3, 30.0), ds) is None
    assert ds == []  # nothing recorded on failure


def test_resolve_building_obstacle_true_records_source_on_success(monkeypatch) -> None:
    """A successful OSM footprint fetch returns its URI + records a DataSource."""
    import grace2_agent.tools.data_fetch as data_fetch

    class _Layer:
        uri = "s3://cache/buildings.fgb"

    monkeypatch.setattr(data_fetch, "fetch_buildings", lambda *_a, **_k: _Layer())
    ds: list = []
    uri = _resolve_building_obstacle_uri(True, (-85.4, 29.9, -85.3, 30.0), ds)
    assert uri == "s3://cache/buildings.fgb"
    assert len(ds) == 1
    assert ds[0].uri == "s3://cache/buildings.fgb"


# --------------------------------------------------------------------------- #
# COMPOUND-FLOOD PHASE 1 (sprint-17 J2): the LLM-facing wrapper
# ``run_model_flood_scenario`` must FORWARD ``surge_forcing`` (+ the companion
# params the internal ``model_flood_scenario`` already accepts) so the
# already-built coastal-surge + fluvial-discharge engine is reachable from the
# agent. Prior to this lane the wrapper OMITTED the param and the internal call
# DROPPED it, so the surge / discharge path was dead from the agent surface.
#
# The wrapper resolves ``model_flood_scenario`` as a module global, so a
# monkeypatched capturing stub on the module intercepts the forwarded kwargs
# without touching the network / solver chain.
# --------------------------------------------------------------------------- #


class _CapturingEnvelope:
    """Minimal stand-in for ``AssessmentEnvelope`` — the wrapper only touches
    ``.layers`` (empty → falls back to ``model_dump``) and ``model_dump``."""

    layers: list = []

    def model_dump(self, *_a, **_k) -> dict:
        return {"captured": True}


def _capture_internal_call(monkeypatch) -> dict:
    """Patch the module-global ``model_flood_scenario`` with an async stub that
    records the kwargs the wrapper forwards. Returns the capture dict."""
    captured: dict = {}

    async def _stub(**kwargs):
        captured.update(kwargs)
        return _CapturingEnvelope()

    monkeypatch.setattr(mfs, "model_flood_scenario", _stub)
    return captured


_SURGE_SPEC = {
    "waterlevel": {
        "timeseries_uri": "/tmp/wl.csv",
        "locations_uri": "/tmp/bnd.fgb",
        "offset": 0.2,
    },
    "discharge": {
        "timeseries_uri": "/tmp/dis.csv",
        "rivers_uri": "/tmp/riv.fgb",
        "river_upa_km2": 12.0,
    },
}


def test_wrapper_forwards_surge_forcing_to_internal(monkeypatch) -> None:
    """``run_model_flood_scenario(surge_forcing=...)`` reaches the internal call.

    This is the compound-flood unlock: the wrapper must thread the exact
    ``surge_forcing`` dict (the same shape ``_resolve_surge_forcing_from_fetchers``
    / ``_build_surge_forcing_members`` / the deck emission consume) into
    ``model_flood_scenario``. Before this lane the param was unreachable.
    """
    captured = _capture_internal_call(monkeypatch)
    asyncio.run(
        mfs.run_model_flood_scenario(
            location_query="Fort Myers, FL",
            surge_forcing=_SURGE_SPEC,
        )
    )
    # The forwarded dict is IDENTICAL to what was passed in (mirror the internal
    # contract exactly — no reshaping at the wrapper boundary).
    assert captured["surge_forcing"] == _SURGE_SPEC
    assert captured["surge_forcing"]["waterlevel"]["timeseries_uri"] == "/tmp/wl.csv"
    assert captured["surge_forcing"]["discharge"]["river_upa_km2"] == 12.0


def test_wrapper_surge_forcing_default_is_none_pure_pluvial(monkeypatch) -> None:
    """Zero-regression guarantee: with no ``surge_forcing`` the wrapper forwards
    ``surge_forcing=None`` (the pure-pluvial path) — the deck stays byte-identical
    to today (no surge / discharge blocks emitted; the internal None branch in
    ``_build_surge_forcing_members`` / ``_resolve_surge_forcing_from_fetchers``
    short-circuits)."""
    captured = _capture_internal_call(monkeypatch)
    asyncio.run(mfs.run_model_flood_scenario(location_query="Boise, ID"))
    assert captured["surge_forcing"] is None
    # The companion knobs also default to their byte-identical-today values.
    assert captured["enable_subgrid"] is False
    assert captured["coastal"] is False
    assert captured["quadtree"] is False
    assert captured["building_obstacles"] is False


def test_wrapper_forwards_companion_params(monkeypatch) -> None:
    """The other internal-accepted-but-previously-dropped params
    (``enable_subgrid`` / ``project_id`` / ``session_id``) also forward."""
    captured = _capture_internal_call(monkeypatch)
    asyncio.run(
        mfs.run_model_flood_scenario(
            location_query="New Orleans, LA",
            surge_forcing=_SURGE_SPEC,
            enable_subgrid=True,
            project_id="proj-ulid-123",
            session_id="sess-ulid-456",
        )
    )
    assert captured["enable_subgrid"] is True
    assert captured["project_id"] == "proj-ulid-123"
    assert captured["session_id"] == "sess-ulid-456"


def test_wrapper_surge_forcing_via_tool_registry(monkeypatch) -> None:
    """End-to-end through the registry dispatch path (the production call site):
    the registered ``run_model_flood_scenario`` accepts + forwards
    ``surge_forcing`` (no TypeError; the kwarg is signature-accepted, not
    swallowed by ``**_extra_ignored``)."""
    from grace2_agent.tools import TOOL_REGISTRY

    captured = _capture_internal_call(monkeypatch)
    entry = TOOL_REGISTRY["run_model_flood_scenario"]
    asyncio.run(
        entry.fn(location_query="Fort Myers, FL", surge_forcing=_SURGE_SPEC)
    )
    assert captured["surge_forcing"] == _SURGE_SPEC


# --------------------------------------------------------------------------- #
# DISCHARGE UNIT WIRING (Invariant-7 silent-wrong-physics guard) — the PRODUCTION
# resolver _resolve_surge_forcing_from_fetchers must thread value_unit into
# discharge_forcing_from_fgb, so a USGS NWIS hydrograph (ft^3/s) is converted to
# m^3/s (NOT fed to SFINCS ~35.3x too large). These drive the REAL resolver (not
# a bypass) and capture the forwarded kwarg on a stubbed adapter.
# --------------------------------------------------------------------------- #


def _capture_discharge_kwargs(monkeypatch) -> dict:
    """Stub sfincs_forcing_adapter.discharge_forcing_from_fgb on the adapter
    module (the resolver re-imports it per call), capturing the kwargs the
    PRODUCTION resolver forwards. Returns the captured-kwargs dict."""
    seen: dict = {}

    def _spy(fgb, **kwargs):
        seen["fgb"] = fgb
        seen.update(kwargs)
        return {"timeseries_uri": "/tmp/dis.csv", "locations_uri": "/tmp/dis.fgb"}

    monkeypatch.setattr(_sfa, "discharge_forcing_from_fgb", _spy)
    return seen


_BBOX = (-85.75, 29.55, -85.25, 30.20)


def test_resolver_infers_cfs_for_usgs_discharge(monkeypatch) -> None:
    """A USGS/NWIS discharge fetch_uri (ft^3/s) -> value_unit='cfs' threaded."""
    seen = _capture_discharge_kwargs(monkeypatch)
    _resolve_surge_forcing_from_fetchers(
        {"discharge": {"fetch_uri": "s3://b/cache/usgs_hydrograph_abc.fgb"}}, _BBOX
    )
    assert seen.get("value_unit") == "cfs"


def test_resolver_defaults_cms_for_nwm_discharge(monkeypatch) -> None:
    """A non-USGS (NWM) discharge source stays value_unit='cms' (already metric)."""
    seen = _capture_discharge_kwargs(monkeypatch)
    _resolve_surge_forcing_from_fetchers(
        {"discharge": {"fetch_uri": "s3://b/cache/nwm_streamflow_xyz.fgb"}}, _BBOX
    )
    assert seen.get("value_unit") == "cms"


def test_resolver_honors_explicit_discharge_value_unit(monkeypatch) -> None:
    """An explicit discharge.value_unit overrides the source-based inference."""
    seen = _capture_discharge_kwargs(monkeypatch)
    _resolve_surge_forcing_from_fetchers(
        {"discharge": {"fetch_uri": "s3://b/cache/nwm_streamflow_xyz.fgb", "value_unit": "cfs"}},
        _BBOX,
    )
    assert seen.get("value_unit") == "cfs"
