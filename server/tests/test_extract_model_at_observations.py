"""Offline unit tests for ``extract_model_at_observations`` (no network).

Mode A (static raster vs observation points) inputs are SYNTHESIZED locally: a
UTM linear-ramp raster (so bilinear sampling reduces to an exact value) + a
small EPSG:4326 point GeoJSON of surveyed observations with HWM-shaped
attributes (``elev_ft`` / ``vertical_datum`` / ``hwm_id`` / ``survey_date``).
Mode B (time-series) inputs are two synthesized point FlatGeobufs each carrying
an inline ``time_series_csv``. Mirrors the ``test_compute_model_residuals.py``
helper pattern.
"""

from __future__ import annotations

import json
import pathlib

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from shapely.geometry import Point

from trid3nt_contracts.execution import LayerURI
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.extract_model_at_observations import (
    PairedObsLayerURI,
    PairingDatumMismatchError,
    PairingInputError,
    PairingNoPairsError,
    extract_model_at_observations,
)

# Synthetic model grid: 40x40 at 30 m, UTM 11N, head = BASE + SLOPE*col.
N = 40
RES = 30.0
X0, Y0 = 500000.0, 4000000.0
CRS = "EPSG:32611"
NODATA = -9999.0
BASE = 100.0
SLOPE = 0.5


def _cell_center(col: int, row: int) -> tuple[float, float]:
    return X0 + (col + 0.5) * RES, Y0 + (N - row - 0.5) * RES


def _utm_to_lonlat(x: float, y: float) -> tuple[float, float]:
    import pyproj

    tf = pyproj.Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    return tf.transform(x, y)


def _write_ramp_raster(path: str, nodata_cells=None) -> str:
    data = np.zeros((N, N), dtype="float64")
    for row in range(N):
        for col in range(N):
            data[row, col] = BASE + SLOPE * col
    for row, col in nodata_cells or []:
        data[row, col] = NODATA
    transform = from_bounds(X0, Y0, X0 + N * RES, Y0 + N * RES, N, N)
    with rasterio.open(
        path, "w", driver="GTiff", height=N, width=N, count=1, dtype="float64",
        crs=CRS, transform=transform, nodata=NODATA,
    ) as dst:
        dst.write(data, 1)
    return path


def _write_constant_raster_4326(path: str, bbox, value: float, n: int = 60) -> str:
    """A constant-value single-band raster in EPSG:4326 over ``bbox`` (metres
    units assumed, matching the model convention) for the STN pairing test."""
    w, s, e, nn = bbox
    data = np.full((n, n), value, dtype="float64")
    transform = from_bounds(w, s, e, nn, n, n)
    with rasterio.open(
        path, "w", driver="GTiff", height=n, width=n, count=1, dtype="float64",
        crs="EPSG:4326", transform=transform, nodata=NODATA,
    ) as dst:
        dst.write(data, 1)
    return path


def _stn_hwm_layer(path: str):
    """Load the committed USGS STN Hurricane Michael HWM fixture (elev_ft in
    FEET, e.g. 5.64) into an EPSG:4326 point GeoJSON for pairing."""
    fixture = (
        pathlib.Path(__file__).parent / "fixtures" / "validation" / "stn"
        / "michael_2018_filtered_hwms.json"
    )
    records = json.loads(fixture.read_text())
    gdf = gpd.GeoDataFrame(
        {
            "hwm_id": [str(r["hwm_id"]) for r in records],
            "elev_ft": [float(r["elev_ft"]) for r in records],
            "verticalDatumName": [r.get("verticalDatumName") for r in records],
        },
        geometry=[Point(float(r["longitude"]), float(r["latitude"])) for r in records],
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="GeoJSON")
    return path, records


def _write_obs(path: str, records) -> str:
    features = []
    for rec in records:
        rec = dict(rec)
        col, row = rec.pop("col"), rec.pop("row")
        lon, lat = _utm_to_lonlat(*_cell_center(col, row))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": rec,
            }
        )
    pathlib.Path(path).write_text(
        json.dumps({"type": "FeatureCollection", "features": features})
    )
    return path


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_registered() -> None:
    entry = TOOL_REGISTRY["extract_model_at_observations"]
    assert entry.fn is extract_model_at_observations
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.open_world_hint is False


# ---------------------------------------------------------------------------
# Mode A -- static raster vs observation points.
# ---------------------------------------------------------------------------


