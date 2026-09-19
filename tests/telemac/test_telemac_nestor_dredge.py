"""The dredge on the sediment deck: what NESTOR's own readers would take.

Offline: the three files are TEXT the composite writes, so what is proved here is
the grammar those readers demand - the keywords they spell, the three-digit field
names, the terminators - the refusals that come before a run, and the numbers a
solved run's listing and result hand back."""

from __future__ import annotations

from typing import Any

import pytest

from trid3nt_server.workflows.telemac.authoring.nestor import NestorRefused
from trid3nt_server.workflows.telemac.modules import GAIA, fill
from trid3nt_server.workflows.telemac.modules.gaia import (
    Backfill,
    Dig,
    Dredging,
    Dump,
    SaveWaterLevel,
)
from trid3nt_server.workflows.telemac.modules.module import SlotRefused

#: The run's own clock, which every action is dated against; the profiles are
#: the surface its levels are read from.
_ORIGIN = [2026, 3, 1, 6, 0, 0]
_PROFILES = [[0.0, 100.0, 3.0, 0.0, -100.0, 3.0, 0.0],
             [900.0, 100.0, 2.0, 900.0, -100.0, 2.0, 0.9]]


def _area(label: str) -> dict[str, Any]:
    return {"label": label,
            "vertices": [[0.0, 0.0], [100.0, 0.0], [100.0, 50.0], [0.0, 50.0]]}


def _sheet(*actions: Any, reference: Any = None):
    return fill(GAIA, **dict(GAIA.bed(
        geometry="a.slf", boundary="a.cli", MASS_BALANCE=True,
        gradation=None, presets={}, CLASSES_SEDIMENT_DIAMETERS=[0.0002], LAYERS_INITIAL_THICKNESS=[5.0], BED_LOAD_TRANSPORT_FORMULA_FOR_ALL_SANDS=1,
        HIDING_FACTOR_FORMULA=1, MORPHOLOGICAL_FACTOR=10.0,
        dredging=Dredging(actions=list(actions), measured={},
                          reference=_PROFILES if reference is None else reference,
                          origin=_ORIGIN))["slots"]))


def _criterion_dig(**over: Any) -> Any:
    asked = dict(field=_area("fairway"), level="SECTIONS", start=3600.0,
                 end=86400.0, repeat=7200.0, rate=0.002, depth=3.0,
                 crit_depth=3.0, min_volume=0.0, min_volume_radius=12.0,
                 dump=_area("spoil ground"), dump_rate=0.002)
    return Dig(**{**asked, **over})


def test_the_dredge_arms_nestor_and_names_the_files_it_wrote():
    """The composite expands to the arming keyword and the three files it authors.

    The restart file is NOT among them: this composite writes none, so a deck
    naming one would send the engine to a file nobody staged."""
    sheet = _sheet(_criterion_dig())
    written = dict(sheet.resolved())
    assert written["NESTOR"] is True
    assert written["NESTOR ACTION FILE"] == "nestor_actions.dat"
    assert written["NESTOR POLYGON FILE"] == "nestor_polygons.dat"
    assert written["NESTOR SURFACE REFERENCE FILE"] == "nestor_reference.dat"
    assert "NESTOR RESTART FILE" not in written
    assert sorted(sheet.files) == ["nestor_actions.dat", "nestor_polygons.dat",
                                   "nestor_reference.dat"]


def test_the_action_file_is_the_grammar_the_reader_takes():
    """RESTART once, one ACTION/ENDACTION pair per action, ENDFILE at column one,
    and every keyword spelled the way the reader's own SELECT CASE spells it."""
    text = _sheet(_criterion_dig()).files["nestor_actions.dat"]
    lines = text.splitlines()
    assert "RESTART = FALSE" in lines
    assert lines.count("ACTION") == 1 and lines.count("ENDACTION") == 1
    assert lines[-1] == "ENDFILE"
    stated = [line.split("=")[0].strip() for line in lines if "=" in line]
    assert stated == ["RESTART", "ActionType", "FieldDig", "ReferenceLevel",
                      "TimeStart", "TimeRepeat", "TimeEnd", "DigRate",
                      "DigDepth", "CritDepth", "MinVolume", "MinVolumeRadius",
                      "FieldDump", "DumpRate"]


