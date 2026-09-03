"""WAQTEL O2 dissolved-oxygen sag (telemac_do_sag) - offline V&V + tool tests.

No solve, no network. The live V&V (the 12 km straight-channel WAQTEL O2 solve
through the LANDED worker author_deck, trid3nt-local/telemac:latest, 2026-08-07)
is captured as a committed profile fixture; this test re-checks it against the
Streeter-Phelps 1925 closed form deterministically (the 0163/0167 committed-V&V
pattern), so a regression in the O2 machinery is caught without re-solving.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tests.reach_chain import install_reach_chain

from trid3nt_server.workflows.telemac.streeter_phelps import (
    sp_critical_point,
    sp_do_profile,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "telemac_o2_sp_idealized_profile.json"


# --- Streeter-Phelps closed form: known-value + shape ----------------------- #
def test_sp_critical_point_known_values():
    # k1=5, k2=10 /d, Cs=9, L0=20, D0=0: tc=ln(2)/5 d, min DO = Cs - (k1/k2)L0 e^{-k1 tc}
    crit = sp_critical_point(0.5, 9.0, 20.0, 0.0, 5.0, 10.0)
    assert crit["min_do_mgl"] == pytest.approx(4.0, abs=1e-6)   # 9 - 0.5*20*0.5
    assert crit["tc_day"] == pytest.approx(np.log(2.0) / 5.0, abs=1e-9)


def test_sp_profile_is_a_sag():
    xs = list(np.linspace(0, 12000, 200))
    do, _ = sp_do_profile(xs, 0.54, 9.0, 20.0, 0.0, 5.0, 10.0)
    do = np.asarray(do)
    i = int(do.argmin())
    assert 0 < i < len(do) - 1            # interior minimum (a genuine sag)
    assert do[0] > do[i] and do[-1] > do[i]  # drops then recovers
    assert do.min() < 5.0                 # sags below the 5 mg/L standard


def test_sp_k1_equals_k2_limit_is_finite():
    do, d = sp_do_profile([0, 1000, 5000], 0.5, 9.0, 20.0, 1.0, 3.0, 3.0)
    assert all(np.isfinite(do)) and all(np.isfinite(d))


# --- COMMITTED live V&V: WAQTEL O2 solve vs Streeter-Phelps ------------------ #
def test_waqtel_o2_reproduces_streeter_phelps():
    """The landed worker's WAQTEL O2 solve (committed profile) matches the S-P
    closed form to well under 0.05 mg/L at the sag minimum - the machinery V&V."""
    d = json.loads(_FIXTURE.read_text())
    p = d["params"]
    x = np.asarray(d["x"]); o2 = np.asarray(d["o2"])
    U = float(np.mean(d["U"]))
    D0 = p["Cs"] - p["up_do"]
    sp, _ = sp_do_profile(list(x), U, p["Cs"], p["L0"], D0, p["k1_day"], p["k2_day"])
    sp = np.asarray(sp)
    crit = sp_critical_point(U, p["Cs"], p["L0"], D0, p["k1_day"], p["k2_day"])
    i = int(o2.argmin())
    # sag minimum matches the analytic sag minimum
    assert abs(o2[i] - crit["min_do_mgl"]) < 0.05
    # sag LOCATION matches within one mesh cell-ish (< 1% of the reach)
    assert abs(x[i] - crit["xc_m"]) < 0.01 * p["L"]
    # whole-profile agreement (numerical diffusion only)
    assert np.sqrt(np.mean((o2 - sp) ** 2)) < 0.05
    # and the modeled sag violates the 5 mg/L standard (the permit answer)
    assert o2[i] < p["standard"]


# --- tool arg handling (no dispatch) ---------------------------------------- #
def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["telemac_do_sag"].fn.workflow


def test_do_saturation_temperature_relation():
    from trid3nt_server.workflows.telemac.helpers.water_quality import do_saturation_mgl

    def sat(t):
        return do_saturation_mgl(SimpleNamespace(water_temp_c=t))

    assert sat(20.0) == pytest.approx(9.0, abs=0.2)   # ~9 mg/L at 20C
    assert sat(5.0) > sat(25.0)                       # colder holds more


def test_declared_params_and_plan_validate():
    from trid3nt_server.workflows.lib import resolve_params, validate_plan

    wf = _workflow()
    p = asyncio.run(resolve_params(wf.params,
                                   {"location": "Eel River near Scotia, California"}))
    validate_plan(wf.plan, wf.params, wf.data)
    assert p.value_of("do_saturation_mgl") == pytest.approx(9.022, abs=1e-3)  # Cs at 20 C
    assert p.value_of("upstream_do_mgl") == p.value_of("do_saturation_mgl")   # saturated inflow
    assert p.row("k1_per_day").consequence == "numerical"    # never refuses in auto


def test_the_plan_reads_as_the_universal_stage_sequence():
    """The skeleton owns the sequence; the facade's five ops stamp each step."""
    from trid3nt_server.workflows.lib import resolve_params

    wf = _workflow()
    plan = wf.plan
    stages = [s.stage for s in plan.declared() if s.stage]
    assert stages == ["acquire", "acquire", "acquire", "prep", "gates", "mesh",
                      "mesh", "author", "solve", "publish"]
    assert [s.name for s in plan.declared()][-1] == "do_field"


