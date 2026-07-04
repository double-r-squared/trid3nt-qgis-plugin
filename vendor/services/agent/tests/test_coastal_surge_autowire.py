"""Unit tests for the COASTAL surge-with-waves AUTO-WIRE in model_flood_scenario.

The fix: a coastal run (``is_coastal`` True) with NO explicit ``surge_forcing``
used to silently degrade to a pure-RAINFALL deck  -  ``coastal=True`` only swapped
``fetch_dem`` -> ``fetch_topobathy`` (a deeper bed) and added ZERO sea water, and
waves were gated on ``quadtree`` which defaulted OFF. Now:

1. A coastal call with ``surge_forcing=None`` AUTO-WIRES a time-varying sea-surge
   water-level boundary, so the resolved ``ForcingSpec.waterlevel`` is NON-None
   (the deck emits a ``bzs`` boundary -> water rises from the sea and marches
   inland across the frames).
2. ``quadtree`` is FORCED True for any coastal run, firing the cht_sfincs
   quadtree + SnapWave deck (so ``run_sfincs_quadtree`` is submitted, not the
   regular ``run_solver``) -> the wave-height field exists.
3. A NON-coastal (inland / pluvial) call is UNCHANGED: ``ForcingSpec.waterlevel``
   stays None (precip-only), the auto-wire helper is NEVER called, and
   ``quadtree`` stays as passed (the regular-grid ``run_solver`` path)  -  the v0.1
   regression contract.

The parametric LAST-RESORT surge path is exercised deterministically by mocking
both fetchers (CO-OPS + GTSM) to raise, so the test needs NO network / CDS key.
The surge scaling with return period is unit-tested directly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.tools.fetch_topobathy import TopobathyResult
from grace2_agent.workflows import model_flood_scenario as mfs
from grace2_agent.workflows.model_flood_scenario import (
    _autowire_coastal_surge_forcing,
    _parametric_surge_peak_m,
    _synthesize_parametric_surge_forcing,
    model_flood_scenario,
)
from grace2_contracts import new_ulid
from grace2_contracts.execution import (
    ExecutionHandle,
    LayerURI,
    ModelSetup,
    RunResult,
)

# Coastal AOI  -  Florida panhandle / Mexico Beach (the SFINCS North Star demo).
_COASTAL_BBOX = (-85.75, 29.55, -85.25, 30.20)
# Inland AOI  -  Idaho (no coast, pure pluvial).
_INLAND_BBOX = (-116.30, 43.55, -116.10, 43.70)


# --------------------------------------------------------------------------- #
# Mock builders
# --------------------------------------------------------------------------- #


def _topobathy_result() -> TopobathyResult:
    return TopobathyResult(
        layer_id="topobathy-test",
        name="Merged topo-bathymetry (3DEP + CUDEM)",
        layer_type="raster",
        uri="s3://test-cache/cache/static-30d/topobathy/coastal-test.tif",
        style_preset="continuous_dem",
        role="input",
        units="meters",
        bathymetry_present=True,
        cudem_tile_count=3,
        fallback_warning=None,
    )


def _mock_layer_uri(prefix: str) -> LayerURI:
    return LayerURI(
        layer_id=f"{prefix}-test",
        name=f"{prefix} test layer",
        layer_type="raster",
        uri=f"s3://test-cache/cache/static-30d/{prefix}/test.tif",
        style_preset="continuous_dem",
        role="input",
        units="meters",
    )


def _landcover_result() -> dict:
    return {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }


def _precip_result() -> dict:
    return {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [29.95, -85.41],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }


def _model_setup(bbox) -> ModelSetup:
    return ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="s3://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=bbox,
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )


def _run_result_ok(run_id: str) -> RunResult:
    return RunResult(
        run_id=run_id,
        handle_id=new_ulid(),
        status="complete",
        output_uri=f"s3://grace-2-hazard-prod-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )


def _make_handle(run_id: str) -> ExecutionHandle:
    return ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
        solver="sfincs",
        compute_class="standard",
        workflows_execution_id=(
            "projects/test/locations/us-central1/workflows/"
            "grace-2-sfincs-orchestrator/executions/test-exec"
        ),
        workflow_name="grace-2-sfincs-orchestrator",
        workflow_location="us-central1",
        submitted_at=datetime.now(timezone.utc),
    )


def _depth_layers(run_id: str) -> list[LayerURI]:
    return [
        LayerURI(
            layer_id=f"flood-depth-peak-{run_id}",
            name="Peak flood depth",
            layer_type="raster",
            uri=f"s3://grace-2-hazard-prod-runs/{run_id}/flood_depth_peak.tif",
            style_preset="continuous_flood_depth",
            role="primary",
            units="meters",
        ),
    ]


_DEPTH_METRICS = {
    "max_depth_m": 1.8,
    "mean_depth_m": 0.4,
    "p95_depth_m": 1.2,
    "flooded_cell_count": 8_000,
    "crs": "EPSG:32616",
    "units": "meters",
}


class _FakeEmitter:
    """No-op emitter so the workflow's substep/emit seams are inert in test."""

    def __init__(self) -> None:
        self.loaded: list[LayerURI] = []

    async def add_loaded_layer(self, layer) -> None:  # noqa: ANN001
        self.loaded.append(layer)

    async def update_current_progress(self, *_a, **_k) -> None:  # noqa: ANN002
        return None

    async def emit_solve_progress(self, *_a, **_k) -> None:  # noqa: ANN002
        return None

    async def emit_map_command(self, *_a, **_k) -> None:  # noqa: ANN002
        return None

    def begin_substeps(self, *_a, **_k) -> None:  # noqa: ANN002
        return None

    @asynccontextmanager
    async def substep(self, *_a, **_k):  # noqa: ANN002
        yield None


