"""P4 tests for the TELEMAC river-dye LLM surface: the ``run_telemac`` tool +
the ``model_river_dye_release_scenario`` composer.

Exercised in ISOLATION with geocode / fetch_river_geometry / run_solver / boto3 /
postprocess / publish all MOCKED (no network, no docker, no TELEMAC). These pin:

  1. Tool registration + FR-DC-6 metadata (workflow_dispatch, uncacheable).
  2. Tool arg validation/coercion (bad bbox, both/neither location+bbox).
  3. Composer input validation (exactly one of location / bbox).
  4. Composer chain: geocode -> river seed -> manifest build -> run_solver
     (solver='telemac_river_dye' + the staged manifest_uri) -> download ->
     postprocess -> publish -> returns the peak TelemacDyeLayerURI (layer attach).
  5. The manifest ReachConfig overrides carry the coerced spill args.
  6. Tool happy path returns the TelemacDyeLayerURI (add_loaded_layer gate).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from trid3nt_contracts.telemac_contracts import (
    TELEMAC_DYE_STYLE_PRESET,
    TelemacDyeLayerURI,
)

_AOI = (-114.50, 42.52, -114.38, 42.62)  # Twin Falls, Idaho-ish


def _amock(ret):
    async def _inner(*a, **k):
        return ret
    return _inner


class _FakeHandle:
    run_id = "TELERID"
    workflow_name = "local-docker"


class _FakeRunResult:
    run_id = "TELERID"
    status = "complete"
    output_uri = "s3://runs/TELERID/"
    error_code = None
    error_message = None
    cancellation_reason = None


def _fake_peak(run_id: str, reach_name: str) -> TelemacDyeLayerURI:
    return TelemacDyeLayerURI(
        layer_id=f"telemac-dye-peak-{run_id}",
        name=f"Peak dye concentration ({reach_name})",
        layer_type="raster",
        uri=f"s3://runs/{run_id}/telemac_dye_peak.tif",
        style_preset=TELEMAC_DYE_STYLE_PRESET,
        role="primary",
        units="mg/L",
        bbox=list(_AOI),
        dye_cmax_mgl=97.3,
        dye_peak_time_s=420.0,
        plume_reach_m=1830.0,
        active_frames=7,
    )


# ===========================================================================
# (1) Tool registration + metadata.
# ===========================================================================
def test_run_telemac_registered_with_workflow_dispatch_metadata():
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("run_telemac")
    assert entry is not None
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"


# ===========================================================================
# (2) Tool arg validation / coercion.
# ===========================================================================
def test_tool_rejects_invalid_bbox():
    from trid3nt_server.tools.simulation.run_telemac_tool import run_telemac

    out = asyncio.run(run_telemac(bbox="not,a,bbox"))
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"


def test_tool_rejects_neither_location_nor_bbox():
    from trid3nt_server.tools.simulation.run_telemac_tool import run_telemac

    out = asyncio.run(run_telemac())
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


def test_tool_rejects_both_location_and_bbox():
    from trid3nt_server.tools.simulation.run_telemac_tool import run_telemac

    out = asyncio.run(run_telemac(location="Twin Falls, Idaho", bbox=list(_AOI)))
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


# ===========================================================================
# (3) Composer input validation.
# ===========================================================================
def test_composer_requires_exactly_one_of_location_or_bbox():
    from trid3nt_server.workflows.model_river_dye_release_scenario import (
        TelemacDyeScenarioInputError,
        model_river_dye_release_scenario,
    )

    with pytest.raises(TelemacDyeScenarioInputError):
        asyncio.run(model_river_dye_release_scenario())  # neither
    with pytest.raises(TelemacDyeScenarioInputError):
        asyncio.run(
            model_river_dye_release_scenario(location="X", bbox=_AOI)  # both
        )


# ===========================================================================
# (4)+(5) Composer chain: dispatch + manifest overrides + layer return.
# ===========================================================================
def _install_composer_mocks(comp, solver_mod, captured: dict):
    from unittest.mock import patch

    def _fake_registry_fn(name):
        if name == "geocode_location":
            def _geo(q, **_k):
                captured["geocode_query"] = q
                return {
                    "name": "Twin Falls, Idaho",
                    "latitude": 42.5629,
                    "longitude": -114.4609,
                }
            return _geo
        if name == "fetch_river_geometry":
            def _river(*, bbox, **_k):
                captured["river_bbox"] = bbox
                class _L:
                    uri = "s3://cache/river.fgb"
                return _L()
            return _river
        raise AssertionError(f"unexpected tool {name}")

    def _fake_seed(uri):
        captured["seed_uri"] = uri
        return (-114.31, 42.58)  # a mid-reach point on the Snake

    def _fake_stage(reach, run_tag):
        captured["reach"] = reach
        captured["run_tag"] = run_tag
        return f"s3://cache/telemac/{run_tag}/manifest.json"

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):
        captured["solver"] = solver
        captured["model_setup_uri"] = model_setup_uri
        captured["compute_class"] = compute_class
        return _FakeHandle()

    def _fake_download(run_id):
        captured["download_run_id"] = run_id
        return ("/tmp/telemac/does-not-matter.slf", 32611)

    def _fake_postprocess(slf_path, *, run_id, utm_epsg, reach_name, **_kw):
        captured["pp_run_id"] = run_id
        captured["pp_utm_epsg"] = utm_epsg
        return [_fake_peak(run_id, reach_name)], {
            "dye_cmax_mgl": 97.3,
            "dye_peak_time_s": 420.0,
        }

    def _fake_publish(raw_peak, run_id, location_name, reach_name):
        captured["published"] = True
        return raw_peak.model_copy(update={"uri": "https://tiles/dye_peak.png"})

    return patch.multiple(
        comp,
        _registry_fn=_fake_registry_fn,
        _river_seed_from_geometry=_fake_seed,
        _stage_manifest=_fake_stage,
        mint_dispatch_and_sim_cards=_amock(None),
        route_sim_terminal=_amock(None),
        _download_telemac_result=_fake_download,
        postprocess_telemac=_fake_postprocess,
        _publish_peak_layer=_fake_publish,
        current_emitter=lambda: None,
        drive_live_solve_progress=_amock(None),
    ), patch.object(solver_mod, "run_solver", _fake_run_solver), \
        patch.object(solver_mod, "wait_for_completion", _amock(_FakeRunResult())), \
        patch.object(solver_mod, "set_emitter_binding", lambda *a, **k: None)


def test_composer_geocode_dispatch_and_manifest_overrides():
    from unittest.mock import patch  # noqa: F401 (used via _install)

    from trid3nt_server.workflows import model_river_dye_release_scenario as comp
    from trid3nt_server.tools.simulation import solver as solver_mod

    captured: dict = {}
    cm_multi, cm_solver, cm_wait, cm_bind = _install_composer_mocks(
        comp, solver_mod, captured
    )
    with cm_multi, cm_solver, cm_wait, cm_bind:
        peak = asyncio.run(
            comp.model_river_dye_release_scenario(
                location="Twin Falls, Idaho",
                spill_fraction=0.4,
                spill_duration_s=600.0,
                dye_concentration_mgl=250.0,
                reach_length_km=4.0,
                sim_duration_s=1800.0,
            )
        )

    # Layer attach: a TelemacDyeLayerURI (LayerURI subtype) came back, published.
    assert isinstance(peak, TelemacDyeLayerURI)
    assert peak.uri == "https://tiles/dye_peak.png"
    assert peak.dye_cmax_mgl == pytest.approx(97.3)

    # F46: the place was GEOCODED (not hand-typed).
    assert captured["geocode_query"] == "Twin Falls, Idaho"

    # run_solver dispatched with the TELEMAC solver + the staged manifest.
    assert captured["solver"] == "telemac_river_dye"
    assert captured["model_setup_uri"].endswith("manifest.json")

    # The download + postprocess ran under the SOLVER'S run_id (handle.run_id),
    # not the manifest run_tag -- outputs land under the real run prefix.
    assert captured["download_run_id"] == "TELERID"
    assert captured["pp_run_id"] == "TELERID"
    assert captured["pp_utm_epsg"] == 32611

    # The manifest ReachConfig overrides carry the coerced spill intent + the
    # extracted river seed (NOT the raw geocoded centroid).
    reach = captured["reach"]
    assert reach["spill_frac"] == pytest.approx(0.4)
    assert reach["pulse_window_s"] == pytest.approx(600.0)
    assert reach["dye_conc_mgl"] == pytest.approx(250.0)
    assert reach["distance_km"] == pytest.approx(4.0)
    assert reach["duration_s"] == pytest.approx(1800.0)
    assert reach["seed_lon"] == pytest.approx(-114.31, abs=1e-4)
    assert reach["seed_lat"] == pytest.approx(42.58, abs=1e-4)
    assert reach["nav_direction"] == "DM"


def test_composer_reuses_prefetched_river_geometry_uri():
    """When a river_geometry_uri is supplied the composer reuses it for the seed
    and does NOT call fetch_river_geometry (the live post-fetch routing path)."""
    from trid3nt_server.workflows import model_river_dye_release_scenario as comp
    from trid3nt_server.tools.simulation import solver as solver_mod

    captured: dict = {}
    cm_multi, cm_solver, cm_wait, cm_bind = _install_composer_mocks(
        comp, solver_mod, captured
    )
    provided = "s3://trid3nt-cache/cache/static-30d/river_geometry/prefetched.fgb"
    with cm_multi, cm_solver, cm_wait, cm_bind:
        peak = asyncio.run(
            comp.model_river_dye_release_scenario(
                location="Twin Falls, Idaho",
                river_geometry_uri=provided,
            )
        )
    assert isinstance(peak, TelemacDyeLayerURI)
    # The provided uri was used for the seed; fetch_river_geometry was NOT called.
    assert captured["seed_uri"] == provided
    assert "river_bbox" not in captured  # _fake_registry_fn('fetch_river_geometry') never ran


def test_composer_falls_back_to_centroid_when_no_river_seed():
    """When river-seed extraction returns None the composer seeds the geocoded
    centroid (the worker NLDI-snaps it) -- honest degrade, never a dead-end."""
    from trid3nt_server.workflows import model_river_dye_release_scenario as comp
    from trid3nt_server.tools.simulation import solver as solver_mod

    captured: dict = {}
    cm_multi, cm_solver, cm_wait, cm_bind = _install_composer_mocks(
        comp, solver_mod, captured
    )
    # Override the seed extractor to fail (None).
    from unittest.mock import patch

    with cm_multi, cm_solver, cm_wait, cm_bind, patch.object(
        comp, "_river_seed_from_geometry", lambda uri: None
    ):
        peak = asyncio.run(
            comp.model_river_dye_release_scenario(location="Twin Falls, Idaho")
        )
    assert isinstance(peak, TelemacDyeLayerURI)
    # Seed fell back to the geocoded centroid.
    reach = captured["reach"]
    assert reach["seed_lon"] == pytest.approx(-114.4609, abs=1e-3)
    assert reach["seed_lat"] == pytest.approx(42.5629, abs=1e-3)


# ===========================================================================
# (6) Tool happy path returns the layer.
# ===========================================================================
def test_tool_happy_path_returns_layer():
    from trid3nt_server.tools.simulation import run_telemac_tool as tool_mod

    async def _fake_composer(**kwargs):
        assert kwargs["location"] == "Twin Falls, Idaho"
        assert kwargs["bbox"] is None
        assert kwargs["spill_fraction"] == pytest.approx(0.25)
        return _fake_peak("TELERID", "twin_falls_idaho")

    from unittest.mock import patch

    with patch.object(tool_mod, "model_river_dye_release_scenario", _fake_composer):
        out = asyncio.run(tool_mod.run_telemac(location="Twin Falls, Idaho"))
    assert isinstance(out, TelemacDyeLayerURI)
    assert out.dye_cmax_mgl == pytest.approx(97.3)


def test_tool_maps_composer_error_to_typed_dict():
    from trid3nt_server.tools.simulation import run_telemac_tool as tool_mod
    from trid3nt_server.workflows.model_river_dye_release_scenario import (
        TelemacDyeScenarioError,
    )

    async def _boom(**kwargs):
        raise TelemacDyeScenarioError("TELEMAC_DYE_RUN_FAILED", "solve did not complete")

    from unittest.mock import patch

    with patch.object(tool_mod, "model_river_dye_release_scenario", _boom):
        out = asyncio.run(tool_mod.run_telemac(location="Twin Falls, Idaho"))
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_DYE_RUN_FAILED"
