"""Per-source replication drivers (router-pilot-contract sec 4.2).

Each ``run_<source>`` runs the TWIN and the ROUTER over the SAME fixed request
with IDENTICAL synthetic upstream and records a ``SourceResult`` of field-by-field
envelope checks + a forced upstream-failure comparison. Twin behavior is the
contract; divergences are recorded, never fudged.
"""

from __future__ import annotations

import json
import math
from typing import Any
from unittest import mock

from harness import (
    FakeResp,
    SourceResult,
    err_frame,
    load_specs,
    raster_stats,
    route_layer,
    vector_info,
    _make_stub_read_through,
    _patched,
)

# Twin modules.
from trid3nt_server.agent.tools.fetchers.climate.fetch_gridmet import fetch_gridmet as gridmet_mod
from trid3nt_server.agent.tools.fetchers.hazard.fetch_hifld_critical_infrastructure import (
    fetch_hifld_critical_infrastructure as hifld_mod,
)
from trid3nt_server.agent.tools.fetchers.ocean.fetch_noaa_coops_tides import (
    fetch_noaa_coops_tides as coops_mod,
)
from trid3nt_server.agent.tools.fetchers.terrain.fetch_esri_landcover_10m import (
    fetch_esri_landcover_10m as esri_mod,
)
from trid3nt_server.agent.tools.fetchers.socioeconomic.fetch_census_acs import (
    fetch_census_acs as census_mod,
)

# Router executor modules (for the esri capability probe + census join patch).
from trid3nt_server.agent.tools.fetchers._router.executors import raster_cog as router_raster
from trid3nt_server.agent.tools.fetchers._router.transforms import join as router_join


def _run_twin(mod, fn_name: str, kwargs: dict, sink: dict):
    """Call a twin's registered fn with its read_through stubbed (offline)."""
    stub = _make_stub_read_through(sink)
    with _patched(mock.patch.object(mod, "read_through", stub)):
        return getattr(mod, fn_name)(**kwargs)


def _cmp_layer(res: SourceResult, twin_layer, router_layer) -> None:
    """Compare the shared LayerURI output fields (contract: layer-output)."""
    res.add("layer.type", twin_layer.layer_type == router_layer.layer_type,
            twin_layer.layer_type, router_layer.layer_type)
    res.add("layer.style_preset", twin_layer.style_preset == router_layer.style_preset,
            twin_layer.style_preset, router_layer.style_preset)
    res.add("layer.role", twin_layer.role == router_layer.role,
            twin_layer.role, router_layer.role)
    res.add("layer.units", twin_layer.units == router_layer.units,
            twin_layer.units, router_layer.units)
    # bbox present/absent is a contract-4.2 LAYER-OUTPUT field (GATING). gridmet's
    # twin omits LayerURI.bbox; the router honors output.emit_bbox=false to match.
    tb = twin_layer.bbox is not None
    rb = router_layer.bbox is not None
    res.add("layer.bbox_present", tb == rb, tb, rb,
            note="" if tb == rb else "router LayerURI.bbox presence != twin (emit_bbox mismatch)")


def _close(a, b, tol=1e-3) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _cmp_raster_values(res: SourceResult, tw: dict, rt: dict) -> None:
    res.add("values.band_count", tw["band_count"] == rt["band_count"], tw["band_count"], rt["band_count"])
    res.add("values.dtype", tw["dtype"] == rt["dtype"], tw["dtype"], rt["dtype"])
    res.add("values.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"])
    # nodata: the router honors the twin's DECLARED nodata=float("nan"); the twin's
    # rioxarray to_raster silently DROPS the nodata= kwarg (rioxarray quirk -- it
    # needs rio.write_nodata()), emitting None. This IS an observable COG-metadata
    # difference (nodata tag set vs unset), so it is scored honestly as ok=False --
    # NOT the old hard-coded PASS the panel refuted. It is marked ``reported``
    # (non-gating) because the ROOT CAUSE is a twin-writer defect the router must
    # NOT copy (standing directive: flag for NATE, never silently propagate a bug).
    tw_nod, rt_nod = tw["nodata"], rt["nodata"]
    if str(tw_nod) == str(rt_nod):
        res.add("values.nodata", True, tw_nod, rt_nod)
    elif tw_nod is None and str(rt_nod) == "nan":
        res.add("values.nodata", False, tw_nod, rt_nod, reported=True,
                note="TWIN DEFECT (flag NATE): twin rioxarray writer drops declared nodata=nan -> None; "
                     "router correctly writes nodata=nan. Router must NOT copy the twin bug; byte-parity "
                     "needs a twin fix (rio.write_nodata).")
    else:
        res.add("values.nodata", False, tw_nod, rt_nod)
    res.add("values.min", _close(tw["min"], rt["min"]), tw["min"], rt["min"])
    res.add("values.max", _close(tw["max"], rt["max"]), tw["max"], rt["max"])
    res.add("values.mean", _close(tw["mean"], rt["mean"]), tw["mean"], rt["mean"])
    # bounds: cell-center (rioxarray) vs edge (from_bounds) can differ by a half
    # cell; tolerate <=1 native cell, note otherwise.
    tbd, rbd = tw["bounds"], rt["bounds"]
    maxdiff = max(abs(a - b) for a, b in zip(tbd, rbd))
    res.add("values.bounds", maxdiff <= 0.5, tbd, rbd,
            note="" if maxdiff <= 0.5 else f"bounds differ by {maxdiff:.3f} deg")