def _common_patches(*, bbox, run_id, emitter, forcing_capture):
    """Patch the full fetch+build+solve chain. Captures the ForcingSpec handed to
    ``build_sfincs_model`` so the test can assert ``.waterlevel`` directly.

    Both surge fetchers (CO-OPS + GTSM) raise so the auto-wire deterministically
    exercises the PARAMETRIC last-resort path (no network, no CDS key).
    """

    def _capture_build(**kw):  # noqa: ANN003
        forcing_capture["forcing"] = kw.get("forcing")
        return _model_setup(bbox)

    async def _run_quadtree(*_a, **_k):  # noqa: ANN002
        return _run_result_ok(run_id)

    async def _run_solver(*_a, **_k):  # noqa: ANN002
        return _make_handle(run_id)

    async def _wait(*_a, **_k):  # noqa: ANN002
        return _run_result_ok(run_id)

    return (
        patch.object(mfs, "fetch_topobathy", return_value=_topobathy_result()),
        patch.object(mfs, "fetch_dem", return_value=_mock_layer_uri("dem")),
        patch.object(mfs, "fetch_landcover", return_value=_landcover_result()),
        patch.object(
            mfs, "fetch_river_geometry", return_value=_mock_layer_uri("rivers")
        ),
        patch.object(
            mfs, "lookup_precip_return_period", return_value=_precip_result()
        ),
        # Both surge fetchers raise -> parametric last-resort fires.
        patch(
            "grace2_agent.tools.fetch_noaa_coops_tides.fetch_noaa_coops_tides",
            side_effect=RuntimeError("no CO-OPS station (test)"),
        ),
        patch(
            "grace2_agent.tools.fetch_gtsm_tide_surge.fetch_gtsm_tide_surge",
            side_effect=RuntimeError("no CDS key (test)"),
        ),
        patch.object(mfs, "build_sfincs_model", side_effect=_capture_build),
        patch.object(mfs, "_resolve_building_obstacle_uri", return_value=None),
        patch.object(mfs, "_resolve_quadtree_rivers_uri", return_value=None),
        patch.object(
            mfs,
            "_compose_and_upload_deckbuild_spec",
            return_value="s3://test-cache/cache/static-30d/sfincs_deck/x/spec.json",
        ),
        patch.object(mfs, "make_sfincs_mesh_layer_uri", return_value=None),
        patch.object(
            mfs, "postprocess_flood", return_value=(_depth_layers(run_id), _DEPTH_METRICS)
        ),
        patch.object(
            mfs, "postprocess_waves", MagicMock(return_value=([], _DEPTH_METRICS))
        ),
        patch.object(
            mfs,
            "publish_layer",
            side_effect=lambda **kw: f"https://cf.example.net/tiles/{kw['layer_id']}",
        ),
        patch.object(mfs, "current_emitter", return_value=emitter),
    )


