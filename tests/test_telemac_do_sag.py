"""WAQTEL O2 dissolved-oxygen sag (telemac_do_sag) - offline V&V + tool tests.

No solve, no network. The live V&V (the 12 km straight-channel WAQTEL O2 solve
through the LANDED worker author_deck, trid3nt-local/telemac:latest, 2026-08-07)
is captured as a committed profile fixture; this test re-checks it against the
Streeter-Phelps 1925 closed form deterministically (the 0163/0167 committed-V&V
pattern), so a regression in the O2 machinery is caught without re-solving.
"""
import json
from pathlib import Path

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
    from trid3nt_server.workflows.telemac.do_sag.do_sag import _do_saturation_mgl
    assert _do_saturation_mgl(20.0) == pytest.approx(9.0, abs=0.2)   # ~9 mg/L at 20C
    assert _do_saturation_mgl(5.0) > _do_saturation_mgl(25.0)        # colder holds more


@pytest.mark.asyncio
async def test_do_sag_requires_location_or_bbox():
    from trid3nt_server.workflows.telemac.do_sag.do_sag import telemac_do_sag
    out = await telemac_do_sag()
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


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
