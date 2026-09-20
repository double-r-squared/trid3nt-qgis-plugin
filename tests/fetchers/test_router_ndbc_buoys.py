"""``fetch_ndbc_buoys``: one buoy's sea state, out of the file the window names.

The listing is what says which stations measure waves at all and where they
float, so the slot's ask reaches the nearest one by NDBC's own id; the realtime
table and the yearly archive are one parse over recorded bytes; a window between
the two refuses, and a window whose wave columns are NDBC's missing value is
empty rather than a fabricated sea state.
"""

from __future__ import annotations

import datetime as dt
import gzip

import pytest

from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError, RouterInputError)
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.ocean.fetch_ndbc_buoys import hooks as nb

#: Recorded from ``GET https://www.ndbc.noaa.gov/activestations.xml`` (five of
#: its 1353 stations: three wave buoys, a fixed CO-OPS platform carrying the
#: same met payload, and a moored buoy reporting no met record).
_LISTING_BODY = b"""<?xml version="1.0" encoding="utf-8"?><stations created="2026-09-20T12:55:03UTC" count="5">
  <station id="46001" lat="56.296" lon="-148.027" elev="0" name="WESTERN GULF OF ALASKA  - 175NM SE of Kodiak, AK" owner="NDBC" pgm="NDBC Meteorological/Ocean" type="buoy" met="y" currents="n" waterquality="n" dart="n"/>
  <station id="44085" lat="41.387" lon="-71.032" elev="0" name="Buzzards Bay, MA (260)" owner="Woods Hole Group/NERACOOS" pgm="IOOS Partners" type="buoy" met="y" currents="n" waterquality="n" dart="n"/>
  <station id="44097" lat="40.966" lon="-71.123" elev="0" name="Block Island, RI  (154)" owner="SCRIPPS" pgm="IOOS Partners" type="buoy" met="y" currents="n" waterquality="n" dart="n"/>
  <station id="nwpr1" lat="41.504" lon="-71.326" elev="2.2" name="8452660 - Newport, RI" owner="NOS" pgm="NOS/CO-OPS" type="fixed" met="y" currents="n" waterquality="n" dart="n"/>
  <station id="13001" lat="12" lon="-23" elev="0" name="NE Extension" owner="Prediction and Research Moored Array in the Atlantic" pgm="International Partners" type="buoy" met="n" currents="n" waterquality="n" dart="n"/>
</stations>
"""

#: Recorded from ``GET /data/realtime2/44097.txt`` (its two header lines and the
#: six newest rows of 2026-09-20, newest first, the wind and pressure columns
#: reporting NDBC's "MM").
_REALTIME_BODY = b"""#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS PTDY  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC  nmi  hPa    ft
2026 09 20 12 30  MM   MM   MM   0.8     4   3.4 108     MM  17.9  21.3    MM   MM   MM    MM
2026 09 20 12 00  MM   MM   MM   0.8     4   3.3 114     MM  17.7  21.3    MM   MM   MM    MM
2026 09 20 11 30  MM   MM   MM   0.7     7   3.3 128     MM  17.3  21.2    MM   MM   MM    MM
2026 09 20 11 00  MM   MM   MM   0.7     7   3.2 122     MM  17.1  21.2    MM   MM   MM    MM
2026 09 20 10 30  MM   MM   MM   0.7     7   3.4 122     MM  16.9  21.0    MM   MM   MM    MM
2026 09 20 10 00  MM   MM   MM   0.6     8   3.5 131     MM  17.0  20.9    MM   MM   MM    MM
"""

#: Recorded from ``GET /data/historical/stdmet/44097h2024.txt.gz`` (its headers
#: and four rows of the 2024-01-14 swell; the wind, pressure and visibility
#: columns carry the archive's 999 / 99.0 / 9999.0 missing values).
_ARCHIVE_BODY = b"""#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS  TIDE
#yr  mo dy hr mn degT m/s  m/s     m   sec   sec degT   hPa  degC  degC  degC   mi    ft
2024 01 14 10 26 999 99.0 99.0  3.78 10.53  7.22 190 9999.0 999.0   8.4 999.0 99.0 99.00
2024 01 14 10 56 999 99.0 99.0  3.55  9.88  7.18 190 9999.0 999.0   8.4 999.0 99.0 99.00
2024 01 14 11 26 999 99.0 99.0  3.34 10.53  6.87 184 9999.0 999.0   8.4 999.0 99.0 99.00
2024 01 15 11 56 999 99.0 99.0  3.73  9.09  7.19 210 9999.0 999.0   8.5 999.0 99.0 99.00
"""