def test_declared_bounds_clamp_the_wq_knobs():
    from trid3nt_server.workflows.lib import resolve_params

    wf = _workflow()
    p = asyncio.run(resolve_params(wf.params, {"location": "x", "reach_length_km": 900.0,
                                               "k1_per_day": 0.0}))
    assert p.value_of("reach_length_km") == 15.0 \
        and "CLAMPED" in p.row("reach_length_km").note
    assert p.value_of("k1_per_day") == 0.01


@pytest.mark.asyncio
async def test_do_sag_requires_location_or_bbox():
    from trid3nt_server.workflows.telemac.do_sag.do_sag import telemac_do_sag
    out = await telemac_do_sag()
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


# --- the outfall: absent DERIVES, malformed REFUSES -------------------------- #
@pytest.mark.parametrize("bad", ["somewhere", [1.0], [1.0, 2.0, 3.0], {"lon": 1},
                                 [200.0, 10.0], ["a", "b"]])
@pytest.mark.asyncio
async def test_malformed_outfall_coords_refuse_they_never_fall_back(bad):
    """A garbage discharge location must not silently become the reach seed."""
    from trid3nt_server.workflows.telemac.do_sag.do_sag import telemac_do_sag
    out = await telemac_do_sag(location="Eel River near Scotia, California",
                               outfall_coords=bad)
    assert isinstance(out, dict) and out["error_code"] == "TELEMAC_PARAMS_INVALID"
    assert "outfall_coords" in out["error_message"]


def test_an_absent_outfall_leaves_a_derived_provenance_row():
    """The user has to see what the sag distance is measured FROM."""
    from trid3nt_server.workflows.lib import provenance_entries, resolve_params

    wf = _workflow()
    p = asyncio.run(resolve_params(wf.params, {"location": "Eel River near Scotia"}))
    row = next(r for r in provenance_entries(p, wf.params)
               if r.param == "outfall_coords")
    assert row.basis == "derived"
    assert "mid-reach" in (row.note or "")


def test_a_supplied_outfall_is_carried_as_a_user_row():
    from trid3nt_server.workflows.lib import provenance_entries, resolve_params

    wf = _workflow()
    supplied, err = wf._normalize({"location": "x",
                                   "outfall_coords": ["-124.1", "40.5"]})
    assert err is None and supplied["outfall_coords"] == (-124.1, 40.5)
    p = asyncio.run(resolve_params(wf.params, supplied))
    row = next(r for r in provenance_entries(p, wf.params)
               if r.param == "outfall_coords")
    assert row.basis == "user"


# --- the gate-mode lever reaches the resolved-input review ------------------- #
def test_the_plan_declares_the_run_mode_read_for_the_input_review():
    """input_mode is the gate lever, not a Param: without this the user_gated
    review of the resolved NWM discharge is silently lost."""
    from trid3nt_server.workflows.lib import RunMode, resolve_params

    wf = _workflow()
    review = next(s for s in wf.plan.declared()
                  if s.name == "reviewed_discharge")
    assert review.kwargs["input_mode"] is RunMode
    assert review.self_gating is True    # so no second FormGate may be declared