def test_static_pairing_and_interop(tmp_path) -> None:
    # ``elev_m`` (metres) isolates the pairing mechanics from unit conversion,
    # so the observed-minus-simulated invariant stays a clean +2.0.
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "hwm_id": "H1", "elev_m": BASE + SLOPE * 5 + 2.0,
             "vertical_datum": "NAVD88", "survey_date": "2018-10-12"},
            {"col": 10, "row": 20, "hwm_id": "H2", "elev_m": BASE + SLOPE * 10 + 2.0,
             "vertical_datum": "NAVD88", "survey_date": "2018-10-12"},
            {"col": 15, "row": 20, "hwm_id": "H3", "elev_m": BASE + SLOPE * 15 + 2.0,
             "vertical_datum": "NAVD88", "survey_date": "2018-10-12"},
        ],
    )
    result = extract_model_at_observations(
        model_layer_uri=raster,
        observations_layer_uri=obs,
        model_datum="NAVD88",
        _output_dir=str(tmp_path),
    )
    assert isinstance(result, PairedObsLayerURI)
    assert isinstance(result, LayerURI)
    assert result.layer_type == "vector"
    assert result.style_preset == "model_obs_pairs"
    assert result.name == "Model-obs pairs (3 points)"
    assert result.n_paired == 3
    assert result.n_dropped == 0
    assert result.paired_table_uri == result.uri
    assert result.alignment["temporal"] == "none_static"
    assert result.alignment["datum"] == "NAVD88"
    assert "no conversion" in result.alignment["units"]  # elev_m -> metres
    assert "32611" in result.alignment["crs"] and "EPSG:4326" in result.alignment["crs"]

    # Lane-B interop: the exact columns compute_skill_metrics reads.
    gdf = gpd.read_file(result.uri)
    assert {"obs_id", "observed", "simulated", "time"}.issubset(gdf.columns)
    assert set(gdf["obs_id"]) == {"H1", "H2", "H3"}
    # observed - simulated == +2.0 at every colinear point.
    diff = gdf["observed"].to_numpy() - gdf["simulated"].to_numpy()
    assert np.allclose(diff, 2.0, atol=1e-3)
    assert [str(t)[:10] for t in gdf["time"]] == ["2018-10-12"] * 3


def test_datum_shift_applied(tmp_path) -> None:
    # ``elev_m`` (metres) isolates the datum shift from unit conversion.
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [{"col": 10, "row": 20, "hwm_id": "H1", "elev_m": BASE + SLOPE * 10,
          "vertical_datum": "NGVD29"}],
    )
    result = extract_model_at_observations(
        model_layer_uri=raster, observations_layer_uri=obs,
        model_datum="NAVD88", datum_shift_m=0.5, _output_dir=str(tmp_path),
    )
    gdf = gpd.read_file(result.uri)
    # observed was shifted +0.5; simulated == BASE+SLOPE*10, so diff == +0.5.
    assert float(gdf["observed"].iloc[0] - gdf["simulated"].iloc[0]) == pytest.approx(0.5, abs=1e-3)
    assert "shift" in result.alignment["datum"]
    assert "0.5" in result.units_warning or "shift" in result.units_warning.lower()


def test_feet_converted_to_meters(tmp_path) -> None:
    # ``elev_ft`` is feet -> converted ft->m x0.3048 at ingestion (the model
    # raster is metres), and the conversion is recorded in alignment.units.
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [{"col": 10, "row": 20, "hwm_id": "H1", "elev_ft": 30.0}],
    )
    result = extract_model_at_observations(
        model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
    )
    gdf = gpd.read_file(result.uri)
    assert float(gdf["observed"].iloc[0]) == pytest.approx(30.0 * 0.3048, abs=1e-4)
    assert "ft->m" in result.alignment["units"] and "0.3048" in result.alignment["units"]


def test_observed_units_override(tmp_path) -> None:
    # An explicit observed_units='feet' forces conversion even for a
    # metre-named field, and is recorded in the alignment block.
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [{"col": 10, "row": 20, "hwm_id": "H1", "elev_m": 30.0}],
    )
    result = extract_model_at_observations(
        model_layer_uri=raster, observations_layer_uri=obs,
        observed_units="feet", _output_dir=str(tmp_path),
    )
    gdf = gpd.read_file(result.uri)
    assert float(gdf["observed"].iloc[0]) == pytest.approx(30.0 * 0.3048, abs=1e-4)
    assert "feet" in result.alignment["units"]


