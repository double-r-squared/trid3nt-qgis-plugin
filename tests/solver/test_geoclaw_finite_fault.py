"""The FiniteFault composite: a real earthquake's published slip as a dtopo file.

Offline. The records are USGS's own Kamchatka 2025 and Tohoku 2011 inversions read by the row's parser. Pinned: the
subfaults carry the inversion's slip unscaled in Clawpack's SubFault names; the dtopo's uplift and subsidence match
the Okada maxima first computed from the same files (+8.24/-4.68 m Kamchatka, +11.30/-5.16 m Tohoku) within 0.1 m on
a 0.05 degree grid; a rupture another composite wrote, or an unstated value, refuses by name.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers.hazard.fetch_finite_fault import hooks
from trid3nt_server.workflows.geoclaw.authoring.dtopo import SUBFAULT_FIELDS, finite_fault_subfaults
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO

_FIX = Path(__file__).parents[1] / "fixtures" / "geoclaw" / "finite_fault"
_RUN = {"GEO_COORDINATE_SYSTEM": 2}


def _record(name: str) -> dict:
    spec = get_spec("fetch_finite_fault")
    return hooks.record(spec, {"event_id": name}, [(_FIX / f"{name}.fsp").read_bytes()])


def _stated(name: str, **over) -> dict:
    return {"finite_fault": _record(name), "spacing": 0.05, "buffer": 1.0, "times": [1.0], **over}


def _uplift(name: str) -> tuple[float, float]:
    from clawpack.geoclaw import dtopotools

    expanded, files = GEO.COMPOSITES["FiniteFault"].apply(_stated(name), _RUN)
    spec = files[expanded["DTOPO_DTOPOFILES"][0][1]]["dtopo"]
    subs = []
    for stated in spec["subfaults"]:
        sub = dtopotools.SubFault()
        for field in SUBFAULT_FIELDS:
            setattr(sub, field, stated[field])
        sub.calculate_geometry()
        subs.append(sub)
    fault = dtopotools.Fault()
    fault.subfaults = subs
    x, y = (np.linspace(*spec[axis][:2], int(spec[axis][2])) for axis in ("x", "y"))
    dz = fault.create_dtopography(x, y, [1.0]).dZ[-1]
    return float(dz.max()), float(dz.min())


def test_the_subfaults_carry_the_inversions_slip_unscaled_in_clawpacks_names():
    record = _record("chignik_2021")
    ours = finite_fault_subfaults(record)
    row, mine = record["subfaults"][5], ours[5]

    assert len(ours) == 294
    assert mine == {"longitude": row["longitude"], "latitude": row["latitude"], "depth": row["depth_km"] * 1000.0,
                    "strike": 243.0, "dip": 14.0, "rake": row["rake"], "slip": row["slip_m"],
                    "length": 14_000.0, "width": 14_000.0, "coordinate_specification": "centroid"}


@pytest.mark.parametrize("name, up, down", [("kamchatka_2025", 8.24, -4.68), ("tohoku_2011", 11.30, -5.16)])
def test_the_dtopo_uplift_and_subsidence_equal_the_first_computed_okada_extremes(name, up, down):
    high, low = _uplift(name)

    assert high == pytest.approx(up, abs=0.1) and low == pytest.approx(down, abs=0.1)


def test_a_rupture_already_written_or_an_unstated_value_refuses_by_name():
    with pytest.raises(GeoclawError) as beside:
        GEO.COMPOSITES["FiniteFault"].apply(_stated("chignik_2021"), {**_RUN, "DTOPO_DTOPOFILES": [[3, "x"]]})
    with pytest.raises(GeoclawError) as unstated:
        GEO.COMPOSITES["FiniteFault"].apply(_stated("chignik_2021", spacing=None), _RUN)
    with pytest.raises(GeoclawError) as empty:
        GEO.COMPOSITES["FiniteFault"].apply(_stated("chignik_2021", finite_fault={"subfaults": []}), _RUN)

    assert beside.value.error_code == "GEOCLAW_FINITE_FAULT_RUPTURE_STATED"
    assert unstated.value.error_code == "GEOCLAW_FINITE_FAULT_UNSTATED" and "spacing" in str(unstated.value)
    assert empty.value.error_code == "GEOCLAW_FINITE_FAULT_UNSTATED"
