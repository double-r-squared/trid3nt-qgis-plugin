"""P4 tests for the TELEMAC river-dye LLM surface: the ``telemac_river_dye``
template (engine-door refactor, TELEMAC slice - was ``run_telemac``, now the
door owns that name) + the ``model_telemac_river_dye`` composer.

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
def test_telemac_river_dye_registered_as_engine_template():
    # engine-door refactor (TELEMAC slice, name flip): the old run_telemac engine
    # tool re-tiered to the telemac_river_dye TEMPLATE (engine=telemac,
    # tier=template); the run_telemac name is now the read-only door.
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("telemac_river_dye")
    assert entry is not None
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.metadata.engine == "telemac"
    assert entry.metadata.tier == "template"
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    # Door dissolution (ADR 0094): the run_telemac door is DELETED; telemac_river_dye
    # is a standalone retrieval-pool template.
    assert TOOL_REGISTRY.get("run_telemac") is None


# ===========================================================================
# (2) Tool arg validation / coercion.
# ===========================================================================
def test_tool_rejects_invalid_bbox():
    from trid3nt_server.agent.workflows.telemac.river_dye.river_dye import telemac_river_dye

    out = asyncio.run(telemac_river_dye(bbox="not,a,bbox"))
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"


def test_domain_extent_clamp_labels_when_it_binds():
    """ADR 0223 (audit #9): an out-of-window domain-extent value is clamped AND a
    labeled note is returned so the guardrail is visible on the envelope; an
    in-range value passes through with no note."""
    from trid3nt_server.agent.workflows.telemac.river_dye.river_dye import (
        _clamp_domain_extent,
    )

    # a 50 km reach binds the clamp -> clamped value + a naming note.
    val, note = _clamp_domain_extent(
        50.0, valid_lo=0.5, valid_hi=15.0, clamp_lo=0.5, clamp_hi=8.0,
        name="reach_length_km", unit="km")
    assert val == 8.0
    assert note is not None
    assert "reach_length_km 50" in note and "clamped" in note

    # an in-range value passes through unlabeled.
    val2, note2 = _clamp_domain_extent(
        6.0, valid_lo=0.5, valid_hi=15.0, clamp_lo=0.5, clamp_hi=8.0,
        name="reach_length_km", unit="km")
    assert val2 == 6.0
    assert note2 is None


def test_tool_rejects_neither_location_nor_bbox():
    from trid3nt_server.agent.workflows.telemac.river_dye.river_dye import telemac_river_dye

    out = asyncio.run(telemac_river_dye())
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


def test_tool_rejects_both_location_and_bbox():
    from trid3nt_server.agent.workflows.telemac.river_dye.river_dye import telemac_river_dye

    out = asyncio.run(telemac_river_dye(location="Twin Falls, Idaho", bbox=list(_AOI)))
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


# ===========================================================================
# (3) Composer input validation.
# ===========================================================================
def test_composer_requires_exactly_one_of_location_or_bbox():
    from trid3nt_server.agent.workflows.telemac.river_dye.river_dye import (
        TelemacDyeScenarioInputError,
        model_telemac_river_dye,
    )

    with pytest.raises(TelemacDyeScenarioInputError):
        asyncio.run(model_telemac_river_dye())  # neither
    with pytest.raises(TelemacDyeScenarioInputError):
        asyncio.run(
            model_telemac_river_dye(location="X", bbox=_AOI)  # both
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

    def _fake_discharge(seed_lon, seed_lat, explicit):
        # Carrier discharge wiring (NWM) is mocked: return a real-looking value
        # so the composer reaches staging exactly as before this leg.
        captured["discharge_seed"] = (seed_lon, seed_lat)
        if explicit is not None:
            return (float(explicit), "user-supplied")
        return (312.0, "NOAA NWM (mock)")

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

    def _fake_publish(raw_peak, *args, **kwargs):
        # _publish_peak_layer grew mesh_* / substance / bank_source / synthetic
        # params; accept them all so the mock tracks the real signature by shape.
        captured["published"] = True
        captured["publish_substance"] = kwargs.get("substance", (
            args[6] if len(args) > 6 else None))
        return raw_peak.model_copy(update={"uri": "https://tiles/dye_peak.png"})

    return patch.multiple(
        comp,
        _registry_fn=_fake_registry_fn,
        _river_seed_from_geometry=_fake_seed,
        _resolve_reach_discharge=_fake_discharge,
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

    from trid3nt_server.agent.workflows.telemac.river_dye import river_dye as comp
    from trid3nt_server.agent.tools.simulation.solver import solver as solver_mod

    captured: dict = {}
    cm_multi, cm_solver, cm_wait, cm_bind = _install_composer_mocks(
        comp, solver_mod, captured
    )
    with cm_multi, cm_solver, cm_wait, cm_bind:
        peak = asyncio.run(
            comp.model_telemac_river_dye(
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
    from trid3nt_server.agent.workflows.telemac.river_dye import river_dye as comp
    from trid3nt_server.agent.tools.simulation.solver import solver as solver_mod

    captured: dict = {}
    cm_multi, cm_solver, cm_wait, cm_bind = _install_composer_mocks(
        comp, solver_mod, captured
    )
    provided = "s3://trid3nt-cache/cache/static-30d/river_geometry/prefetched.fgb"
    with cm_multi, cm_solver, cm_wait, cm_bind:
        peak = asyncio.run(
            comp.model_telemac_river_dye(
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
    from trid3nt_server.agent.workflows.telemac.river_dye import river_dye as comp
    from trid3nt_server.agent.tools.simulation.solver import solver as solver_mod

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
            comp.model_telemac_river_dye(location="Twin Falls, Idaho")
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
    from trid3nt_server.agent.workflows.telemac.river_dye import river_dye as tool_mod

    async def _fake_composer(**kwargs):
        assert kwargs["location"] == "Twin Falls, Idaho"
        assert kwargs["bbox"] is None
        assert kwargs["spill_fraction"] == pytest.approx(0.25)
        return _fake_peak("TELERID", "twin_falls_idaho")

    from unittest.mock import patch

    with patch.object(tool_mod, "model_telemac_river_dye", _fake_composer):
        out = asyncio.run(tool_mod.telemac_river_dye(location="Twin Falls, Idaho"))
    assert isinstance(out, TelemacDyeLayerURI)
    assert out.dye_cmax_mgl == pytest.approx(97.3)


def test_tool_maps_composer_error_to_typed_dict():
    from trid3nt_server.agent.workflows.telemac.river_dye import river_dye as tool_mod
    from trid3nt_server.agent.workflows.telemac.river_dye.river_dye import (
        TelemacDyeScenarioError,
    )

    async def _boom(**kwargs):
        raise TelemacDyeScenarioError("TELEMAC_DYE_RUN_FAILED", "solve did not complete")

    from unittest.mock import patch

    with patch.object(tool_mod, "model_telemac_river_dye", _boom):
        out = asyncio.run(tool_mod.telemac_river_dye(location="Twin Falls, Idaho"))
    assert out["status"] == "error"
    assert out["error_code"] == "TELEMAC_DYE_RUN_FAILED"


# ===========================================================================
# (7) ADR 0216 false-green fix: the erodible-bed / GAIA single gate.
#     An armed erodible bed (explicit knob OR scour/erosion/bedload phrasing)
#     MUST route to the sediment class so the erodible_bed flag and the
#     substance_class gate cannot diverge into a plain tracer solve that only
#     LOOKS morphodynamic (accepts an erodible bed + publishes layers, couples
#     no GAIA). The old bug: substance='scour' fell through classify to 'tracer'
#     while _scour_hint independently auto-armed erodible_bed=True.
# ===========================================================================
@pytest.mark.parametrize("s", [
    "scour", "bed scour below the weir", "erosion", "bed erosion",
    "erodible bed", "bedload", "bed load transport", "bed degradation",
    "channel aggradation", "mobile bed morphodynamics", "bed lowering",
    "morphological change",
])
def test_scour_phrasing_classifies_as_sediment(s):
    from trid3nt_server.agent.workflows.telemac.river_dye.river_dye import (
        classify_substance,
    )

    cls, payload = classify_substance(s)
    # scour/erosion/bedload phrasing routes to the GAIA sediment class (was the
    # false-green: it fell through to 'tracer').
    assert cls == "sediment", (s, cls)
    assert isinstance(payload, dict) and payload.get("grain_size", 0) > 0.0


def test_sediment_and_tracer_regression_unchanged():
    # the scour keyword branch must not shadow the existing classes.
    from trid3nt_server.agent.workflows.telemac.river_dye.river_dye import (
        classify_substance,
    )

    assert classify_substance("dye") == ("tracer", None)
    assert classify_substance("water") == ("tracer", None)
    assert classify_substance("oil")[0] == "oil"
    assert classify_substance("sewage")[0] == "decay"
    assert classify_substance("sand")[0] == "sediment"
    assert classify_substance("oily scour")[0] == "oil"      # oil still wins
    assert classify_substance("sewage erosion")[0] == "decay"  # decay still wins


def test_tool_auto_arms_erodible_bed_for_scour_phrasing():
    # The tool arms erodible_bed=True from scour phrasing (no explicit knob) AND
    # forwards the substance - the pair that must not diverge downstream.
    from trid3nt_server.agent.workflows.telemac.river_dye import river_dye as tool_mod
    from unittest.mock import patch

    captured: dict = {}

    async def _cap(**kwargs):
        captured.update(kwargs)
        return _fake_peak("TELERID", "reach")

    with patch.object(tool_mod, "model_telemac_river_dye", _cap):
        out = asyncio.run(tool_mod.telemac_river_dye(
            location="Twin Falls, Idaho", substance="scour below the weir"))
    assert isinstance(out, TelemacDyeLayerURI)
    assert captured["erodible_bed"] is True
    assert "scour" in captured["substance"]


def test_composer_scour_prompt_routes_sediment_and_arms_gaia():
    # END-TO-END tool -> composer for a pure scour prompt (no erodible_bed knob):
    # the staged manifest reach MUST carry substance_class='sediment' (so the
    # worker author_deck couples GAIA) AND erodible_bed=True (the v2 path). The
    # old bug staged NO substance_class (a tracer solve) - the false green.
    from trid3nt_server.agent.workflows.telemac.river_dye import river_dye as comp
    from trid3nt_server.agent.tools.simulation.solver import solver as solver_mod

    captured: dict = {}
    cm_multi, cm_solver, cm_wait, cm_bind = _install_composer_mocks(
        comp, solver_mod, captured
    )
    with cm_multi, cm_solver, cm_wait, cm_bind:
        peak = asyncio.run(comp.telemac_river_dye(
            location="Twin Falls, Idaho", substance="scour below the weir"))
    assert isinstance(peak, TelemacDyeLayerURI)
    reach = captured["reach"]
    assert reach["substance_class"] == "sediment"   # NOT tracer (the bug)
    assert reach["erodible_bed"] is True             # GAIA v2 erodible-bed armed


@pytest.mark.parametrize("subst", ["dye", "water", "red dye", "some chemical"])
def test_composer_erodible_bed_forces_sediment_over_any_tracer(subst):
    # IMPOSSIBLE-DIVERGENCE: erodible_bed=True with a plain tracer substance can
    # NEVER stage a tracer-classified run - it is forced to the sediment/GAIA
    # class by construction, so the two gates cannot disagree.
    from trid3nt_server.agent.workflows.telemac.river_dye import river_dye as comp
    from trid3nt_server.agent.tools.simulation.solver import solver as solver_mod

    captured: dict = {}
    cm_multi, cm_solver, cm_wait, cm_bind = _install_composer_mocks(
        comp, solver_mod, captured
    )
    with cm_multi, cm_solver, cm_wait, cm_bind:
        peak = asyncio.run(comp.model_telemac_river_dye(
            location="Twin Falls, Idaho", substance=subst, erodible_bed=True))
    assert isinstance(peak, TelemacDyeLayerURI)
    reach = captured["reach"]
    assert reach["substance_class"] == "sediment"
    assert reach["erodible_bed"] is True


def test_composer_never_stages_erodible_tracer_invariant():
    # Direct proof of the honesty-floor invariant: for a matrix of substances,
    # an armed erodible bed always stages the sediment class (never tracer).
    from trid3nt_server.agent.workflows.telemac.river_dye import river_dye as comp
    from trid3nt_server.agent.tools.simulation.solver import solver as solver_mod

    for subst in ("dye", "scour", "oil", "sewage", "sand"):
        captured: dict = {}
        cm_multi, cm_solver, cm_wait, cm_bind = _install_composer_mocks(
            comp, solver_mod, captured
        )
        with cm_multi, cm_solver, cm_wait, cm_bind:
            asyncio.run(comp.model_telemac_river_dye(
                location="Twin Falls, Idaho", substance=subst, erodible_bed=True))
        reach = captured["reach"]
        assert not (reach.get("erodible_bed") and
                    reach.get("substance_class") != "sediment"), (subst, reach)