def test_ambiguous_observed_units_raises(tmp_path) -> None:
    # ``water_level`` carries no unit tell -> a typed error, never a silent
    # feet-vs-metres guess; an explicit observed_units resolves it.
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [{"col": 10, "row": 20, "hwm_id": "H1", "water_level": BASE + SLOPE * 10}],
    )
    with pytest.raises(PairingInputError):
        extract_model_at_observations(
            model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
        )
    result = extract_model_at_observations(
        model_layer_uri=raster, observations_layer_uri=obs,
        observed_units="meters", _output_dir=str(tmp_path),
    )
    assert result.n_paired == 1
    assert "no conversion" in result.alignment["units"]


def test_datum_mismatch_raises(tmp_path) -> None:
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [{"col": 10, "row": 20, "hwm_id": "H1", "elev_ft": 105.0,
          "vertical_datum": "NGVD29"}],
    )
    with pytest.raises(PairingDatumMismatchError):
        extract_model_at_observations(
            model_layer_uri=raster, observations_layer_uri=obs,
            model_datum="NAVD88", _output_dir=str(tmp_path),
        )


def test_datum_assumed_match_warns(tmp_path) -> None:
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [{"col": 10, "row": 20, "hwm_id": "H1", "elev_ft": 105.0}],
    )
    result = extract_model_at_observations(
        model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
    )
    assert result.alignment["datum"] == "assumed_match"
    assert result.units_warning  # never empty


def test_nearest_wet_cell_snap(tmp_path) -> None:
    # Obs point sits on a nodata (dry) cell (20,10); the neighbor (20,9)/(20,11)
    # is wet within tolerance -> snapped, not dropped.
    raster = _write_ramp_raster(str(tmp_path / "head.tif"), nodata_cells=[(20, 10)])
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [{"col": 10, "row": 20, "hwm_id": "H1", "elev_ft": 120.0}],
    )
    result = extract_model_at_observations(
        model_layer_uri=raster, observations_layer_uri=obs,
        nearest_wet_tolerance_m=100.0, _output_dir=str(tmp_path),
    )
    assert result.n_paired == 1
    assert "nearest wet cell" in result.alignment["spatial"]


def test_nodata_beyond_tolerance_dropped(tmp_path) -> None:
    # A whole nodata block around the point; no wet cell within a tiny tolerance.
    holes = [(r, c) for r in range(18, 23) for c in range(8, 13)]
    raster = _write_ramp_raster(str(tmp_path / "head.tif"), nodata_cells=holes)
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 10, "row": 20, "hwm_id": "DRY", "elev_ft": 120.0},
            {"col": 30, "row": 20, "hwm_id": "WET", "elev_ft": BASE + SLOPE * 30 + 1.0},
        ],
    )
    result = extract_model_at_observations(
        model_layer_uri=raster, observations_layer_uri=obs,
        nearest_wet_tolerance_m=10.0, _output_dir=str(tmp_path),
    )
    assert result.n_paired == 1
    assert {"obs_id": "DRY", "reason": "nodata_sample"} in result.dropped


def test_outside_footprint_dropped(tmp_path) -> None:
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 10, "row": 20, "hwm_id": "IN", "elev_ft": BASE + SLOPE * 10 + 1.0},
            {"col": -5000, "row": -5000, "hwm_id": "OUT", "elev_ft": 1.0},
        ],
    )
    result = extract_model_at_observations(
        model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
    )
    assert result.n_paired == 1
    assert any(d["obs_id"] == "OUT" and d["reason"] == "outside_footprint" for d in result.dropped)


def test_no_pairs_raises(tmp_path) -> None:
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [{"col": -5000, "row": -5000, "hwm_id": "OUT", "elev_ft": 1.0}],
    )
    with pytest.raises(PairingNoPairsError):
        extract_model_at_observations(
            model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
        )


def test_missing_observed_field_raises(tmp_path) -> None:
    raster = _write_ramp_raster(str(tmp_path / "head.tif"))
    obs = _write_obs(
        str(tmp_path / "obs.geojson"),
        [{"col": 10, "row": 20, "hwm_id": "H1", "note": "no numeric obs here"}],
    )
    with pytest.raises(PairingInputError):
        extract_model_at_observations(
            model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
        )