def test_every_time_is_an_absolute_date_against_the_run_s_own_origin():
    """The reader takes exactly 19 characters and differences them against the
    host deck's time origin; nothing here carries a clock of its own."""
    text = _sheet(_criterion_dig()).files["nestor_actions.dat"]
    stamps = {line.split("=")[0].strip(): line.split("=")[1].strip()
              for line in text.splitlines() if "Time" in line}
    assert stamps["TimeStart"] == "2026.03.01-07:00:00"
    assert stamps["TimeEnd"] == "2026.03.02-06:00:00"
    assert all(len(v) == 19 for v in (stamps["TimeStart"], stamps["TimeEnd"]))


def test_a_dig_states_a_volume_or_a_rate_and_never_both():
    """A volume is Dig_by_time and a rate is Dig_by_criterion: two action types,
    so a value stating both or neither names none of them."""
    timed = _sheet(Dig(field=_area("fairway"), start=0.0, end=600.0,
                       volume=100.0)).files["nestor_actions.dat"]
    assert "ActionType = Dig_by_time" in timed
    assert "DigVolume = 100.000000" in timed
    with pytest.raises(SlotRefused, match="volume OR a rate"):
        Dig(field=_area("fairway"), start=0.0, end=600.0)
    with pytest.raises(SlotRefused, match="volume OR a rate"):
        Dig(field=_area("fairway"), start=0.0, end=600.0, volume=1.0, rate=1.0)


def test_the_other_action_types_state_only_what_their_own_reader_reads():
    """Each type demands its own fields and no others - a timed dump is refused a
    rate by the reader itself, so the value carries none to state."""
    text = _sheet(
        SaveWaterLevel(start=0.0, level="WATERLVL1"),
        Dump(field=_area("spoil"), start=60.0, end=600.0, volume=250.0,
             grain_class=[1.0]),
        Backfill(field=_area("scour hole"), start=600.0, end=1200.0,
                 crit_depth=2.0, level="WATERLVL1", grain_class=[1.0]),
    ).files["nestor_actions.dat"]
    assert "ActionType = Save_water_level" in text
    assert "ActionType = Dump_by_time" in text
    assert "ActionType = Backfill_to_level" in text
    assert "DumpVolume = 250.000000" in text
    assert text.count("GrainClass = 1.000000") == 2
    assert "DumpRate" not in text


def test_a_field_named_twice_refuses_before_the_reader_does():
    """The engine identifies a field by its three leading numerals alone and
    refuses a file referencing one twice, as dig or as dump."""
    with pytest.raises(NestorRefused, match="more than one action"):
        _sheet(_criterion_dig(dump=_area("fairway")))


def test_the_polygon_file_names_each_field_with_three_leading_numerals():
    """The name is read from column six and its first three characters must be
    numerals; the file closes with ENDFILE."""
    text = _sheet(_criterion_dig()).files["nestor_polygons.dat"]
    lines = text.splitlines()
    names = [line[5:] for line in lines if line.startswith("NAME:")]
    assert names == ["101_fairway", "102_spoil_ground"]
    assert all(name[:3].isdigit() for name in names)
    assert lines[-1] == "ENDFILE"
    action = _sheet(_criterion_dig()).files["nestor_actions.dat"]
    assert "FieldDig = 101_fairway" in action
    assert "FieldDump = 102_spoil_ground" in action


def test_the_reference_file_is_seven_reals_a_line_and_ends_with_END():
    text = _sheet(_criterion_dig()).files["nestor_reference.dat"]
    rows = [line for line in text.splitlines()
            if line and not line.startswith(("#", "END"))]
    assert len(rows) == 2 and all(len(row.split()) == 7 for row in rows)
    assert text.splitlines()[-1] == "END"


