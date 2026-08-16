"""Router-fold test parity for ``fetch_storm_tracks`` (ADR 0111).

Migrated from ``test_fetch_storm_tracks.py`` when the coded twin was folded onto
the router (a ``library_delegate`` vector-fgb spec + the ``storm_tracks.*`` hooks +
the fetch-time provenance channel). Follows the ``test_router_topobathy.py`` /
``test_router_dem.py`` migrated-test style: pure hook/helper tests offline, plus
end-to-end drives through the promoted router closure (``TOOL_REGISTRY``) with the
delegate's network seam (``storm_tracks._http_get``) monkeypatched and the S3 cache
faked (``fake_s3``). Proves:

- registry shape + typed-error envelope + payload estimator;
- the historical-mode bbox-required gate + geometry/storm_name shape checks
  (``storm_tracks.validate``);
- year resolution + IBTrACS per-basin file selection (recent -> last3years,
  older -> per-basin, >2 basins rejected, polar bbox honest-empty);
- the IBTrACS CSV parser + storm-wise bbox selection (full track kept) + the
  Saffir-Simpson label map + the line/point feature builders;
- the NHC CurrentStorms.json parser (numeric + hemisphere-string coords);
- END-TO-END via the promoted router closure for both modes: StormTracksLayerURI
  fields populated, FlatGeobuf round-trips via geopandas, honest-empty raises;
- THE CHANNEL: a cache-hit REPLAYS the mode/storm_count/storm_names provenance
  fields identically (no re-fetch).
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

import pytest

from trid3nt_contracts.execution import StormTracksLayerURI
from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.data.fetchers._fetch_common import FetchError
from trid3nt_server.data.fetchers._router.hooks import storm_tracks as st
from trid3nt_server.data.fetchers._router.hooks.storm_tracks import (
    IBTRACS_CSV_BASE,
    NHC_CURRENT_STORMS_URL,
    StormTracksError,
    StormTracksInputError,
    StormTracksNoActiveStormsError,
    StormTracksNoStormsError,
    StormTracksUpstreamError,
    estimate_payload_mb,
)

_CURRENT_YEAR = _dt.datetime.now(_dt.timezone.utc).year

# SW Florida bbox that IAN's middle fix and NINE's only fix fall inside.
_FL_BBOX = (-83.5, 25.5, -81.0, 27.5)


def _fetch_storm_tracks(**kw: Any) -> Any:
    return TOOL_REGISTRY["fetch_storm_tracks"].fn(**kw)


# --------------------------------------------------------------------------- #
# Synthetic IBTrACS CSV body (ported verbatim from the deleted twin test).
# --------------------------------------------------------------------------- #

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


def _historical_selection() -> dict[str, list[dict[str, Any]]]:
    storms = st._parse_ibtracs_csv(_IBTRACS_BODY, y0=2022, y1=2022, storm_name=None)
    return st._select_storms_in_bbox(storms, _FL_BBOX)


# --------------------------------------------------------------------------- #
# Registry shape.
# --------------------------------------------------------------------------- #


def test_storm_tracks_registered_with_expected_metadata() -> None:
    assert "fetch_storm_tracks" in TOOL_REGISTRY
    md = TOOL_REGISTRY["fetch_storm_tracks"].metadata
    assert md.ttl_class == "dynamic-1h"
    assert md.source_class == "storm_tracks"
    assert md.cacheable is True
    assert getattr(md, "supports_global_query", None) is False
    assert getattr(md, "payload_mb_estimator_name", None) == "estimate_payload_mb"


# --------------------------------------------------------------------------- #
# Typed-error envelope.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "cls, code, retryable",
    [
        (st.StormTracksError, "STORM_TRACKS_ERROR", True),
        (StormTracksInputError, "STORM_TRACKS_INPUT_ERROR", False),
        (StormTracksUpstreamError, "STORM_TRACKS_UPSTREAM_ERROR", True),
        (StormTracksNoStormsError, "STORM_TRACKS_NO_STORMS", False),
        (StormTracksNoActiveStormsError, "STORM_TRACKS_NO_ACTIVE_STORMS", False),
    ],
)
def test_typed_error_envelope(cls: type, code: str, retryable: bool) -> None:
    err = cls("boom")
    assert err.error_code == code
    assert err.retryable is retryable
    assert isinstance(err, RuntimeError)
    assert isinstance(err, FetchError)  # library_delegate.invoke passes the code through
    assert issubclass(cls, StormTracksError)


def test_estimate_payload_mb_positive_and_scales() -> None:
    assert estimate_payload_mb(bbox=_FL_BBOX, start_year=2004, end_year=2024) > 0.0
    assert estimate_payload_mb(active_only=True) > 0.0
    assert estimate_payload_mb(bbox=None) > 0.0
    assert estimate_payload_mb(
        bbox=_FL_BBOX, geometry="points"
    ) >= estimate_payload_mb(bbox=_FL_BBOX, geometry="lines")


# --------------------------------------------------------------------------- #
# Input validation (``storm_tracks.validate``, pre-cache).
# --------------------------------------------------------------------------- #


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
        st._validate_bbox(bad)  # type: ignore[arg-type]


def test_validate_hook_historical_requires_bbox() -> None:
    with pytest.raises(StormTracksInputError, match="requires"):
        st.validate_storm_tracks(None, {"active_only": False})


def test_validate_hook_active_bbox_optional() -> None:
    st.validate_storm_tracks(None, {"active_only": True})  # no raise


def test_validate_hook_bad_geometry_mode() -> None:
    with pytest.raises(StormTracksInputError, match="geometry"):
        st.validate_storm_tracks(None, {"bbox": _FL_BBOX, "geometry": "polygons"})


def test_validate_hook_bad_storm_name_type() -> None:
    with pytest.raises(StormTracksInputError, match="storm_name"):
        st.validate_storm_tracks(None, {"bbox": _FL_BBOX, "storm_name": 12345})


def test_resolve_years_default_is_last_three_seasons() -> None:
    y0, y1 = st._resolve_years(None, None)
    assert y1 == _CURRENT_YEAR
    assert y0 == _CURRENT_YEAR - 2


def test_resolve_years_one_sided() -> None:
    assert st._resolve_years(2004, None) == (2004, _CURRENT_YEAR)
    assert st._resolve_years(None, 2005) == (2003, 2005)


@pytest.mark.parametrize(
    ("y0", "y1"),
    [(2020, 2018), (1700, 2000), (2020, _CURRENT_YEAR + 5), ("abc", 2020)],
)
def test_resolve_years_rejects(y0: Any, y1: Any) -> None:
    with pytest.raises(StormTracksInputError):
        st._resolve_years(y0, y1)


# --------------------------------------------------------------------------- #
# IBTrACS file selection.
# --------------------------------------------------------------------------- #


def test_select_files_recent_uses_last3years() -> None:
    files = st._select_ibtracs_files(_FL_BBOX, _CURRENT_YEAR - 1, _CURRENT_YEAR)
    assert files == ["ibtracs.last3years.list.v04r01.csv"]


def test_select_files_old_uses_basin() -> None:
    files = st._select_ibtracs_files(_FL_BBOX, 2004, 2006)
    assert files == ["ibtracs.NA.list.v04r01.csv"]


def test_select_files_polar_bbox_raises_no_storms() -> None:
    with pytest.raises(StormTracksNoStormsError):
        st._select_ibtracs_files((-40.0, 80.0, -30.0, 85.0), 2000, 2001)


def test_select_files_too_many_basins_rejected() -> None:
    # A near-global tropical belt touches >2 basin envelopes.
    with pytest.raises(StormTracksInputError, match="basins"):
        st._select_ibtracs_files((-170.0, -30.0, 170.0, 30.0), 2000, 2001)


# --------------------------------------------------------------------------- #
# CSV parsing + bbox selection.
# --------------------------------------------------------------------------- #


def test_parse_ibtracs_filters_and_fallbacks() -> None:
    storms = st._parse_ibtracs_csv(_IBTRACS_BODY, y0=2022, y1=2022, storm_name=None)
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
    storms = st._parse_ibtracs_csv(_IBTRACS_BODY, y0=2022, y1=2022, storm_name="ian")
    assert set(storms) == {"2022266N12294"}


def test_parse_ibtracs_missing_columns_is_upstream_error() -> None:
    with pytest.raises(StormTracksUpstreamError, match="missing expected"):
        st._parse_ibtracs_csv(b"A,B,C\n1,2,3\n", y0=2022, y1=2022, storm_name=None)


def test_select_storms_keeps_full_track() -> None:
    selected = _historical_selection()
    # IAN touches the bbox with one fix but keeps all 3; FARAWAY excluded.
    assert set(selected) == {"2022266N12294", "2022300N20280"}
    assert len(selected["2022266N12294"]) == 3
    # fixes come back time-ordered
    times = [f["iso_time"] for f in selected["2022266N12294"]]
    assert times == sorted(times)


def test_saffir_labels() -> None:
    assert st._saffir_label(5) == "category 5"
    assert st._saffir_label(0) == "tropical storm"
    assert st._saffir_label(-1) == "tropical depression"
    assert st._saffir_label(None) == "unknown"
    assert st._saffir_label(99) == "unknown"


# --------------------------------------------------------------------------- #
# Feature builders (delegate returns features; vector_fgb serializes).
# --------------------------------------------------------------------------- #


def test_line_features_shape_and_single_fix_drop() -> None:
    selected = _historical_selection()
    feats = st._line_features(selected)
    # NINE has a single fix -> dropped as a line; only IAN remains.
    assert len(feats) == 1
    feat = feats[0]
    assert feat["geometry"]["type"] == "LineString"
    assert len(feat["geometry"]["coordinates"]) == 3
    props = feat["properties"]
    assert props["sid"] == "2022266N12294"
    assert props["name"] == "IAN"
    assert props["max_wind_kt"] == 125.0
    assert props["min_pres_mb"] == 940.0
    assert props["max_category"] == 4
    assert props["max_category_label"] == "category 4"
    assert props["n_fixes"] == 3
    assert props["start_time"] == "2022-09-27 12:00:00"
    assert props["end_time"] == "2022-09-29 12:00:00"


def test_line_features_raises_when_every_storm_is_single_fix() -> None:
    storms = st._parse_ibtracs_csv(_IBTRACS_BODY, y0=2022, y1=2022, storm_name=None)
    single_fix_only = {"2022300N20280": storms["2022300N20280"]}  # NINE, 1 fix
    with pytest.raises(StormTracksNoStormsError):
        st._line_features(single_fix_only)


def test_point_features_shape_excludes_lat_lon() -> None:
    selected = _historical_selection()
    all_fixes = [f for fixes in selected.values() for f in fixes]
    feats = st._point_features(all_fixes)
    assert len(feats) == 4  # IAN (3) + NINE (1)
    assert all(f["geometry"]["type"] == "Point" for f in feats)
    for f, fix in zip(feats, all_fixes):
        assert f["geometry"]["coordinates"] == [fix["lon"], fix["lat"]]
        assert "lat" not in f["properties"] and "lon" not in f["properties"]
        assert "forecast_track_zip" not in f["properties"]


# --------------------------------------------------------------------------- #
# NHC active-storms parsing.
# --------------------------------------------------------------------------- #


def test_parse_current_storms_mixed_coords() -> None:
    recs = st._parse_current_storms(_ACTIVE_BODY)
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
        st._parse_current_storms(b"{}")


# --------------------------------------------------------------------------- #
# Pure envelope hook (layer_id / name / provenance replay).
# --------------------------------------------------------------------------- #


def test_envelope_hook_historical_layer_id_and_name() -> None:
    params = {
        "bbox": _FL_BBOX,
        "start_year": 2022,
        "end_year": 2022,
        "storm_name": None,
        "geometry": "lines",
        "active_only": False,
    }
    out = st.envelope_storm_tracks(
        None, params, None, None,
        provenance={"mode": "historical", "storm_count": 2, "storm_names": ["IAN", "NINE"]},
    )
    assert out["layer_id"].startswith("storm-tracks-")
    assert out["name"] == "Storm tracks - IBTrACS tracks (2022..2022)"
    assert out["mode"] == "historical"
    assert out["storm_count"] == 2
    assert out["storm_names"] == ["IAN", "NINE"]


def test_envelope_hook_active_layer_id_and_name() -> None:
    params = {"bbox": None, "storm_name": None, "active_only": True}
    out = st.envelope_storm_tracks(
        None, params, None, None,
        provenance={"mode": "active", "storm_count": 1, "storm_names": ["ERNESTO"]},
    )
    assert out["layer_id"].startswith("storm-tracks-")
    assert out["name"] == "Storm tracks - NHC active storms (active (NHC))"


def test_envelope_hook_pre_channel_defaults() -> None:
    """No provenance sidecar (pre-channel cache object) -> declared defaults hold."""
    params = {"bbox": None, "storm_name": None, "active_only": True}
    out = st.envelope_storm_tracks(None, params, None, None, provenance=None)
    assert out["mode"] == "active"
    assert out["storm_count"] == 0
    assert out["storm_names"] == []


# --------------------------------------------------------------------------- #
# END-TO-END via the promoted router closure (network seam mocked + fake_s3).
# --------------------------------------------------------------------------- #


def test_end_to_end_historical_lines_happy_path(monkeypatch, fake_s3) -> None:
    urls: list[str] = []

    def _fake_get(url: str, timeout: float = 0.0) -> bytes:
        urls.append(url)
        return _IBTRACS_BODY

    monkeypatch.setattr(st, "_http_get", _fake_get)

    res = _fetch_storm_tracks(bbox=_FL_BBOX, start_year=2022, end_year=2022)

    # 2022 is older than the last-3-seasons window -> the NA basin file.
    assert urls == [IBTRACS_CSV_BASE + "ibtracs.NA.list.v04r01.csv"]
    assert isinstance(res, StormTracksLayerURI)
    assert res.layer_type == "vector"
    assert res.role == "primary"
    assert res.style_preset == "storm_tracks"
    assert res.units == "kt / mb"
    assert res.uri is not None and res.uri.endswith(".fgb")
    assert res.layer_id.startswith("storm-tracks-")
    assert "IBTrACS tracks" in res.name
    assert res.mode == "historical"
    assert res.storm_count == 2  # IAN + NINE touch the bbox
    assert set(res.storm_names) == {"IAN", "NINE"}
    assert res.bbox is not None
    w, s, e, n = res.bbox
    assert w <= -83.4 and e >= -80.9 and s <= 23.4 and n >= 29.9

    gpd = pytest.importorskip("geopandas")
    import tempfile as _tf

    fgb_key = next(k for k in fake_s3.store if k.endswith(".fgb"))
    with _tf.NamedTemporaryFile(suffix=".fgb") as f:
        f.write(fake_s3.store[fgb_key])
        f.flush()
        gdf = gpd.read_file(f.name)
    # NINE has a single fix -> dropped; only IAN remains as a line.
    assert len(gdf) == 1
    row = gdf.iloc[0]
    assert row.geometry.geom_type == "LineString"
    assert row["name"] == "IAN"
    assert row["max_wind_kt"] == 125.0


def test_end_to_end_historical_points_mode_roundtrip(monkeypatch, fake_s3) -> None:
    monkeypatch.setattr(st, "_http_get", lambda url, timeout=0.0: _IBTRACS_BODY)

    res = _fetch_storm_tracks(
        bbox=_FL_BBOX, start_year=2022, end_year=2022, geometry="points"
    )
    assert res.layer_type == "vector"

    gpd = pytest.importorskip("geopandas")
    import tempfile as _tf

    fgb_key = next(k for k in fake_s3.store if k.endswith(".fgb"))
    with _tf.NamedTemporaryFile(suffix=".fgb") as f:
        f.write(fake_s3.store[fgb_key])
        f.flush()
        gdf = gpd.read_file(f.name)
    # IAN (3 fixes) + NINE (1 fix) = 4 points.
    assert len(gdf) == 4
    assert set(gdf.geometry.geom_type) == {"Point"}
    ian_peak = gdf[gdf["iso_time"] == "2022-09-28 12:00:00"].iloc[0]
    assert ian_peak["wind_kt"] == 125.0
    assert ian_peak["category_label"] == "category 4"


def test_end_to_end_historical_honest_empty_raises(monkeypatch, fake_s3) -> None:
    monkeypatch.setattr(st, "_http_get", lambda url, timeout=0.0: _IBTRACS_BODY)
    with pytest.raises(StormTracksNoStormsError) as ei:
        _fetch_storm_tracks(
            bbox=_FL_BBOX, start_year=2022, end_year=2022, storm_name="KATRINA"
        )
    assert ei.value.error_code == "STORM_TRACKS_NO_STORMS"


def test_end_to_end_too_many_basins_propagates_input_error(monkeypatch, fake_s3) -> None:
    """The >2-basins gate lives inside the delegate read hook (not delegate_validate);
    prove the passthrough still surfaces the pinned STORM_TRACKS_INPUT_ERROR code."""
    with pytest.raises(StormTracksInputError) as ei:
        _fetch_storm_tracks(bbox=(-170.0, -30.0, 170.0, 30.0), start_year=2000, end_year=2001)
    assert ei.value.error_code == "STORM_TRACKS_INPUT_ERROR"


def test_end_to_end_active_happy_path_with_forecast_points(monkeypatch, fake_s3) -> None:
    pytest.importorskip("geopandas")

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

    monkeypatch.setattr(st, "_http_get", lambda url, timeout=0.0: _ACTIVE_BODY)
    monkeypatch.setattr(st, "_fetch_forecast_track_points", _fake_forecast)

    res = _fetch_storm_tracks(active_only=True)

    assert res.layer_type == "vector"
    assert res.mode == "active"
    assert "NHC active storms" in res.name
    assert res.storm_count == 2
    assert set(res.storm_names) == {"Blas", "Ernesto"}

    import geopandas as gpd
    import tempfile as _tf

    fgb_key = next(k for k in fake_s3.store if k.endswith(".fgb"))
    with _tf.NamedTemporaryFile(suffix=".fgb") as f:
        f.write(fake_s3.store[fgb_key])
        f.flush()
        gdf = gpd.read_file(f.name)
    # Ernesto current + 1 forecast point; Blas current only (no zip).
    assert len(gdf) == 3
    ern = gdf[gdf["name"] == "Ernesto"].sort_values("tau_h")
    assert list(ern["tau_h"]) == [0.0, 24.0]
    assert list(ern["is_forecast"]) == [0, 1]


def test_end_to_end_active_empty_raises_no_active_storms(monkeypatch, fake_s3) -> None:
    monkeypatch.setattr(st, "_http_get", lambda url, timeout=0.0: _ACTIVE_EMPTY_BODY)
    with pytest.raises(StormTracksNoActiveStormsError) as ei:
        _fetch_storm_tracks(active_only=True)
    assert ei.value.error_code == "STORM_TRACKS_NO_ACTIVE_STORMS"


def test_active_upstream_url_used(monkeypatch, fake_s3) -> None:
    urls: list[str] = []

    def _fake_get(url: str, timeout: float = 0.0) -> bytes:
        urls.append(url)
        return _ACTIVE_EMPTY_BODY

    monkeypatch.setattr(st, "_http_get", _fake_get)
    with pytest.raises(StormTracksNoActiveStormsError):
        _fetch_storm_tracks(active_only=True)
    assert urls == [NHC_CURRENT_STORMS_URL]


# --------------------------------------------------------------------------- #
# THE CHANNEL: cache-hit replay of mode / storm_count / storm_names.
# --------------------------------------------------------------------------- #


def test_cache_hit_replays_provenance_identically(monkeypatch, fake_s3) -> None:
    """A second call over the same params is a CACHE HIT that never re-fetches,
    yet mode/storm_count/storm_names REPLAY IDENTICAL from the provenance sidecar
    (ADR 0110) -- the fact a pre-channel cache object would have lost."""
    calls = {"n": 0}

    def _counting_get(url: str, timeout: float = 0.0) -> bytes:
        calls["n"] += 1
        return _IBTRACS_BODY

    monkeypatch.setattr(st, "_http_get", _counting_get)

    r1 = _fetch_storm_tracks(bbox=_FL_BBOX, start_year=2022, end_year=2022)
    assert calls["n"] == 1
    fields1 = (r1.mode, r1.storm_count, sorted(r1.storm_names))

    r2 = _fetch_storm_tracks(bbox=_FL_BBOX, start_year=2022, end_year=2022)
    assert calls["n"] == 1, "cache hit must NOT re-fetch"
    fields2 = (r2.mode, r2.storm_count, sorted(r2.storm_names))
    assert fields1 == fields2
    assert fields1 == ("historical", 2, ["IAN", "NINE"])