# --------------------------------------------------------------------------- #
# 1. parametric surge scaling  -  pure unit test (no I/O)
# --------------------------------------------------------------------------- #


def test_parametric_surge_peak_scales_monotone_with_return_period() -> None:
    p2 = _parametric_surge_peak_m(2)
    p10 = _parametric_surge_peak_m(10)
    p100 = _parametric_surge_peak_m(100)
    p500 = _parametric_surge_peak_m(500)
    # Monotone increasing with ARI.
    assert p2 < p10 < p100 < p500
    # The 100-yr anchor is a real, visually-meaningful multi-metre surge.
    assert p100 >= 3.0
    # Clamped to a sane window (never negative / runaway).
    assert _parametric_surge_peak_m(1) >= 0.6
    assert _parametric_surge_peak_m(1_000_000) <= 7.5
    # None / 0 defaults to the 100-yr anchor (no crash).
    assert _parametric_surge_peak_m(None) == pytest.approx(p100)


def test_synthesize_parametric_surge_yields_materialised_waterlevel() -> None:
    out = _synthesize_parametric_surge_forcing(
        _COASTAL_BBOX, duration_hr=24, return_period_yr=100
    )
    # The materialised dict carries timeseries_uri -> a NON-None WaterlevelForcing.
    assert out.get("timeseries_uri")
    assert out.get("locations_uri")
    wl, _dq, _wind, _press = mfs._build_surge_forcing_members({"waterlevel": out})
    assert wl is not None
    assert wl.timeseries_uri == out["timeseries_uri"]


def test_autowire_falls_back_to_parametric_when_fetchers_fail() -> None:
    with patch(
        "grace2_agent.tools.fetch_noaa_coops_tides.fetch_noaa_coops_tides",
        side_effect=RuntimeError("no station"),
    ), patch(
        "grace2_agent.tools.fetch_gtsm_tide_surge.fetch_gtsm_tide_surge",
        side_effect=RuntimeError("no key"),
    ):
        sf = _autowire_coastal_surge_forcing(
            _COASTAL_BBOX, duration_hr=24, return_period_yr=100
        )
    assert isinstance(sf, dict)
    wl, *_ = mfs._build_surge_forcing_members(sf)
    assert wl is not None
    assert wl.timeseries_uri  # the parametric bzs CSV


# --------------------------------------------------------------------------- #
# 2. coastal call -> auto-wired non-None waterlevel + quadtree forced True
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_coastal_call_autowires_waterlevel_and_forces_quadtree() -> None:
    run_id = new_ulid()
    emitter = _FakeEmitter()
    forcing_capture: dict = {}
    quadtree_mock = MagicMock(return_value=_run_result_ok(run_id))

    async def _run_quadtree(*_a, **_k):  # noqa: ANN002
        quadtree_mock()
        return _run_result_ok(run_id)

    def _run_solver(*_a, **_k):  # noqa: ANN002  (sync dispatch)
        raise AssertionError("regular run_solver must NOT run on the coastal path")

    patches = _common_patches(
        bbox=_COASTAL_BBOX, run_id=run_id, emitter=emitter, forcing_capture=forcing_capture
    )
    extra = (
        patch.object(mfs, "run_sfincs_quadtree", side_effect=_run_quadtree),
        patch.object(mfs, "run_solver", side_effect=_run_solver),
    )
    for p in patches + extra:
        p.start()
    try:
        await model_flood_scenario(
            bbox=_COASTAL_BBOX,
            coastal=True,
            quadtree=False,  # NOT passed by the LLM  -  coastal must force it on
            surge_forcing=None,  # NOT supplied  -  must be auto-wired
            return_period_yr=100,
            duration_hr=24,
        )
    finally:
        for p in reversed(patches + extra):
            p.stop()

    # The auto-wired surge produced a NON-None waterlevel boundary on the
    # ForcingSpec handed to build_sfincs_model.
    spec = forcing_capture.get("forcing")
    assert spec is not None, "build_sfincs_model never received a ForcingSpec"
    assert spec.waterlevel is not None, (
        "coastal run must auto-wire a NON-None waterlevel surge boundary"
    )
    assert spec.waterlevel.timeseries_uri
    # quadtree was forced True for the coastal AOI -> the combined SnapWave job ran.
    assert quadtree_mock.called, "coastal run must force the quadtree+SnapWave deck"


