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
def test_do_saturation_temperature_relation():
    from trid3nt_server.workflows.telemac.do_sag.steps import do_saturation_mgl

    def sat(t):
        return do_saturation_mgl(SimpleNamespace(water_temp_c=t))

    assert sat(20.0) == pytest.approx(9.0, abs=0.2)   # ~9 mg/L at 20C
    assert sat(5.0) > sat(25.0)                       # colder holds more


def test_declared_params_and_plan_validate():
    from trid3nt_server.workflows.lib import resolve_params, validate_plan
    from trid3nt_server.workflows.telemac.do_sag.do_sag import DATA, PARAMS, plan

    p = asyncio.run(resolve_params(PARAMS, {"location": "Eel River near Scotia, California"}))
    validate_plan(plan(p, None), PARAMS, DATA)
    assert p.get("do_saturation_mgl") == pytest.approx(9.022, abs=1e-3)  # Cs at 20 C
    assert p.get("upstream_do_mgl") == p.get("do_saturation_mgl")               # saturated inflow
    assert p.row("k1_per_day").consequence == "numerical"         # never refuses in auto


def test_declared_bounds_clamp_the_wq_knobs():
    from trid3nt_server.workflows.lib import resolve_params
    from trid3nt_server.workflows.telemac.do_sag.do_sag import PARAMS

    p = asyncio.run(resolve_params(PARAMS, {"location": "x", "reach_length_km": 900.0,
                                            "k1_per_day": 0.0}))
    assert p.get("reach_length_km") == 15.0 and "CLAMPED" in p.row("reach_length_km").note
    assert p.get("k1_per_day") == 0.01


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
    from trid3nt_server.workflows.telemac.do_sag.do_sag import PARAMS

    p = asyncio.run(resolve_params(PARAMS, {"location": "Eel River near Scotia"}))
    row = next(r for r in provenance_entries(p, PARAMS) if r.param == "outfall_coords")
    assert row.basis == "derived"
    assert "mid-reach" in (row.note or "")


def test_a_supplied_outfall_is_carried_as_a_user_row():
    from trid3nt_server.workflows.lib import provenance_entries, resolve_params
    from trid3nt_server.workflows.telemac.do_sag.do_sag import PARAMS, _normalize

    supplied, err = _normalize({"location": "x", "outfall_coords": ["-124.1", "40.5"]})
    assert err is None and supplied["outfall_coords"] == (-124.1, 40.5)
    p = asyncio.run(resolve_params(PARAMS, supplied))
    row = next(r for r in provenance_entries(p, PARAMS) if r.param == "outfall_coords")
    assert row.basis == "user"


# --- the gate-mode lever reaches the reach pipeline -------------------------- #
def test_the_plan_declares_the_run_mode_read_for_the_reach_pipeline():
    """input_mode is the gate lever, not a Param: without this the user_gated
    review of NWM discharge / bank_source is silently lost."""
    from trid3nt_server.workflows.lib import RunMode, resolve_params
    from trid3nt_server.workflows.telemac.do_sag.do_sag import PARAMS, plan

    p = asyncio.run(resolve_params(PARAMS, {"location": "x"}))
    solve = next(s for s in plan(p, None).flat() if s.name == "do_field")
    assert solve.kwargs["input_mode"] is RunMode


@pytest.mark.asyncio
async def test_input_mode_reaches_the_reach_solve(tmp_path, monkeypatch):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC_DO_STYLE_PRESET,
        TelemacDoLayerURI,
    )
    from trid3nt_server.workflows.telemac.do_sag import steps as do_sag_steps
    from trid3nt_server.workflows.telemac.do_sag.do_sag import telemac_do_sag

    seen: dict = {}

    async def _fake_solve(**kwargs):
        seen.update(kwargs)
        return TelemacDoLayerURI(
            layer_id="t", name="Dissolved oxygen sag (reach)", layer_type="raster",
            uri="s3://b/k.tif", style_preset=TELEMAC_DO_STYLE_PRESET, role="primary",
            do_min_mgl=8.0, do_min_distance_m=100.0, do_standard_mgl=5.0,
            do_violates_standard=False,
        )

    monkeypatch.setattr(do_sag_steps, "solve_waqtel_o2", _fake_solve)
    out = await telemac_do_sag(location="Eel River near Scotia, California",
                               input_mode="user_gated")
    assert not isinstance(out, dict), out
    assert seen["input_mode"] == "user_gated"


# --- the sag chart ----------------------------------------------------------- #
def test_the_chart_title_is_not_doubled_on_a_bbox_only_invocation():
    from trid3nt_server.workflows.lib import ResolvedParams
    from trid3nt_server.workflows.telemac.do_sag.steps import build_sag_chart

    result = SimpleNamespace(
        name="Dissolved oxygen sag (Eel_River_near_Scotia)",
        sag_curve_distance_m=[0.0, 1000.0], sag_curve_do_mgl=[9.0, 8.0],
        sag_curve_bod_mgl=[20.0, 18.0], do_standard_mgl=5.0, do_min_mgl=8.0,
        do_min_distance_m=1000.0, do_violates_standard=False,
    )
    payload = build_sag_chart(result=result, params=ResolvedParams({}))
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


