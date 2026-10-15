"""The moment a run opens at reaches the deck in each module's own spelling.

Covered: the date and UTC hour triples of the hydrodynamic modules, the wave
module's one real, the meridian the ice module's solar clock reads the hour on
with the minutes the engine drops, the latitude its sun is computed at, the
staged routine that folds the solar hour onto one day, a stated keyword winning
over each, and a run that states no moment writing none of them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trid3nt_server.workflows.telemac.modules import KHIONE, T2D
from trid3nt_server.workflows.telemac.modules.artemis import ART
from trid3nt_server.workflows.telemac.modules.khione import STEERING_FILENAME
from trid3nt_server.workflows.runtime.module import MOMENT
from trid3nt_server.workflows.runtime.sheet import fill, fill_coupled
from trid3nt_server.workflows.telemac.modules.telemac3d import T3D
from trid3nt_server.workflows.telemac.modules.tomawac import WAC

#: The Willamette cold snap: noon UTC is four in the morning on the river.
_AT = datetime(2024, 1, 14, 12, 0, 0, tzinfo=timezone.utc)
_WILLAMETTE = {MOMENT: {"at": _AT, "lon": -122.67, "lat": 45.52}}


class _Iced(T2D):
    coupling = [KHIONE.ice(geometry="domain.slf", boundary="domain.cli")]


def _ice(sheet):
    return sheet.files[STEERING_FILENAME]["slots"]


@pytest.mark.parametrize("body", [T2D, T3D, ART])
def test_a_hydrodynamic_deck_states_the_date_and_the_utc_hour(body):
    sheet = fill(body, produced=_WILLAMETTE)
    assert sheet.filled["ORIGINAL_DATE_OF_TIME"].value == [2024, 1, 14]
    assert sheet.filled["ORIGINAL_HOUR_OF_TIME"].value == [12, 0, 0]
    assert sheet.filled["ORIGINAL_DATE_OF_TIME"].provenance.origin.value == "derived"


def test_the_wave_deck_states_its_one_real_to_the_minute():
    sheet = fill(WAC, produced=_WILLAMETTE)
    assert sheet.filled["DATE_OF_COMPUTATION_BEGINNING"].value == 202401141200.0
    assert "ORIGINAL_DATE_OF_TIME" not in WAC.MODULE_INPUT


def _solar_hour(ice, utc_hour):
    """The engine's own shift, thermal_khione.f: HS = T - ETADIR / 15 * (ALSM - ALLM),
    the two keywords the deck leaves unstated read at the dictionary's values."""
    default = lambda name: KHIONE.MODULE_INPUT[name].engine_default  # noqa: E731
    return utc_hour - ice.get("EAST_OR_WEST_LONGITUDE",
                              default("EAST_OR_WEST_LONGITUDE")) / 15.0 * (
        ice["GLOBAL_LONGITUDE__IN_DEGREES"]
        - default("LOCAL_LONGITUDE__IN__DEGREES"))


def test_the_ice_deck_reads_the_utc_hour_as_the_domains_solar_hour_at_its_latitude():
    sheet = fill(_Iced, produced=_WILLAMETTE)
    ice = _ice(sheet)
    assert ice["GLOBAL_LONGITUDE__IN_DEGREES"] == pytest.approx(75.43 - 122.67)
    assert _solar_hour(ice, 12.0) == pytest.approx(12.0 - 122.67 / 15.0)
    assert sheet.files[STEERING_FILENAME]["derived"] == [
        "GLOBAL_LONGITUDE__IN_DEGREES"]
    assert sheet.filled["LATITUDE_OF_ORIGIN_POINT"].value == 45.52


def test_an_eastern_domain_runs_ahead_of_utc():
    east = {MOMENT: {"at": _AT, "lon": 8.5, "lat": 47.4}}
    assert _solar_hour(_ice(fill(_Iced, produced=east)), 12.0) == pytest.approx(
        12.0 + 8.5 / 15.0)