# --------------------------------------------------------------------------- #
# 3. inland / pluvial call UNCHANGED (regression contract)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_inland_call_unchanged_no_surge_no_quadtree() -> None:
    run_id = new_ulid()
    emitter = _FakeEmitter()
    forcing_capture: dict = {}

    autowire_mock = MagicMock(wraps=_autowire_coastal_surge_forcing)

    # run_solver is a SYNC dispatch (returns an ExecutionHandle, not awaited).
    def _run_solver(*_a, **_k):  # noqa: ANN002
        return _make_handle(run_id)

    async def _wait(*_a, **_k):  # noqa: ANN002
        return _run_result_ok(run_id)

    async def _run_quadtree(*_a, **_k):  # noqa: ANN002
        raise AssertionError("quadtree must NOT run on the inland/pluvial path")

    patches = _common_patches(
        bbox=_INLAND_BBOX, run_id=run_id, emitter=emitter, forcing_capture=forcing_capture
    )
    extra = (
        patch.object(mfs, "run_solver", side_effect=_run_solver),
        patch.object(mfs, "wait_for_completion", side_effect=_wait),
        patch.object(mfs, "run_sfincs_quadtree", side_effect=_run_quadtree),
        patch.object(mfs, "_autowire_coastal_surge_forcing", autowire_mock),
    )
    for p in patches + extra:
        p.start()
    try:
        await model_flood_scenario(
            bbox=_INLAND_BBOX,
            coastal=False,
            quadtree=False,
            surge_forcing=None,
            return_period_yr=100,
            duration_hr=24,
        )
    finally:
        for p in reversed(patches + extra):
            p.stop()

    # The auto-wire helper was NEVER called on the inland path.
    assert not autowire_mock.called, "auto-wire must NOT fire for a non-coastal AOI"
    # The ForcingSpec carries NO surge boundary (pure pluvial  -  byte-identical v0.1).
    spec = forcing_capture.get("forcing")
    assert spec is not None
    assert spec.waterlevel is None, "inland/pluvial run must NOT carry a surge boundary"
    assert spec.discharge is None
    # forcing_type stays the design-storm pluvial path.
    assert spec.forcing_type == "pluvial_synthetic"
    assert spec.precip_inches == pytest.approx(12.1)


# --------------------------------------------------------------------------- #
# 4. NATE 2026-06-26 — SFINCS scenario-coverage composer auto-wire tests
#    (fluvial / compound / wind / infiltration / levee-breach / tsunami).
#    Each test mocks the relevant fetchers + patches build_sfincs_model to
#    capture the ForcingSpec + BuildOptions it receives, then asserts per-flag
#    the right member is set. The magnitude gates assert a typed failed envelope.
# --------------------------------------------------------------------------- #


