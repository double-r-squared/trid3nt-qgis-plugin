"""The remaining hooks keep every field their source returns under the source's own names and add only what they make.

Faults, tsunamis, volcanoes, storm tracks, WFIGS, eHydro, the NHD waterbody, NLDI, OSM (rivers, roads, points, coastline,
buildings), field boundaries, usSEABED, bed material, LTER, the epicentre, the moment tensor and the FEMA declarations read
recorded answers (tests/fixtures/fetcher_fields/README.md). LTER's service answers no public request from this host, so its
entity is the documented delimited shape stated by hand, and the NHC active-storm feed holds no storm on the recording day,
so its record is the documented CurrentStorms.json shape stated by hand."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest

from trid3nt_server.tools.fetchers._router import registration
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import features_to_fgb_bytes

_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "fetcher_fields"


def _spec(name):
    return registration.get_spec(name)


def _bytes(name):
    return (_FIX / name).read_bytes()


def _written(spec, features, params=None):
    """The features as the router writes them: the FlatGeobuf read back, every column kept."""
    data = features_to_fgb_bytes(features, spec, params)
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as handle:
        handle.write(data)
    try:
        return data, pyogrio.read_dataframe(handle.name)
    finally:
        os.unlink(handle.name)


def _osm(name):
    return gpd.read_parquet(_FIX / f"osm_{name}.parquet").set_index(["element", "id"])


def _tags(row):
    return {str(k): str(v) for k, v in row.items()
            if k not in ("geometry", "nodes", "ways", "element") and not pd.isna(v)}


# ---------------------------------------------------------------- faults

def test_a_fault_keeps_the_gem_record_whole_and_produces_the_parsed_kinematics():
    from trid3nt_server.tools.fetchers.hazard.fetch_fault_sources import hooks as faults

    spec = _spec("fetch_fault_sources")
    found = faults.parse_response(spec, {"bbox": [-122.6, 37.2, -121.6, 38.0]}, [_bytes("gem_faults_excerpt.geojson")])
    diablo = next(f for f in found if f["properties"]["name"] == "Mount Diablo Thrust")
    assert diablo["properties"]["net_slip_rate"] == "(1.55,0.8,2.22)"
    assert (diablo["properties"]["catalog_id"], diablo["properties"]["dip_dir"]) == ("UCF_2", "E")
    assert diablo["produced"]["net_slip_rate_mm_yr"] == 1.55 and diablo["produced"]["dip_deg"] == 38.0
    assert diablo["produced"]["dropped_reason"] is None
    data, frame = _written(spec, found)
    assert {"average_dip", "catalog_id", "net_slip_rate", "net_slip_rate_mm_yr", "dropped_reason"} <= set(frame.columns)
    record = faults.envelope(spec, {"bbox": [-122.6, 37.2, -121.6, 38.0]}, None, data)
    assert {f["name"] for f in record["faults"]} >= {"Mount Diablo Thrust"}
    assert next(f for f in record["faults"] if f["name"] == "Mount Diablo Thrust")["slip_type"] == "Reverse"


def test_a_fault_the_source_gives_no_rake_is_kept_with_every_field_and_the_reason():
    from trid3nt_server.tools.fetchers.hazard.fetch_fault_sources import hooks as faults

    spec = _spec("fetch_fault_sources")
    (ardrossan,) = faults.parse_response(spec, {"bbox": [138.0, -34.0, 138.5, -33.5]}, [_bytes("gem_faults_excerpt.geojson")])
    assert ardrossan["properties"]["reference"] == "Fault_slip_rates_A.doc"
    assert "average_rake" in ardrossan["produced"]["dropped_reason"] and ardrossan["produced"]["rake_deg"] is None


# ---------------------------------------------------------------- tsunamis

def test_a_tsunami_event_keeps_every_field_ncei_returns():
    from trid3nt_server.tools.fetchers.hazard.fetch_tsunami_events import hooks as tsunami

    spec = _spec("fetch_tsunami_events")
    found = tsunami.parse_response(spec, {"observation_type": "events"}, [_bytes("ncei_tsunami_events_2011.json")])
    tohoku = next(f for f in found if f["properties"]["id"] == 5413)
    assert tohoku["properties"]["deathsTotal"] == 18423 and tohoku["properties"]["damageMillionsDollars"] == 220136.6
    assert tohoku["properties"]["eqDepth"] == 29 and tohoku["properties"]["oceanicTsunami"] is True
    assert tohoku["produced"] == {"cause": "Earthquake", "observation_type": "event"}
    assert "cause_code" not in tohoku["properties"] and "max_water_height" not in tohoku["properties"]
    _data, frame = _written(spec, found)
    assert {"causeCode", "numRunups", "eventValidity"} <= set(frame.columns)


def test_a_tsunami_runup_keeps_its_distance_and_source_fields():
    from trid3nt_server.tools.fetchers.hazard.fetch_tsunami_events import hooks as tsunami

    spec = _spec("fetch_tsunami_events")
    found = tsunami.parse_response(spec, {"observation_type": "runups"}, [_bytes("ncei_tsunami_runups_2011.json")])
    assert found[0]["properties"]["distFromSource"] == pytest.approx(243.13139693547646)
    assert found[0]["properties"]["sourceCauseCode"] == 1 and found[0]["properties"]["area"] == "MIYAGI"
    assert found[0]["produced"] == {"cause": "Earthquake", "observation_type": "runup"}


# ---------------------------------------------------------------- volcanoes

def test_a_volcano_keeps_both_hans_records_whole_and_produces_its_ranks():
    from trid3nt_server.tools.fetchers.hazard.fetch_usgs_volcano_alerts import hooks as volcano

    spec = _spec("fetch_usgs_volcano_alerts")
    found = volcano.parse_response(spec, {}, [_bytes("hans_monitored_volcanoes.json"), _bytes("hans_us_volcanoes.json")])
    assert len(found) == 3
    first = found[0]
    assert first["properties"]["alert"]["notice_data"].startswith("https://volcanoes.usgs.gov/hans-public/api/notice/")
    assert "sent_unixtime" in first["properties"]["alert"] and "obs_email" in first["properties"]["us_volcano"]
    assert first["properties"]["us_volcano"]["icao_coordinates"]
    assert set(first["produced"]) == {"alert_rank", "color_rank"}
    _data, frame = _written(spec, found)
    assert {"alert.notice_data", "alert.volcano_cd", "us_volcano.icao_coordinates", "us_volcano.nvews_threat",
            "alert_rank", "color_rank"} <= set(frame.columns)


# ---------------------------------------------------------------- storm tracks

_NA_BBOX = (-90.0, 16.0, -80.0, 30.0)


def _ibtracs(monkeypatch):
    from trid3nt_server.tools.fetchers.weather.fetch_storm_tracks import hooks as storms

    monkeypatch.setattr(storms, "_http_get", lambda url, timeout=0.0: _bytes("ibtracs_idalia_2023_excerpt.csv"))
    return storms


def test_a_best_track_fix_keeps_every_ibtracs_column(monkeypatch):
    storms = _ibtracs(monkeypatch)
    found, names = storms._fetch_historical(bbox=_NA_BBOX, y0=2023, y1=2023, storm_name=None, geometry="points")
    assert names == ["IDALIA"] and len(found) == 9
    first, landing = found[0], found[5]
    assert first["properties"]["ISO_TIME"] == "2023-08-26 15:00:00" and first["properties"]["USA_RMW"] == 80.0
    assert first["properties"]["SID"] == "2023239N21274" and first["properties"]["USA_RECORD"] is None
    assert landing["properties"]["USA_RECORD"] == "L"
    assert {"DIST2LAND", "WMO_AGENCY", "TOKYO_LAT", "USA_R34_NE"} <= set(first["properties"])
    assert first["produced"]["wind_kt"] == 25.0 and first["produced"]["category_label"]
    assert "iso_time" not in first["properties"] and "rmw_nmi" not in first["properties"]
    _data, frame = _written(_spec("fetch_storm_tracks"), found)
    assert {"USA_ROCI", "NATURE", "wind_kt", "pres_mb", "category_label"} <= set(frame.columns)


def test_a_storm_line_carries_the_storms_own_fields_and_its_summary(monkeypatch):
    storms = _ibtracs(monkeypatch)
    (line,), _names = storms._fetch_historical(bbox=_NA_BBOX, y0=2023, y1=2023, storm_name=None, geometry="lines")
    assert (line["properties"]["SID"], line["properties"]["NAME"], line["properties"]["BASIN"]) == (
        "2023239N21274", "IDALIA", "NA")
    assert line["properties"]["SEASON"] == 2023
    assert line["produced"]["n_fixes"] == 9 and line["produced"]["max_wind_kt"] == 38.0
    assert "sid" not in line["properties"]


def test_the_active_storm_record_and_its_forecast_points_keep_what_nhc_serves():
    from trid3nt_server.tools.fetchers.weather.fetch_storm_tracks import hooks as storms

    body = json.dumps({"activeStorms": [{
        "id": "al092026", "binNumber": "AT4", "name": "Ida", "classification": "HU", "intensity": "85",
        "pressure": "970", "latitude": "25.5N", "longitude": "88.1W", "latitudeNumeric": 25.5,
        "longitudeNumeric": -88.1, "movementDir": 330, "movementSpeed": 12, "lastUpdate": "2026-09-01T15:00:00.000Z",
        "forecastTrack": {"zipFile": "https://www.nhc.noaa.gov/gis/forecast/archive/al092026_5day_latest.zip"}}]}).encode()
    parsed = storms._parse_current_storms(body)
    found = storms._point_features([storms._current_position(storm) for storm in parsed])
    assert found[0]["properties"]["binNumber"] == "AT4" and found[0]["properties"]["movementDir"] == 330
    assert found[0]["properties"]["forecastTrack"]["zipFile"].endswith("al092026_5day_latest.zip")
    assert found[0]["produced"] == {"tau_h": 0.0, "is_forecast": 0}
    assert found[0]["geometry"]["coordinates"] == [-88.1, 25.5]


# ---------------------------------------------------------------- WFIGS

def test_an_incident_record_keeps_every_field_wfigs_returns():
    from trid3nt_server.tools.fetchers.hazard.fetch_wfigs_incident import hooks as wfigs

    spec = _spec("fetch_wfigs_incident")
    record = wfigs.record(spec, {"incident_name": "Ranger Road"}, [_bytes("wfigs_year_to_date_features.json")])
    assert record["IncidentName"] == "Ranger Road" and record["IncidentSize"] == 283283
    assert record["IrwinID"] == "{22417EB2-8828-46A3-9D59-CCDB3BC07B51}" and record["POOCounty"] == "Beaver"
    assert record["FireDiscoveryDateTime"] == 1771348740000
    assert record["fire_discovery_datetime"] == "2026-02-17T17:19:00Z"
    assert (record["lon"], record["lat"]) == pytest.approx((-100.50389764359892, 36.810839561891484))
    assert "incident_size_acres" not in record and "irwin_id" not in record


# ---------------------------------------------------------------- eHydro

_PACKAGES = _FIX.parent / "ehydro"
_MLLW = "SP_01_SPH_20261015_CS_2026_180_HF"


class _Recorded:
    def __init__(self, *names):
        self._members = {n: (_PACKAGES / n).read_bytes() for n in names}

    def namelist(self):
        return list(self._members)

    def read(self, name):
        return self._members[name]


def test_an_ehydro_survey_keeps_its_index_record_and_every_sounding_its_point_record(monkeypatch):
    from trid3nt_server.tools.fetchers.hydrology.fetch_ehydro_surveys import hooks as eh

    spec = _spec("fetch_ehydro_surveys")
    index = json.loads(_bytes("ehydro_index_feature.json"))["features"]
    monkeypatch.setattr(eh, "_index", lambda *_a: index)
    monkeypatch.setattr(eh, "_selected", lambda *_a: index)
    monkeypatch.setattr(eh, "_package", lambda *_a: _Recorded(f"{_MLLW}.XML"))
    monkeypatch.setattr(eh, "_soundings", lambda *_a: gpd.read_file(_PACKAGES / f"{_MLLW}_SurveyPoint_head20.fgb"))
    rows = eh.read(spec, {"bbox": (-95.945, 41.19, -95.895, 41.265)}, timeout_s=60.0)
    survey = next(r for r in rows if r["produced"]["part"] == "survey")
    held = survey["properties"]["survey"]
    assert held["usacedistrictcode"] == "CENWO" and held["surveydateend"] == 1634910468000
    assert held["sdsfeaturename"] == "MO_11_OMM" and held["surveytype"] == "CS"
    assert survey["produced"]["survey_date"] == "2021-10-22" and survey["produced"]["sounding_count"] == 20
    sounding = next(r for r in rows if r["produced"]["part"] == "sounding")
    recorded = gpd.read_file(_PACKAGES / f"{_MLLW}_SurveyPoint_head20.fgb")
    assert sounding["properties"]["SurveyId"] == recorded.iloc[0]["SurveyId"]
    assert sounding["properties"]["elevationUOM"] == recorded.iloc[0]["elevationUOM"]
    assert sounding["properties"]["Z_depth"] == pytest.approx(float(recorded.iloc[0]["Z_depth"]))
    assert {"surveyPointElev", "Z_use", "Z_label", "channelAreaIDFK"} <= set(sounding["properties"])
    assert sounding["produced"]["vertical_datum"] == "MLLW" and sounding["produced"]["depth_below_datum_m"] is not None
    assert "district" not in survey["properties"] and "source_uom" not in survey["produced"]
    _data, frame = _written(spec, rows)
    assert {"survey.usacedistrictcode", "SurveyId", "Z_label", "depth_below_datum_m", "vertical_datum"} <= set(frame.columns)
    assert len({c.lower() for c in frame.columns}) == len(frame.columns)


# ---------------------------------------------------------------- NHD waterbody and NLDI

def _inside(body):
    from shapely.geometry import shape

    feature = json.loads(body)["features"][0]
    point = shape(feature["geometry"]).representative_point()
    return [point.x, point.y]


@pytest.mark.parametrize("name,mirror", [("nhd_waterbody_hr.json", "hr"), ("nhd_waterbody_medium.json", "medium")])
def test_a_waterbody_keeps_the_nhd_record_whole_under_one_spelling_of_its_names(name, mirror):
    from trid3nt_server.tools.fetchers.hydrology.fetch_nhd_waterbody_at_point import hooks as wb

    spec = _spec("fetch_nhd_waterbody_at_point")
    body = _bytes(name)
    (feature,) = wb.parse_response(spec, {"seed_point": _inside(body), "search_distance_km": 2.0}, [body])
    props = feature["properties"]
    assert props["permanent_identifier"] == "{1D017476-8ADD-42BA-832D-B4FB83B615D2}"
    assert props["reachcode"] == "10230006004224" and props["ftype"] == 390 and props["fcode"] == 39004
    assert {"fdate", "resolution", "elevation", "visibilityfilter", "shape_length", "shape_area"} <= set(props)
    if mirror == "hr":
        assert props["nhdplusid"] == 23000300049216
    else:
        assert props["globalid"] == "{3347744B-DC8B-4430-A670-287311452732}"
    assert feature["produced"]["part"] == "waterbody" and feature["produced"]["seed_distance_km"] == 0.0
    assert not (set(props) & set(feature["produced"]))


def test_a_flowline_keeps_the_properties_nldi_serves(monkeypatch):
    import dataretrieval.nldi as nldi

    from trid3nt_server.tools.fetchers.hydrology.fetch_nhdplus_nldi_navigate import hooks as walk

    served = json.loads(_bytes("nldi_flowlines_comid13294314.json"))
    monkeypatch.setattr(nldi, "_query_nldi", lambda url, query: served)
    monkeypatch.setattr(nldi, "_AVAILABLE_DATA_SOURCES", None)
    found = walk.features(_spec("fetch_nhdplus_nldi_navigate"), {"comid": 13294314, "direction": "DM", "distance_km": 3.0},
                          timeout_s=30.0)
    assert found[0]["properties"] == served["features"][0]["properties"] == {"nhdplus_comid": 13294314}


# ---------------------------------------------------------------- OSM

def test_a_river_way_keeps_every_osm_tag_it_carries(monkeypatch):
    from trid3nt_server.tools.fetchers.hydrology.fetch_river_geometry import hooks as rivers

    frame = _osm("waterway")
    monkeypatch.setattr(rivers, "overpass_features", lambda *_a, **_k: frame)
    found = rivers.delegate(_spec("fetch_river_geometry"), {"bbox": [-95.95, 41.23, -95.90, 41.27]}, timeout_s=1.0)
    assert found
    for feature in found:
        way = feature["produced"]["osm_id"]
        row = frame[frame.index.get_level_values("id") == way].iloc[0]
        assert feature["properties"] == _tags(row)
    assert any(len(f["properties"]) > 2 for f in found)


def test_a_building_keeps_every_osm_tag_and_states_its_ids(monkeypatch):
    from trid3nt_server.tools.fetchers.socioeconomic.fetch_buildings import hooks as buildings

    frame = _osm("buildings")
    monkeypatch.setattr(buildings, "overpass_features", lambda *_a, **_k: frame)
    found = buildings.features(_spec("fetch_buildings"), {"bbox": [-95.936, 41.255, -95.931, 41.259]}, timeout_s=1.0)
    assert found
    for feature in found:
        ident = feature["produced"]["osm_id"]
        element, _ = next(k for k in frame.index if k[1] == ident)
        row = frame.loc[(element, ident)]
        assert feature["properties"] == _tags(row)
        assert feature["produced"] == {"osm_id": ident, "osm_type": element, "fid": f"{element[:1]}{ident}"}
    assert any(len(f["properties"]) > 1 for f in found)


def test_osm_roads_points_and_edges_keep_their_tags(monkeypatch):
    from trid3nt_server.tools.fetchers.socioeconomic.fetch_osm_features import hooks as osmf

    spec = _spec("fetch_osm_features")
    roads = _osm("roads")
    monkeypatch.setattr(osmf, "overpass_features", lambda *_a, **_k: roads)
    found = osmf.delegate(spec, {"feature": "roads", "bbox": [-95.936, 41.255, -95.931, 41.259],
                                 "road_classes": ["primary", "secondary", "residential"]}, timeout_s=1.0)
    assert found and "tiger:cfcc" in {k for f in found for k in f["properties"]}
    assert all("lanes" not in f["produced"] and "maxspeed" not in f["produced"] for f in found)
    buildings = _osm("buildings")
    monkeypatch.setattr(osmf, "overpass_features", lambda *_a, **_k: buildings)
    points = osmf.delegate(spec, {"feature": "pois", "tag": "building=yes", "bbox": [-95.936, 41.255, -95.931, 41.259]},
                           timeout_s=1.0)
    assert points[0]["produced"].keys() == {"osm_id", "osm_type", "key", "value"}
    assert "tags_json" not in points[0]["produced"] and "building" in points[0]["properties"]
    coast = _osm("coast")
    monkeypatch.setattr(osmf, "overpass_features", lambda *_a, **_k: coast)
    edge = osmf.delegate(spec, {"feature": "coastline", "bbox": [-70.26, 43.64, -70.24, 43.66]}, timeout_s=1.0)
    assert edge[0]["properties"]["natural"] == "coastline"
    assert set(edge[0]["produced"]) == {"osm_id"}


def test_the_osm_source_declares_no_column_projection():
    assert "properties_by_param" not in (_spec("fetch_osm_features").ingest or {})


# ---------------------------------------------------------------- field boundaries

def test_a_field_keeps_every_column_of_the_fiboa_file(monkeypatch):
    import fsspec

    from trid3nt_server.tools.fetchers.socioeconomic.fetch_field_boundaries import hooks as fields

    spec = _spec("fetch_field_boundaries")

    class _Fs:
        def open(self, url):
            return open(_FIX / "fiboa_us_cropland_omaha.parquet", "rb")

    monkeypatch.setattr(fsspec, "filesystem", lambda *_a, **_k: _Fs())
    found = fields.read_fields(spec, {"bbox": [-95.99, 41.20, -95.90, 41.29], "dataset": "us_usda_cropland"}, timeout_s=30.0)
    assert len(found) == 3
    props = found[0]["properties"]
    assert {"id", "crop:code", "crop:name", "administrative_area_level_2", "determination_datetime", "crop:code_list"} <= set(props)
    assert "crop_name" not in props


# ---------------------------------------------------------------- usSEABED

def test_a_seabed_sample_keeps_every_column_with_the_services_sentinels_null():
    from trid3nt_server.tools.fetchers.ocean.fetch_usseabed import hooks as seabed

    spec = _spec("fetch_usseabed")
    found = seabed.parse_response(spec, {}, [_bytes("usseabed_chesapeake_ext_excerpt.zip")])
    assert len(found) == 12
    first = found[0]["properties"]
    assert first["locnname"] == "VA93-627" and first["sand"] == 8 and first["mud"] == 92
    assert first["clay"] is None and first["grainsze"] is None and first["waterdepth"] == 19
    assert first["facies"] is None and first["datatypes"] == "TXR.CMP.ACU"
    assert {"datasetkey", "locnkey", "obsvnkey", "device", "orgcarbn", "porosity", "geolage", "datesrc"} <= set(first)
    assert "gravel_pct" not in first and "waterdepth_m" not in first and "geom" not in first
    assert spec.fields["value"].field == "grainsze"


# ---------------------------------------------------------------- bed material

def test_a_bed_sample_keeps_the_station_record_and_every_result_row(monkeypatch):
    from trid3nt_server.tools.fetchers._router.hooks import dataretrieval
    from trid3nt_server.tools.fetchers.hydrology.fetch_nwis_bed_material import hooks as bed

    spec = _spec("fetch_nwis_bed_material")
    sites = pd.read_csv(_FIX / "wqp_bed_material_sites.csv")
    results = pd.read_csv(_FIX / "wqp_bed_material_results.csv")
    monkeypatch.setattr(dataretrieval, "retrieve",
                        lambda _spec, fn, **_k: ((sites if fn.__name__ == "what_sites" else results), None))
    (feature,) = bed.read(spec, {"bbox": [-121.4, 37.9, -121.2, 38.1], "characteristic": "Bed sediment particle size"},
                          timeout_s=30.0)
    props = feature["properties"]
    assert props["MonitoringLocationIdentifier"] == sites.iloc[0]["MonitoringLocationIdentifier"]
    assert props["HUCEightDigitCode"] and "ProviderName" in props
    held = props["results"]
    assert len(held) == len(results) and held[0]["ResultParticleSizeBasisText"] == "< 2 mm"
    assert "DataQuality/PrecisionValue" in held[0]
    produced = feature["produced"]
    assert produced["pct_finer_2mm"] is not None and produced["n_size_classes"] > 5
    assert produced["vertical_datum"] == sites.iloc[0]["VerticalCoordinateReferenceSystemDatumName"]
    assert "site_id" not in props and "huc8" not in produced


# ---------------------------------------------------------------- LTER

def test_an_lter_entity_keeps_every_column_in_the_window():
    """The entity is the documented delimited shape stated by hand: EDI's service answers no public request from this host."""
    from trid3nt_server.tools.fetchers.hydrology.fetch_lter_records import hooks as lter

    spec = _spec("fetch_lter_records")
    text = "\n".join(["Date\tSite\tDischarge\tDischarge_flag\tNote",
                      "2020-01-01\tA\t1.5\tok\tfirst", "2020-01-02\tA\t2.5\tok\t", "2020-01-03\tB\tNA\tbad\tlast"])
    record = lter.build_record(spec, {"package_id": "knb-lter-cwt.3037.19", "_delimiter": "\t", "_skip": 1,
                                      "_units": json.dumps({"Discharge": "m3/s"}), "_entity_name": "q"}, [text.encode()])
    assert record["numeric_columns"] == ["Discharge"] and record["series"]["Discharge"] == [1.5, 2.5, None]
    assert record["series"]["Site"] == ["A", "A", "B"] and record["series"]["Discharge_flag"] == ["ok", "ok", "bad"]
    assert record["series"]["Note"] == ["first", None, "last"]
    assert "Date" not in record["series"] and record["times"] == ["2020-01-01", "2020-01-02", "2020-01-03"]