def test_stn_hwm_feet_converted(tmp_path) -> None:
    # The committed USGS STN HWM fixture carries elev_ft in FEET (e.g. 5.64).
    # Paired against a metre-unit model raster, every observed value MUST come
    # back converted to metres (elev_ft * 0.3048), NOT the raw foot value.
    obs, records = _stn_hwm_layer(str(tmp_path / "hwm.geojson"))
    raster = _write_constant_raster_4326(
        str(tmp_path / "model.tif"), (-86.0, 29.0, -82.8, 30.3), value=2.0
    )
    result = extract_model_at_observations(
        model_layer_uri=raster,
        observations_layer_uri=obs,
        model_datum="NAVD88",  # matches the fixture verticalDatumName -> no shift
        _output_dir=str(tmp_path),
    )
    assert "ft->m" in result.alignment["units"]
    assert "0.3048" in result.alignment["units"]
    assert result.n_paired == len(records)

    gdf = gpd.read_file(result.uri)
    got = dict(zip(gdf["obs_id"].astype(str), gdf["observed"].astype(float)))
    for r in records:
        oid = str(r["hwm_id"])
        assert got[oid] == pytest.approx(float(r["elev_ft"]) * 0.3048, abs=1e-4)
    # Converted metres are ~1.7-3.5 m; a raw-foot bug would leave 5.5-11.3 ft.
    assert all(v < 4.0 for v in got.values())


def test_bad_uri_raises(tmp_path) -> None:
    with pytest.raises(PairingInputError):
        extract_model_at_observations(
            model_layer_uri="", observations_layer_uri="x", _output_dir=str(tmp_path)
        )


# ---------------------------------------------------------------------------
# Mode B -- time-series model vs time-series observations.
# ---------------------------------------------------------------------------


def _write_ts_layer(path: str, lon: float, lat: float, series, obs_id: str) -> str:
    csv = "".join(f"{t},{v}\n" for t, v in series)
    gdf = gpd.GeoDataFrame(
        {"site_no": [obs_id], "time_series_csv": [csv]},
        geometry=[Point(lon, lat)], crs="EPSG:4326",
    )
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")
    return path


def test_timeseries_pairing(tmp_path) -> None:
    lon, lat = -85.0, 30.0
    model = _write_ts_layer(
        str(tmp_path / "model.fgb"), lon, lat,
        [("2018-10-10T00:00:00", 1.0), ("2018-10-10T01:00:00", 2.0),
         ("2018-10-10T02:00:00", 3.0)],
        "S1",
    )
    obs = _write_ts_layer(
        str(tmp_path / "obs.fgb"), lon + 0.0005, lat,
        [("2018-10-10T00:00:00", 1.1), ("2018-10-10T01:00:00", 2.1),
         ("2018-10-10T09:00:00", 9.9)],  # last has no model match within 1h
        "S1",
    )
    result = extract_model_at_observations(
        model_layer_uri=model, observations_layer_uri=obs,
        station_tolerance_m=200.0, time_tolerance_s=3600.0, _output_dir=str(tmp_path),
    )
    assert result.n_paired == 2  # two exact matches; third dropped
    assert any(d["reason"] == "no_time_match" for d in result.dropped)
    assert result.alignment["temporal"] in ("exact", "nearest_within_tolerance:3600")
    # FIX 4b: mode B has its own station_tolerance_m, recorded in the block.
    assert result.alignment["station_tolerance_m"] == 200.0
    assert "200" in result.alignment["spatial"]
    gdf = gpd.read_file(result.uri)
    assert {"obs_id", "observed", "simulated", "time"}.issubset(gdf.columns)
    assert len(gdf) == 2


def test_station_tolerance_excludes_far_station(tmp_path) -> None:
    # A model station >500 m (the default) from the observed station is not a
    # match -> dropped outside_footprint, proving mode B no longer reuses the
    # 250 m nearest_wet_tolerance_m as its station radius.
    lon, lat = -85.0, 30.0
    model = _write_ts_layer(
        str(tmp_path / "model.fgb"), lon + 0.01, lat,  # ~965 m east
        [("2018-10-10T00:00:00", 1.0), ("2018-10-10T01:00:00", 2.0)], "S1",
    )
    obs = _write_ts_layer(
        str(tmp_path / "obs.fgb"), lon, lat,
        [("2018-10-10T00:00:00", 1.1), ("2018-10-10T01:00:00", 2.1)], "S1",
    )
    with pytest.raises(PairingNoPairsError):
        extract_model_at_observations(
            model_layer_uri=model, observations_layer_uri=obs,
            station_tolerance_m=500.0, _output_dir=str(tmp_path),
        )