#: Recorded from ``GET /data/historical/stdmet/46001h1976.txt.gz``: the network's
#: oldest table shape - one header line, a two-digit year, three-hourly rows and
#: no minute column at all.
_OLD_ARCHIVE_BODY = b"""YY MM DD hh WD   WSPD GST  WVHT  DPD   APD  MWD  BAR    ATMP  WTMP  DEWP  VIS
76 01 01 00 999 06.2 99.0 04.10 99.00 09.00 999 1021.8  01.1 999.0 999.0 99.0
76 01 01 03 999 06.7 99.0 03.80 99.00 08.00 999 1022.3  00.9 999.0 999.0 99.0
76 01 01 06 999 06.7 99.0 03.10 99.00 08.00 999 1022.0  00.9 999.0 999.0 99.0
"""

#: Recorded from ``GET /data/historical/stdmet/46001h1974.txt.gz``: the last of
#: the 99.00 rows the network's first two and a half years of files are written
#: in, and the first rows whose WVHT is a reading rather than that missing value.
_FIRST_WAVE_BODY = b"""YY MM DD hh WD   WSPD GST  WVHT  DPD   APD  MWD  BAR    ATMP  WTMP  DEWP  VIS
74 06 27 09 034 03.6 99.0 99.00 99.00 99.00 999 1009.0  08.1 999.0 999.0 99.0
74 06 27 12 083 03.5 99.0 99.00 99.00 99.00 999 9999.0  08.2 999.0 999.0 99.0
74 06 27 15 053 03.1 99.0 99.00 99.00 99.00 999 1009.0  08.7 999.0 999.0 99.0
74 12 03 05 999 05.1 99.0 01.50 99.00 99.00 999 1000.1  02.8 999.0 999.0 99.0
74 12 03 06 999 05.7 99.0 01.50 99.00 99.00 999 1000.2  02.6 999.0 999.0 99.0
74 12 03 07 999 07.7 99.0 01.50 99.00 99.00 999 1000.2  02.4 999.0 999.0 99.0
"""

#: The instant the archive's first MEASURED wave was read at, which is the floor
#: the row states: the files before it are published and measure no sea state.
_FIRST_WAVE = "1974-12-03T05:00Z"

#: Point Judith, RI - the harbour of refuge the agitation deck is asked about.
_POINT_JUDITH = (-71.4814, 41.3611)

_AT_44097 = {"_station_id": "44097", "_station_name": "Block Island, RI  (154)",
             "_owner": "SCRIPPS", "_lon": -71.123, "_lat": 40.966}

_AT_46001 = {"_station_id": "46001", "_station_name": "WESTERN GULF OF ALASKA",
             "_owner": "NDBC", "_lon": -148.027, "_lat": 56.296}


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_ndbc_buoys"]


@pytest.fixture(scope="module")
def row(spec):
    return spec.coverage[0]


def test_the_row_states_a_measured_sea_state_over_a_station_network(row):
    assert (row.data_class, row.kind) == ("wave series", "measured")
    assert row.extent.kind == "stations"
    assert row.extent.read_from == "ndbc_buoys.stations"
    assert row.reach_km == 60.0
    assert row.window.series and row.window.earliest == _FIRST_WAVE[:10]
    # A wave height is a height OF the water and is counted from no zero.
    assert row.datum is None


def test_every_column_the_row_names_is_published_in_a_stated_unit(row):
    assert row.value_column == "wave_height_m"
    assert row.series_column == "wave_height_series_csv"
    assert row.units["wave_height_series_csv"] == "m"
    assert row.units["peak_period_series_csv"] == "s"
    # degT is the compass bearing the waves come FROM, not a heading of travel.
    assert row.units["wave_direction_deg_true"] == "degT"


def test_the_buoy_answers_for_the_peak_direction_it_measures_and_no_mean_one(row):
    assert row.word_for("WAVE HEIGHT HM0") == "WVHT"
    assert row.word_for("PEAK DIRECTION") == "MWD"
    assert row.word_for("MEAN DIRECTION") == ""