# --- the sag chart ----------------------------------------------------------- #
def test_the_chart_title_is_not_doubled_on_a_bbox_only_invocation():
    from trid3nt_server.workflows.lib import ParamValues
    from trid3nt_server.workflows.telemac.do_sag.do_sag import build_sag_chart

    result = SimpleNamespace(
        name="Dissolved oxygen sag (Eel_River_near_Scotia)",
        sag_curve_distance_m=[0.0, 1000.0], sag_curve_do_mgl=[9.0, 8.0],
        sag_curve_bod_mgl=[20.0, 18.0], do_standard_mgl=5.0, do_min_mgl=8.0,
        do_min_distance_m=1000.0, do_violates_standard=False,
    )
    payload = build_sag_chart(result=result, params=ParamValues({}))
    assert payload["title"] == "Dissolved oxygen sag (Eel_River_near_Scotia)"
    assert payload["title"].count("sag") == 1


def test_do_layer_contract_fields():
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC_DO_STYLE_PRESET,
        TelemacDoLayerURI,
    )
    lay = TelemacDoLayerURI(
        layer_id="t", name="n", layer_type="raster", uri="s3://b/k.tif",
        style_preset=TELEMAC_DO_STYLE_PRESET, role="primary",
        do_min_mgl=4.0, do_min_distance_m=6500.0, do_standard_mgl=5.0,
        do_violates_standard=True, sag_curve_distance_m=[0.0, 100.0],
        sag_curve_do_mgl=[9.0, 8.0], sag_curve_bod_mgl=[20.0, 18.0],
    )
    assert lay.do_violates_standard is True
    assert lay.style_preset == "continuous_dissolved_oxygen"


# --- the REAL composition, driven through the declared plan ------------------ #
def _stub_reach_pipeline(monkeypatch, order, seen, *, layer, review, tmp_path=None):
    """Patch the shared step family at the modules the plan's runners resolve to."""
    from trid3nt_server.gates import input_review as gate_mod
    from trid3nt_server.workflows.mesh import step as mesh_step_mod
    from trid3nt_server.workflows.telemac.helpers import (
        forcing as forcing_mod,
        reach as reach_mod,
    )
    from trid3nt_server.workflows.telemac.steps import (
        deck as deck_mod,
        products as products_mod,
        solve as solve_mod,
    )

    def _step(name, ret):
        async def _inner(**kwargs):
            order.append(name)
            seen[name] = kwargs
            return ret
        return _inner

    reach = {"bbox": (-124.2, 40.4, -124.0, 40.6), "name": "Eel", "slug": "eel"}
    monkeypatch.setattr(reach_mod, "geocode_reach", _step("geocode", reach))
    monkeypatch.setattr(reach_mod, "fetch_reach_flowline",
                        _step("rivers", "s3://r/rivers.geojson"))
    monkeypatch.setattr(reach_mod, "reach_seed",
                        _step("seed", {"lon": -124.1, "lat": 40.5,
                                       "source": "flowline"}))
    monkeypatch.setattr(forcing_mod, "resolve_carrier_discharge",
                        _step("discharge", {"m3s": 2.0, "basis": "fetched",
                                            "note": "NWM 2.0 m3/s"}))
    monkeypatch.setattr(mesh_step_mod, "build_declared_mesh",
                        _step("mesh", {"mesh_id": "M", "slf_uri": "s3://m/river.slf",
                                       "topology_uri": "s3://m/mesh_topology.json",
                                       "min_edge_m": 9.0}))
    # The mesh session stands in, so its display face is a uri nothing wrote: what
    # the mesh holds of the reach is measured in its own test module.
    monkeypatch.setattr(reach_mod, "_meshed_fraction",
                        lambda mesh, centerline: 1.0)
    if tmp_path is not None:
        install_reach_chain(monkeypatch, tmp_path, seen)
    monkeypatch.setattr(deck_mod, "write_reach_deck",
                        _step("deck", {"deck": {"name": "eel"}, "run_tag": "T"}))
    monkeypatch.setattr(solve_mod, "solve_reach", _step("solve", {"run_id": "R"}))
    monkeypatch.setattr(products_mod, "publish_do_products", _step("products", layer))
    monkeypatch.setattr(gate_mod, "gate_input_review", review)


