"""Unit tests for the ``fetch_storm_tracks`` atomic tool.

Hurricane / tropical-cyclone tracks: IBTrACS v04r01 best-track archive
(historical, bbox + season range + optional name) and the NHC
CurrentStorms.json feed (active storms + best-effort forecast-track points).
All HTTP is mocked - no live network in this file.

Coverage:
- Error classes carry correct retryable + error_code attributes.
- Input validation: malformed/degenerate bbox, missing bbox (historical),
  bad year ranges (reversed / future / pre-1842), bad geometry mode.
- Year resolution defaults (last 3 seasons) and one-sided anchoring.
- IBTrACS file selection: recent range -> last3years; older range -> per-basin
  by bbox; >2 basins -> typed input error; polar bbox -> honest no-storms.
- CSV parser: units-row skip, spur-row skip, season + name filters, blank
  numerics -> None, USA_WIND -> WMO_WIND fallback, category parse.
- Storm-wise bbox selection keeps the FULL track of a touching storm.
- Saffir-Simpson label mapping.
- Happy paths (mocked HTTP + read_through stub) for lines and points modes:
  LayerURI shape, FlatGeobuf geometry types + attributes round-trip.
- Honest empty -> StormTracksNoStormsError / StormTracksNoActiveStormsError.
- Active mode: numeric + hemisphere-string coordinate parsing, name/bbox
  filters, forecast-point merge (tau_h / is_forecast), schema-drift error.
- Registration: fetch_storm_tracks appears in get_registered_tools().
- Payload estimator returns a positive float.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

import trid3nt_server.tools.fetch_storm_tracks as mod
from trid3nt_server.tools import get_registered_tools
from trid3nt_server.tools.fetch_storm_tracks import (
    IBTRACS_CSV_BASE,
    NHC_CURRENT_STORMS_URL,
    StormTracksError,
    StormTracksInputError,
    StormTracksNoActiveStormsError,
    StormTracksNoStormsError,
    StormTracksUpstreamError,
    _parse_current_storms,
    _parse_ibtracs_csv,
    _parse_signed_coord,
    _records_bbox,
    _resolve_years,
    _saffir_label,
    _select_ibtracs_files,
    _select_storms_in_bbox,
    _validate_bbox,
    estimate_payload_mb,
    fetch_storm_tracks,
)

_CURRENT_YEAR = __import__("datetime").datetime.now(
    __import__("datetime").timezone.utc
).year


# ---------------------------------------------------------------------------
# Fixtures - synthetic IBTrACS CSV + NHC CurrentStorms bodies.
# ---------------------------------------------------------------------------

_CSV_HEADER = (
    "SID,SEASON,NUMBER,BASIN,SUBBASIN,NAME,ISO_TIME,NATURE,LAT,LON,"
    "WMO_WIND,WMO_PRES,WMO_AGENCY,TRACK_TYPE,DIST2LAND,LANDFALL,IFLAG,"
    "USA_AGENCY,USA_ATCF_ID,USA_LAT,USA_LON,USA_RECORD,USA_STATUS,"
    "USA_WIND,USA_PRES,USA_SSHS"
)
_CSV_UNITS = (
    " ,Year, , , , , , ,degrees_north,degrees_east,kts,mb, , ,km,km, , , ,"
    "degrees_north,degrees_east, , ,kts,mb,1"
)


def _row(
    sid: str,
    season: int,
    name: str,
    iso_time: str,
    lat: float,
    lon: float,
    *,
    nature: str = "TS",
    track_type: str = "main",
    wmo_wind: str = " ",
    usa_wind: str = " ",
    usa_pres: str = " ",
    usa_sshs: str = " ",
    usa_status: str = "HU",
) -> str:
    return (
        f"{sid},{season},1,NA,GM,{name},{iso_time},{nature},{lat},{lon},"
        f"{wmo_wind}, , ,{track_type},10,0,O______________,"
        f"atcf,AL092022,{lat},{lon}, ,{usa_status},{usa_wind},{usa_pres},{usa_sshs}"
    )


# IAN: 3 fixes, track crosses the SW-Florida bbox; peaks at cat 4 / 125 kt.
# NINE: single fix inside the bbox (dropped in lines mode).
# FARAWAY: 2 fixes far outside the bbox (never selected).
# A spur duplicate of an IAN fix and a 2019-season row must both be skipped.
_IBTRACS_BODY = "\n".join(
    [
        _CSV_HEADER,
        _CSV_UNITS,
        _row(
            "2022266N12294", 2022, "IAN", "2022-09-27 12:00:00", 23.4, -83.4,
            usa_wind="100", usa_pres="947", usa_sshs="3",
        ),
        _row(
            "2022266N12294", 2022, "IAN", "2022-09-28 12:00:00", 26.7, -82.2,
            usa_wind="125", usa_pres="940", usa_sshs="4",
        ),
        # spur duplicate of the fix above - must be skipped.
        _row(
            "2022266N12294", 2022, "IAN", "2022-09-28 12:00:00", 26.7, -82.2,
            usa_wind="125", usa_pres="940", usa_sshs="4", track_type="spur",
        ),
        # blank USA_WIND -> falls back to WMO_WIND=35.
        _row(
            "2022266N12294", 2022, "IAN", "2022-09-29 12:00:00", 29.9, -80.9,
            wmo_wind="35", usa_sshs="0",
        ),
        _row(
            "2022300N20280", 2022, "NINE", "2022-10-27 00:00:00", 26.0, -82.5,
            usa_wind="30", usa_sshs="-1",
        ),
        _row(
            "2022200N30310", 2022, "FARAWAY", "2022-07-20 00:00:00", 35.0, -45.0,
            usa_wind="60", usa_sshs="0",
        ),
        _row(
            "2022200N30310", 2022, "FARAWAY", "2022-07-21 00:00:00", 36.0, -44.0,
            usa_wind="65", usa_sshs="1",
        ),
        # wrong season - filtered out.
        _row(
            "2019250N15300", 2019, "IAN", "2019-09-01 00:00:00", 26.5, -82.4,
            usa_wind="50", usa_sshs="0",
        ),
    ]
).encode("utf-8")

# SW Florida bbox that IAN's middle fix and NINE's only fix fall inside.
_FL_BBOX = (-83.5, 25.5, -81.0, 27.5)

_ACTIVE_BODY = json.dumps(
    {
        "activeStorms": [
            {
                "id": "al052026",
                "binNumber": "AT5",
                "name": "Ernesto",
                "classification": "HU",
                "intensity": "85",
                "pressure": "970",
                "latitude": "24.5N",
                "longitude": "70.1W",
                "latitudeNumeric": 24.5,
                "longitudeNumeric": -70.1,
                "movementDir": 315,
                "movementSpeed": 12,
                "lastUpdate": "2026-07-07T15:00:00.000Z",
                "forecastTrack": {
                    "zipFile": "https://www.nhc.noaa.gov/gis/forecast/archive/al052026_5day_latest.zip"
                },
            },
            {
                # string-only coordinates - exercises _parse_signed_coord.
                "id": "ep022026",
                "name": "Blas",
                "classification": "TS",
                "intensity": "45",
                "pressure": "1000",
                "latitude": "14.8N",
                "longitude": "112.9W",
                "movementDir": 280,
                "movementSpeed": 9,
                "lastUpdate": "2026-07-07T15:00:00.000Z",
            },
        ]
    }
).encode("utf-8")

_ACTIVE_EMPTY_BODY = json.dumps({"activeStorms": []}).encode("utf-8")


class _FakeResult:
    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.data = b""
        self.hit = False


def _stub_read_through(*, metadata: Any, params: Any, ext: str, fetch_fn: Any) -> Any:
    # Invoke the real fetch_fn so the parse/build/capture path is exercised,
    # but return a synthetic S3 uri instead of touching S3.
    captured = fetch_fn()
    assert isinstance(captured, bytes) and len(captured) > 0
    return _FakeResult(f"s3://test-bucket/cache/{ext}/stub.fgb")


# ---------------------------------------------------------------------------
# Error classes.
# ---------------------------------------------------------------------------


def test_error_classes_codes_and_retryable() -> None:
    assert StormTracksError.retryable is True
    assert StormTracksUpstreamError.retryable is True
    assert StormTracksInputError.retryable is False
    assert StormTracksNoStormsError.retryable is False
    assert StormTracksNoActiveStormsError.retryable is False
    assert StormTracksInputError.error_code == "STORM_TRACKS_INPUT_ERROR"
    assert StormTracksUpstreamError.error_code == "STORM_TRACKS_UPSTREAM_ERROR"
    assert StormTracksNoStormsError.error_code == "STORM_TRACKS_NO_STORMS"
    assert (
        StormTracksNoActiveStormsError.error_code
        == "STORM_TRACKS_NO_ACTIVE_STORMS"
    )
    for cls in (
        StormTracksInputError,
        StormTracksUpstreamError,
        StormTracksNoStormsError,
        StormTracksNoActiveStormsError,
    ):
        assert issubclass(cls, StormTracksError)


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        (1.0, 2.0, 3.0),  # wrong arity
        (-200.0, 25.0, -80.0, 27.0),  # lon out of range
        (-83.0, -95.0, -80.0, 27.0),  # lat out of range
        (-80.0, 25.0, -83.0, 27.0),  # reversed lon
        (-83.0, 27.0, -80.0, 25.0),  # reversed lat
        (float("nan"), 25.0, -80.0, 27.0),  # non-finite
    ],
)
def test_validate_bbox_rejects(bad: tuple[float, ...]) -> None:
    with pytest.raises(StormTracksInputError):
        _validate_bbox(bad)  # type: ignore[arg-type]


def test_historical_requires_bbox() -> None:
    with pytest.raises(StormTracksInputError, match="requires"):
        fetch_storm_tracks()


def test_bad_geometry_mode_rejected() -> None:
    with pytest.raises(StormTracksInputError, match="geometry"):
        fetch_storm_tracks(bbox=_FL_BBOX, geometry="polygons")


def test_resolve_years_default_is_last_three_seasons() -> None:
    y0, y1 = _resolve_years(None, None)
    assert y1 == _CURRENT_YEAR
    assert y0 == _CURRENT_YEAR - 2


def test_resolve_years_one_sided() -> None:
    assert _resolve_years(2004, None) == (2004, _CURRENT_YEAR)
    assert _resolve_years(None, 2005) == (2003, 2005)


@pytest.mark.parametrize(
    ("y0", "y1"),
    [(2020, 2018), (1700, 2000), (2020, _CURRENT_YEAR + 5), ("abc", 2020)],
)
def test_resolve_years_rejects(y0: Any, y1: Any) -> None:
    with pytest.raises(StormTracksInputError):
        _resolve_years(y0, y1)


# ---------------------------------------------------------------------------
# IBTrACS file selection.
# ---------------------------------------------------------------------------


def test_select_files_recent_uses_last3years() -> None:
    files = _select_ibtracs_files(_FL_BBOX, _CURRENT_YEAR - 1, _CURRENT_YEAR)
    assert files == ["ibtracs.last3years.list.v04r01.csv"]


def test_select_files_old_uses_basin() -> None:
    files = _select_ibtracs_files(_FL_BBOX, 2004, 2006)
    assert files == ["ibtracs.NA.list.v04r01.csv"]


def test_select_files_polar_bbox_raises_no_storms() -> None:
    with pytest.raises(StormTracksNoStormsError):
        _select_ibtracs_files((-40.0, 80.0, -30.0, 85.0), 2000, 2001)


def test_select_files_too_many_basins_rejected() -> None:
    # A near-global tropical belt touches >2 basin envelopes.
    with pytest.raises(StormTracksInputError, match="basins"):
        _select_ibtracs_files((-170.0, -30.0, 170.0, 30.0), 2000, 2001)


# ---------------------------------------------------------------------------
# CSV parsing + bbox selection.
# ---------------------------------------------------------------------------


def test_parse_ibtracs_filters_and_fallbacks() -> None:
    storms = _parse_ibtracs_csv(
        _IBTRACS_BODY, y0=2022, y1=2022, storm_name=None
    )
    # 2019 season filtered; spur skipped; 3 storms remain.
    assert set(storms) == {"2022266N12294", "2022300N20280", "2022200N30310"}
    ian = storms["2022266N12294"]
    assert len(ian) == 3  # spur duplicate NOT double-counted
    assert ian[0]["wind_kt"] == 100.0
    assert ian[0]["pres_mb"] == 947.0
    assert ian[1]["category"] == 4
    # blank USA_WIND falls back to WMO_WIND
    assert ian[2]["wind_kt"] == 35.0
    assert ian[2]["pres_mb"] is None
    assert ian[0]["basin"] == "NA"
    assert ian[0]["name"] == "IAN"


def test_parse_ibtracs_name_filter_case_insensitive() -> None:
    storms = _parse_ibtracs_csv(
        _IBTRACS_BODY, y0=2022, y1=2022, storm_name="ian"
    )
    assert set(storms) == {"2022266N12294"}


def test_parse_ibtracs_missing_columns_is_upstream_error() -> None:
    with pytest.raises(StormTracksUpstreamError, match="missing expected"):
        _parse_ibtracs_csv(b"A,B,C\n1,2,3\n", y0=2022, y1=2022, storm_name=None)


def test_select_storms_keeps_full_track() -> None:
    storms = _parse_ibtracs_csv(
        _IBTRACS_BODY, y0=2022, y1=2022, storm_name=None
    )
    selected = _select_storms_in_bbox(storms, _FL_BBOX)
    # IAN touches the bbox with one fix but keeps all 3; FARAWAY excluded.
    assert set(selected) == {"2022266N12294", "2022300N20280"}
    assert len(selected["2022266N12294"]) == 3
    # fixes come back time-ordered
    times = [f["iso_time"] for f in selected["2022266N12294"]]
    assert times == sorted(times)


def test_saffir_labels() -> None:
    assert _saffir_label(5) == "category 5"
    assert _saffir_label(0) == "tropical storm"
    assert _saffir_label(-1) == "tropical depression"
    assert _saffir_label(None) == "unknown"
    assert _saffir_label(99) == "unknown"


def test_records_bbox_pads_degenerate() -> None:
    ext = _records_bbox([{"lon": -82.0, "lat": 26.0}])
    assert ext == (-82.5, 25.5, -81.5, 26.5)
    assert _records_bbox([]) is None


# ---------------------------------------------------------------------------
# Historical happy paths (mocked HTTP + read_through stub).
# ---------------------------------------------------------------------------


def test_historical_lines_happy_path() -> None:
    pytest.importorskip("geopandas")
    captured: dict[str, Any] = {}
    orig_build = mod._build_line_flatgeobuf

    def _spy(storms: Any) -> bytes:
        captured["storms"] = storms
        return orig_build(storms)

    urls: list[str] = []

    def _fake_get(url: str, timeout: float = 0.0) -> bytes:
        urls.append(url)
        return _IBTRACS_BODY

    with patch.object(mod, "_http_get", _fake_get), patch.object(
        mod, "read_through", _stub_read_through
    ), patch.object(mod, "_build_line_flatgeobuf", _spy):
        uri = fetch_storm_tracks(bbox=_FL_BBOX, start_year=2022, end_year=2022)

    # 2022 is older than the last-3-seasons window -> the NA basin file.
    assert urls == [IBTRACS_CSV_BASE + "ibtracs.NA.list.v04r01.csv"]
    assert uri.layer_type == "vector"
    assert uri.role == "primary"
    assert uri.style_preset == "storm_tracks"
    assert uri.uri == "s3://test-bucket/cache/fgb/stub.fgb"
    assert uri.layer_id.startswith("storm-tracks-")
    assert "IBTrACS" in uri.name
    # extent covers the FULL IAN track, i.e. wider than the query bbox.
    assert uri.bbox is not None
    w, s, e, n = uri.bbox
    assert w <= -83.4 and e >= -80.9 and s <= 23.4 and n >= 29.9
    # lines mode passed only bbox-touching storms to the builder.
    assert set(captured["storms"]) == {"2022266N12294", "2022300N20280"}


def test_historical_lines_fgb_roundtrip() -> None:
    gpd = pytest.importorskip("geopandas")
    import tempfile as _tf

    storms = _parse_ibtracs_csv(
        _IBTRACS_BODY, y0=2022, y1=2022, storm_name=None
    )
    selected = _select_storms_in_bbox(storms, _FL_BBOX)
    fgb = mod._build_line_flatgeobuf(selected)
    with _tf.NamedTemporaryFile(suffix=".fgb") as f:
        f.write(fgb)
        f.flush()
        gdf = gpd.read_file(f.name)
    # NINE has a single fix -> dropped; only IAN remains as a line.
    assert len(gdf) == 1
    row = gdf.iloc[0]
    assert row.geometry.geom_type == "LineString"
    assert row["name"] == "IAN"
    assert row["max_wind_kt"] == 125.0
    assert row["min_pres_mb"] == 940.0
    assert int(row["max_category"]) == 4
    assert row["max_category_label"] == "category 4"
    assert int(row["n_fixes"]) == 3


def test_historical_points_mode_roundtrip() -> None:
    gpd = pytest.importorskip("geopandas")
    import tempfile as _tf

    fgb_holder: dict[str, bytes] = {}

    def _stub_rt(*, metadata: Any, params: Any, ext: str, fetch_fn: Any) -> Any:
        fgb_holder["fgb"] = fetch_fn()
        return _FakeResult(f"s3://test-bucket/cache/{ext}/stub.fgb")

    with patch.object(
        mod, "_http_get", lambda url, timeout=0.0: _IBTRACS_BODY
    ), patch.object(mod, "read_through", _stub_rt):
        fetch_storm_tracks(
            bbox=_FL_BBOX, start_year=2022, end_year=2022, geometry="points"
        )

    with _tf.NamedTemporaryFile(suffix=".fgb") as f:
        f.write(fgb_holder["fgb"])
        f.flush()
        gdf = gpd.read_file(f.name)
    # IAN (3 fixes) + NINE (1 fix) = 4 points.
    assert len(gdf) == 4
    assert set(gdf.geometry.geom_type) == {"Point"}
    ian_peak = gdf[gdf["iso_time"] == "2022-09-28 12:00:00"].iloc[0]
    assert ian_peak["wind_kt"] == 125.0
    assert ian_peak["category_label"] == "category 4"


def test_historical_honest_empty_raises() -> None:
    with patch.object(
        mod, "_http_get", lambda url, timeout=0.0: _IBTRACS_BODY
    ), patch.object(mod, "read_through", _stub_read_through):
        with pytest.raises(StormTracksNoStormsError):
            fetch_storm_tracks(
                bbox=_FL_BBOX,
                start_year=2022,
                end_year=2022,
                storm_name="KATRINA",
            )


# ---------------------------------------------------------------------------
# Active mode.
# ---------------------------------------------------------------------------


def test_parse_signed_coord() -> None:
    assert _parse_signed_coord("14.8N") == pytest.approx(14.8)
    assert _parse_signed_coord("52.9W") == pytest.approx(-52.9)
    assert _parse_signed_coord("112.9E") == pytest.approx(112.9)
    assert _parse_signed_coord("10.0S") == pytest.approx(-10.0)
    assert _parse_signed_coord("") is None
    assert _parse_signed_coord("junk") is None


def test_parse_current_storms_mixed_coords() -> None:
    recs = _parse_current_storms(_ACTIVE_BODY)
    assert len(recs) == 2
    ernesto = recs[0]
    assert ernesto["name"] == "Ernesto"
    assert ernesto["lat"] == pytest.approx(24.5)
    assert ernesto["lon"] == pytest.approx(-70.1)
    assert ernesto["intensity_kt"] == 85.0
    assert ernesto["forecast_track_zip"].endswith("al052026_5day_latest.zip")
    blas = recs[1]
    assert blas["lat"] == pytest.approx(14.8)
    assert blas["lon"] == pytest.approx(-112.9)
    assert blas["forecast_track_zip"] is None


def test_parse_current_storms_schema_drift_is_upstream_error() -> None:
    with pytest.raises(StormTracksUpstreamError, match="activeStorms"):
        _parse_current_storms(b"{}")


def test_active_empty_raises_no_active_storms() -> None:
    with patch.object(
        mod, "_http_get", lambda url, timeout=0.0: _ACTIVE_EMPTY_BODY
    ), patch.object(mod, "read_through", _stub_read_through):
        with pytest.raises(StormTracksNoActiveStormsError):
            fetch_storm_tracks(active_only=True)


def test_active_happy_path_with_forecast_points() -> None:
    gpd = pytest.importorskip("geopandas")
    import tempfile as _tf

    def _fake_forecast(zip_url: str, storm: dict[str, Any]) -> list[dict[str, Any]]:
        assert zip_url.endswith(".zip")
        return [
            {
                "id": storm["id"],
                "name": storm["name"],
                "classification": "HU",
                "intensity_kt": 90.0,
                "pressure_mb": 965.0,
                "lat": 26.0,
                "lon": -72.0,
                "movement_dir_deg": None,
                "movement_speed_kt": None,
                "last_update": "2026-07-08 12:00 AST",
                "tau_h": 24.0,
            }
        ]

    fgb_holder: dict[str, bytes] = {}

    def _stub_rt(*, metadata: Any, params: Any, ext: str, fetch_fn: Any) -> Any:
        fgb_holder["fgb"] = fetch_fn()
        return _FakeResult(f"s3://test-bucket/cache/{ext}/stub.fgb")

    with patch.object(
        mod, "_http_get", lambda url, timeout=0.0: _ACTIVE_BODY
    ), patch.object(
        mod, "_fetch_forecast_track_points", _fake_forecast
    ), patch.object(mod, "read_through", _stub_rt):
        uri = fetch_storm_tracks(active_only=True)

    assert uri.layer_type == "vector"
    assert "NHC active" in uri.name
    with _tf.NamedTemporaryFile(suffix=".fgb") as f:
        f.write(fgb_holder["fgb"])
        f.flush()
        gdf = gpd.read_file(f.name)
    # Ernesto current + 1 forecast point; Blas current only (no zip).
    assert len(gdf) == 3
    ern = gdf[gdf["name"] == "Ernesto"].sort_values("tau_h")
    assert list(ern["tau_h"]) == [0.0, 24.0]
    assert list(ern["is_forecast"]) == [0, 1]
    cur = ern.iloc[0]
    assert cur["intensity_kt"] == 85.0
    assert cur["movement_dir_deg"] == 315.0


def test_active_name_and_bbox_filters() -> None:
    with patch.object(
        mod, "_http_get", lambda url, timeout=0.0: _ACTIVE_BODY
    ), patch.object(mod, "read_through", _stub_read_through):
        # name filter that matches nothing -> honest empty
        with pytest.raises(StormTracksNoActiveStormsError):
            fetch_storm_tracks(active_only=True, storm_name="ZETA")
        # bbox around neither storm -> honest empty
        with pytest.raises(StormTracksNoActiveStormsError):
            fetch_storm_tracks(active_only=True, bbox=(0.0, 0.0, 10.0, 10.0))


def test_active_degrades_without_forecast_zip() -> None:
    pytest.importorskip("geopandas")

    def _forecast_fails(zip_url: str, storm: dict[str, Any]) -> list[dict[str, Any]]:
        return []  # the real function returns [] on any fetch/parse failure

    with patch.object(
        mod, "_http_get", lambda url, timeout=0.0: _ACTIVE_BODY
    ), patch.object(
        mod, "_fetch_forecast_track_points", _forecast_fails
    ), patch.object(mod, "read_through", _stub_read_through):
        uri = fetch_storm_tracks(active_only=True)
    assert uri.layer_type == "vector"  # current positions still delivered


def test_active_upstream_url_used() -> None:
    urls: list[str] = []

    def _fake_get(url: str, timeout: float = 0.0) -> bytes:
        urls.append(url)
        return _ACTIVE_EMPTY_BODY

    with patch.object(mod, "_http_get", _fake_get), patch.object(
        mod, "read_through", _stub_read_through
    ):
        with pytest.raises(StormTracksNoActiveStormsError):
            fetch_storm_tracks(active_only=True)
    assert urls == [NHC_CURRENT_STORMS_URL]


# ---------------------------------------------------------------------------
# Registration + estimator.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    names = [t.metadata.name for t in get_registered_tools()]
    assert "fetch_storm_tracks" in names
    reg = next(
        t for t in get_registered_tools()
        if t.metadata.name == "fetch_storm_tracks"
    )
    assert reg.metadata.ttl_class == "dynamic-1h"
    assert reg.metadata.cacheable is True


def test_estimate_payload_mb_positive() -> None:
    assert estimate_payload_mb(bbox=_FL_BBOX, start_year=2004, end_year=2024) > 0.0
    assert estimate_payload_mb(active_only=True) > 0.0
    assert estimate_payload_mb(bbox=None) > 0.0
    assert (
        estimate_payload_mb(bbox=_FL_BBOX, geometry="points")
        >= estimate_payload_mb(bbox=_FL_BBOX, geometry="lines")
    )