# --------------------------------------------------------------------------- #
# 1. gridmet -- raster-cog / OPeNDAP.
# --------------------------------------------------------------------------- #


def _synthetic_gridmet_ds():
    import numpy as np
    import xarray as xr

    day = np.array(["2022-08-01", "2022-08-02", "2022-08-03"], dtype="datetime64[D]")
    lat = np.array([34.5, 34.0, 33.5])   # descending (gridMET native)
    lon = np.array([-117.5, -117.0, -116.5])
    vals = np.arange(27, dtype="float32").reshape(3, 3, 3)
    return xr.Dataset(
        {"dead_fuel_moisture_100hr": (("day", "lat", "lon"), vals)},
        coords={"day": day, "lat": lat, "lon": lon},
    )


def run_gridmet(specs) -> SourceResult:
    res = SourceResult("fetch_gridmet")
    spec = specs["fetch_gridmet"]
    bbox = [-117.5, 33.5, -116.5, 34.5]
    kwargs = dict(bbox=tuple(bbox), variable="fm100",
                  start_date="2022-08-01", end_date="2022-08-03")
    params = dict(bbox=bbox, variable="fm100", start_date="2022-08-01", end_date="2022-08-03")
    try:
        ds = _synthetic_gridmet_ds()
        with _patched(mock.patch("xarray.open_dataset", lambda *a, **k: ds.copy(deep=True))):
            tw_sink, rt_sink = {}, {}
            twin_layer = _run_twin(gridmet_mod, "fetch_gridmet", kwargs, tw_sink)
            router_layer = route_layer(spec, params, rt_sink)
        _cmp_raster_values(res, raster_stats(tw_sink["bytes"]), raster_stats(rt_sink["bytes"]))
        _cmp_layer(res, twin_layer, router_layer)
        res.add("caveats.reproduced",
                any("CONUS" in c for c in spec.caveats) and any("EMPTY" in c for c in spec.caveats),
                note="spec carries CONUS-gate + typed-empty honesty")
        # Forced upstream failure: DAP open raises.
        def _boom(*a, **k):
            raise OSError("THREDDS DAP unreachable")
        with _patched(mock.patch("xarray.open_dataset", _boom)):
            tw_err = _capture_err(_run_twin, gridmet_mod, "fetch_gridmet", kwargs, {})
            rt_err = _capture_err(route_layer, spec, params, {})
        _cmp_error(res, "error.upstream", tw_err, rt_err)

        # --- edge matrix (contract 4.2: invalid-param classes + gates + empty) ---
        def _edge(name, k, p):
            tw = _capture_err(_run_twin, gridmet_mod, "fetch_gridmet", k, {})
            rt = _capture_err(route_layer, spec, p, {})
            _cmp_error(res, name, tw, rt)

        v = dict(variable="fm100", start_date="2022-08-01", end_date="2022-08-03")
        # malformed bbox (degenerate; in-CONUS lon so bbox-class, not conus)
        _edge("error.bad_bbox", dict(bbox=(-100.0, 40.0, -100.0, 41.0), **v),
              dict(bbox=[-100.0, 40.0, -100.0, 41.0], **v))
        # bad enum (variable)
        _edge("error.bad_enum", dict(bbox=(-117.5, 33.5, -116.5, 34.5), variable="banana",
                                     start_date="2022-08-01", end_date="2022-08-03"),
              dict(bbox=[-117.5, 33.5, -116.5, 34.5], variable="banana",
                   start_date="2022-08-01", end_date="2022-08-03"))
        # date order (start > end) -> INPUT_ERROR
        _edge("error.date_order", dict(bbox=(-117.5, 33.5, -116.5, 34.5), variable="fm100",
                                       start_date="2022-08-05", end_date="2022-08-01"),
              dict(bbox=[-117.5, 33.5, -116.5, 34.5], variable="fm100",
                   start_date="2022-08-05", end_date="2022-08-01"))
        # date range over the 366-day cap -> INPUT_ERROR
        _edge("error.date_range", dict(bbox=(-117.5, 33.5, -116.5, 34.5), variable="fm100",
                                       start_date="2020-01-01", end_date="2021-06-01"),
              dict(bbox=[-117.5, 33.5, -116.5, 34.5], variable="fm100",
                   start_date="2020-01-01", end_date="2021-06-01"))
        # coverage: before the 1979 start -> NOT_AVAILABLE
        _edge("error.date_before_coverage", dict(bbox=(-117.5, 33.5, -116.5, 34.5), variable="fm100",
                                                 start_date="1850-01-01", end_date="1850-01-05"),
              dict(bbox=[-117.5, 33.5, -116.5, 34.5], variable="fm100",
                   start_date="1850-01-01", end_date="1850-01-05"))
        # coverage: end beyond real time -> NOT_AVAILABLE
        _edge("error.date_future", dict(bbox=(-117.5, 33.5, -116.5, 34.5), variable="fm100",
                                        start_date="2020-06-01", end_date="2999-06-01"),
              dict(bbox=[-117.5, 33.5, -116.5, 34.5], variable="fm100",
                   start_date="2020-06-01", end_date="2999-06-01"))
        # conus gate: bbox in Europe -> INPUT_ERROR
        _edge("gate.conus", dict(bbox=(2.0, 48.0, 3.0, 49.0), variable="fm100",
                                 start_date="2022-08-01", end_date="2022-08-03"),
              dict(bbox=[2.0, 48.0, 3.0, 49.0], variable="fm100",
                   start_date="2022-08-01", end_date="2022-08-03"))
        # empty window: in-CONUS bbox outside the synthetic grid extent -> GRIDMET_EMPTY
        ds2 = _synthetic_gridmet_ds()
        ek = dict(bbox=(-100.0, 40.0, -99.0, 41.0), variable="fm100",
                  start_date="2022-08-01", end_date="2022-08-03")
        ep = dict(bbox=[-100.0, 40.0, -99.0, 41.0], variable="fm100",
                  start_date="2022-08-01", end_date="2022-08-03")
        with _patched(mock.patch("xarray.open_dataset", lambda *a, **k: ds2.copy(deep=True))):
            tw_e = _capture_err(_run_twin, gridmet_mod, "fetch_gridmet", ek, {})
            rt_e = _capture_err(route_layer, spec, ep, {})
        _cmp_error(res, "error.empty", tw_e, rt_e)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