def test_the_listing_names_the_wave_buoys_and_passes_over_every_other_station(
        monkeypatch):
    """A fixed platform states the same met payload and measures no waves; a
    moored buoy stating none reports no stdmet at all."""
    monkeypatch.setattr(nb, "get_client", lambda: object())
    monkeypatch.setattr(nb, "get_bytes",
                        lambda client, url, headers=None: (_LISTING_BODY, "", url))
    assert [(p.id, p.datum) for p in nb.stations()] == [("44085", None),
                                                        ("44097", None),
                                                        ("46001", None)]


def test_point_judith_reaches_the_nearest_buoy_within_the_rows_reach(
        row, monkeypatch):
    """The ask is made by the buoy's own id: the row's listing is read in, the
    nearest station is the one passed, and the reach is what puts it on the
    list at all."""
    monkeypatch.setattr(nb, "get_client", lambda: object())
    monkeypatch.setattr(nb, "get_bytes",
                        lambda client, url, headers=None: (_LISTING_BODY, "", url))
    listed = row.extent.model_copy(update={"points": nb.stations()})
    assert listed.nearest(*_POINT_JUDITH).id == "44085"
    assert listed.distance_km(*_POINT_JUDITH) == pytest.approx(37.7, abs=0.5)
    assert listed.distance_km(*_POINT_JUDITH) < row.reach_km


def test_resolve_parse_reads_the_buoys_own_place_and_name_off_the_listing(spec):
    merged = nb.resolve_parse(spec, {"station": "44097"}, [_LISTING_BODY])
    assert merged["_station_id"] == "44097"
    assert merged["_station_name"] == "Block Island, RI  (154)"
    assert (merged["_lon"], merged["_lat"]) == (-71.123, 40.966)


def test_a_station_that_measures_no_waves_refuses_by_name(spec):
    with pytest.raises(RouterInputError) as excinfo:
        nb.resolve_parse(spec, {"station": "nwpr1"}, [_LISTING_BODY])
    assert excinfo.value.error_code == "NDBC_BUOYS_INPUT_INVALID"
    assert "nwpr1" in str(excinfo.value)


def test_a_window_inside_the_last_45_days_is_the_realtime_file(spec):
    today = dt.date.today()
    plans = nb.build_request(spec, {**_AT_44097,
                                    "start_date": (today - dt.timedelta(days=3)).isoformat(),
                                    "end_date": today.isoformat()})
    assert [p.url for p in plans] == [
        "https://www.ndbc.noaa.gov/data/realtime2/44097.txt"]


def test_a_dated_window_is_one_yearly_archive_per_year_it_spans(spec):
    plans = nb.build_request(spec, {**_AT_44097, "start_date": "2023-12-30",
                                    "end_date": "2024-01-20"})
    assert [p.url for p in plans] == [
        "https://www.ndbc.noaa.gov/data/historical/stdmet/44097h2023.txt.gz",
        "https://www.ndbc.noaa.gov/data/historical/stdmet/44097h2024.txt.gz"]


def test_a_window_between_the_realtime_file_and_the_archive_refuses(spec):
    """The months of the current year the realtime file no longer reaches are
    published in neither file, and answering from another moment is not an
    answer to this one."""
    today = dt.date.today()
    with pytest.raises(RouterInputError) as excinfo:
        nb.build_request(spec, {**_AT_44097,
                                "start_date": (today - dt.timedelta(days=120)).isoformat(),
                                "end_date": (today - dt.timedelta(days=100)).isoformat()})
    assert excinfo.value.error_code == "NDBC_BUOYS_INPUT_INVALID"


def test_the_realtime_table_becomes_one_point_with_its_window_inline(spec):
    features = nb.parse_response(
        spec, {**_AT_44097, "start_date": "2026-09-20", "end_date": "2026-09-20"},
        [_REALTIME_BODY])
    assert len(features) == 1
    props = features[0]["properties"]
    assert features[0]["geometry"]["coordinates"] == [-71.123, 40.966]
    assert props["wave_height_m"] == 0.8
    assert props["peak_period_s"] == 4.0
    assert props["wave_direction_deg_true"] == 108.0
    assert props["n_timesteps"] == 6
    assert props["time_start"] == "2026-09-20T10:00Z"
    assert props["time_end"] == "2026-09-20T12:30Z"
    assert props["wave_height_series_csv"].splitlines()[:2] == [
        "iso,wave_height_m", "2026-09-20T10:00Z,0.6"]


