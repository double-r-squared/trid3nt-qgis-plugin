"""WAQTEL O2 dissolved-oxygen sag: offline V and V plus the tool tests.

No solve, no network. A live solve's O2 profile is a committed fixture, re-checked
here against the Streeter-Phelps closed form deterministically, so a regression in
the O2 machinery is caught without re-solving."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from tests._fakes.reach_chain import install_reach_chain

from trid3nt_server.workflows.publishing import Line, Profile
from trid3nt_server.workflows.telemac.templates.do_sag.streeter_phelps import (
    overlay,
    sp_critical_point,
    sp_do_profile,
)

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "telemac_o2_sp_idealized_profile.json"


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
    from trid3nt_server.workflows.runtime import resolve_params, validate_plan

    wf = _workflow()
    p = asyncio.run(resolve_params(wf.params,
                                   {"location": "Eel River near Scotia, California"}))
    validate_plan(wf.plan, wf.params, wf.data)
    assert p.value_of("do_saturation_mgl") == pytest.approx(9.022, abs=1e-3)  # Cs at 20 C
    assert p.value_of("upstream_do_mgl") == p.value_of("do_saturation_mgl")   # saturated inflow
    assert p.row("k1_per_day").consequence == "numerical"    # never refuses in auto


def test_the_plan_reads_as_the_universal_stage_sequence():
    """The skeleton owns the sequence; the facade's five ops stamp each step."""
    from trid3nt_server.workflows.runtime import resolve_params

    wf = _workflow()
    plan = wf.plan
    stages = [s.stage for s in plan.declared() if s.stage]
    assert stages == ["acquire", "acquire", "acquire", "mesh", "mesh",
                      "author", "author", "author", "solve", "publish"]
    assert [s.name for s in plan.declared()][-1] == "outputs"


def test_declared_bounds_clamp_the_wq_knobs():
    from trid3nt_server.workflows.runtime import resolve_params

    wf = _workflow()
    p = asyncio.run(resolve_params(wf.params, {"location": "x", "reach_length_km": 900.0,
                                               "k1_per_day": 0.0}))
    assert p.value_of("reach_length_km") == 15.0 \
        and "CLAMPED" in p.row("reach_length_km").note
    assert p.value_of("k1_per_day") == 0.01


@pytest.mark.asyncio
async def test_do_sag_requires_location_or_bbox():
    from trid3nt_server.workflows.telemac.templates.do_sag.do_sag import telemac_do_sag
    out = await telemac_do_sag()
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


# --- the outfall: absent DERIVES, malformed REFUSES -------------------------- #
@pytest.mark.parametrize("bad", [[1.0], [1.0, 2.0, 3.0], {"lon": 1},
                                 [200.0, 10.0], ["a", "b"]])
@pytest.mark.asyncio
async def test_malformed_outfall_coords_refuse_they_never_fall_back(bad):
    """A garbage discharge location must not silently become the reach seed.

    A bare word is not garbage: it is a place name the geocoder is asked about."""
    from trid3nt_server.workflows.telemac.templates.do_sag.do_sag import telemac_do_sag
    out = await telemac_do_sag(location="Eel River near Scotia, California",
                               outfall_coords=bad)
    assert isinstance(out, dict) and out["error_code"] == "TELEMAC_PARAMS_INVALID"
    assert "outfall_coords" in out["error_message"]


def test_an_absent_outfall_leaves_a_derived_provenance_row():
    """The user has to see what the sag distance is measured FROM."""
    from trid3nt_server.workflows.runtime import provenance_entries, resolve_params

    wf = _workflow()
    p = asyncio.run(resolve_params(wf.params, {"location": "Eel River near Scotia"}))
    row = next(r for r in provenance_entries(p, wf.params)
               if r.param == "outfall_coords")
    assert row.basis == "derived"
    assert "mid-reach" in (row.note or "")