def test_the_keyword_the_engines_two_readers_disagree_on_is_never_written_and_refuses():
    """DAMOCLES stops on the one-space spelling the launcher writes and the
    launcher stops on the two-space one DAMOCLES reads, so the deck states the
    meridian without it and a caller naming it is told why."""
    from trid3nt_server.workflows.runtime.module import SlotRefused

    unreadable = "LOCAL_LONGITUDE__IN__DEGREES"
    assert unreadable not in _ice(fill(_Iced, produced=_WILLAMETTE))
    assert all(unreadable not in keywords
               for keywords in KHIONE.FILLED_BY.values())
    with pytest.raises(SlotRefused, match="doubled space"):
        fill_coupled(fill(_Iced), {"khione": {unreadable: 122.67}})


def test_a_stated_keyword_wins_over_the_moment():
    sheet = fill(_Iced, produced=_WILLAMETTE, ORIGINAL_HOUR_OF_TIME=[4, 0, 0],
                 LATITUDE_OF_ORIGIN_POINT=44.0)
    assert sheet.filled["ORIGINAL_HOUR_OF_TIME"].value == [4, 0, 0]
    assert sheet.filled["ORIGINAL_HOUR_OF_TIME"].provenance.origin.value == "user"
    assert sheet.filled["LATITUDE_OF_ORIGIN_POINT"].value == 44.0
    assert sheet.filled["ORIGINAL_DATE_OF_TIME"].value == [2024, 1, 14]
    stated = fill_coupled(sheet, {"khione": {"GLOBAL_LONGITUDE__IN_DEGREES": 120.0}})
    assert _ice(stated)["GLOBAL_LONGITUDE__IN_DEGREES"] == 120.0

    class _Dated(T2D):
        ORIGINAL_DATE_OF_TIME = [2020, 6, 1]

    assert fill(_Dated, produced=_WILLAMETTE).filled[
        "ORIGINAL_DATE_OF_TIME"].value == [2020, 6, 1]


def test_a_run_that_states_no_moment_writes_none_of_them():
    written = {name for body in (T2D, T3D, ART, WAC, KHIONE)
               for name in body.FILLED_BY[MOMENT]}
    sheet = fill(_Iced)
    assert not written & set(sheet.filled)
    assert not written & set(_ice(sheet))
    assert not written & set(fill(WAC).filled)


def test_a_date_on_the_deck_arms_no_date_driven_physics():
    """The tide-generating force and the tidal boundary are the two terms of
    these modules that read the date beside the sun; neither is switched on."""
    for body in (T2D, T3D):
        sheet = fill(body, produced=_WILLAMETTE)
        assert "TIDE_GENERATING_FORCE" not in sheet.filled
        assert body.MODULE_INPUT["TIDE_GENERATING_FORCE"].engine_default is False
        assert "OPTION_FOR_TIDAL_BOUNDARY_CONDITIONS" not in sheet.filled


def test_the_minutes_the_engine_drops_ride_on_the_meridian():
    """thermal_khione.f reads MARTIM(1) alone, so 12:30:36 would be read as
    12:00; the half hour and the seconds are fifteen degrees an hour of
    meridian, on the side that moves the solar hour later."""
    late = {MOMENT: {"at": _AT.replace(minute=30, second=36),
                     "lon": -122.67, "lat": 45.52}}
    ice = _ice(fill(_Iced, produced=late))
    assert _solar_hour(ice, 12.0) == pytest.approx(
        12.0 + 30 / 60 + 36 / 3600 - 122.67 / 15.0)