def _coverage_patches(*, bbox, run_id, emitter, capture):
    """Patch the fetch+build+solve chain for the scenario-coverage tests.

    Captures BOTH the ForcingSpec and the BuildOptions handed to
    ``build_sfincs_model``. Does NOT force the surge fetchers to raise (each test
    controls its own fetcher mocks via ``extra`` patches). The fluvial / breach /
    tsunami archetypes drive the INLAND (run_solver) solve path by default; tests
    that need a coastal solve patch ``run_sfincs_quadtree`` separately.
    """

    def _capture_build(**kw):  # noqa: ANN003
        capture["forcing"] = kw.get("forcing")
        capture["options"] = kw.get("options")
        return _model_setup(bbox)

    def _run_solver(*_a, **_k):  # noqa: ANN002  (sync dispatch)
        return _make_handle(run_id)

    async def _wait(*_a, **_k):  # noqa: ANN002
        return _run_result_ok(run_id)

    async def _run_quadtree(*_a, **_k):  # noqa: ANN002
        return _run_result_ok(run_id)

    def _fake_resolve(surge_forcing, _bbox, *, window_hours=None, data_sources=None):
        """Materialise RAW fetcher sub-dicts WITHOUT hitting the real geo adapter.

        The real ``_resolve_surge_forcing_from_fetchers`` reads the FlatGeobuf at
        ``fetch_uri`` (network / file I/O on a fake URI -> FORCING_FGB_READ_FAILED).
        For the composer-level auto-wire unit we only need the RAW ``fetch_uri`` to
        become a materialised ``timeseries_uri`` so ``_build_surge_forcing_members``
        yields a non-None member. Pre-materialised sub-dicts pass through unchanged.
        """
        if not surge_forcing:
            return surge_forcing
        out = dict(surge_forcing)
        for key in ("waterlevel", "discharge"):
            sub = out.get(key)
            if isinstance(sub, dict):
                fetch = sub.get("fetch_uri") or sub.get("fgb_uri")
                already = sub.get("timeseries_uri") or sub.get("geodataset_uri")
                if fetch and not already:
                    out[key] = {
                        "timeseries_uri": f"/tmp/{key}_materialised.csv",
                        "locations_uri": f"/tmp/{key}_materialised.fgb",
                        "rivers_uri": sub.get("rivers_uri"),
                    }
        return out

    return (
        patch.object(mfs, "fetch_topobathy", return_value=_topobathy_result()),
        patch.object(mfs, "fetch_dem", return_value=_mock_layer_uri("dem")),
        patch.object(mfs, "fetch_landcover", return_value=_landcover_result()),
        patch.object(
            mfs, "fetch_river_geometry", return_value=_mock_layer_uri("rivers")
        ),
        patch.object(
            mfs, "lookup_precip_return_period", return_value=_precip_result()
        ),
        patch.object(mfs, "build_sfincs_model", side_effect=_capture_build),
        patch.object(mfs, "_resolve_building_obstacle_uri", return_value=None),
        patch.object(mfs, "_resolve_quadtree_rivers_uri", return_value=None),
        patch.object(
            mfs,
            "_compose_and_upload_deckbuild_spec",
            return_value="s3://test-cache/cache/static-30d/sfincs_deck/x/spec.json",
        ),
        patch.object(mfs, "make_sfincs_mesh_layer_uri", return_value=None),
        patch.object(
            mfs, "postprocess_flood", return_value=(_depth_layers(run_id), _DEPTH_METRICS)
        ),
        patch.object(
            mfs, "postprocess_waves", MagicMock(return_value=([], _DEPTH_METRICS))
        ),
        patch.object(
            mfs,
            "publish_layer",
            side_effect=lambda **kw: f"https://cf.example.net/tiles/{kw['layer_id']}",
        ),
        patch.object(mfs, "current_emitter", return_value=emitter),
        patch.object(mfs, "run_solver", side_effect=_run_solver),
        patch.object(mfs, "wait_for_completion", side_effect=_wait),
        patch.object(mfs, "run_sfincs_quadtree", side_effect=_run_quadtree),
        patch.object(
            mfs, "_resolve_surge_forcing_from_fetchers", side_effect=_fake_resolve
        ),
    )


async def _drive(bbox, *extra_patches, **kwargs):
    """Run model_flood_scenario under the coverage patches; return (envelope, capture)."""
    run_id = new_ulid()
    emitter = _FakeEmitter()
    capture: dict = {}
    patches = _coverage_patches(
        bbox=bbox, run_id=run_id, emitter=emitter, capture=capture
    )
    started = list(patches) + list(extra_patches)
    for p in started:
        p.start()
    try:
        env = await model_flood_scenario(bbox=bbox, **kwargs)
    finally:
        for p in reversed(started):
            p.stop()
    return env, capture


# --- FLUVIAL ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_river_autowires_discharge_no_topobathy() -> None:
    """river=True -> NWM discharge wired; is_coastal stays False (fetch_dem)."""
    nwm = patch(
        "grace2_agent.tools.fetch_noaa_nwm_streamflow.fetch_noaa_nwm_streamflow",
        return_value=_mock_layer_uri("nwm"),
    )
    dem_mock = MagicMock(return_value=_mock_layer_uri("dem"))
    topo_mock = MagicMock(return_value=_topobathy_result())
    env, capture = await _drive(
        _INLAND_BBOX,
        nwm,
        patch.object(mfs, "fetch_dem", dem_mock),
        patch.object(mfs, "fetch_topobathy", topo_mock),
        river=True,
    )
    spec = capture.get("forcing")
    assert spec is not None
    assert spec.discharge is not None, "river run must auto-wire a discharge boundary"
    assert spec.discharge.timeseries_uri  # materialised from the NWM fetch
    # Fluvial-only stays INLAND: fetch_dem ran, fetch_topobathy did NOT.
    assert dem_mock.called
    assert not topo_mock.called, "a fluvial-only run must NOT route through topobathy"