# --------------------------------------------------------------------------- #
# 2. hifld -- vector-fgb / ArcGIS.
# --------------------------------------------------------------------------- #


def _hifld_fc():
    feats = []
    for i, name in enumerate(["General Hospital", "Mercy Medical", "St. Lukes"]):
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-95.4 + i * 0.05, 29.7 + i * 0.05]},
            "properties": {"NAME": name, "ADDRESS": f"{i+1} Main St", "CITY": "Houston",
                           "STATE": "TX", "BEDS": 100 + i * 50},
        })
    return {"type": "FeatureCollection", "features": feats}


def run_hifld(specs) -> SourceResult:
    res = SourceResult("fetch_hifld_critical_infrastructure")
    spec = specs["fetch_hifld_critical_infrastructure"]
    bbox = [-95.8, 29.5, -95.0, 30.1]
    kwargs = dict(facility_type="hospitals", bbox=tuple(bbox))
    params = dict(facility_type="hospitals", bbox=bbox)
    fc = _hifld_fc()
    try:
        import httpx
        ok_get = lambda self, *a, **k: FakeResp(json_body=fc, status_code=200)
        with _patched(mock.patch.object(httpx.Client, "get", ok_get)):
            tw_sink, rt_sink = {}, {}
            twin_layer = _run_twin(hifld_mod, "fetch_hifld_critical_infrastructure", kwargs, tw_sink)
            router_layer = route_layer(spec, params, rt_sink)
        tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
        res.add("values.n", tw["n"] == rt["n"], tw["n"], rt["n"])
        res.add("values.geom", tw["geom_types"] == rt["geom_types"], tw["geom_types"], rt["geom_types"])
        res.add("values.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"])
        tw_names = sorted(tw["gdf"]["NAME"]) if "NAME" in tw["gdf"] else []
        rt_names = sorted(rt["gdf"]["NAME"]) if "NAME" in rt["gdf"] else []
        res.add("values.value_spotcheck", tw_names == rt_names, tw_names, rt_names)
        extra = tw["columns"] - rt["columns"]
        res.add("schema.columns", tw["columns"] == rt["columns"],
                sorted(tw["columns"]), sorted(rt["columns"]),
                note="" if not extra else f"router lacks twin-derived columns {sorted(extra)}")
        _cmp_layer(res, twin_layer, router_layer)
        res.add("caveats.reproduced",
                any("honest-empty" in c for c in spec.caveats), note="honest-empty FGB caveat present")
        # Forced upstream failure: ArcGIS 500.
        def bad_get(self, *a, **k):
            return FakeResp(json_body=None, status_code=500, text="boom")
        with _patched(mock.patch.object(httpx.Client, "get", bad_get)):
            tw_err = _capture_err(_run_twin, hifld_mod, "fetch_hifld_critical_infrastructure", kwargs, {})
            rt_err = _capture_err(route_layer, spec, params, {})
        _cmp_error(res, "error.upstream", tw_err, rt_err)

        # --- edge matrix (contract 4.2) ---
        def _edge(name, k, p):
            tw = _capture_err(_run_twin, hifld_mod, "fetch_hifld_critical_infrastructure", k, {})
            rt = _capture_err(route_layer, spec, p, {})
            _cmp_error(res, name, tw, rt)

        # malformed bbox -> HIFLD_INFRA_INPUT_INVALID
        _edge("error.bad_bbox", dict(facility_type="hospitals", bbox=(-95.0, 30.0, -95.0, 30.1)),
              dict(facility_type="hospitals", bbox=[-95.0, 30.0, -95.0, 30.1]))
        # bad enum (facility_type) -> HIFLD_INFRA_INPUT_INVALID
        _edge("error.bad_enum", dict(facility_type="banana", bbox=tuple(bbox)),
              dict(facility_type="banana", bbox=bbox))
        # empty result: ArcGIS returns zero features -> header-only FGB both (honest-empty, NO error)
        empty_fc = {"type": "FeatureCollection", "features": []}
        empty_get = lambda self, *a, **k: FakeResp(json_body=empty_fc, status_code=200)
        with _patched(mock.patch.object(httpx.Client, "get", empty_get)):
            tw_sink2, rt_sink2 = {}, {}
            _run_twin(hifld_mod, "fetch_hifld_critical_infrastructure", kwargs, tw_sink2)
            route_layer(spec, params, rt_sink2)
        twe, rte = vector_info(tw_sink2["bytes"]), vector_info(rt_sink2["bytes"])
        res.add("error.empty", twe["n"] == 0 and rte["n"] == 0, twe["n"], rte["n"],
                note="honest header-only FGB (US-only, empty bbox); no fabricated error")
        # max_features soft paging cap: identical accumulate-truncate loop; parity = cap value
        res.add("gate.max_features_cap", spec.gates.max_features == hifld_mod._MAX_TOTAL_FEATURES,
                hifld_mod._MAX_TOTAL_FEATURES, spec.gates.max_features,
                note="soft paging cap (not an error frame); both truncate at the same value")
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


