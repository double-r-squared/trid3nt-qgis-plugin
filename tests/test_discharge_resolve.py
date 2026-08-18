"""Offline ladder checks for the shared NWM discharge resolver (ADR 0285 P5).

The live derive-or-refuse is proven by the SCHISM baroclinic A/B; here we pin the
seam's three paths without network (user / NWM-derived / UNRESOLVED-refuse) so a
regression that silently reintroduces an invented inflow fails offline.
"""

from __future__ import annotations

from unittest.mock import patch

from trid3nt_server.workflows.shared import discharge_resolve as dr

_BBOX = [-75.6, 38.9, -75.0, 39.6]  # Delaware Bay-ish


def test_user_supplied_discharge_is_used_verbatim() -> None:
    res = dr.resolve_dominant_discharge(_BBOX, 850.0)
    assert res.resolved
    assert res.source == "user_supplied"
    assert res.discharge_m3s == 850.0
    assert res.entry.basis == "user"
    assert res.entry.consequence == "physics"


def test_nwm_derived_dominant_reach() -> None:
    with patch.object(dr, "dominant_reach_discharge", return_value=(432.1, {"n_reaches": 12})):
        res = dr.resolve_dominant_discharge(_BBOX, None)
    assert res.resolved
    assert res.source == "nwm_dominant_reach"
    assert res.discharge_m3s == 432.1
    assert res.entry.basis == "derived"
    assert res.entry.consequence == "physics"
    assert res.entry.real_source_if_any == "fetch_noaa_nwm_streamflow (NWM analysis, dominant reach)"


def test_unresolved_refuses_with_physics_default_demo() -> None:
    with patch.object(dr, "dominant_reach_discharge", return_value=(None, {"reason": "off CONUS"})):
        res = dr.resolve_dominant_discharge(_BBOX, None)
    assert not res.resolved
    assert res.source == "unresolved"
    assert res.discharge_m3s is None
    # the input-review gate refuses in auto ONLY on physics + default_demo:
    assert res.entry.basis == "default_demo"
    assert res.entry.consequence == "physics"
    assert res.entry.value is None