# --- the REAL composition over the shared step family ------------------------ #
@pytest.mark.asyncio
async def test_the_reach_solve_composes_the_shared_steps_in_order(monkeypatch):
    """The wave-3 rewrite itself, not a stand-in for it: solve_waqtel_o2 composes
    geocode -> flowline -> seed -> discharge -> review -> deck -> solve -> DO
    products, and the outfall rides as the reach SEED (it pins which water body
    is meshed), never as a dye release point."""
    from trid3nt_contracts.telemac_contracts import (
        TELEMAC_DO_STYLE_PRESET,
        TelemacDoLayerURI,
    )
    from trid3nt_server.gates import input_review as gate_mod
    from trid3nt_server.workflows.telemac import steps as shared
    from trid3nt_server.workflows.telemac.do_sag.steps import solve_waqtel_o2

    order: list[str] = []
    seen: dict = {}

    def _step(name, ret):
        async def _inner(**kwargs):
            order.append(name)
            seen[name] = kwargs
            return ret
        return _inner

    reach = {"bbox": (-124.2, 40.4, -124.0, 40.6), "name": "Eel", "slug": "eel",
             "river_name": "Eel River"}
    seed = {"lon": -124.1, "lat": 40.5, "source": "flowline"}
    layer = TelemacDoLayerURI(
        layer_id="t", name="Dissolved oxygen sag (eel)", layer_type="raster",
        uri="s3://b/k.tif", style_preset=TELEMAC_DO_STYLE_PRESET, role="primary",
        do_min_mgl=8.0, do_min_distance_m=100.0, do_standard_mgl=5.0,
        do_violates_standard=False)

    monkeypatch.setattr(shared, "geocode_reach", _step("geocode", reach))
    monkeypatch.setattr(shared, "fetch_reach_flowline", _step("rivers", {"uri": "s3://r"}))
    monkeypatch.setattr(shared, "reach_seed", _step("seed", seed))
    monkeypatch.setattr(shared, "resolve_carrier_discharge",
                        _step("discharge", {"m3s": 2.0, "basis": "fetched",
                                            "note": "NWM 2.0 m3/s"}))
    monkeypatch.setattr(shared, "write_reach_deck",
                        _step("deck", {"deck": {"name": "eel"}, "run_tag": "T"}))
    monkeypatch.setattr(shared, "solve_reach", _step("solve", {"run_id": "R"}))
    monkeypatch.setattr(shared, "publish_do_products", _step("products", layer))

    async def _review(**kwargs):
        order.append("review")
        seen["review"] = kwargs
        from trid3nt_server.gates.input_review import ReviewOutcome

        return ReviewOutcome(proceed=True, entries=list(kwargs["entries"]),
                             params=dict(kwargs["params"]))

    monkeypatch.setattr(gate_mod, "gate_input_review", _review)

    out = await solve_waqtel_o2(
        location="Eel River near Scotia, California", bbox=None,
        discharge_bod_mgl=20.0, upstream_do_mgl=99.0, do_saturation_mgl=9.022,
        water_temp_c=20.0, do_standard_mgl=5.0, k1_per_day=0.3, k2_per_day=0.9,
        reach_length_km=12.0, channel_width_m=60.0, sim_duration_s=3600.0,
        discharge_m3s=None, mesh_resolution="auto", mesh_resolution_m=None,
        bank_source="nhd_area", compute_class="medium",
        outfall_coords=[-124.11, 40.51], input_mode="user_gated")

    assert out is layer
    assert order == ["geocode", "rivers", "seed", "discharge", "review", "deck",
                     "solve", "products"]
    # the outfall pins the MESHED water body, so it rides as the reach seed
    assert seen["deck"]["reach_seed_coords"] == [-124.11, 40.51]
    assert "release_coords" not in seen["deck"]
    # DO cannot ride in above its own saturation - the one coupled clamp
    assert seen["deck"]["do_sag_config"]["upstream_do_mgl"] == pytest.approx(9.022)
    assert seen["deck"]["do_sag_config"]["k2_formula"] == 0
    assert seen["review"]["mode"] == "user_gated"
    assert seen["solve"]["deck"] is seen["products"]["deck"]


@pytest.mark.asyncio
async def test_a_cancelled_review_refuses_before_the_solve(monkeypatch):
    from trid3nt_server.gates import input_review as gate_mod
    from trid3nt_server.workflows.telemac import steps as shared
    from trid3nt_server.workflows.telemac.do_sag.steps import solve_waqtel_o2

    def _step(ret):
        async def _inner(**kwargs):
            return ret
        return _inner

    monkeypatch.setattr(shared, "geocode_reach",
                        _step({"bbox": (-1, 1, 1, 2), "name": "n", "slug": "s"}))
    monkeypatch.setattr(shared, "fetch_reach_flowline", _step({}))
    monkeypatch.setattr(shared, "reach_seed", _step({"lon": 0.0, "lat": 1.5}))
    monkeypatch.setattr(shared, "resolve_carrier_discharge",
                        _step({"m3s": 2.0, "basis": "fetched", "note": "n"}))

    async def _solve_must_not_run(**_kw):
        raise AssertionError("the solve ran past a cancelled review")

    monkeypatch.setattr(shared, "solve_reach", _solve_must_not_run)

    async def _cancelled(**_kw):
        from trid3nt_server.gates.input_review import ReviewOutcome

        return ReviewOutcome(proceed=False, entries=[], params={},
                             cancelled=True, cancel_reason="user declined")

    monkeypatch.setattr(gate_mod, "gate_input_review", _cancelled)

    with pytest.raises(shared.TelemacDyeScenarioError) as ei:
        await solve_waqtel_o2(
            location="x", bbox=None, discharge_bod_mgl=20.0, upstream_do_mgl=9.0,
            do_saturation_mgl=9.0, water_temp_c=20.0, do_standard_mgl=5.0,
            k1_per_day=0.3, k2_per_day=0.9, reach_length_km=12.0,
            channel_width_m=60.0, sim_duration_s=3600.0, discharge_m3s=None,
            mesh_resolution="auto", mesh_resolution_m=None, bank_source="nhd_area",
            compute_class="medium", outfall_coords=None, input_mode="user_gated")
    assert ei.value.error_code == "USER_INPUT_CANCELLED"
