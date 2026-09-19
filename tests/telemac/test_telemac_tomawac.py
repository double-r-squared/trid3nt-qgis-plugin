"""TOMAWAC: the module's own table, the deck a host couples, and the standalone
base a template stands on.

Offline: nothing here starts a solve. What is proved is the deck the product
would hand the engine - the variables keyword it generates in the engine's own
grammar, the spectra it keeps beside the wave field, the host switch a coupling
arms, and the two roles one wrapper serves."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.modules import (
    T2D,
    T3D,
    WAC,
    SlotRefused,
    fill,
)
from trid3nt_server.workflows.telemac.modules.module import identify_on
from trid3nt_server.workflows.telemac.modules.sheet import _kept, _user_code
from trid3nt_server.workflows.telemac.modules.tomawac import (
    RESULT_FILENAME,
    STEERING_FILENAME,
)
from trid3nt_server.workflows.telemac.workflow import run_bodies

#: The column a DAMOCLES line is read to, which is where the engine truncates
#: this keyword's own value.
_COLUMNS = 72
_PRINTOUTS = "VARIABLES FOR 2D GRAPHIC PRINTOUTS"
_WAVE_CURRENTS = "WAVE DRIVEN CURRENTS"


def _wave(**keywords):
    return WAC.wave(geometry="geo_littoral.slf", boundary="geo_tom_littoral.cli",
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


def _reached(value: str) -> set:
    choices = dict(WAC.slot(WAC.PRINTOUTS).choices)
    return {mnemonic for mnemonic in choices
            for token in value.split(",") if _matches(token, mnemonic)}


def test_the_wrapper_writes_its_own_wave_field_beside_the_host_s():
    body = _wave()
    assert body["module"] == "tomawac"
    assert body["steering"] == STEERING_FILENAME
    assert body["slots"]["ED_RESULTS_FILE"] == RESULT_FILENAME
    assert WAC.RESULT_FILE == RESULT_FILENAME


def test_the_table_is_the_engine_s_forty_less_the_slot_it_never_writes():
    """``nomvar_tomawac.f`` indexes forty variables and the distributed code
    writes thirty-nine of them: PRIVATE 1 is the user-Fortran table nothing
    fills, so the wildcard may reach it and the table never rows it."""
    choices = dict(WAC.slot(WAC.PRINTOUTS).choices)
    assert len(choices) == 40
    assert set(WAC.MODULE_OUTPUT) | set(WAC.UNWRITTEN) == set(choices)
    assert WAC.UNWRITTEN == frozenset({"PRI"})
    assert all(row.style for row in WAC.MODULE_OUTPUT.values())
    assert WAC.MODULE_OUTPUT["TMOY"].name == "MEAN PERIOD TMOY"
    assert WAC.MODULE_OUTPUT["TMOY"].unit == "s"


def test_the_table_is_written_in_the_engine_s_own_wildcard_and_fits_its_line():
    literal = ",".join(WAC.written())
    assert len(literal) > _COLUMNS
    value = fill(WAC, **_wave()["slots"]).printouts()[_PRINTOUTS]
    assert len(value) <= _COLUMNS
    # The wildcard asks for the rows the table carries and, beyond them, only
    # for the slot the engine never writes.
    assert _reached(value) == set(WAC.written()) | set(WAC.UNWRITTEN)


def test_a_deep_deck_does_not_ask_for_the_bottom_velocity_the_engine_leaves():
    """LECDON clears every other bottom row over infinite depth and DUMP2D
    computes no orbital velocity there, but that one row it does not clear: a
    deep deck asking for it would publish an untouched work array."""
    deep = fill(WAC, **_wave(INFINITE_DEPTH=True)["slots"])
    assert "UWB" not in WAC.table(deep.stated())
    assert "UWB" not in _reached(deep.printouts()[_PRINTOUTS])
    assert "UWB" in _reached(fill(WAC, **_wave()["slots"]).printouts()[_PRINTOUTS])


def test_the_module_appends_no_tracer_and_arms_the_switch_it_is_felt_through():
    """The wave forces reach the host as a momentum source PROSOU adds into FU
    and FV, and the host's result carries no wave row at all, so there is
    nothing to append; without WAVE DRIVEN CURRENTS that addition never runs."""
    assert WAC.APPENDS is None
    assert WAC.APPENDABLE == ()
    sheet = fill(T2D, coupling=[_wave()])
    assert dict(sheet.resolved())[_WAVE_CURRENTS] is True
    assert str(sheet.filled["WAVE_DRIVEN_CURRENTS"].provenance) \
        == "producer: coupling with tomawac"
    assert not [row.name for row in sheet.tracers]


def test_a_deck_that_states_the_switch_off_keeps_it_off():
    """A run may want the wave field beside a current nobody drove with it."""
    sheet = fill(T2D, coupling=[_wave()], WAVE_DRIVEN_CURRENTS=False)
    assert dict(sheet.resolved())[_WAVE_CURRENTS] is False


@pytest.mark.parametrize("host", [T2D, T3D])
def test_both_hosts_couple_the_waves_through_the_seam_they_share(host):
    """One shared coupling composite, registered once on each carrier: TOMAWAC
    has no keyword a two-dimensional host could not build, so neither refuses
    it."""
    sheet = fill(host, coupling=[_wave(MINIMAL_FREQUENCY=0.05)],
                 COUPLING_PERIOD_FOR_TOMAWAC=1)
    stated = dict(sheet.resolved())
    assert stated["COUPLING WITH"] == "TOMAWAC"
    assert stated["TOMAWAC STEERING FILE"] == STEERING_FILENAME
    assert stated["COUPLING PERIOD FOR TOMAWAC"] == 1
    assert stated[_WAVE_CURRENTS] is True
    coupled = sheet.files[STEERING_FILENAME]
    assert coupled["module"] == "tomawac"
    assert coupled["slots"]["MINIMAL_FREQUENCY"] == 0.05
    assert WAC.ONLY_3D == frozenset()


def test_the_qualified_floor_reaches_the_coupled_body():
    class Littoral(T2D):
        coupling = [_wave()]

    body, identifier = identify_on(run_bodies(Littoral), "tomawac: MINIMAL FREQUENCY")
    assert body is WAC
    assert identifier == "MINIMAL_FREQUENCY"


def test_a_keyword_both_bodies_spell_names_the_body_it_belongs_to():
    class Littoral(T2D):
        coupling = [_wave()]

    with pytest.raises(SlotRefused) as refused:
        identify_on(run_bodies(Littoral), "TIME STEP")
    assert "telemac2d: TIME STEP" in str(refused.value)
    assert "tomawac: TIME STEP" in str(refused.value)


def test_a_standalone_deck_stands_on_the_wrapper_and_names_its_own_files():
    """The other role: a template's STEERING derives from the wrapper the way
    one derives from TELEMAC-2D, and the wave engine reads its mesh and its
    boundary walk off its own deck rather than off a host's."""
    class Steering(WAC):
        GEOMETRY_FILE = "geometry.slf"
        BOUNDARY_CONDITIONS_FILE = "boundary.cli"
        ED_RESULTS_FILE = RESULT_FILENAME
        PERIOD_FOR_GRAPHIC_PRINTOUTS = 20
        NUMBER_OF_DIRECTIONS = 72
        NUMBER_OF_FREQUENCIES = 25

    sheet = fill(Steering)
    stated = dict(sheet.resolved())
    assert stated["2D RESULTS FILE"] == WAC.RESULT_FILE
    assert stated["GEOMETRY FILE"] == "geometry.slf"
    assert _PRINTOUTS in sheet.printouts()
    assert not sheet.required()
    # A keyword the module does not have refuses at IMPORT, by name.
    with pytest.raises(SlotRefused):
        type("Bad", (WAC,), {"WAVE_DRIVEN_CURRENTS": True})