# --- COMPOUND --------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_compound_autowires_waterlevel_discharge_and_precip() -> None:
    """compound=True -> ONE spec with waterlevel AND discharge AND precip."""
    # Surge fetchers raise -> parametric waterlevel; NWM provides discharge.
    coops = patch(
        "grace2_agent.tools.fetch_noaa_coops_tides.fetch_noaa_coops_tides",
        side_effect=RuntimeError("no station"),
    )
    gtsm = patch(
        "grace2_agent.tools.fetch_gtsm_tide_surge.fetch_gtsm_tide_surge",
        side_effect=RuntimeError("no key"),
    )
    nwm = patch(
        "grace2_agent.tools.fetch_noaa_nwm_streamflow.fetch_noaa_nwm_streamflow",
        return_value=_mock_layer_uri("nwm"),
    )
    env, capture = await _drive(_COASTAL_BBOX, coops, gtsm, nwm, compound=True)
    spec = capture.get("forcing")
    assert spec is not None
    assert spec.waterlevel is not None, "compound must carry a surge waterlevel"
    assert spec.discharge is not None, "compound must carry a river discharge"
    assert spec.precip_inches is not None, "compound must keep the design-storm precip"


# --- WIND ------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wind_sets_windforcing_and_advanced_physics_default() -> None:
    """wind={...} -> WindForcing set AND advanced_physics defaults to advection=1."""
    env, capture = await _drive(
        _INLAND_BBOX,
        wind={"magnitude": 45.0, "direction": 170.0},
    )
    spec = capture.get("forcing")
    opts = capture.get("options")
    assert spec is not None and spec.wind is not None
    assert spec.wind.magnitude == pytest.approx(45.0)
    assert spec.wind.direction == pytest.approx(170.0)
    # The composer injects advanced_physics={"advection":1} for a wind run so the
    # momentum scheme flips (resolved through the registry onto BuildOptions).
    assert opts is not None
    assert opts.advanced_physics is not None
    assert opts.advanced_physics.get("advection") == 1


@pytest.mark.asyncio
async def test_explicit_advanced_physics_resolves_onto_options() -> None:
    """advanced_physics overrides are validated + threaded onto BuildOptions."""
    env, capture = await _drive(
        _INLAND_BBOX,
        advanced_physics={"advection": 1, "coriolis_latitude": 29.9},
    )
    opts = capture.get("options")
    assert opts is not None and opts.advanced_physics is not None
    assert opts.advanced_physics.get("advection") == 1
    assert opts.advanced_physics.get("coriolis_latitude") == pytest.approx(29.9)


@pytest.mark.asyncio
async def test_invalid_advanced_physics_returns_failed_envelope() -> None:
    """An unknown physics key -> a typed ADVANCED_PHYSICS_INVALID failed envelope."""
    env, capture = await _drive(
        _INLAND_BBOX,
        advanced_physics={"not_a_real_key": 1},
    )
    assert env.layers == []
    assert env.flood.metrics.solver_version == "failed:ADVANCED_PHYSICS_INVALID"


# --- INFILTRATION ----------------------------------------------------------- #


@pytest.mark.asyncio
async def test_infiltration_true_autowires_cn_uri() -> None:
    """infiltration=True -> GCN250 CN raster wired onto ForcingSpec.infiltration."""
    gcn = patch(
        "grace2_agent.tools.fetch_gcn250_curve_numbers.fetch_gcn250_curve_numbers",
        return_value=_mock_layer_uri("gcn250"),
    )
    env, capture = await _drive(_INLAND_BBOX, gcn, infiltration=True)
    spec = capture.get("forcing")
    assert spec is not None and spec.infiltration is not None
    assert spec.infiltration.cn_uri  # the GCN250 raster URI
    # Single-band GCN250 -> antecedent_moisture None (avoids the cn_avg ValueError).
    assert spec.infiltration.antecedent_moisture is None