# --------------------------------------------------------------------------- #
# 3. coops -- station-timeseries-fgb.
# --------------------------------------------------------------------------- #


def _coops_catalog(in_bbox=True):
    if in_bbox:
        stations = [
            {"id": "8725520", "name": "Fort Myers", "lat": 26.647, "lng": -81.807},
            {"id": "8725114", "name": "Naples Bay", "lat": 26.132, "lng": -81.808},
        ]
    else:
        stations = [{"id": "9999999", "name": "Far Away", "lat": 60.0, "lng": 10.0}]
    return {"stations": stations}


_COOPS_SERIES = {"data": [
    {"t": "2022-09-28 00:00", "v": "0.512"},
    {"t": "2022-09-28 01:00", "v": "0.734"},
    {"t": "2022-09-28 02:00", "v": "0.301"},
]}


def run_coops(specs) -> SourceResult:
    res = SourceResult("fetch_noaa_coops_tides")
    spec = specs["fetch_noaa_coops_tides"]
    bbox = [-82.5, 25.5, -81.0, 27.5]
    kwargs = dict(bbox=tuple(bbox), start_date="2022-09-28", end_date="2022-09-28", product="water_level")
    params = dict(bbox=bbox, start_date="2022-09-28", end_date="2022-09-28", product="water_level")
    try:
        import httpx
        cat = _coops_catalog(in_bbox=True)

        def twin_http_get(url, timeout):  # patches coops_mod._http_get
            body = cat if "stations.json" in url else _COOPS_SERIES
            return json.dumps(body).encode("utf-8")

        def router_get(self, url, params=None, headers=None, **k):
            body = cat if "stations.json" in url else _COOPS_SERIES
            return FakeResp(json_body=body, status_code=200)

        with _patched(
            mock.patch.object(coops_mod, "_http_get", twin_http_get),
            mock.patch.object(httpx.Client, "get", router_get),
        ):
            tw_sink, rt_sink = {}, {}
            twin_layer = _run_twin(coops_mod, "fetch_noaa_coops_tides", kwargs, tw_sink)
            router_layer = route_layer(spec, params, rt_sink)
        tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
        res.add("values.n", tw["n"] == rt["n"], tw["n"], rt["n"])
        res.add("values.geom", tw["geom_types"] == rt["geom_types"], tw["geom_types"], rt["geom_types"])
        res.add("values.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"])
        res.add("schema.columns", tw["columns"] == rt["columns"],
                sorted(tw["columns"]), sorted(rt["columns"]))
        # Value spot-check: wl_mean_m for station 8725520.
        twm = _station_val(tw["gdf"], "8725520", "wl_mean_m")
        rtm = _station_val(rt["gdf"], "8725520", "wl_mean_m")
        res.add("values.value_spotcheck", _close(twm, rtm), twm, rtm)
        # Timestamp normalization: twin -> ISO+Z, router -> verbatim.
        tts = _station_val(tw["gdf"], "8725520", "time_start")
        rts = _station_val(rt["gdf"], "8725520", "time_start")
        res.add("schema.time_format", tts == rts, tts, rts,
                note="" if tts == rts else "router station executor omits twin's ISO-8601+Z timestamp normalization")
        _cmp_layer(res, twin_layer, router_layer)
        res.add("caveats.reproduced", any("EMPTY" in c for c in spec.caveats),
                note="typed-empty + one-bad-station honesty present")
        # Forced upstream failure: catalog fetch 500 / transport error.
        def twin_boom(url, timeout):
            raise coops_mod.COOPSTidesUpstreamError(f"upstream HTTP 500 for {url}")

        def router_boom(self, *a, **k):
            raise httpx.ConnectError("boom")

        with _patched(
            mock.patch.object(coops_mod, "_http_get", twin_boom),
            mock.patch.object(httpx.Client, "get", router_boom),
        ):
            tw_err = _capture_err(_run_twin, coops_mod, "fetch_noaa_coops_tides", kwargs, {})
            rt_err = _capture_err(route_layer, spec, params, {})
        _cmp_error(res, "error.upstream", tw_err, rt_err)
        # Forced empty path: stations outside bbox.
        cat_empty = _coops_catalog(in_bbox=False)

        def twin_empty(url, timeout):
            body = cat_empty if "stations.json" in url else _COOPS_SERIES
            return json.dumps(body).encode("utf-8")

        def router_empty(self, url, params=None, headers=None, **k):
            body = cat_empty if "stations.json" in url else _COOPS_SERIES
            return FakeResp(json_body=body, status_code=200)

        with _patched(
            mock.patch.object(coops_mod, "_http_get", twin_empty),
            mock.patch.object(httpx.Client, "get", router_empty),
        ):
            tw_e = _capture_err(_run_twin, coops_mod, "fetch_noaa_coops_tides", kwargs, {})
            rt_e = _capture_err(route_layer, spec, params, {})
        _cmp_error(res, "error.empty", tw_e, rt_e)

        # --- edge matrix (contract 4.2: invalid-param classes + cap parity) ---
        def _edge(name, k, p):
            tw = _capture_err(_run_twin, coops_mod, "fetch_noaa_coops_tides", k, {})
            rt = _capture_err(route_layer, spec, p, {})
            _cmp_error(res, name, tw, rt)

        base = dict(start_date="2022-09-28", end_date="2022-09-28", product="water_level")
        # malformed bbox -> COOPS_TIDES_INPUT_ERROR
        _edge("error.bad_bbox", dict(bbox=(-82.5, 25.5, -82.5, 27.5), **base),
              dict(bbox=[-82.5, 25.5, -82.5, 27.5], **base))
        # bad enum (product) -> COOPS_TIDES_INPUT_ERROR
        _edge("error.bad_enum", dict(bbox=tuple(bbox), start_date="2022-09-28",
                                     end_date="2022-09-28", product="banana"),
              dict(bbox=bbox, start_date="2022-09-28", end_date="2022-09-28", product="banana"))
        # date order (start > end) -> COOPS_TIDES_INPUT_ERROR
        _edge("error.date_order", dict(bbox=tuple(bbox), start_date="2022-09-30",
                                       end_date="2022-09-28", product="water_level"),
              dict(bbox=bbox, start_date="2022-09-30", end_date="2022-09-28", product="water_level"))
        # date range over the 366-day cap -> COOPS_TIDES_INPUT_ERROR
        _edge("error.date_range", dict(bbox=tuple(bbox), start_date="2020-01-01",
                                       end_date="2021-06-01", product="water_level"),
              dict(bbox=bbox, start_date="2020-01-01", end_date="2021-06-01", product="water_level"))
        # max_stations soft cap: identical catalog-truncate; parity = cap value
        res.add("gate.max_stations_cap", spec.gates.max_stations == coops_mod._MAX_STATIONS,
                coops_mod._MAX_STATIONS, spec.gates.max_stations,
                note="soft station cap (not an error frame); both truncate at the same value")
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def _station_val(gdf, station_id, col):
    try:
        row = gdf[gdf["station_id"] == station_id]
        if len(row) == 0:
            return None
        v = row.iloc[0][col]
        return float(v) if isinstance(v, (int, float)) else v
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# 4. esri_landcover -- raster-cog STAC + tiled-mosaic (HYBRID). Router STAC stub.
# --------------------------------------------------------------------------- #