def test_the_spectra_are_kept_as_result_files_and_never_rowed_as_a_mesh():
    """A spectrum is written over the polar frequency-direction grid, not over
    the domain, so no primitive of the geographic mesh reads one; the run still
    declares and keeps every spectral file its deck names."""
    assert WAC.RESULT_FILES == ("PUNCTUAL_RESULTS_FILE", "ZD_SPECTRA_RESULTS_FILE")
    assert not {"PUNCTUAL_RESULTS_FILE", "ZD_SPECTRA_RESULTS_FILE"} & set(
        WAC.MODULE_OUTPUT)
    bare = fill(WAC, **_wave()["slots"])
    assert _kept(bare) == []
    named = fill(bare, PUNCTUAL_RESULTS_FILE="resWac.spe",
                 ZD_SPECTRA_RESULTS_FILE="resWac.1d")
    assert _kept(named) == ["resWac.spe", "resWac.1d"]
    # A coupled deck's own spectra are the run's too.
    assert _kept(fill(T2D, coupling=[_wave(PUNCTUAL_RESULTS_FILE="resWac.spe")])) \
        == ["resWac.spe"]


def test_the_user_fortran_of_every_deck_of_the_run_is_staged():
    """The engine compiles the directory EACH deck names, so a coupled module's
    own patch is staged off its own deck rather than off the host's."""
    host = fill(T2D, coupling=[_wave(FORTRAN_FILE="Tom_user_fortran")],
                FORTRAN_FILE="T2D_user_fortran")
    assert _user_code(host) == ["T2D_user_fortran", "Tom_user_fortran"]
    assert _user_code(fill(T2D, coupling=[_wave()])) == []