@pytest.mark.asyncio
async def test_the_declared_plan_composes_the_shared_steps_in_order(monkeypatch,
                                                                    tmp_path):
    """The migrated plan itself, not a stand-in: geocode -> flowline -> seed ->
    discharge -> waqtel -> review -> corridor mesh -> deck -> solve -> DO
    products, with the
    outfall riding as the reach SEED (it pins which water body is meshed), never
    as a dye release point."""
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC_DO_STYLE_PRESET,
        TelemacDoLayerURI,
    )
    from trid3nt_server.workflows.telemac.do_sag.do_sag import telemac_do_sag

    order: list[str] = []
    seen: dict = {}
    layer = TelemacDoLayerURI(
        layer_id="t", name="Dissolved oxygen sag (eel)", layer_type="raster",
        uri="s3://b/k.tif", style_preset=TELEMAC_DO_STYLE_PRESET, role="primary",
        do_min_mgl=8.0, do_min_distance_m=100.0, do_standard_mgl=5.0,
        do_violates_standard=False)

    async def _review(**kwargs):
        order.append("review")
        seen["review"] = kwargs
        from trid3nt_server.gates.input_review import ReviewOutcome

        return ReviewOutcome(proceed=True, entries=list(kwargs["entries"]),
                             params=dict(kwargs["params"]))

    _stub_reach_pipeline(monkeypatch, order, seen, layer=layer, review=_review,
                         tmp_path=tmp_path)

    out = await telemac_do_sag(
        location="Eel River near Scotia, California", upstream_do_mgl=99.0,
        outfall_coords=[-124.11, 40.51], input_mode="user_gated")

    assert not isinstance(out, dict), out
    assert order == ["geocode", "rivers", "seed", "discharge", "review", "mesh",
                     "deck", "solve", "products"]
    # the outfall pins the MESHED water body, so it rides as the reach seed the
    # ONE centerline is navigated from - never as a dye release point
    assert seen["seed"]["supplied"] == (-124.11, 40.51)
    assert "release_coords" not in seen["deck"]
    # DO cannot ride in above its own saturation - the one coupled clamp
    assert seen["deck"]["do_sag_config"]["upstream_do_mgl"] == pytest.approx(9.022)
    assert seen["deck"]["do_sag_config"]["k2_formula"] == 0
    assert seen["review"]["mode"] == "user_gated"
    assert seen["solve"]["deck"] is seen["products"]["deck"]
    # the REVIEWED discharge is what the deck and the products both read
    assert seen["deck"]["carrier_discharge"] is seen["products"]["carrier_discharge"]


@pytest.mark.asyncio
async def test_a_cancelled_review_refuses_before_the_solve(monkeypatch, tmp_path):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    from trid3nt_server.workflows.telemac.do_sag.do_sag import telemac_do_sag
    from trid3nt_server.workflows.telemac.steps import solve as solve_mod

    order: list[str] = []
    seen: dict = {}

    async def _cancelled(**_kw):
        from trid3nt_server.gates.input_review import ReviewOutcome

        return ReviewOutcome(proceed=False, entries=[], params={},
                             cancelled=True, cancel_reason="user declined")

    _stub_reach_pipeline(monkeypatch, order, seen, layer=None, review=_cancelled,
                         tmp_path=tmp_path)

    async def _solve_must_not_run(**_kw):
        raise AssertionError("the solve ran past a cancelled review")

    monkeypatch.setattr(solve_mod, "solve_reach", _solve_must_not_run)

    out = await telemac_do_sag(location="Eel River near Scotia, California",
                               input_mode="user_gated")
    assert isinstance(out, dict) and out["error_code"] == "USER_INPUT_CANCELLED"
    assert "solve" not in order


