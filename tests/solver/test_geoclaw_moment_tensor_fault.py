"""The MomentTensorFault composite: a magnitude-only source as one rupture through a named published scaling relation.

Offline. The record is the row's read of USGS's Ecuador 2016 moment-tensor products. Pinned: the Wells and Coppersmith
(1994) Table 2A coefficients give the lengths and widths a hand calculation of the printed table gives; the slip is
M0 / (mu L W) with Clawpack's SubFault rigidity and the rupture's Okada moment returns M0; the stated plane's
angles ride unchanged; a magnitude outside a relation's fitted range, an unnamed relation, solution or plane, a rupture
another composite wrote and a rupture reaching the surface refuse by name. The slab placement test moves the real
centroid onto the Cascadia excerpt (a banner-labeled isolated gate: the tensor is Ecuador's, the place is not).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers import _slab2_grids as slab2
from trid3nt_server.tools.fetchers._router.registration import get_spec
from trid3nt_server.tools.fetchers.hazard.fetch_moment_tensor import hooks as tensor_hooks
from trid3nt_server.tools.fetchers.hazard.fetch_slab_geometry import hooks as slab_hooks
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.helpers.scaling import RELATIONS, rupture_dimensions
from trid3nt_server.workflows.geoclaw.modules import GEO

_FIX = Path(__file__).parents[1] / "fixtures" / "geoclaw"
_RUN = {"GEO_COORDINATE_SYSTEM": 2}
_MWW = "us_20005j32_mww"


def _record(event: str = "ecuador_2016") -> dict:
    return tensor_hooks.record(get_spec("fetch_moment_tensor"), {"event_id": "e"},
                               [(_FIX / "moment_tensor" / f"{event}_event.json").read_bytes()])


def _stated(**over) -> dict:
    return {"moment_tensor": _record(), "solution": _MWW, "plane": 2, "relation": "wells_coppersmith_1994_all",
            "placement": "centroid", "spacing": 0.05, "buffer": 1.0, "times": [1.0], **over}


def _subfault(**over) -> dict:
    expanded, files = GEO.COMPOSITES["MomentTensorFault"].apply(_stated(**over), _RUN)
    return files[expanded["DTOPO_DTOPOFILES"][0][1]]["dtopo"]["subfaults"][0]


def test_the_relations_return_the_printed_tables_lengths_and_widths():
    length, width = rupture_dimensions("wells_coppersmith_1994_all", 7.0)
    assert length == pytest.approx(10 ** (-2.44 + 0.59 * 7.0) * 1000.0) and width == pytest.approx(10 ** (-1.01 + 0.32 * 7.0) * 1000.0)
    assert (length / 1000, width / 1000) == pytest.approx((48.97, 16.98), abs=0.01)
    reverse = rupture_dimensions("wells_coppersmith_1994_reverse", 7.0)
    assert (reverse[0] / 1000, reverse[1] / 1000) == pytest.approx((10 ** (-2.42 + 0.58 * 7), 10 ** (-1.61 + 0.41 * 7)), rel=1e-12)
    assert set(RELATIONS) == {f"wells_coppersmith_1994_{k}" for k in ("all", "reverse", "strike_slip", "normal")} | {"thingbaijam_2017_interface"}


def test_one_subfault_with_the_stated_planes_angles_the_published_centroid_and_slip_over_the_scaled_rectangle():
    from clawpack.geoclaw import dtopotools

    sub = _subfault()
    mw = dtopotools.Mw(7.05e20)
    length, width = rupture_dimensions("wells_coppersmith_1994_all", mw)

    assert (sub["strike"], sub["dip"], sub["rake"]) == (26.0, 16.0, 113.0)
    assert (sub["longitude"], sub["latitude"], sub["depth"]) == (-79.97, -0.25, 21500.0)
    assert (sub["length"], sub["width"]) == (length, width) and sub["coordinate_specification"] == "centroid"
    assert sub["slip"] == pytest.approx(7.05e20 / (4e10 * length * width), rel=1e-12)
    other = _subfault(plane=1)
    assert (other["strike"], other["dip"], other["rake"]) == (183.0, 75.0, 83.0)


def test_the_okada_moment_of_the_written_subfault_returns_the_solutions_scalar_moment():
    from clawpack.geoclaw import dtopotools

    sub = dtopotools.SubFault()
    for key, value in _subfault().items():
        setattr(sub, key, value)
    sub.calculate_geometry()

    assert sub.Mo() == pytest.approx(7.05e20, rel=1e-12)


def test_the_slab_placement_takes_only_the_depth_from_the_interface(monkeypatch):
    """ISOLATED GATE: the real Ecuador tensor with its centroid moved onto the Cascadia excerpt."""
    tar = _FIX / "slab2" / "slab2_excerpt.tar.gz"
    monkeypatch.setattr(slab2, "archive_bytes", lambda spec: tar.read_bytes())
    slab = slab_hooks.record(get_spec("fetch_slab_geometry"), {"bbox": [-126.2, 46.3, -123.6, 47.6]}, [])
    moved = _record()
    moved["solutions"][0]["centroid"] = {"longitude": -125.0, "latitude": 47.0, "depth_km": 21.5}
    read = slab2.sample(slab2.blocks_from_record(slab), -125.0, 47.0)

    sub = _subfault(moment_tensor=moved, placement="slab", slab=slab)

    assert sub["depth"] == pytest.approx(read["depth_km"] * 1000.0) and sub["depth"] != 21500.0
    assert (sub["strike"], sub["dip"]) == (26.0, 16.0)
    with pytest.raises(GeoclawError, match="state slab"):
        _subfault(moment_tensor=moved, placement="slab")
    off = _record()
    with pytest.raises(GeoclawError, match="not on the slab record"):
        _subfault(placement="slab", slab=slab, moment_tensor=off)


def test_a_magnitude_outside_the_fitted_range_refuses_naming_the_range_and_the_paper():
    with pytest.raises(GeoclawError, match=r"outside the wells_coppersmith_1994_reverse length regression's fitted range 4.8 to 7.6.*Table 2A, p. 990"):
        _subfault(relation="wells_coppersmith_1994_reverse")
    big = _record("kamchatka_2025")
    with pytest.raises(GeoclawError, match="Mw 8.86 is outside"):
        _subfault(moment_tensor=big, solution="us_6000qw60_mww")


def test_an_unnamed_or_unknown_pick_and_an_unstated_value_refuse_by_name():
    with pytest.raises(GeoclawError, match="scaling relation 'strasser_2010' is none built"):
        _subfault(relation="strasser_2010")
    with pytest.raises(GeoclawError, match="solution 'nope' is none the event holds"):
        _subfault(solution="nope")
    with pytest.raises(GeoclawError, match="neither 1 nor 2"):
        _subfault(plane=3)
    with pytest.raises(GeoclawError, match="placement 'hypocentre'"):
        _subfault(placement="hypocentre")
    for key in ("solution", "plane", "relation", "placement"):
        stated = _stated()
        del stated[key]
        with pytest.raises(GeoclawError, match=f"states no {key}"):
            GEO.COMPOSITES["MomentTensorFault"].apply(stated, _RUN)
    with pytest.raises(GeoclawError, match="states bogus"):
        _subfault(bogus=1)


def test_a_rupture_another_composite_wrote_and_a_surface_reaching_rupture_refuse():
    with pytest.raises(GeoclawError, match="already stated"):
        GEO.COMPOSITES["MomentTensorFault"].apply(_stated(), {**_RUN, "DTOPO_DTOPOFILES": [[3, "x"]]})
    shallow = _record()
    shallow["solutions"][0]["centroid"]["depth_km"] = 2.0
    with pytest.raises(GeoclawError, match="above the surface"):
        _subfault(moment_tensor=shallow, plane=1)
    none = _record()
    none["solutions"][0]["centroid"] = None
    with pytest.raises(GeoclawError, match="publishes no centroid"):
        _subfault(moment_tensor=none)


def test_the_thingbaijam_interface_relation_pins_its_coefficients_and_refuses_outside_its_range():
    citation, length, width = RELATIONS["thingbaijam_2017_interface"]
    assert length == (-2.412, 0.583, 5.4, 9.2) and width == (-0.880, 0.366, 5.4, 9.2)
    assert "thingbaijam2017.py" in citation and "10.1785/0120170017" in citation
    got_length, got_width = rupture_dimensions("thingbaijam_2017_interface", 9.0)
    assert got_length == pytest.approx(10 ** (-2.412 + 0.583 * 9.0) * 1000.0)
    assert got_width == pytest.approx(10 ** (-0.880 + 0.366 * 9.0) * 1000.0)
    for mw in (5.3, 9.3):
        with pytest.raises(GeoclawError, match=r"outside the thingbaijam_2017_interface length regression's fitted range 5.4 to 9.2"):
            rupture_dimensions("thingbaijam_2017_interface", mw)
