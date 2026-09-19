"""KHIONE: the module's own table, the deck a host couples, and the weather the
two of them share.

Offline: nothing here starts a solve. What is proved is the deck the product
would hand the engine - the variables keyword it generates, the file it writes
beside the host's, the keywords a two-dimensional carrier refuses, and the
atmospheric column two readers cannot share."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.authoring.atmosphere import COLUMNS
from trid3nt_server.workflows.telemac.errors import TelemacError
from trid3nt_server.workflows.telemac.modules import (
    KHIONE,
    T2D,
    T3D,
    SlotRefused,
    fill,
)
from trid3nt_server.workflows.telemac.modules.khione import (
    RESULT_FILENAME,
    STEERING_FILENAME,
)
from trid3nt_server.workflows.telemac.modules.module import identify_on
from trid3nt_server.workflows.telemac.modules.telemac2d import Atmosphere
from trid3nt_server.workflows.telemac.workflow import run_bodies

#: The column a DAMOCLES line is read to, which is where the engine truncates
#: this keyword's own value.
_COLUMNS = 72
_PRINTOUTS = "VARIABLES FOR GRAPHIC PRINTOUTS"


def _ice(**keywords):
    return KHIONE.ice(geometry="geo_longflume.slf", boundary="geo_longflume.cli",
                      **keywords)


def _matches(token: str, mnemonic: str) -> bool:
    """The engine's own matching rule for one word against one mnemonic.

    ``bief/sortie.f`` compares eight characters, and a ``~`` ends the comparison
    matching wherever the character under it is a letter."""
    padded = mnemonic.ljust(8)
    for position, char in enumerate(token.ljust(8)):
        if char == padded[position]:
            continue
        if char == "~" and padded[position] != " " and padded[position].isalpha():
            return True
        return False
    return True


def test_the_wrapper_writes_its_own_result_beside_the_host_s():
    body = _ice()
    assert body["module"] == "khione"
    assert body["steering"] == STEERING_FILENAME
    assert body["slots"]["RESULTS_FILE"] == RESULT_FILENAME
    assert KHIONE.RESULT_FILE == RESULT_FILENAME


def test_the_temperature_is_appended_to_every_host_and_the_rest_ride_a_switch():
    """``nametrac_khione.f`` ADDTRACERs the temperature in both of its branches
    and the engine stops a coupled run that opens neither, so the host counts it
    whatever the deck says; the salinity, the frazil and the dynamic cover each
    ride the branch they are written in."""
    assert [row.name for row in KHIONE.APPENDS(_ice(HEAT_BUDGET=False,
                                                    ICE_COVER_IMPACT_ON_HYDRODYNAMIC=True))] \
        == ["TEMPERATURE"]
    # The heat budget is the dictionary's own default, so a deck that states no
    # switch at all is a deck the engine suspends frazil under.
    assert [row.name for row in KHIONE.APPENDS(_ice())] == ["TEMPERATURE", "FRAZIL"]
    assert [row.name for row in KHIONE.APPENDS(
        _ice(SALINITY=True, DYNAMIC_ICE_COVER=True))] == [
        "TEMPERATURE", "SALINITY", "FRAZIL", "ICE COVER FRAC.", "ICE COVER THICK."]
    assert [row.unit for row in KHIONE.APPENDS(_ice(SALINITY=True))] == [
        "oC", "ppt", "VOLUME FRACTION"]
    assert all(row.style for _, rows in KHIONE.APPENDABLE for row in rows)


def test_one_frazil_row_per_class_the_deck_counts_on_the_host_and_the_ice_file():
    """The engine numbers its frazil mnemonics from one, on both files: the
    tracers it appends to the host and the four rows per class its own result
    carries. The count is the deck's, and nothing it states is left unrowed."""
    assert [row.name for row in KHIONE.APPENDS(
        _ice(NUMBER_OF_CLASSES_FOR_SUSPENDED_FRAZIL_ICE=3))] == [
        "TEMPERATURE", "FRAZIL 1", "FRAZIL 2", "FRAZIL 3"]
    stated = dict(_ice(NUMBER_OF_CLASSES_FOR_SUSPENDED_FRAZIL_ICE=3)["slots"])
    table = KHIONE.table(stated)
    for mnemonic in ("F", "N", "SF", "SN"):
        assert {f"{mnemonic}{n}" for n in (1, 2, 3)} <= set(table)
    assert table["F2"].name == "FRAZIL CLASS 2"
    assert table["SF2"].name == "FRAZIL CLASS 2S"
    # One class is named without a number, which is the engine's own spelling.
    assert KHIONE.table(dict(_ice()["slots"]))["F1"].name == "FRAZIL"