def test_one_profile_or_a_short_row_refuses_by_what_the_reader_demands():
    with pytest.raises(NestorRefused, match="more than one line of seven reals"):
        _sheet(_criterion_dig(), reference=[[0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 0.0]])
    with pytest.raises(NestorRefused, match="more than one line of seven reals"):
        _sheet(_criterion_dig(), reference=[[0.0, 0.0], [1.0, 1.0]])


def test_a_dredge_over_a_bed_with_no_stock_refuses_by_name():
    """A suspension is one settling class over a bed of zero thickness: there is
    nothing in it to dig and nowhere the dumped material would be accounted."""
    with pytest.raises(SlotRefused, match="nothing to dig"):
        GAIA.suspended(geometry="a.slf", boundary="a.cli", MASS_BALANCE=True,
                       CLASSES_SEDIMENT_DIAMETERS=[3e-05], concentration_mgl=250.0,
                       SUSPENSION_TRANSPORT_FORMULA_FOR_ALL_SANDS=3, SCHEME_FOR_ADVECTION_OF_SUSPENDED_SEDIMENTS=[1],
                       dredging=Dredging(
                           actions=[], measured={}, reference=_PROFILES, origin=_ORIGIN))


def test_the_carrier_deck_has_no_dredge_of_its_own():
    """NESTOR on the telemac2d deck adds a thickness to the bottom with no
    sediment stock behind it, which is not what this surface exposes."""
    from trid3nt_server.workflows.telemac.modules import T2D

    with pytest.raises(SlotRefused, match="no keyword 'dredging'"):
        fill(T2D, dredging=Dredging(actions=[], measured={}, reference=_PROFILES,
                                    origin=_ORIGIN))


def test_the_volumes_are_read_off_the_engine_s_own_report_lines():
    """The criterion dig reports the volume of the LAST maintenance period and
    resets its own sum at the next, so the run's figure is the sum over them."""
    from trid3nt_server.workflows.telemac.modules.listing import nestor_volumes

    listing = "\n".join((
        " ?>        dug volume  [m^3]  :    1200.5",
        " ?>        dumped vol  [m^3]  :    1100.0",
        " ?>        dug volume  [m^3]  :    800.25",
        " ?>        dumped vol  [m^3]  :    700.0",
        " ?>   relocated volume [m**3] :    100.0",
    ))
    read = nestor_volumes(listing)
    assert {k: v for k, v in read.items() if k != "dredge_report"} == {
        "dug_volume_m3": 2000.75, "dumped_volume_m3": 1800.0,
        "relocated_volume_m3": 100.0}
    assert "summed over the passes that finished" in read["dredge_report"]
    assert nestor_volumes("") == {}


#: The engine's own activation line, verbatim: it prints this when an action
#: STARTS and its volume line only when a pass finishes.
_STARTED = "\n".join((
    " ?>          action number    :   1",
    " ?>          start action     : Dig_by_criterion",
    " ?>  nominal start time  [s]  :    82800.000000000000",
    " ?>          start time  [s]  :    82800.000000000000",
    " ?>          FieldDig         : 101_dredge_area",
))


def test_a_pass_that_did_not_finish_is_stated_rather_than_left_null():
    """TimeEnd stops the next pass, not the one cutting, so a pass that has not
    reached grade when the clock runs out prints no volume at all."""
    from trid3nt_server.workflows.telemac.modules.listing import nestor_volumes

    read = nestor_volumes(_STARTED)
    assert "dug_volume_m3" not in read and "dumped_volume_m3" not in read
    assert read["dredge_report"] == (
        "a Dig_by_criterion pass started at 82800 s and had not cut to grade "
        "when the run's clock ended; the engine prints a volume only for a pass "
        "that finishes, so the bed moved with no volume to state it")


def test_a_dredge_that_never_began_is_told_apart_from_one_that_did_not_finish():
    """NESTOR marks its initialisation banner too, so a run that armed a dredge
    whose schedule the clock never reached is not read as a run without one."""
    from trid3nt_server.workflows.telemac.modules.listing import nestor_volumes

    banner = " ?>       Each info line is marked with the Lable"
    assert nestor_volumes(banner)["dredge_report"].startswith(
        "no dredging pass began inside the run's clock")