# --- LEVEE-BREACH ----------------------------------------------------------- #


@pytest.mark.asyncio
async def test_breach_without_peak_returns_user_input_gate() -> None:
    """breach_point given but peak missing -> USER_INPUT_REQUIRED (no fabrication)."""
    capture: dict = {}

    def _capture_build(**kw):  # noqa: ANN003
        capture["forcing"] = kw.get("forcing")
        return _model_setup(_INLAND_BBOX)

    # build_sfincs_model must NOT be reached -- the gate fires first.
    with patch.object(mfs, "build_sfincs_model", side_effect=_capture_build):
        env = await model_flood_scenario(
            bbox=_INLAND_BBOX,
            breach_point=(-116.2, 43.6),
            breach_peak_discharge_m3s=None,
        )
    assert env.layers == []
    assert env.flood.metrics.solver_version == "failed:USER_INPUT_REQUIRED"
    assert "forcing" not in capture, "the breach gate must fire BEFORE the build"


@pytest.mark.asyncio
async def test_breach_with_peak_builds_breach_member() -> None:
    """breach_point + peak -> a DischargeForcing on ForcingSpec.breach."""
    env, capture = await _drive(
        _INLAND_BBOX,
        breach_point=(-116.2, 43.6),
        breach_peak_discharge_m3s=250.0,
        breach_arrival_hr=4.0,
    )
    spec = capture.get("forcing")
    assert spec is not None and spec.breach is not None
    assert spec.breach.timeseries_uri  # the breach dis CSV
    assert spec.breach.locations_uri  # the 1-point breach src FGB
    # The breach is an INTERIOR jet, not a domain-edge river: no rivers_uri.
    assert spec.breach.rivers_uri is None


# --- TSUNAMI ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tsunami_without_height_returns_user_input_gate() -> None:
    """tsunami=True without a height -> USER_INPUT_REQUIRED (no fabrication)."""
    env = await model_flood_scenario(
        bbox=_COASTAL_BBOX,
        tsunami=True,
        tsunami_wave_height_m=None,
    )
    assert env.layers == []
    assert env.flood.metrics.solver_version == "failed:USER_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_tsunami_with_height_wires_waterlevel_not_storm_surge() -> None:
    """tsunami=True + height -> waterlevel from the N-wave synth; storm synth NOT called."""
    storm_mock = MagicMock(wraps=mfs._synthesize_parametric_surge_forcing)
    env, capture = await _drive(
        _COASTAL_BBOX,
        patch.object(mfs, "_synthesize_parametric_surge_forcing", storm_mock),
        tsunami=True,
        tsunami_wave_height_m=3.0,
        tsunami_period_min=15.0,
    )
    spec = capture.get("forcing")
    assert spec is not None and spec.waterlevel is not None
    assert spec.waterlevel.timeseries_uri  # the tsunami bzs CSV
    # A tsunami is NOT a storm surge: the parametric storm synth must NOT fire.
    assert not storm_mock.called, "tsunami must NOT also wire the storm surge synth"


# --- REGRESSION ------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_all_flags_default_regress_to_pluvial() -> None:
    """All scenario-coverage flags default OFF -> byte-identical pluvial spec."""
    river_wire = MagicMock(wraps=mfs._autowire_river_discharge_forcing)
    inf_wire = MagicMock(wraps=mfs._resolve_infiltration_uri)
    env, capture = await _drive(
        _INLAND_BBOX,
        patch.object(mfs, "_autowire_river_discharge_forcing", river_wire),
        patch.object(mfs, "_resolve_infiltration_uri", inf_wire),
    )
    spec = capture.get("forcing")
    opts = capture.get("options")
    assert spec is not None
    assert spec.waterlevel is None
    assert spec.discharge is None
    assert spec.breach is None
    assert spec.wind is None
    assert spec.infiltration is None
    assert spec.forcing_type == "pluvial_synthetic"
    # advanced_physics stays None for a pluvial run (deck byte-identical).
    assert opts is not None and opts.advanced_physics is None
    # The fluvial auto-wire helper was NEVER called (river defaults False).
    assert not river_wire.called
    # _resolve_infiltration_uri IS called with infiltration=False -> returns None.
    assert inf_wire.called
    inf_wire.assert_called_once()
    assert inf_wire.call_args.args[0] is False
