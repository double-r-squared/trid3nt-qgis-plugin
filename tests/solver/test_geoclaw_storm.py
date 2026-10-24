"""The Storm composite: one real storm's track as the surge storm file Clawpack's own writer makes.

Offline. The fixes are rows of the IBTrACS v04r01 North Atlantic table for Hurricane Ike (2008) as fetch_storm_tracks
returns them. Pinned: the file reads back through Clawpack's own ``Storm.read`` equal to the fixes in SI units; the run's
seconds count from the stated event_time, else from the last landfall fix; a fix lacking the radius of maximum wind or
the outer radius, two storms in one record, a track that begins after the run opens or ends before it closes, and a
record with no landfall and no stated event_time each refuse by name; FixedGrids frames start at CLAW_T0.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.geoclaw.authoring.storm import landfall_instant, storm_track, write_stormfiles
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO

#: IBTrACS v04r01 NA rows of IKE 2008: time, lat, lon, record, USA_WIND kt, USA_PRES mb, USA_RMW nmi, USA_ROCI nmi.
_ROWS = [
    ("2008-09-13 03:00:00", 28.6, -94.3, None, 95, 952, 35, 313),
    ("2008-09-13 06:00:00", 29.1, -94.6, None, 95, 951, 30, 325),
    ("2008-09-13 07:00:00", 29.3, -94.7, "L", 95, 950, 30, 325),
    ("2008-09-13 09:00:00", 29.7, -94.9, None, 91, 954, 30, 325),
    ("2008-09-13 12:00:00", 30.3, -95.2, None, 85, 959, 30, 325),
]
_LANDFALL = "2008-09-13T07:00:00+00:00"


def _doc(rows=_ROWS, sid="2008245N17323", name="IKE", **gaps) -> dict:
    features = []
    for time, lat, lon, record, wind, pres, rmw, roci in rows:
        props = {"sid": sid, "name": name, "iso_time": time, "record": record, "wind_kt": wind, "pres_mb": pres,
                 "rmw_nmi": rmw, "roci_nmi": roci, **gaps.get(time, {})}
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props})
    return {"type": "FeatureCollection", "features": features}


def _refused(code: str, *args, **kwargs) -> str:
    with pytest.raises(GeoclawError) as caught:
        storm_track(*args, **kwargs)
    assert caught.value.error_code == f"GEOCLAW_{code}"
    return str(caught.value)


def test_the_file_reads_back_through_clawpacks_own_reader_equal_to_the_fixes_in_si(tmp_path):
    import clawpack.geoclaw.units as units
    from clawpack.geoclaw.surge.storm import Storm

    track = storm_track(_doc(), offset=_LANDFALL, t0=-3600.0, tfinal=7200.0)
    write_stormfiles(type("S", (), {"files": {"ike.storm": {"storm": track}}})(), tmp_path)
    read = Storm(str(tmp_path / "ike.storm"), file_format="geoclaw")

    kept = _ROWS[1:4]
    assert list(read.t) == [-3600.0, 0.0, 7200.0] and read.time_offset == 0.0
    assert np.allclose(read.eye_location, [[lon, lat] for _, lat, lon, *_ in kept])
    assert np.allclose(read.max_wind_speed, [units.convert(float(r[4]), "knots", "m/s") for r in kept])
    assert np.allclose(read.max_wind_radius, [r[6] * 1852.0 for r in kept])
    assert np.allclose(read.central_pressure, [r[5] * 100.0 for r in kept])
    assert np.allclose(read.storm_radius, [r[7] * 1852.0 for r in kept])
    assert read.max_wind_speed[0] == pytest.approx(95 * 1852.0 / 3600.0, rel=1e-6)


def test_the_seconds_count_from_the_stated_instant_else_from_the_last_landfall_fix():
    stated = storm_track(_doc(), offset="2008-09-13T06:00:00Z", t0=0.0, tfinal=10800.0)
    landed = storm_track(_doc(), offset=None, t0=-3600.0, tfinal=7200.0)

    assert [fix["t"] for fix in stated["fixes"]] == [0.0, 3600.0, 10800.0]
    assert stated["time_offset"] == "2008-09-13T06:00:00"
    assert [fix["t"] for fix in landed["fixes"]] == [-3600.0, 0.0, 7200.0]
    assert landed["time_offset"] == "2008-09-13T07:00:00"
    assert landfall_instant(_doc()) == _LANDFALL


def test_the_window_keeps_the_fixes_bracketing_the_run_and_nothing_beyond():
    track = storm_track(_doc(), offset=_LANDFALL, t0=-1800.0, tfinal=1800.0)

    assert [fix["t"] for fix in track["fixes"]] == [-3600.0, 0.0, 7200.0]


@pytest.mark.parametrize("field, name", [("rmw_nmi", "USA_RMW"), ("roci_nmi", "USA_ROCI")])
def test_a_fix_in_the_window_without_a_radius_refuses_naming_the_fix_and_the_field(field, name):
    message = _refused("STORM_FIX_INCOMPLETE", _doc(**{"2008-09-13 09:00:00": {field: None}}),
                       offset=_LANDFALL, t0=-3600.0, tfinal=7200.0)

    assert "2008-09-13 09:00:00" in message and name in message


def test_a_fix_outside_the_window_without_a_radius_is_not_read():
    track = storm_track(_doc(**{"2008-09-13 12:00:00": {"rmw_nmi": None}}), offset=_LANDFALL, t0=-3600.0, tfinal=7200.0)

    assert len(track["fixes"]) == 3


def test_two_storms_in_one_record_refuse_naming_both():
    other = _doc(sid="2008999N10300", name="OTHER")["features"]
    both = {"type": "FeatureCollection", "features": _doc()["features"] + other}

    message = _refused("STORM_SEVERAL", both, offset=_LANDFALL, t0=0.0, tfinal=3600.0)

    assert "IKE 2008245N17323" in message and "OTHER 2008999N10300" in message


def test_a_track_that_begins_after_the_run_or_ends_before_it_refuses_with_the_times():
    assert "2008-09-13 03:00:00" in _refused("STORM_STARTS_LATE", _doc(), offset=_LANDFALL, t0=-86400.0, tfinal=0.0)
    assert "2008-09-13 12:00:00" in _refused("STORM_ENDS_EARLY", _doc(), offset=_LANDFALL, t0=0.0, tfinal=86400.0)


def test_a_fix_without_wind_or_pressure_is_the_track_between_reports_and_is_dropped():
    track = storm_track(_doc(**{"2008-09-13 09:00:00": {"wind_kt": None}}), offset=_LANDFALL, t0=-3600.0, tfinal=18000.0)

    assert [fix["t"] for fix in track["fixes"]] == [-3600.0, 0.0, 18000.0]


def test_a_record_with_no_landfall_and_no_stated_instant_refuses_and_a_stated_one_runs():
    rows = [row[:3] + (None,) + row[4:] for row in _ROWS]

    assert "event_time" in _refused("STORM_NO_LANDFALL", _doc(rows), offset=None, t0=0.0, tfinal=3600.0)
    assert storm_track(_doc(rows), offset=_LANDFALL, t0=0.0, tfinal=3600.0)["fixes"]


def test_two_fixes_at_one_instant_refuse():
    rows = [*_ROWS, _ROWS[-1]]

    assert "2008-09-13 12:00:00" in _refused("STORM_FIX_DUPLICATE", _doc(rows), offset=_LANDFALL, t0=0.0, tfinal=3600.0)


def _stated(**over) -> dict:
    return {"storm_tracks": _doc(), "storm_name": "IKE", "season": 2008, **over}


_RUN = {"CLAW_T0": -3600.0, "CLAW_TFINAL": 7200.0, "event_time": _LANDFALL}


def test_the_composite_states_the_storm_file_and_names_its_track():
    expanded, files = GEO.COMPOSITES["Storm"].apply(_stated(), _RUN)

    assert expanded == {"SURGE_STORM_FILE": "storm_01.storm"}
    assert files["storm_01.storm"]["storm"]["time_offset"] == "2008-09-13T07:00:00"
    assert len(files["storm_01.storm"]["storm"]["fixes"]) == 3


def test_the_composite_reads_the_run_values_it_names():
    composite = GEO.COMPOSITES["Storm"]
    expanded, files = composite.apply({"storm_tracks": "track", "storm_name": "name", "season": "year"},
                                      {**_RUN, "track": _doc(), "name": "IKE", "year": 2008})

    assert files["storm_01.storm"]["storm"]["fixes"][1]["t"] == 0.0


@pytest.mark.parametrize("run, over, code", [
    ({**_RUN, "SURGE_STORM_FILE": "mine.storm"}, {}, "STORM_FILE_STATED"),
    ({"event_time": _LANDFALL}, {}, "STORM_WINDOW_UNSTATED"),
    (_RUN, {"storm_tracks": None}, "STORM_UNSTATED"),
    (_RUN, {"model": "holland80"}, "STORM_UNSTATED"),
])
def test_the_composite_refuses_what_it_cannot_write(run, over, code):
    with pytest.raises(GeoclawError) as caught:
        GEO.COMPOSITES["Storm"].apply(_stated(**over), run)

    assert caught.value.error_code == f"GEOCLAW_{code}"


def test_fixed_grids_frames_start_at_the_runs_first_instant():
    grids = {"extent": (-95.0, 29.0, -94.5, 29.5), "interval": 10800.0, "tstart_max": -259200.0, "dt_check": 10.0,
             "arrival_tol": 0.2, "interp_method": 0}
    mesh = type("A", (), {"probes": {"claw_grid": {"lower": [-99.0, 8.0], "finest_cell_deg": [0.01, 0.01], "levels": 3}}})()
    run = {"mesh": {"artifact": mesh}, "CLAW_T0": -259200.0, "CLAW_TFINAL": 86400.0}

    expanded, _ = GEO.COMPOSITES["FixedGrids"].apply(grids, run)
    fgout = expanded["FGOUT_FGOUT_GRIDS"][0]

    assert fgout[2:5] == [33, -259200.0, 86400.0]