def test_the_ice_file_rows_nothing_the_budget_did_not_allocate():
    """Every row past the twenty-four indexed ones is allocated inside the
    thermal budget, so a deck that turns it off rows none of them."""
    off = KHIONE.table(dict(_ice(HEAT_BUDGET=False)["slots"]))
    assert len(off) == 20
    assert not {"F1", "TEMP", "NTOT"} & set(off)
    on = KHIONE.table(dict(_ice(SALINITY=True, DYNAMIC_ICE_COVER=True)["slots"]))
    assert {"SAL", "SALS", "DYNCOVC", "DYNCOVT"} <= set(on)


def test_a_host_couples_khione_through_the_seam_the_other_modules_use():
    sheet = fill(T2D, coupling=[_ice(HEAT_BUDGET=False)])
    stated = dict(sheet.resolved())
    assert stated["COUPLING WITH"] == "KHIONE"
    assert stated["KHIONE STEERING FILE"] == STEERING_FILENAME
    coupled = sheet.files[STEERING_FILENAME]
    assert coupled["module"] == "khione"
    assert coupled["slots"]["HEAT_BUDGET"] is False


def test_the_qualified_floor_reaches_the_coupled_body():
    class Cover(T2D):
        coupling = [_ice()]

    body, identifier = identify_on(run_bodies(Cover), "khione: HEAT BUDGET")
    assert body is KHIONE
    assert identifier == "HEAT_BUDGET"


def test_a_keyword_two_bodies_spell_names_the_body_it_belongs_to():
    class Cover(T2D):
        coupling = [_ice()]

    with pytest.raises(SlotRefused) as refused:
        identify_on(run_bodies(Cover), "GRAPHIC PRINTOUT PERIOD")
    assert "telemac2d: GRAPHIC PRINTOUT PERIOD" in str(refused.value)
    assert "khione: GRAPHIC PRINTOUT PERIOD" in str(refused.value)


def test_the_table_is_written_in_the_engine_s_own_wildcard_and_fits_its_line():
    stated = dict(_ice(HEAT_BUDGET=False)["slots"])
    literal = ",".join(KHIONE.written(stated))
    assert len(literal) > _COLUMNS
    value = fill(KHIONE, **_ice(HEAT_BUDGET=False)["slots"]).printouts()[_PRINTOUTS]
    assert len(value) <= _COLUMNS
    tokens = value.split(",")
    choices = dict(KHIONE.slot(KHIONE.PRINTOUTS).choices)
    reached = {mnemonic for mnemonic in choices
               for token in tokens if _matches(token, mnemonic)}
    # The wildcard asks for exactly the rows the table carries and for nothing
    # else the keyword spells.
    assert reached == set(KHIONE.written(stated))


def test_a_row_under_a_keyword_is_written_unless_the_deck_switches_it_off():
    """The engine reads the dictionary's own default where a deck states
    nothing, so an unstated HEAT BUDGET is the budget ON and the rows it
    allocates are asked for; a deck that states NO drops every one of them."""
    choices = dict(KHIONE.slot(KHIONE.PRINTOUTS).choices)

    def reached(value: str) -> set:
        return {mnemonic for mnemonic in choices
                for token in value.split(",") if _matches(token, mnemonic)}

    unstated = fill(KHIONE, **_ice()["slots"]).printouts()[_PRINTOUTS]
    stated = fill(KHIONE, **_ice(HEAT_BUDGET=True)["slots"]).printouts()[_PRINTOUTS]
    assert unstated == stated
    assert {"NTOT", "CTOT", "NTOTS", "CTOTS", "TEMP", "TEMPS"} <= reached(stated)
    # The engine numbers its per-class mnemonics at run time, so the deck names
    # them literally; the dictionary spells that index ``i``.
    assert {"F1", "N1", "SF1", "SN1"} <= set(stated.split(","))

    off = fill(KHIONE, **_ice(HEAT_BUDGET=False)["slots"]).printouts()[_PRINTOUTS]
    assert not {"NTOT", "TEMP"} & reached(off)
    assert "F1" not in off