def test_a_supplied_outfall_is_carried_as_a_user_row():
    from trid3nt_server.workflows.runtime import provenance_entries, resolve_params

    wf = _workflow()
    from trid3nt_server.inputs import Point

    supplied, err = asyncio.run(wf._normalize({"location": "x",
                                               "outfall_coords": ["-124.1", "40.5"]}))
    assert err is None and supplied["outfall_coords"] == Point(-124.1, 40.5)
    p = asyncio.run(resolve_params(wf.params, supplied))
    row = next(r for r in provenance_entries(p, wf.params)
               if r.param == "outfall_coords")
    assert row.basis == "user"


# --- the gate-mode lever reaches the resolved-input review ------------------- #
def test_the_door_declares_the_run_mode_read_for_the_sheet_review():
    """input_mode is the gate lever, not a Param: without this the user_gated
    review of the filled sheet is silently lost."""
    from trid3nt_server.workflows.runtime import RunMode

    wf = _workflow()
    review = next(s for s in wf.plan.declared() if s.name == "sheet")
    assert review.kwargs["input_mode"] is RunMode
    assert review.self_gating is True    # so no gate may be declared in front


# --- the outputs list: the oxygen field, its animation, its profile ---------- #
def test_the_outputs_list_reads_the_oxygen_and_charts_it_down_the_reach():
    """The oxygen is the SECOND tracer the O2 process appends, read at the last
    instant as the map and down the centerline as the chart; the load is read
    for the answer, and the closed form rides the chart as a reference."""
    from trid3nt_server.workflows.telemac.templates.do_sag.do_sag import (
        ANSWER,
        CAPTIONS,
        OUTPUTS,
    )

    assert [(p.kind, p.variable, p.t, p.publish) for p in OUTPUTS] == [
        ("field", "T2", -1, "layer"), ("field", "T2", "every", "animate"),
        ("profile", "T2", -1, "chart")]
    assert OUTPUTS[2].reference is overlay
    assert CAPTIONS == {"T2": "dissolved oxygen", "T3": "organic load"}
    assert {name: (m.primitive.kind, m.primitive.variable, m.stat)
            for name, m in ANSWER.items()} == {
        "do_min_mgl": ("profile", "T2", "min"),
        "do_below_standard": ("profile", "T2", "min"),
        "do_min_distance_m": ("profile", "T2", "x_min_m"),
        "bod_mixed_mgl": ("profile", "T3", "max"),
        "mean_velocity_mps": ("profile", "T2", "velocity_mps"),
        "mesh_size_m": ("mesh", None, "size_m")}
    # the verdict is the minimum held below the standard the sheet declares
    assert ANSWER["do_below_standard"].op == "below"
    assert ANSWER["do_below_standard"].against.name == "do_standard_mgl"