def test_a_stated_sign_without_the_meridian_is_refused_by_name():
    """The derived meridian is placed against the dictionary's sign; a caller
    who takes the placement over states both, and a run with no moment derives
    nothing to protect."""
    from trid3nt_server.workflows.runtime.module import SlotRefused

    sheet = fill(_Iced, produced=_WILLAMETTE)
    with pytest.raises(SlotRefused, match="EAST OR WEST LONGITUDE is stated and "
                                          "GLOBAL LONGITUDE, IN DEGREES is not"):
        fill_coupled(sheet, {"khione": {"EAST_OR_WEST_LONGITUDE": 1.0}})
    both = _ice(fill_coupled(sheet, {"khione": {
        "EAST_OR_WEST_LONGITUDE": 1.0, "GLOBAL_LONGITUDE__IN_DEGREES": 15.0}}))
    assert (both["EAST_OR_WEST_LONGITUDE"],
            both["GLOBAL_LONGITUDE__IN_DEGREES"]) == (1.0, 15.0)
    alone = fill_coupled(fill(_Iced), {"khione": {"EAST_OR_WEST_LONGITUDE": 1.0}})
    assert _ice(alone)["EAST_OR_WEST_LONGITUDE"] == 1.0

    class _Signed(T2D):
        coupling = [KHIONE.ice(geometry="domain.slf", boundary="domain.cli",
                               EAST_OR_WEST_LONGITUDE=1.0)]

    with pytest.raises(SlotRefused, match="EAST OR WEST LONGITUDE is stated"):
        fill(_Signed, produced=_WILLAMETTE)


#: sha256 of opentelemac v9.0.0 sources/khione/thermal_khione.f, as the solver image holds it.
_PRISTINE = "71be1f4bf6cd1e8f1d253db3f166f92c0fd664036f076e06257d727410fd75f7"
_FOLDED = ("      HS1 = MODULO(T1 - ETADIR / 15.D0 * (ALSM - ALLM) + ET, 24.D0)\n",
           "      HS2 = HS1 + (T2 - T1)\n")
_ENGINE = ("      HS1 = T1 - ETADIR / 15.D0 * (ALSM - ALLM) + ET  ! HOURS\n",
           "      HS2 = T2 - ETADIR / 15.D0 * (ALSM - ALLM) + ET  ! HOURS\n")


def test_the_staged_routine_is_the_engines_own_but_for_the_fold():
    import hashlib

    from trid3nt_server.workflows.telemac.authoring.solar import solar_routine

    staged = solar_routine()
    for ours, theirs in zip(_FOLDED, _ENGINE):
        assert staged.count(ours) == 1 and len(ours) <= 73
        staged = staged.replace(ours, theirs)
    assert hashlib.sha256(staged.encode()).hexdigest() == _PRISTINE


def _sunlit(solar_hour, sunrise=4.3, sunset=19.7):
    """The engine's two early returns, thermal_khione.f: no sun outside the day."""
    return sunrise <= solar_hour <= sunset


@pytest.mark.parametrize(("utc_start", "lon"), [(20, -122.67), (18, 10.0)])
def test_the_fold_keeps_the_daylight_a_utc_clock_would_lose(utc_start, lon):
    """A June day read hour by hour from a UTC start: unfolded, every solar
    hour past 24 or below 0 is night; folded, the day has all its daylight."""
    hours = [utc_start + step / 4 + lon / 15.0 for step in range(96)]
    lost = sum(_sunlit(hour) for hour in hours)
    kept = sum(_sunlit(hour % 24.0) for hour in hours)
    # One sample either way: the day is cut at a different quarter hour.
    assert abs(kept - sum(_sunlit(step / 4) for step in range(96))) <= 1
    assert lost < 0.6 * kept


def test_a_dated_ice_run_compiles_the_folded_routine_into_its_host():
    from trid3nt_server.workflows.telemac.modules.module import USER_FORTRAN_DIR

    sheet = fill(_Iced, produced=_WILLAMETTE)
    assert sheet.filled["FORTRAN_FILE"].value == USER_FORTRAN_DIR
    assert _FOLDED[0] in sheet.files[f"{USER_FORTRAN_DIR}/thermal_khione.f"]
    assert sheet.user_code() == (USER_FORTRAN_DIR,)
    bare = fill(_Iced)
    assert "FORTRAN_FILE" not in bare.filled
    assert not [name for name in bare.files if name.startswith(USER_FORTRAN_DIR)]
    assert not [name for name in fill(T2D, produced=_WILLAMETTE).files
                if name.startswith(USER_FORTRAN_DIR)]