# ---------------------------------------------------------------- epicentre and moment tensor

def test_the_epicentre_record_keeps_every_event_property_and_the_origin_products():
    from trid3nt_server.tools.fetchers.hazard.fetch_earthquake_epicentre import hooks as epicentre

    record = epicentre.record(_spec("fetch_earthquake_epicentre"), {"event_id": "us6000qw60"},
                              [_bytes("comcat_event_us6000qw60.json")])
    assert (record["mag"], record["magType"], record["status"], record["alert"], record["sig"]) == (8.8, "mww", "reviewed", "red", 2114)
    assert record["time"] == 1753831492483 and record["origin_time"] == "2025-07-29T23:24:52.483000Z"
    assert (record["longitude"], record["latitude"], record["depth_km"]) == (160.2395, 52.4948, 35.0)
    origin = record["products"]["origin"][0]["properties"]
    assert "vertical-error" in origin and "horizontal-error" in origin
    assert record["event_id"] == "us6000qw60" and "magnitude" not in record and "magnitude_type" not in record


def test_a_moment_tensor_solution_keeps_its_product_and_the_numbers_read_off_it():
    from trid3nt_server.tools.fetchers.hazard.fetch_moment_tensor import hooks as tensor

    record = tensor.record(_spec("fetch_moment_tensor"), {"event_id": "us6000qw60"}, [_bytes("comcat_event_us6000qw60.json")])
    (solution,) = record["solutions"]
    assert solution["properties"]["derived-magnitude-type"] and solution["updateTime"] > 0
    assert solution["status"] and "contents" in solution and solution["type"] == "moment-tensor"
    assert solution["scalar_moment_nm"] == float(solution["properties"]["scalar-moment"])
    assert len(solution["planes"]) == 2 and solution["centroid"]["depth_km"] == float(solution["properties"]["derived-depth"])
    assert "updated_ms" not in solution and "magnitude" not in solution and "duration_s" not in solution


# ---------------------------------------------------------------- FEMA declarations

def test_the_declarations_are_returned_whole_with_no_county_aggregate():
    from trid3nt_server.tools.fetchers.hazard.fetch_openfema_disasters import hooks as fema

    spec = _spec("fetch_openfema_disasters")
    found = fema.parse_response(spec, {"state_code": "RI"}, [_bytes("openfema_declarations_ri.json")])
    raw = json.loads(_bytes("openfema_declarations_ri.json"))["DisasterDeclarationsSummaries"]
    assert len(found) == len(raw) == 6
    assert [f["properties"] for f in found] == raw
    assert all(f["geometry"] is None and not f.get("produced") for f in found)
    assert found[0]["properties"]["designatedArea"].startswith("Providence")
    assert "enrich" not in spec.ingest and spec.output.keep_null_geometry is True
    _data, frame = _written(spec, found)
    assert len(frame) == 6 and {"femaDeclarationString", "hash", "lastRefresh", "fipsCountyCode"} <= set(frame.columns)