#: An L in lon/lat: the notch is the quadrant its bounding box holds and its
#: interior does not.
_L_SHAPE = {"type": "Polygon", "coordinates": [[
    [-124.10, 40.50], [-124.09, 40.50], [-124.09, 40.505], [-124.095, 40.505],
    [-124.095, 40.51], [-124.10, 40.51], [-124.10, 40.50]]]}
#: A point in the notch: inside the box, outside the L.
_IN_THE_NOTCH = (-124.0915, 40.5085)
#: A point in the L's own arm.
_IN_THE_ARM = (-124.0975, 40.5025)


def _nodes_at(*lonlat: tuple[float, float]):
    from shapely.geometry import mapping, Point as _Point

    from trid3nt_server.workflows.telemac.authoring.assembler import to_utm

    return [list(to_utm(mapping(_Point(lon, lat)), 32610).coords)[0]
            for lon, lat in lonlat]


#: The fairway the Willamette canary draws, as the accepted mesh reads it: a bed
#: between -12.2 and -9.3 m against a surface standing at 2.776 m.
_FAIRWAY_RING = [[0.0, 0.0], [80.0, 0.0], [80.0, 90.0], [0.0, 90.0], [0.0, 0.0]]
_FAIRWAY_XY = [(20.0, 20.0), (40.0, 45.0), (60.0, 70.0)]
_FAIRWAY_BED = [-9.309, -11.0, -12.179]
_OPENS_AT = 2.776


def test_a_grade_the_bed_has_no_stock_to_reach_refuses_at_authoring():
    """A grade deeper than the erodible bed is a pass the engine abandons part
    way through its first cut, so the cut the declaration asks for is measured
    against the stated stock before the run dispatches."""
    from trid3nt_server.workflows.telemac.authoring.assembler import (
        _refuse_a_cut_past_the_stock,
    )
    from trid3nt_server.workflows.telemac.errors import TelemacError

    def _measure(grade_depth_m: float) -> None:
        _refuse_a_cut_past_the_stock(
            _FAIRWAY_RING, node_xy=_FAIRWAY_XY, node_bed=_FAIRWAY_BED,
            name="dredge_area", level_m=_OPENS_AT, depth_m=None,
            grade_depth_m=grade_depth_m, stock_m=5.0)

    with pytest.raises(TelemacError, match="cut of 11.515 m") as refused:
        _measure(23.6)
    assert refused.value.error_code == "TELEMAC_DREDGE_CUT_PAST_STOCK"
    said = str(refused.value)
    assert "node 1 of the mesh sits at -9.309 m" in said
    assert "holds the dredge_area at -20.824 m" in said
    assert "5.000 m of erodible bed" in said
    # The maintenance grade the same channel has the material to reach: 0.915 m
    # out of the shallow end and nothing where the bed is already under it.
    _measure(13.0)


def test_a_dredge_area_is_guarded_by_its_polygon_not_its_bounding_box():
    """The engine works the ring, so an area whose box holds nodes its interior
    does not has nothing in it to move and refuses before the solve."""
    from trid3nt_server.workflows.telemac.authoring.assembler import _dredge_field
    from trid3nt_server.workflows.telemac.errors import TelemacError

    with pytest.raises(TelemacError, match="holds no node") as refused:
        _dredge_field(_L_SHAPE, "dredge_area", utm_epsg=32610,
                      node_xy=_nodes_at(_IN_THE_NOTCH))
    assert refused.value.error_code == "TELEMAC_DREDGE_AREA_OFF_MESH"
    field = _dredge_field(_L_SHAPE, "dredge_area", utm_epsg=32610,
                          node_xy=_nodes_at(_IN_THE_NOTCH, _IN_THE_ARM))
    assert field["label"] == "dredge_area" and len(field["vertices"]) == 7
