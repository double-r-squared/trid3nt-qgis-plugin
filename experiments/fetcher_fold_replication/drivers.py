"""Per-source replication drivers -- phase-2 wave-2 ArcGIS vector family.

Each source runs the hand-written TWIN and the spec-driven ROUTER over the SAME
fixed request with IDENTICAL synthetic upstream (httpx.Client.get patched to one
FeatureCollection both consume) and records a ``SourceResult`` of field-by-field
envelope checks + the full contract-4.2 edge matrix (values / schema /
docstring-verbatim / layer-output / caveats + forced upstream failure + empty +
every invalid-param class + declared gates). Twin behavior is the contract;
divergences are recorded, never fudged.

The 5 phase-2 wave-1 PILOT drivers were retired here: their twins were DELETED at
promotion (ADR 0038), so an offline twin-vs-router A/B is no longer runnable for
them; their parity is locked by the router unit suites + test_router_promotion.
This wave migrates the ArcGIS FeatureServer/MapServer vector family.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable
from unittest import mock

from harness import (
    FakeResp,
    SourceResult,
    err_frame,
    load_specs,
    route_layer,
    vector_info,
    _make_stub_read_through,
    _patched,
)

# Twin modules (present until promotion; the harness gate runs BEFORE the cut).
from trid3nt_server.agent.tools.fetchers.hazard.fetch_nifc_fire_perimeters import (
    fetch_nifc_fire_perimeters as nifc_mod,
)
from trid3nt_server.agent.tools.fetchers.hazard.fetch_hifld_transmission_lines import (
    fetch_hifld_transmission_lines as transmission_mod,
)
from trid3nt_server.agent.tools.fetchers.hazard.fetch_mtbs_burn_severity import (
    fetch_mtbs_burn_severity as mtbs_mod,
)
from trid3nt_server.agent.tools.fetchers.socioeconomic.fetch_cdc_svi import (
    fetch_cdc_svi as cdc_mod,
)
from trid3nt_server.agent.tools.fetchers.hydrology.fetch_nhd_waterbodies import (
    fetch_nhd_waterbodies as nhd_mod,
)
from trid3nt_server.agent.tools.fetchers.climate.fetch_us_drought_monitor import (
    fetch_us_drought_monitor as drought_mod,
)


# --------------------------------------------------------------------------- #
# Shared helpers.
# --------------------------------------------------------------------------- #


def _run_twin(mod, fn_name: str, kwargs: dict, sink: dict):
    stub = _make_stub_read_through(sink)
    with _patched(mock.patch.object(mod, "read_through", stub)):
        return getattr(mod, fn_name)(**kwargs)


def _capture_err(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
        return {"type": None, "error_code": None, "retryable": None, "raised": False}
    except BaseException as exc:  # noqa: BLE001
        d = err_frame(exc)
        d["raised"] = True
        return d


def _cmp_error(res: SourceResult, name: str, tw_err: dict, rt_err: dict) -> None:
    both = tw_err.get("raised") and rt_err.get("raised")
    code_match = tw_err.get("error_code") == rt_err.get("error_code")
    retry_match = tw_err.get("retryable") == rt_err.get("retryable")
    ok = both and code_match and retry_match
    note = ""
    if both and not code_match:
        note = f"error_code diverges: twin={tw_err['error_code']} router={rt_err['error_code']}"
    elif not both:
        note = f"raise mismatch: twin={tw_err.get('raised')} router={rt_err.get('raised')}"
    res.add(name, ok, tw_err.get("error_code"), rt_err.get("error_code"), note=note)


def _cmp_layer(res: SourceResult, tl, rl) -> None:
    res.add("layer.type", tl.layer_type == rl.layer_type, tl.layer_type, rl.layer_type)
    res.add("layer.style_preset", tl.style_preset == rl.style_preset, tl.style_preset, rl.style_preset)
    res.add("layer.role", tl.role == rl.role, tl.role, rl.role)
    res.add("layer.units", tl.units == rl.units, tl.units, rl.units)
    tb, rb = tl.bbox is not None, rl.bbox is not None
    res.add("layer.bbox_present", tb == rb, tb, rb,
            note="" if tb == rb else "router LayerURI.bbox presence != twin (emit_bbox mismatch)")


def _feature(geom_type: str, coords: Any, props: dict) -> dict:
    return {"type": "Feature", "geometry": {"type": geom_type, "coordinates": coords}, "properties": props}


def _fc(feats: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": feats}


_POLY = [[[-95.4, 29.7], [-95.35, 29.7], [-95.35, 29.75], [-95.4, 29.75], [-95.4, 29.7]]]
_LINE = [[-95.4, 29.7], [-95.2, 29.8]]


def _ok_get(fc):
    return lambda self, *a, **k: FakeResp(json_body=fc, status_code=200)


def _bad_get(self, *a, **k):
    return FakeResp(json_body=None, status_code=500, text="boom")


# --------------------------------------------------------------------------- #
# Generic ArcGIS-vector driver (the family's shared edge matrix).
# --------------------------------------------------------------------------- #


def run_vector_source(
    specs,
    *,
    mod,
    name: str,
    twin_kwargs: dict,
    router_params: dict,
    fixture: dict,
    spotcheck: Callable[[Any], Any],
    edges: list[tuple[str, dict, dict]],
    extra: Callable[[SourceResult, Any], None] | None = None,
) -> SourceResult:
    res = SourceResult(name)
    spec = specs[name]
    import httpx
    try:
        # docstring carried VERBATIM from the twin (drives the retrieval index).
        twin_doc = inspect.getdoc(getattr(mod, name))
        res.add("schema.docstring_verbatim", spec.docstring == twin_doc, note="spec.docstring == inspect.getdoc(twin)")

        # --- happy path: identical synthetic upstream both consume ---
        with _patched(mock.patch.object(httpx.Client, "get", _ok_get(fixture))):
            tw_sink, rt_sink = {}, {}
            tl = _run_twin(mod, name, twin_kwargs, tw_sink)
            rl = route_layer(spec, router_params, rt_sink)
        tw, rt = vector_info(tw_sink["bytes"]), vector_info(rt_sink["bytes"])
        res.add("values.n", tw["n"] == rt["n"], tw["n"], rt["n"])
        res.add("values.geom", tw["geom_types"] == rt["geom_types"], tw["geom_types"], rt["geom_types"])
        res.add("values.crs", tw["crs"] == rt["crs"], tw["crs"], rt["crs"])
        extra_cols = tw["columns"] - rt["columns"]
        res.add("schema.columns", tw["columns"] == rt["columns"],
                sorted(tw["columns"]), sorted(rt["columns"]),
                note="" if not extra_cols else f"router lacks twin columns {sorted(extra_cols)}")
        twv, rtv = spotcheck(tw["gdf"]), spotcheck(rt["gdf"])
        res.add("values.value_spotcheck", twv == rtv, twv, rtv)
        _cmp_layer(res, tl, rl)
        res.add("caveats.reproduced", any("honest-empty" in c for c in spec.caveats),
                note="honest-empty FGB caveat present")

        # --- forced upstream failure (ArcGIS 500) ---
        with _patched(mock.patch.object(httpx.Client, "get", _bad_get)):
            tw_err = _capture_err(_run_twin, mod, name, twin_kwargs, {})
            rt_err = _capture_err(route_layer, spec, router_params, {})
        _cmp_error(res, "error.upstream", tw_err, rt_err)

        # --- empty result -> honest header-only FGB both (no fabricated error) ---
        with _patched(mock.patch.object(httpx.Client, "get", _ok_get(_fc([])))):
            tw_s2, rt_s2 = {}, {}
            _run_twin(mod, name, twin_kwargs, tw_s2)
            route_layer(spec, router_params, rt_s2)
        twe, rte = vector_info(tw_s2["bytes"]), vector_info(rt_s2["bytes"])
        res.add("error.empty", twe["n"] == 0 and rte["n"] == 0, twe["n"], rte["n"],
                note="honest header-only FGB; no fabricated error")

        # --- invalid-param edges (pre-network, twin-identical typed error) ---
        for ename, tk, rp in edges:
            tw = _capture_err(_run_twin, mod, name, tk, {})
            rt = _capture_err(route_layer, spec, rp, {})
            _cmp_error(res, ename, tw, rt)

        if extra is not None:
            extra(res, spec)
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    return res


# --------------------------------------------------------------------------- #
# Per-source configs.
# --------------------------------------------------------------------------- #


def run_nifc(specs) -> SourceResult:
    bbox = [-95.8, 29.5, -95.0, 30.1]
    props = {
        "OBJECTID": 1, "poly_IncidentName": "Test Fire", "poly_FeatureCategory": "Wildfire Daily Fire Perimeter",
        "poly_DateCurrent": "2026-07-01", "poly_GISAcres": 1234.5, "attr_IncidentSize": 1200.0,
        "attr_PercentContained": 45.0, "attr_IncidentName": "Test Fire", "attr_FireCauseGeneral": "Natural",
        "attr_FireCause": "Lightning", "attr_POOState": "US-CA", "attr_IrwinID": "abc-123",
        "attr_UniqueFireIdentifier": "2026-CATRT-000123",
    }
    fixture = _fc([_feature("Polygon", _POLY, props), _feature("Polygon", _POLY, {**props, "OBJECTID": 2, "poly_IncidentName": "Second Fire", "attr_IncidentName": "Second Fire"})])
    return run_vector_source(
        specs, mod=nifc_mod, name="fetch_nifc_fire_perimeters",
        twin_kwargs=dict(bbox=tuple(bbox), status="active"),
        router_params=dict(bbox=bbox, status="active"),
        fixture=fixture,
        spotcheck=lambda g: sorted(g["poly_IncidentName"]) if "poly_IncidentName" in g else [],
        edges=[
            ("error.bad_bbox", dict(bbox=(-95.0, 30.0, -95.0, 30.1), status="active"),
             dict(bbox=[-95.0, 30.0, -95.0, 30.1], status="active")),
            ("error.bad_enum", dict(bbox=tuple(bbox), status="banana"),
             dict(bbox=bbox, status="banana")),
        ],
    )


def run_transmission(specs) -> SourceResult:
    bbox = [-95.8, 29.5, -95.0, 30.1]
    props = {"ID": "L1", "TYPE": "AC", "STATUS": "IN SERVICE", "OWNER": "Utility Co",
             "VOLTAGE": 345.0, "VOLT_CLASS": "345", "SUB_1": "A", "SUB_2": "B"}
    fixture = _fc([_feature("LineString", _LINE, props),
                   _feature("MultiLineString", [_LINE], {**props, "ID": "L2", "VOLTAGE": 138.0})])
    return run_vector_source(
        specs, mod=transmission_mod, name="fetch_hifld_transmission_lines",
        twin_kwargs=dict(bbox=tuple(bbox)),
        router_params=dict(bbox=bbox),
        fixture=fixture,
        spotcheck=lambda g: sorted(g["infra_label"]) if "infra_label" in g else [],
        edges=[
            ("error.bad_bbox", dict(bbox=(-95.0, 30.0, -95.0, 30.1)),
             dict(bbox=[-95.0, 30.0, -95.0, 30.1])),
            ("error.bad_min_voltage", dict(bbox=tuple(bbox), min_voltage_kv=-5.0),
             dict(bbox=bbox, min_voltage_kv=-5.0)),
        ],
    )


def run_mtbs(specs) -> SourceResult:
    bbox = [-124.0, 40.0, -120.0, 43.0]
    props = {"FIRE_ID": "CA1", "FIRE_NAME": "Dixie", "YEAR": 2021, "FIRE_TYPE": "Wildfire",
             "ACRES": 963309.0, "LATITUDE": 40.0, "LONGITUDE": -121.0, "MAP_ID": 1,
             "MAP_PROG": "MTBS", "ASMNT_TYPE": "Extended", "IRWINID": "x", "IG_DATE": "2021-07-13"}
    fixture = _fc([_feature("Polygon", _POLY, props),
                   _feature("Polygon", _POLY, {**props, "FIRE_ID": "CA2", "FIRE_NAME": "Camp", "YEAR": 2018})])
    return run_vector_source(
        specs, mod=mtbs_mod, name="fetch_mtbs_burn_severity",
        twin_kwargs=dict(bbox=tuple(bbox), year_range=(2018, 2021)),
        router_params=dict(bbox=bbox, year_range=[2018, 2021]),
        fixture=fixture,
        spotcheck=lambda g: sorted(g["FIRE_NAME"]) if "FIRE_NAME" in g else [],
        edges=[
            ("error.bad_bbox", dict(bbox=(-124.0, 40.0, -124.0, 43.0)),
             dict(bbox=[-124.0, 40.0, -124.0, 43.0])),
            ("error.bad_year_range", dict(bbox=tuple(bbox), year_range=(1800, 1900)),
             dict(bbox=bbox, year_range=[1800, 1900])),
        ],
    )


def run_cdc(specs) -> SourceResult:
    bbox = [-95.45, 29.65, -95.25, 29.85]
    p1 = {"FIPS": "48201010101", "COUNTY": "Harris", "ST_ABBR": "TX", "LOCATION": "Census Tract 1",
          "E_TOTPOP": 4200, "STATE": "Texas", "RPL_THEMES": 0.8123, "RPL_THEME1": 0.7,
          "RPL_THEME2": 0.6, "RPL_THEME3": 0.9, "RPL_THEME4": 0.5}
    # second tract carries the -999 sentinel -> normalized to null in both.
    p2 = {"FIPS": "48201010102", "COUNTY": "Harris", "ST_ABBR": "TX", "LOCATION": "Census Tract 2",
          "E_TOTPOP": 3100, "STATE": "Texas", "RPL_THEMES": -999.0, "RPL_THEME1": -999.0,
          "RPL_THEME2": -999.0, "RPL_THEME3": -999.0, "RPL_THEME4": -999.0}
    fixture = _fc([_feature("Polygon", _POLY, p1), _feature("Polygon", _POLY, p2)])

    def _spot(g):
        row = g[g["fips"] == "48201010101"]
        return round(float(row.iloc[0]["rpl_themes"]), 4) if len(row) else None

    def _extra(res, spec):
        import geopandas as gpd  # noqa: F401
        # sentinel tract's rpl_themes normalized to null in BOTH.
        import httpx
        with _patched(mock.patch.object(httpx.Client, "get", _ok_get(fixture))):
            tw_s, rt_s = {}, {}
            _run_twin(cdc_mod, "fetch_cdc_svi", dict(bbox=tuple(bbox)), tw_s)
            route_layer(spec, dict(bbox=bbox), rt_s)
        tw, rt = vector_info(tw_s["bytes"]), vector_info(rt_s["bytes"])

        def _null(info):
            row = info["gdf"][info["gdf"]["fips"] == "48201010102"]
            v = row.iloc[0]["rpl_themes"] if len(row) else "MISSING"
            import math
            return v is None or (isinstance(v, float) and math.isnan(v))
        res.add("values.sentinel_null", _null(tw) and _null(rt), _null(tw), _null(rt),
                note="-999 sentinel -> null in both (never fabricated)")

    return run_vector_source(
        specs, mod=cdc_mod, name="fetch_cdc_svi",
        twin_kwargs=dict(bbox=tuple(bbox)),
        router_params=dict(bbox=bbox),
        fixture=fixture,
        spotcheck=_spot,
        edges=[("error.bad_bbox", dict(bbox=(-95.4, 29.7, -95.4, 29.8)),
                dict(bbox=[-95.4, 29.7, -95.4, 29.8]))],
        extra=_extra,
    )


def run_nhd(specs) -> SourceResult:
    bbox = [-81.5, 26.0, -81.3, 26.2]
    # HR endpoint returns lowercase keys; medium-res returns UPPERCASE.
    lc = {"permanent_identifier": "nhd-1", "gnis_name": "Lake Trafford", "ftype": 390,
          "fcode": 39004, "reachcode": "0309", "elevation": 6.1, "areasqkm": 6.2}
    fixture = _fc([_feature("Polygon", _POLY, lc),
                   _feature("Polygon", _POLY, {**lc, "permanent_identifier": "nhd-2", "ftype": 436})])

    def _spot(g):
        row = g[g["permanent_identifier"] == "nhd-1"]
        return row.iloc[0]["ftype_label"] if len(row) else None

    def _extra(res, spec):
        import httpx
        upper = {k.upper(): v for k, v in lc.items()}
        upper_fc = _fc([_feature("Polygon", _POLY, upper)])

        # primary 500 -> fallback (UPPERCASE) succeeds; case-insensitive map both.
        def url_get(self, url, params=None, headers=None, **k):
            if "NHDPlus_HR" in url:
                return FakeResp(json_body=None, status_code=500, text="primary down")
            return FakeResp(json_body=upper_fc, status_code=200)

        with _patched(mock.patch.object(httpx.Client, "get", url_get)):
            tw_s, rt_s = {}, {}
            _run_twin(nhd_mod, "fetch_nhd_waterbodies", dict(bbox=tuple(bbox)), tw_s)
            route_layer(spec, dict(bbox=bbox), rt_s)
        tw, rt = vector_info(tw_s["bytes"]), vector_info(rt_s["bytes"])
        res.add("values.fallback_recovers", tw["n"] == 1 and rt["n"] == 1, tw["n"], rt["n"],
                note="primary 500 -> medium-res fallback recovers (UPPERCASE fields), both n=1")
        res.add("schema.fallback_columns", tw["columns"] == rt["columns"],
                sorted(tw["columns"]), sorted(rt["columns"]),
                note="case-insensitive column_map matches UPPERCASE fallback fields")

    return run_vector_source(
        specs, mod=nhd_mod, name="fetch_nhd_waterbodies",
        twin_kwargs=dict(bbox=tuple(bbox)),
        router_params=dict(bbox=bbox),
        fixture=fixture,
        spotcheck=_spot,
        edges=[("error.bad_bbox", dict(bbox=(-81.5, 26.0, -81.5, 26.2)),
                dict(bbox=[-81.5, 26.0, -81.5, 26.2]))],
        extra=_extra,
    )


def run_drought(specs) -> SourceResult:
    bbox = [-114.0, 31.3, -109.0, 37.0]
    props = {"OBJECTID": 1, "dm": 2, "period": "20220802", "ddate": 1659398400000}
    fixture = _fc([_feature("Polygon", _POLY, props),
                   _feature("Polygon", _POLY, {**props, "OBJECTID": 2, "dm": 4})])

    def _spot(g):
        row = g[g["dm"] == 2]
        return row.iloc[0]["label"] if len(row) else None

    def _extra(res, spec):
        import httpx
        # endpoint_select: date present -> archive layer /2. Capture the router URL.
        seen = []

        def cap_get(self, url, params=None, headers=None, **k):
            seen.append(url)
            return FakeResp(json_body=fixture, status_code=200)

        with _patched(mock.patch.object(httpx.Client, "get", cap_get)):
            tw_s, rt_s = {}, {}
            _run_twin(drought_mod, "fetch_us_drought_monitor",
                      dict(bbox=tuple(bbox), date="2022-08-02"), tw_s)
            rl = route_layer(spec, dict(bbox=bbox, date="2022-08-02"), rt_s)
        rt = vector_info(rt_s["bytes"])
        used_archive = any("/2/query" in u for u in seen)
        res.add("gate.endpoint_select_archive", used_archive and rt["n"] == 2, used_archive, rt["n"],
                note="date present -> archive layer /2 selected (endpoint_select)")

    return run_vector_source(
        specs, mod=drought_mod, name="fetch_us_drought_monitor",
        twin_kwargs=dict(bbox=tuple(bbox)),
        router_params=dict(bbox=bbox),
        fixture=fixture,
        spotcheck=_spot,
        edges=[
            ("error.bad_bbox", dict(bbox=(-114.0, 31.3, -114.0, 37.0)),
             dict(bbox=[-114.0, 31.3, -114.0, 37.0])),
            ("error.bad_date", dict(bbox=tuple(bbox), date="2020-13-40"),
             dict(bbox=bbox, date="2020-13-40")),
        ],
        extra=_extra,
    )


def run_all() -> list[SourceResult]:
    specs = load_specs()
    return [
        run_nifc(specs),
        run_transmission(specs),
        run_mtbs(specs),
        run_cdc(specs),
        run_nhd(specs),
        run_drought(specs),
    ]