def test_the_window_is_read_off_the_rows_own_stamps(spec):
    """The file carries whatever NDBC published; what the feature carries is the
    window that was asked for."""
    features = nb.parse_response(
        spec, {**_AT_44097, "start_date": "2024-01-14", "end_date": "2024-01-14"},
        [gzip.compress(_ARCHIVE_BODY)])
    props = features[0]["properties"]
    assert props["n_timesteps"] == 3
    assert props["wave_height_m"] == 3.34
    assert props["time_end"] == "2024-01-14T11:26Z"


def test_the_archives_own_missing_values_are_gaps_and_never_readings(spec):
    """999, 99.0 and 9999.0 are what the archive writes where the instrument
    reported nothing; a run given one as a number is given a fabricated sea."""
    features = nb.parse_response(
        spec, {**_AT_44097, "start_date": "1976-01-01", "end_date": "1976-01-01"},
        [_OLD_ARCHIVE_BODY])
    props = features[0]["properties"]
    assert props["wave_height_m"] == 3.1
    assert props["peak_period_s"] is None
    assert props["peak_period_series_csv"] == "iso,peak_period_s"
    # The oldest tables state a two-digit year, an hour and no minute.
    assert props["time_start"] == "1976-01-01T00:00Z"


def test_a_window_with_no_measured_wave_height_is_empty_not_a_sea_state(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        nb.parse_response(
            spec, {**_AT_44097, "start_date": "2026-09-19", "end_date": "2026-09-19"},
            [_REALTIME_BODY])
    assert excinfo.value.error_code == "NDBC_BUOYS_NO_RECORD"


def test_a_missing_file_is_an_empty_record_and_not_an_upstream_failure(spec):
    typed = nb.classify_status(spec, 404, "")
    assert typed is not None and typed.error_code == "NDBC_BUOYS_NO_RECORD"
    assert nb.classify_status(spec, 503, "") is None


def test_the_source_is_found_through_its_class(class_routes_to_the_match):
    """A covered fetcher carries no corpus: its class is the door."""
    class_routes_to_the_match("wave series", "fetch_ndbc_buoys")


def test_the_rows_floor_is_the_first_measured_wave_and_not_a_files_existence(
        row, spec):
    """46001's 1972 and 1973 files are published and every wave column in them
    is NDBC's missing value; the floor is the instant a wave was first read."""
    with pytest.raises(RouterEmptyError) as excinfo:
        nb.parse_response(spec, {**_AT_46001, "start_date": "1974-06-27",
                                 "end_date": "1974-06-27"}, [_FIRST_WAVE_BODY])
    assert excinfo.value.error_code == "NDBC_BUOYS_NO_RECORD"
    props = nb.parse_response(
        spec, {**_AT_46001, "start_date": "1974-12-03", "end_date": "1974-12-03"},
        [_FIRST_WAVE_BODY])[0]["properties"]
    assert props["time_start"] == _FIRST_WAVE
    assert props["wave_height_m"] == 1.5
    assert row.window.earliest == _FIRST_WAVE[:10]


@pytest.mark.asyncio
async def test_a_1973_window_at_46001_lists_no_buoy(monkeypatch):
    """Under the floor there is nothing to ask: the match drops the source by
    name rather than selling a window whose files measure no sea state."""
    from trid3nt_server.tools.search import match
    from trid3nt_server.tools.search.find_sources import find_sources

    monkeypatch.setattr(nb, "get_client", lambda: object())
    monkeypatch.setattr(nb, "get_bytes",
                        lambda client, url, headers=None: (_LISTING_BODY, "", url))
    monkeypatch.setitem(match._LISTED, "ndbc_buoys.stations", nb.stations())
    found = await find_sources("wave series", [-148.027, 56.296],
                               ["1973-06-01", "1973-06-08"])
    assert found["picked"] == ""
    assert "fetch_ndbc_buoys" in found["sentence"]
    assert "reports from 1974-12-03" in found["sentence"]