# --- the REAL composition, driven through the declared plan ------------------ #
def _stub_reach_pipeline(monkeypatch, order, seen, *, layer, review, tmp_path=None):
    """Patch the shared trees at the modules the plan's runners resolve to."""
    from trid3nt_server.gates import input_review as gate_mod
    from trid3nt_server.workflows.mesh import step as mesh_step_mod
    from trid3nt_server.workflows.telemac.templates import reach as reach_mod
    from trid3nt_server.workflows.telemac.solving import solve as solve_mod
    from trid3nt_server.workflows.telemac.authoring import assembler as asm_mod
    from trid3nt_server.workflows.telemac import workflow as door_mod

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
    monkeypatch.setattr(reach_mod, "resolve_carrier_discharge",
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
    monkeypatch.setattr(asm_mod, "settle_release",
                        _step("outfall", {"at": [0.0, 0.0], "name": None}))
    monkeypatch.setattr(asm_mod, "settle_reach",
                        _step("settled", {
                            "name": "eel", "title": "eel REACH",
                            "graphic_period": 200, "until_s": 3700.0,
                            "time_step_s": 1.0, "depth_m": 1.2,
                            "friction_law": 3, "friction_coefficient": 33.0,
                            "mesh_inputs": [], "server_facts": {},
                            "continue_from": None, "inflow_q_m3s": 2.0,
                            "outflow_stage_m": 1.0,
                            "liquid_boundary_order": ["inflow"],
                            "liquid_boundary_prescribes": ["flowrate"]}))
    monkeypatch.setattr(door_mod, "run_sheet", _step("run", {"run_id": "R"}))
    monkeypatch.setattr(solve_mod, "solve_reach", _step("solve", {"run_id": "R"}))
    monkeypatch.setattr(door_mod, "publish_outputs", _step("outputs", layer))
    monkeypatch.setattr(gate_mod, "gate_input_review", review)


@pytest.mark.asyncio
async def test_the_declared_plan_composes_the_shared_steps_in_order(monkeypatch,
                                                                    tmp_path):
    """The declared plan itself, not a stand-in.

    Geocode, flowline, seed, discharge, corridor mesh, settle, review, run, solve,
    outputs - with the outfall riding as the reach SEED, never as a dye release."""
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    from trid3nt_contracts.execution import AnswerLayerURI
    from trid3nt_server.workflows.telemac.templates.do_sag.do_sag import telemac_do_sag

    order: list[str] = []
    seen: dict = {}
    layer = AnswerLayerURI(
        layer_id="t", name="Dissolved oxygen (mg/L) at t = 600 s (eel)",
        layer_type="raster", uri="s3://b/k.tif", role="primary",
        quantity="dissolved_oxygen",
        answer={"do_min_mgl": 8.0, "do_min_distance_m": 100.0,
                "bod_mixed_mgl": 20.0, "mean_velocity_mps": 0.4,
                "mesh_size_m": 9.0})

    async def _review(**kwargs):
        order.append("review")
        seen["review"] = kwargs
        from trid3nt_server.gates.input_review import ReviewOutcome

        return ReviewOutcome(proceed=True, entries=list(kwargs["entries"]),
                             params=dict(kwargs["params"]))

    _stub_reach_pipeline(monkeypatch, order, seen, layer=layer, review=_review,
                         tmp_path=tmp_path)

    out = await telemac_do_sag(
        location="Eel River near Scotia, California", upstream_do_mgl=7.5,
        outfall_coords=[-124.11, 40.51], input_mode="user_gated")

    assert not isinstance(out, dict), out
    assert order == ["geocode", "rivers", "seed", "discharge", "mesh", "outfall",
                     "settled", "review", "run", "outputs"]
    # the outfall pins the MESHED water body, so it rides as the reach seed the
    # ONE centerline is navigated from - never as a dye release point
    from trid3nt_server.inputs import Point

    assert seen["seed"]["supplied"] == Point(-124.11, 40.51)
    assert seen["outfall"]["point"] == Point(-124.11, 40.51)
    assert seen["outfall"]["label"] == "Outfall"
    # The body reads the declared upstream oxygen where it states its boundary
    # and its initial state, and the user's own value reaches the deck.
    body = seen["run"]["sheet"].body
    assert body.ASSERTED["boundaries"]["tracers"][1].name == "upstream_do_mgl"
    filled = dict(seen["run"]["sheet"].resolved())
    assert filled["PRESCRIBED TRACERS VALUES"][1] == pytest.approx(7.5)
    assert filled["INITIAL VALUES OF TRACERS"] == [0.0, 7.5, 0.0, 0.0]
    # The O2 process is the coupled body's own sheet: the rates and the
    # saturation the template declared, and a constant reaeration formula.
    o2 = dict(seen["run"]["sheet"].files["t2d_river.waqtel"]["slots"])
    assert (o2["CONSTANT_OF_DEGRADATION_OF_ORGANIC_LOAD_K1"],
            o2["K2_REAERATION_COEFFICIENT"], o2["FORMULA_FOR_COMPUTING_K2"],
            o2["O2_SATURATION_DENSITY_OF_WATER__CS_"]) == (0.3, 0.9, 0,
                                                         pytest.approx(9.022))
    assert seen["review"]["mode"] == "user_gated"
    # the door renders the SHEET it just filled, and holds there
    assert seen["review"]["param_sheet"].workflow == "telemac_do_sag"


@pytest.mark.asyncio
async def test_a_cancelled_review_refuses_before_the_solve(monkeypatch, tmp_path):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    from trid3nt_server.workflows.telemac.templates.do_sag.do_sag import telemac_do_sag
    from trid3nt_server.workflows.telemac.solving import solve as solve_mod

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
def test_the_run_records_the_edge_the_accepted_mesh_was_measured_at():
    """DS-3: the granularity a run is judged on is the built mesh's own minimum
    edge, not the number that was asked for and not one re-derived from a channel
    width nobody surveyed."""
    from trid3nt_server.workflows.telemac.helpers.time_step import suggest_time_step_s

    # The measured edge drives the CFL step; the asked edge only stands in until
    # a mesh exists to measure.
    measured = {"probes": {"edge_length_m": {"min": 8.0}}}
    assert suggest_time_step_s(20.0, mesh=_Measured(measured)) == \
        suggest_time_step_s(8.0)


class _Measured:
    def __init__(self, doc):
        self.probes = doc["probes"]


# --- the analytical overlay: anchored, and honest about absence ------------- #
def _profile(x, values, *, velocity=0.5, units="mg/L") -> Profile:
    import numpy as np

    return Profile(name="DISSOLVED O2", units=units, distance_m=np.asarray(x),
                   values=np.asarray(values), along="downstream distance",
                   measures={"min": min(values), "velocity_mps": velocity})


def _load(x, values) -> tuple:
    from trid3nt_server.workflows.telemac.modules import field
    from trid3nt_server.workflows.telemac.modules.outputs import profile

    return {profile("T3", along="line"): _profile(x, values),
            field("T2"): None}


_PARAMS = {"do_standard_mgl": 5.0, "do_saturation_mgl": 9.0,
           "k1_per_day": 2.0, "k2_per_day": 6.0}


def test_the_overlay_is_anchored_at_the_modeled_mix_point():
    """The closed form starts where the solve says the load entered - the CBOD
    peak - not at the top of whatever stretch happened to be wet; the standard
    and the load ride beside it under their own labels."""
    xs = [0.0, 100.0, 200.0, 300.0, 400.0, 500.0]
    bod = [0.0, 0.0, 20.0, 18.0, 16.0, 14.0]
    do = [9.0, 9.0, 8.5, 8.2, 8.0, 7.9]
    lines = overlay(_profile(xs, do), _load(xs, bod), _PARAMS)
    assert [line.label for line in lines] == [
        "5 mg/L standard", "organic load", "Streeter-Phelps closed form"]
    closed = lines[2]
    assert list(closed.x) == xs[2:] and len(closed.values) == 4
    assert closed.values[0] == pytest.approx(do[2])   # anchored ON the modeled state
    assert lines[0].values == [5.0, 5.0] and list(lines[1].values) == bod


@pytest.mark.parametrize("velocity,bod", [
    (0.0, [0.0, 0.0, 20.0, 18.0, 16.0, 14.0]),
    (0.5, [0.0] * 6),
])
def test_the_overlay_draws_no_closed_form_it_cannot_anchor(velocity, bod):
    xs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    lines = overlay(_profile(xs, [9.0] * 6, velocity=velocity), _load(xs, bod),
                    _PARAMS)
    assert "Streeter-Phelps closed form" not in [line.label for line in lines]


def test_the_overlay_reproduces_the_closed_form_it_is_graded_against():
    """Deterministic grading: fed a profile that IS sp_do_profile, the overlay
    returns that same profile - so a stated deviation is the solve's, not the
    overlay's."""
    xs = [0.0, 500.0, 1000.0, 1500.0, 2000.0, 2500.0]
    do_exact, _ = sp_do_profile(xs, 0.4, 9.0, 20.0, 0.5, 2.0, 6.0)
    bod = [20.0] + [0.0] * 5          # the mix point is bin 0
    lines = overlay(_profile(xs, do_exact, velocity=0.4), _load(xs, bod), _PARAMS)
    assert lines[-1].values == pytest.approx(do_exact, abs=1e-9)