# --- mesh granularity: the MEASURED edge, never a re-derived one ------------- #
def test_the_deck_records_the_edge_the_accepted_mesh_was_measured_at():
    """DS-3: the granularity a run is judged on is the built mesh's own minimum
    edge, not the number that was asked for and not one re-derived from a channel
    width nobody surveyed."""
    from trid3nt_server.workflows.telemac.helpers.reach import suggest_time_step_s

    # The measured edge drives the CFL step; the asked edge only stands in until
    # a mesh exists to measure.
    measured = {"probes": {"edge_length_m": {"min": 8.0}}}
    assert suggest_time_step_s(20.0, mesh=_Measured(measured)) == \
        suggest_time_step_s(8.0)


class _Measured:
    def __init__(self, doc):
        self.probes = doc["probes"]


# --- the analytical overlay: wired in, anchored, and honest about absence ---- #
def _overlay(**kw):
    from trid3nt_server.workflows.telemac.postprocess_telemac import (
        _streeter_phelps_overlay,
    )
    return _streeter_phelps_overlay(**kw)


def test_the_overlay_is_anchored_at_the_modeled_mix_point():
    """The closed form starts where the solve says the load entered - the CBOD
    peak - not at the top of whatever stretch happened to be wet."""
    xs = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
    bod = [0.0, 0.0, 20.0, 18.0, 16.0, 14.0]
    do = [9.0, 9.0, 8.5, 8.2, 8.0, 7.9]
    anchor, sp, note = _overlay(curve_x=xs, curve_do=do, curve_bod=bod,
                                velocity_mps=0.5, saturation_mgl=9.0,
                                k1_per_day=2.0, k2_per_day=6.0)
    assert anchor == 2 and len(sp) == 4
    assert sp[0] == pytest.approx(do[2])          # anchored ON the modeled state
    assert "200 m downstream" in note and "0.500 m/s" in note


@pytest.mark.parametrize("kw,reason", [
    (dict(velocity_mps=0.0), "velocity"),
    (dict(curve_bod=[0.0] * 6), "organic load"),
    (dict(k1_per_day=None), "rates"),
])
def test_the_overlay_says_why_there_is_none(kw, reason):
    base = dict(curve_x=[0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
                curve_do=[9.0] * 6, curve_bod=[0.0, 0.0, 20.0, 18.0, 16.0, 14.0],
                velocity_mps=0.5, saturation_mgl=9.0, k1_per_day=2.0,
                k2_per_day=6.0)
    anchor, sp, note = _overlay(**{**base, **kw})
    assert sp == [] and anchor == 0 and reason in note


def test_the_overlay_reproduces_the_closed_form_it_is_graded_against():
    """Deterministic grading: fed a profile that IS sp_do_profile, the overlay
    returns that same profile - so a stated deviation is the solve's, not the
    overlay's."""
    xs = [0.0, 500.0, 1000.0, 1500.0, 2000.0, 2500.0]
    do_exact, _ = sp_do_profile(xs, 0.4, 9.0, 20.0, 0.5, 2.0, 6.0)
    bod = [20.0] + [0.0] * 5          # the mix point is bin 0
    _anchor, sp, _note = _overlay(curve_x=xs, curve_do=do_exact, curve_bod=bod,
                                  velocity_mps=0.4, saturation_mgl=9.0,
                                  k1_per_day=2.0, k2_per_day=6.0)
    assert sp == pytest.approx(do_exact, abs=1e-9)


# --- the downstream axis is pointed by the SOLVED flow ----------------------- #
def test_the_principal_axis_is_oriented_by_the_solved_velocity():
    from trid3nt_server.workflows.telemac.postprocess_telemac import (
        _downstream_coordinate,
    )

    x = np.linspace(0.0, 1000.0, 21)
    y = np.zeros_like(x)
    east, label_e = _downstream_coordinate(x, y, None, flow_uv=(1.0, 0.0))
    west, label_w = _downstream_coordinate(x, y, None, flow_uv=(-1.0, 0.0))
    # the same node cloud, opposite flows: chainage zero moves to the other end
    assert east[0] == pytest.approx(0.0) and east[-1] == pytest.approx(1000.0)
    assert west[0] == pytest.approx(1000.0) and west[-1] == pytest.approx(0.0)
    assert label_e == label_w
    assert "oriented by the solved mean velocity" in label_e
    assert "direction unverified" in _downstream_coordinate(x, y, None)[1]