def test_the_three_dimensional_keywords_are_refused_by_name_under_a_2d_carrier():
    with pytest.raises(SlotRefused) as refused:
        fill(T2D, coupling=[_ice(RD_RESULTS_FILE="ice3d.slf",
                                 SCHEME_FOR_DIFFUSION_OF_FRAZIL_IN_3D=1)])
    assert "3D RESULTS FILE" in str(refused.value)
    assert "SCHEME FOR DIFFUSION OF FRAZIL IN 3D" in str(refused.value)
    assert len(KHIONE.ONLY_3D) == 6


def test_a_three_dimensional_host_couples_khione_and_states_its_column():
    """Both carriers register the SAME coupling composite; the water column is
    the whole of what separates them. A three-dimensional host writes the six
    keywords a two-dimensional one refuses into KHIONE's own steering file."""
    body = _ice(RD_RESULTS_FILE="ice3d.slf",
                SCHEME_FOR_DIFFUSION_OF_FRAZIL_IN_3D=1)
    sheet = fill(T3D, coupling=[body])
    stated = dict(sheet.resolved())
    assert stated["COUPLING WITH"] == "KHIONE"
    assert stated["KHIONE STEERING FILE"] == STEERING_FILENAME
    coupled = sheet.files[STEERING_FILENAME]["slots"]
    assert set(KHIONE.ONLY_3D) & set(coupled) == {
        "RD_RESULTS_FILE", "SCHEME_FOR_DIFFUSION_OF_FRAZIL_IN_3D"}
    with pytest.raises(SlotRefused):
        fill(T2D, coupling=[body])


def test_the_dew_point_rides_the_host_s_own_atmospheric_file():
    sheet = fill(T2D, coupling=[_ice()], atmosphere=Atmosphere(
        times_s=[0.0, 3600.0], air_temp_c=[-5.0, -6.0],
        dew_point_c=[-8.0, -9.0], wind_speed_mps=[2.0, 2.0]))
    header = sheet.files["river_atmosphere.txt"].splitlines()[1].split()
    assert COLUMNS["dew_point_c"][0] in header


@pytest.mark.parametrize("column", ["cloud_octas", "rain_mm"])
def test_a_column_the_two_readers_disagree_on_refuses_by_name(column: str):
    with pytest.raises(TelemacError) as refused:
        fill(T2D, coupling=[_ice()], atmosphere=Atmosphere(
            times_s=[0.0, 3600.0], air_temp_c=[-5.0, -6.0],
            **{column: [1.0, 2.0]}))
    assert COLUMNS[column][0] in str(refused.value)
    assert "khione" in str(refused.value)


def test_every_row_the_table_carries_is_a_mnemonic_the_dictionary_spells():
    from trid3nt_server.workflows.telemac.modules.module import _spelled

    slot = KHIONE.slot(KHIONE.PRINTOUTS)
    assert len(KHIONE.MODULE_OUTPUT) == 24
    table = KHIONE.table(dict(_ice(SALINITY=True, DYNAMIC_ICE_COVER=True,
                                   NUMBER_OF_CLASSES_FOR_SUSPENDED_FRAZIL_ICE=2
                                   )["slots"]))
    assert len(table) == 24 + 8 + 2 + 2 + 2
    assert all(_spelled(token, slot) for token in table)
    assert all(row.name == row.name.strip() and len(row.name) <= 16
               for row in table.values())
