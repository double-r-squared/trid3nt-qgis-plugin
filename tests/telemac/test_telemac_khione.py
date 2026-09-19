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
from trid3nt_server.workflows.telemac.modules import KHIONE, T2D, SlotRefused, fill
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
    # Nothing is appended to the carrier's tracers: the host never counts a row
    # this module writes, because it writes them into a file of its own.
    assert KHIONE.APPENDS is None
    assert KHIONE.APPENDABLE == ()


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
    literal = ",".join(KHIONE.written())
    assert len(literal) > _COLUMNS
    value = fill(KHIONE, **_ice()["slots"]).printouts()[_PRINTOUTS]
    assert len(value) <= _COLUMNS
    tokens = value.split(",")
    choices = dict(KHIONE.slot(KHIONE.PRINTOUTS).choices)
    reached = {mnemonic for mnemonic in choices
               for token in tokens if _matches(token, mnemonic)}
    # The wildcard asks for exactly the rows the table carries and for nothing
    # else the keyword spells.
    assert reached == set(KHIONE.written())


def test_a_row_under_a_keyword_is_written_only_where_the_deck_states_it():
    unstated = fill(KHIONE, **_ice()["slots"]).printouts()[_PRINTOUTS]
    assert "NTOT" not in unstated
    assert "CTOT" not in unstated

    stated = fill(KHIONE, **_ice(HEAT_BUDGET=True)["slots"]).printouts()[_PRINTOUTS]
    choices = dict(KHIONE.slot(KHIONE.PRINTOUTS).choices)
    reached = {mnemonic for mnemonic in choices
               for token in stated.split(",") if _matches(token, mnemonic)}
    assert {"NTOT", "CTOT", "NTOTS", "CTOTS"} <= reached

    refused = fill(KHIONE, **_ice(HEAT_BUDGET=False)["slots"]).printouts()[_PRINTOUTS]
    assert refused == unstated


def test_the_three_dimensional_keywords_are_refused_by_name_under_a_2d_carrier():
    with pytest.raises(SlotRefused) as refused:
        fill(T2D, coupling=[_ice(RD_RESULTS_FILE="ice3d.slf",
                                 SCHEME_FOR_DIFFUSION_OF_FRAZIL_IN_3D=1)])
    assert "3D RESULTS FILE" in str(refused.value)
    assert "SCHEME FOR DIFFUSION OF FRAZIL IN 3D" in str(refused.value)
    assert len(KHIONE.ONLY_3D) == 6


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
    choices = dict(KHIONE.slot(KHIONE.PRINTOUTS).choices)
    assert len(KHIONE.MODULE_OUTPUT) == 24
    assert set(KHIONE.MODULE_OUTPUT) <= set(choices)
    assert all(row.name == row.name.strip() and len(row.name) <= 16
               for row in KHIONE.MODULE_OUTPUT.values())