def _make_landcover_tif(path):
    """A 10x10 uint8 EPSG:4326 categorical raster with a colormap over a tiny bbox."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    arr = (np.arange(100, dtype="uint8").reshape(10, 10) % 7 + 1).astype("uint8")
    transform = from_bounds(0.0, 0.0, 0.1, 0.1, 10, 10)
    cmap = {i: (i * 20 % 255, i * 40 % 255, i * 60 % 255, 255) for i in range(12)}
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1,
                       dtype="uint8", crs="EPSG:4326", transform=transform, nodata=0) as dst:
        dst.write(arr, 1)
        dst.write_colormap(1, cmap)
    return arr, cmap


def run_esri(specs) -> SourceResult:
    res = SourceResult("fetch_esri_landcover_10m")
    spec = specs["fetch_esri_landcover_10m"]
    bbox = [0.0, 0.0, 0.1, 0.1]
    params = dict(bbox=bbox, year=2023)
    try:
        import os
        import tempfile

        from trid3nt_server.agent.tools.fetchers.imagery import _pc_stac

        fd, tif = tempfile.mkstemp(suffix=".tif")
        os.close(fd)
        arr, cmap = _make_landcover_tif(tif)

        # ROUTER: drive the FULL router entrypoint (route -> tiled-mosaic single-tile
        # -> raster_cog.execute -> stac_to_mosaic) via a fake pystac Client whose one
        # item's data asset points at the synthetic tif; SAS-sign is identity offline.
        class _Asset:
            def __init__(self, href):
                self.href = href

        class _Item:
            bbox = [0.0, 0.0, 0.1, 0.1]
            properties = {"start_datetime": "2023-06-01T00:00:00Z"}

            def __init__(self, href):
                self.assets = {"data": _Asset(href)}

        class _Search:
            def __init__(self, href):
                self._href = href

            def items(self):
                return [_Item(self._href)]

        class _Client:
            @staticmethod
            def open(root):
                return _Client()

            def search(self, *a, **k):
                return _Search(tif)

        rt_sink = {}
        with _patched(
            mock.patch("pystac_client.Client", _Client),
            mock.patch.object(_pc_stac, "sas_sign_href", lambda href, coll: href),
        ):
            router_layer = route_layer(spec, params, rt_sink)
        router_bytes = rt_sink["bytes"]
        rt = raster_stats(router_bytes)

        # TWIN reference output: uint8 palette COG from the same mosaic (pure path;
        # the twin STAC read needs live /vsicurl, so its serialization seam is the
        # reference the router's full palette-COG output must match).
        from rasterio.transform import from_bounds
        twin_bytes = esri_mod._write_palette_cog(
            arr, 10, 10, from_bounds(0.0, 0.0, 0.1, 0.1, 10, 10), cmap, tuple(bbox), 2023
        )
        tw = raster_stats(twin_bytes)

        res.add("values.band_count", tw["band_count"] == rt["band_count"], tw["band_count"], rt["band_count"])
        res.add("values.dtype", tw["dtype"] == rt["dtype"], tw["dtype"], rt["dtype"],
                note="" if tw["dtype"] == rt["dtype"] else "router dtype != twin uint8 categorical")
        res.add("values.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"],
                note="" if tw["crs"] == rt["crs"] else "router did not reproject to EPSG:4326")
        # Layer output parity (router entrypoint LayerURI vs twin contract).
        res.add("layer.style_preset", router_layer.style_preset == "categorical_landcover",
                "categorical_landcover", router_layer.style_preset)
        res.add("layer.role", router_layer.role == "input", "input", router_layer.role)
        res.add("layer.units", router_layer.units == "esri_io_lulc_class_code",
                "esri_io_lulc_class_code", router_layer.units)
        tw_pal = _has_colormap(twin_bytes)
        rt_pal = _has_colormap(router_bytes)
        res.add("values.palette", tw_pal == rt_pal, tw_pal, rt_pal,
                note="" if tw_pal == rt_pal else "router dropped the embedded categorical palette")
        res.add("caveats.reproduced", any("NO_COVERAGE" in c for c in spec.caveats),
                note="honest no-coverage caveat present")

        # Forced upstream failure: STAC search raises (twin + router both typed upstream).
        class _BoomClient:
            @staticmethod
            def open(root):
                raise RuntimeError("PC STAC unreachable")

        def twin_boom(*a, **k):
            raise esri_mod.EsriLandcoverUpstreamError("PC STAC search failed: unreachable")

        with _patched(mock.patch("pystac_client.Client", _BoomClient)):
            rt_err = _capture_err(route_layer, spec, params, {})
        tw_err = _capture_err(twin_boom)
        _cmp_error(res, "error.upstream", tw_err, rt_err)

        # --- edge matrix (contract 4.2): year/bbox invalid classes + max_bbox gate
        # + NO_COVERAGE empty, driven through the REAL twin ENTRYPOINT (validation +
        # STAC search precede any network read; a faithful entrypoint A/B). ---
        def _edge(name, k, p):
            tw = _capture_err(_run_twin, esri_mod, "fetch_esri_landcover_10m", k, {})
            rt = _capture_err(route_layer, spec, p, {})
            _cmp_error(res, name, tw, rt)

        # year below range -> ESRI_LANDCOVER_YEAR_INVALID
        _edge("error.year_low", dict(bbox=tuple(bbox), year=1850), dict(bbox=bbox, year=1850))
        # year above range -> ESRI_LANDCOVER_YEAR_INVALID
        _edge("error.year_high", dict(bbox=tuple(bbox), year=2099), dict(bbox=bbox, year=2099))
        # malformed bbox -> ESRI_LANDCOVER_BBOX_INVALID
        _edge("error.bad_bbox", dict(bbox=(0.0, 0.0, 0.0, 0.1), year=2023),
              dict(bbox=[0.0, 0.0, 0.0, 0.1], year=2023))
        # max_bbox ceiling (16 > 8 deg^2) -> ESRI_LANDCOVER_BBOX_INVALID
        _edge("gate.max_bbox", dict(bbox=(0.0, 0.0, 4.0, 4.0), year=2023),
              dict(bbox=[0.0, 0.0, 4.0, 4.0], year=2023))
        # empty / no-coverage: STAC search returns zero items -> ESRI_LANDCOVER_NO_COVERAGE
        class _EmptySearch:
            def items(self):
                return []

        class _EmptyClient:
            @staticmethod
            def open(root):
                return _EmptyClient()

            def search(self, *a, **k):
                return _EmptySearch()

        with _patched(
            mock.patch("pystac_client.Client", _EmptyClient),
            mock.patch.object(_pc_stac, "sas_sign_href", lambda href, coll: href),
        ):
            tw_e = _capture_err(_run_twin, esri_mod, "fetch_esri_landcover_10m",
                                dict(bbox=tuple(bbox), year=2023), {})
            rt_e = _capture_err(route_layer, spec, dict(bbox=bbox, year=2023), {})
        _cmp_error(res, "error.empty", tw_e, rt_e)

        os.unlink(tif)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def _has_colormap(b: bytes) -> bool:
    import rasterio
    from rasterio.io import MemoryFile

    with MemoryFile(b) as mf, mf.open() as ds:
        try:
            ds.colormap(1)
            return True
        except (ValueError, KeyError):
            return False


# --------------------------------------------------------------------------- #
# 5. census_acs -- vector-fgb + JOIN-on-key (HYBRID).
# --------------------------------------------------------------------------- #


def _census_tracts():
    def poly(x, y):
        return {"type": "Polygon", "coordinates": [[[x, y], [x + 0.01, y], [x + 0.01, y + 0.01],
                                                     [x, y + 0.01], [x, y]]]}
    out = []
    for i, gid in enumerate(["48201010101", "48201010102", "48201010103"]):
        out.append({
            "type": "Feature",
            "geometry": poly(-95.40 + i * 0.02, 29.70),
            "properties": {"GEOID": gid, "NAME": f"Tract {gid[-4:]}", "STATE": "48",
                           "COUNTY": "201", "TRACT": gid[-6:]},
        })
    return out


def run_census(specs) -> SourceResult:
    res = SourceResult("fetch_census_acs")
    spec = specs["fetch_census_acs"]
    bbox = [-95.45, 29.65, -95.25, 29.85]
    tracts = _census_tracts()

    def _drive(variable, values_by_key, spot_gid, spot_expect):
        # value_by_key for the twin: keyed by geoid -> {code: value|None}.
        twin_vals = values_by_key

        def twin_tracts(_bbox):
            return tracts

        def twin_values(spec_d, counties, year):
            return twin_vals

        def router_geom(spec_, params_):
            return tracts

        def router_values(spec_, scope_keys, var_spec, params_):
            return values_by_key

        kwargs = dict(bbox=tuple(bbox), variable=variable, year=2022)
        params = dict(bbox=bbox, variable=variable, year=2022)
        with _patched(
            mock.patch.object(census_mod, "_fetch_tiger_tracts", twin_tracts),
            mock.patch.object(census_mod, "_fetch_acs_values", twin_values),
            mock.patch.object(router_join, "fetch_geometry", router_geom),
            mock.patch.object(router_join, "fetch_values", router_values),
        ):
            tw_sink, rt_sink = {}, {}
            twin_layer = _run_twin(census_mod, "fetch_census_acs", kwargs, tw_sink)
            router_layer = route_layer(spec, params, rt_sink)
        tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
        pfx = f"values.{variable}"
        res.add(f"{pfx}.n", tw["n"] == rt["n"], tw["n"], rt["n"])
        res.add(f"{pfx}.geom", tw["geom_types"] == rt["geom_types"], tw["geom_types"], rt["geom_types"])
        res.add(f"{pfx}.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"])
        res.add("schema.columns", tw["columns"] == rt["columns"],
                sorted(tw["columns"]), sorted(rt["columns"]))
        twv = _census_val(tw["gdf"], spot_gid)
        rtv = _census_val(rt["gdf"], spot_gid)
        res.add(f"{pfx}.value_spotcheck", _val_eq(twv, rtv) and _val_eq(twv, spot_expect),
                twv, rtv, note=f"expected {spot_expect}")
        # Null-never-fabricated: the missing/sentinel tract stays null in both.
        twn = _census_val(tw["gdf"], "48201010103")
        rtn = _census_val(rt["gdf"], "48201010103")
        res.add(f"{pfx}.null_floor", (twn is None) and (rtn is None), twn, rtn)
        return twin_layer, router_layer

    try:
        # value kind (median_income) -- tract3 missing -> null. LayerURI.units=usd.
        mi_vals = {"48201010101": {"B19013_001E": 65000.0},
                   "48201010102": {"B19013_001E": None}}
        twin_layer, router_layer = _drive("median_income", mi_vals, "48201010101", 65000.0)
        _cmp_layer(res, twin_layer, router_layer)
        # pct kind (poverty_rate) -- 100*500/2000 = 25.0. LayerURI.units=percent
        # (per-variable, full fidelity: the router now resolves it, not the single
        # normalize.units stamp).
        pr_vals = {"48201010101": {"B17001_002E": 500.0, "B17001_001E": 2000.0},
                   "48201010102": {"B17001_002E": None, "B17001_001E": 1800.0}}
        pr_tw, pr_rt = _drive("poverty_rate", pr_vals, "48201010101", 25.0)
        res.add("layer.units.poverty_rate", pr_tw.units == pr_rt.units, pr_tw.units, pr_rt.units,
                note="per-variable LayerURI.units=percent (full fidelity)")
        # RAW ACS estimate-code passthrough (FULL FIDELITY, NATE-chosen): a raw
        # code (B19013_001E == the median_income code) is accepted alongside the
        # friendly names and returns units=count. Proves the passthrough resolver
        # + per-variable units on both the FGB and the LayerURI.
        raw_vals = {"48201010101": {"B19013_001E": 65000.0},
                    "48201010102": {"B19013_001E": None}}
        raw_tw, raw_rt = _drive("B19013_001E", raw_vals, "48201010101", 65000.0)
        res.add("layer.units.raw_code", raw_tw.units == raw_rt.units, raw_tw.units, raw_rt.units,
                note="raw-code passthrough LayerURI.units=count (full fidelity)")
        res.add("caveats.reproduced", any("fabricated" in c for c in spec.caveats),
                note="null-never-fabricated caveat present")
        # Forced upstream failure: geometry endpoint 500.
        import httpx
        kwargs = dict(bbox=tuple(bbox), variable="median_income", year=2022)
        params = dict(bbox=bbox, variable="median_income", year=2022)

        def bad_get(self, *a, **k):
            return FakeResp(json_body=None, status_code=500, text="boom")
        with _patched(mock.patch.object(httpx.Client, "get", bad_get)):
            tw_err = _capture_err(_run_twin, census_mod, "fetch_census_acs", kwargs, {})
            rt_err = _capture_err(route_layer, spec, params, {})
        _cmp_error(res, "error.upstream", tw_err, rt_err)

        # --- edge matrix (contract 4.2: invalid-param classes + empty + cap) ---
        # Geometry stubbed to real tracts so a bad-variable edge still reaches the
        # variable resolver even if the twin fetches geometry first (no network).
        def _geom_twin(_bbox):
            return tracts

        def _geom_router(spec_, params_):
            return tracts

        def _edge(name, variable, malformed_bbox=None):
            k = dict(bbox=tuple(malformed_bbox or bbox), variable=variable, year=2022)
            p = dict(bbox=(malformed_bbox or bbox), variable=variable, year=2022)
            with _patched(
                mock.patch.object(census_mod, "_fetch_tiger_tracts", _geom_twin),
                mock.patch.object(router_join, "fetch_geometry", _geom_router),
            ):
                tw = _capture_err(_run_twin, census_mod, "fetch_census_acs", k, {})
                rt = _capture_err(route_layer, spec, p, {})
            _cmp_error(res, name, tw, rt)

        # malformed bbox -> CENSUS_ACS_INPUT_INVALID
        _edge("error.bad_bbox", "median_income", malformed_bbox=[-95.4, 29.7, -95.4, 29.8])
        # bad enum (variable) -> CENSUS_ACS_INPUT_INVALID
        _edge("error.bad_enum", "banana")
        # empty result: geometry returns zero tracts -> header-only FGB both (honest-empty)
        def _empty_twin(_bbox):
            return []

        def _empty_router(spec_, params_):
            return []

        ek = dict(bbox=tuple(bbox), variable="median_income", year=2022)
        ep = dict(bbox=bbox, variable="median_income", year=2022)
        with _patched(
            mock.patch.object(census_mod, "_fetch_tiger_tracts", _empty_twin),
            mock.patch.object(router_join, "fetch_geometry", _empty_router),
        ):
            tw_sink3, rt_sink3 = {}, {}
            _run_twin(census_mod, "fetch_census_acs", ek, tw_sink3)
            route_layer(spec, ep, rt_sink3)
        twe, rte = vector_info(tw_sink3["bytes"]), vector_info(rt_sink3["bytes"])
        res.add("error.empty", twe["n"] == 0 and rte["n"] == 0, twe["n"], rte["n"],
                note="honest header-only FGB (US-only / empty bbox); no fabricated error")
        # max_features soft cap parity
        res.add("gate.max_features_cap", spec.gates.max_features == census_mod._MAX_FEATURES,
                census_mod._MAX_FEATURES, spec.gates.max_features,
                note="soft paging cap (not an error frame); both truncate at the same value")
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


def _census_val(gdf, gid):
    try:
        row = gdf[gdf["geoid"] == gid]
        if len(row) == 0:
            return None
        v = row.iloc[0]["value"]
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def _val_eq(a, b) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= 1e-6


# --------------------------------------------------------------------------- #
# Shared error helpers.
# --------------------------------------------------------------------------- #


def _capture_err(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return {"type": None, "error_code": None, "retryable": None, "raised": False}
    except BaseException as exc:  # noqa: BLE001
        d = err_frame(exc)
        d["raised"] = True
        return d


def _cmp_error(res: SourceResult, name: str, tw_err: dict, rt_err: dict) -> None:
    both_raised = tw_err.get("raised") and rt_err.get("raised")
    code_match = tw_err.get("error_code") == rt_err.get("error_code")
    retry_match = tw_err.get("retryable") == rt_err.get("retryable")
    ok = both_raised and code_match and retry_match
    note = ""
    if both_raised and not code_match:
        note = f"error_code diverges: twin={tw_err['error_code']} router={rt_err['error_code']}"
    elif not both_raised:
        note = f"raise mismatch: twin={tw_err.get('raised')} router={rt_err.get('raised')}"
    res.add(name, ok, tw_err.get("error_code"), rt_err.get("error_code"), note=note)


def run_all() -> list[SourceResult]:
    specs = load_specs()
    return [
        run_gridmet(specs),
        run_hifld(specs),
        run_coops(specs),
        run_esri(specs),
        run_census(specs),
    ]
