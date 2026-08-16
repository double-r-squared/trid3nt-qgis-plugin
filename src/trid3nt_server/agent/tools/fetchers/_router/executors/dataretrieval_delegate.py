"""dataretrieval-delegating executor (phase-2 wave-3).

The USGS water-data family folds by DELEGATING to the official USGS
``dataretrieval`` client (PyPI, agency-maintained) instead of raw HTTP + our
bespoke RDB / GeoJSON / CSV parsers -- the client absorbs the ongoing NWIS ->
Water Data OGC API migration churn. A spec opts in with ``ingest.delegate:
{library: dataretrieval, service: <name>}``; ``select_executor`` routes to this
module BEFORE the shape dispatch (strict no-op for every prior spec).

Each service builds a list of GeoJSON features from the ``dataretrieval``
DataFrames, then reuses ``vector_fgb.features_to_fgb_bytes`` for the shared FGB
serialization (same honest-empty header machinery, same pyogrio writer) so a
delegated source is INDISTINGUISHABLE from a hand-written twin at the FGB seam.

Twin behavior is the contract. ``dataretrieval`` typed errors
(``dataretrieval.exceptions``) map to the router's twin-identical error frame:
an HTTP 400 -> input error (bad characteristic), any other HTTP / network / rate
failure -> upstream error (retryable), an empty station/flowline set -> the
twin's typed empty code.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_input_error, router_upstream_error
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.executors.dataretrieval_delegate"
)

__all__ = ["execute", "pre_validate", "wqp_features", "nldi_features"]

#: CONUS envelope (twin _CONUS_BBOX) + flowline cap (twin _MAX_FLOWLINES).
_NLDI_CONUS: tuple[float, float, float, float] = (-130.0, 20.0, -60.0, 55.0)
_NLDI_MAX_FLOWLINES = 5000


def pre_validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Raise every INPUT error the twin raises BEFORE its cache read_through.

    The router calls this after ``validate_params`` (types/gates) and BEFORE
    ``read_through`` for a delegated spec, so a bad request raises pre-cache /
    pre-network -- byte-identical to the twin (which validates in its function
    body before read_through) and offline-testable (no S3 round-trip).
    """
    service = ((spec.ingest or {}).get("delegate") or {}).get("service")
    prefix = spec.error_code_prefix
    if service == "wqp_water_quality":
        if params.get("bbox") is None:
            raise router_input_error(
                prefix,
                "fetch_usgs_water_quality requires bbox=(west, south, east, north) in "
                "EPSG:4326 for the area of interest (a watershed/sub-basin).",
                spec.input_error_suffix,
            )
        char = params.get("characteristic")
        if not char or not str(char).strip():
            raise router_input_error(
                prefix,
                "characteristic is required (e.g. 'nitrate', 'lead', 'arsenic', 'pH', "
                "'dissolved_oxygen', 'specific_conductance')",
                spec.input_error_suffix,
            )
    elif service == "nldi_navigate":
        sfx = spec.input_error_suffix
        seed = params.get("seed_point")
        comid = params.get("comid")
        if (seed is None) == (comid is None):
            raise router_input_error(
                prefix,
                f"exactly one of seed_point or comid must be provided; got "
                f"seed_point={seed!r}, comid={comid!r}",
                sfx,
            )
        if seed is not None:
            lon, lat = float(seed[0]), float(seed[1])
            w, s, e, n = _NLDI_CONUS
            if not (w <= lon <= e and s <= lat <= n):
                raise router_input_error(
                    prefix, f"seed_point ({lon}, {lat}) is outside NLDI's CONUS coverage {_NLDI_CONUS}", sfx
                )
        elif isinstance(comid, bool) or not isinstance(comid, int) or comid <= 0:
            raise router_input_error(prefix, f"comid must be a positive integer; got {comid!r}", sfx)


# --------------------------------------------------------------------------- #
# dataretrieval error -> router error mapping (upstream-provider-errors rule).
# --------------------------------------------------------------------------- #


def _map_http_error(spec: SourceSpec, exc: Exception, *, input_on_400: bool = True) -> None:
    """Re-raise a ``dataretrieval`` exception as the twin-identical router error.

    An HTTP 400 is a bad REQUEST (an unrecognized characteristicName) -> input
    error (not retryable); every other HTTP / network / transient failure ->
    upstream error (retryable), surfacing the provider reason VERBATIM.
    """
    prefix = spec.error_code_prefix
    status = getattr(exc, "status_code", None)
    if input_on_400 and status == 400:
        raise router_input_error(prefix, f"upstream rejected the request (HTTP 400): {exc}", spec.input_error_suffix)
    raise router_upstream_error(prefix, f"{type(exc).__name__}: {exc}")


def _point_feature(lon: float, lat: float, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


# --------------------------------------------------------------------------- #
# Service: wqp_water_quality  (USGS/EPA Water Quality Portal)
#
# Reproduces fetch_usgs_water_quality: Station locations (dataretrieval
# `wqp.what_sites`) LEFT-joined with the latest numeric Result per site
# (`wqp.get_results`, resultPhysChem profile, latest-by-ActivityStartDate).
# Zero stations -> the twin's WQP_NO_SITES typed error (never an empty layer).
# --------------------------------------------------------------------------- #


def _bbox_str(bbox: list[float]) -> str:
    return ",".join(str(v) for v in bbox)


def _str_or_none(v: Any) -> str | None:
    """Trimmed str, or None for pandas NaN / None / empty."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v).strip()
    return s or None


def _latest_results_by_site(res_df: Any) -> dict[str, dict[str, Any]]:
    """Latest NUMERIC result per MonitoringLocationIdentifier.

    Mirrors the twin ``_parse_result_csv``: skip non-numeric ResultMeasureValue,
    keep a row only when its ActivityStartDate is strictly LATER than the current
    best (first-seen wins on an equal date). Column names are the WQP CSV schema
    ``dataretrieval`` returns verbatim (legacy=True default).
    """
    latest: dict[str, dict[str, Any]] = {}
    cols = set(res_df.columns)
    if "MonitoringLocationIdentifier" not in cols:
        return latest
    for row in res_df.itertuples(index=False):
        rd = row._asdict()
        site_id = (_str_or_none(rd.get("MonitoringLocationIdentifier")) or "")
        if not site_id:
            continue
        raw_val = _str_or_none(rd.get("ResultMeasureValue"))
        if raw_val is None:
            continue
        try:
            value = float(raw_val)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        date = _str_or_none(rd.get("ActivityStartDate")) or ""
        cur = latest.get(site_id)
        if cur is not None and date <= cur["date"]:
            continue
        latest[site_id] = {
            "value": value,
            "unit": _str_or_none(rd.get("ResultMeasure/MeasureUnitCode")) or "",
            "date": date,
            "fraction": _str_or_none(rd.get("ResultSampleFractionText")) or "",
            "characteristic": _str_or_none(rd.get("CharacteristicName")) or "",
        }
    return latest


def wqp_features(spec: SourceSpec, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the WQP point features (Station left-join latest Result)."""
    import dataretrieval.wqp as wqp
    from dataretrieval.exceptions import DataRetrievalError

    prefix = spec.error_code_prefix
    bbox = params.get("bbox")
    characteristic = params.get("characteristic")
    if bbox is None:
        raise router_input_error(
            prefix,
            "fetch_usgs_water_quality requires bbox=(west, south, east, north) in "
            "EPSG:4326 for the area of interest (a watershed/sub-basin).",
            spec.input_error_suffix,
        )
    if not characteristic or not str(characteristic).strip():
        raise router_input_error(
            prefix,
            "characteristic is required (e.g. 'nitrate', 'lead', 'arsenic', 'pH', "
            "'dissolved_oxygen', 'specific_conductance')",
            spec.input_error_suffix,
        )
    bbstr = _bbox_str(bbox)

    # 1. Station service -- the authoritative monitoring-location locations.
    try:
        sites_df, _ = wqp.what_sites(bBox=bbstr, characteristicName=characteristic)
    except DataRetrievalError as exc:
        _map_http_error(spec, exc)

    stations: dict[str, dict[str, Any]] = {}
    if sites_df is not None and len(sites_df):
        scols = set(sites_df.columns)
        for row in sites_df.itertuples(index=False):
            rd = row._asdict()
            site_id = _str_or_none(rd.get("MonitoringLocationIdentifier"))
            if not site_id:
                continue
            try:
                lon = float(rd.get("LongitudeMeasure"))
                lat = float(rd.get("LatitudeMeasure"))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lon) and math.isfinite(lat)):
                continue
            stations[site_id] = {
                "site_id": site_id,
                "site_name": _str_or_none(rd.get("MonitoringLocationName")) or "",
                "site_type": _str_or_none(rd.get("MonitoringLocationTypeName")) or "",
                "lon": lon,
                "lat": lat,
            }

    # 2. Honest typed error if no sites -- never an empty success layer.
    if not stations:
        raise router_empty_error(
            prefix,
            f"No Water Quality Portal monitoring sites found for "
            f"characteristic={characteristic!r} in bbox={bbox!r}. The WQP Station "
            f"service returned zero locations; try a different area, a different "
            f"characteristic, or a wider bbox over a monitored watershed.",
            spec.empty_error_suffix,
        )

    # 3. Result service -- latest numeric sample per site (best-effort decoration).
    try:
        res_df, _ = wqp.get_results(
            bBox=bbstr, characteristicName=characteristic, dataProfile="resultPhysChem"
        )
    except DataRetrievalError as exc:
        _map_http_error(spec, exc)
    results = _latest_results_by_site(res_df)

    # 4. Left-join: one record per station; latest result decorates (or nulls).
    feats: list[dict[str, Any]] = []
    for site_id, loc in stations.items():
        res = results.get(site_id) or {}
        feats.append(
            _point_feature(
                loc["lon"],
                loc["lat"],
                {
                    "site_id": site_id,
                    "site_name": loc["site_name"],
                    "site_type": loc["site_type"],
                    "characteristic": res.get("characteristic") or "",
                    "value": res.get("value"),
                    "unit": res.get("unit") or "",
                    "result_date": res.get("date") or "",
                    "fraction": res.get("fraction") or "",
                },
            )
        )
    logger.info(
        "router.dataretrieval[wqp]: %d site(s); %d carry a latest %s value",
        len(feats),
        sum(1 for f in feats if f["properties"]["value"] is not None),
        characteristic,
    )
    return feats


# --------------------------------------------------------------------------- #
# Service: nldi_navigate  (USGS NLDI NHDPlus network traversal)
#
# Reproduces fetch_nhdplus_nldi_navigate: snap a seed_point to a COMID (or take
# an explicit comid), then navigate the connected flowlines UM/UT/DM/DD to the
# distance. dataretrieval `nldi.get_features(lat,long)` snaps; `get_flowlines`
# navigates. Zero flowlines -> the twin's NHDPLUS_NLDI_EMPTY typed error.
# --------------------------------------------------------------------------- #


def _nldi_snap(spec: SourceSpec, lon: float, lat: float) -> int:
    """Snap (lon, lat) to the nearest NHDPlus COMID via NLDI /comid/position."""
    import dataretrieval.nldi as nldi
    from dataretrieval.exceptions import DataRetrievalError

    prefix = spec.error_code_prefix
    try:
        gf = nldi.get_features(lat=lat, long=lon)
    except DataRetrievalError as exc:
        # NLDI /comid/position 404s / errors an off-network point; the twin's
        # _http_get raises upstream for any HTTPError on the snap call.
        raise router_upstream_error(prefix, f"{type(exc).__name__}: {exc}")
    if gf is None or len(gf) == 0 or "comid" not in getattr(gf, "columns", []):
        raise router_empty_error(
            prefix,
            f"NLDI could not snap ({lon}, {lat}) to any NHDPlus reach "
            f"(likely offshore or outside the NHDPlus network)",
            spec.empty_error_suffix,
        )
    try:
        return int(gf.iloc[0]["comid"])
    except (TypeError, ValueError, KeyError) as exc:
        raise router_upstream_error(prefix, f"NLDI snap returned a non-integer COMID: {exc}")


def nldi_features(spec: SourceSpec, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the NLDI flowline LineString features (seed_point XOR comid)."""
    import dataretrieval.nldi as nldi
    from dataretrieval.exceptions import DataRetrievalError

    prefix = spec.error_code_prefix
    sfx = spec.input_error_suffix
    seed = params.get("seed_point")
    comid = params.get("comid")
    direction = params.get("direction") or "DM"
    distance_km = params.get("distance_km")

    # Selector: exactly one of seed_point / comid (twin mutual-exclusion gate).
    if (seed is None) == (comid is None):
        raise router_input_error(
            prefix,
            f"exactly one of seed_point or comid must be provided; got "
            f"seed_point={seed!r}, comid={comid!r}",
            sfx,
        )

    if seed is not None:
        lon, lat = float(seed[0]), float(seed[1])
        w, s, e, n = _NLDI_CONUS
        if not (w <= lon <= e and s <= lat <= n):
            raise router_input_error(
                prefix, f"seed_point ({lon}, {lat}) is outside NLDI's CONUS coverage {_NLDI_CONUS}", sfx
            )
        seed_comid = _nldi_snap(spec, lon, lat)
    else:
        if isinstance(comid, bool) or not isinstance(comid, int) or comid <= 0:
            raise router_input_error(prefix, f"comid must be a positive integer; got {comid!r}", sfx)
        seed_comid = int(comid)

    # Navigate the connected flowlines. dataretrieval get_flowlines(as_json)
    # returns the raw NLDI GeoJSON FeatureCollection (LineStrings tagged with
    # nhdplus_comid) -- the exact shape the twin serializes.
    try:
        fc = nldi.get_flowlines(
            navigation_mode=str(direction),
            distance=int(round(float(distance_km))),
            comid=seed_comid,
            as_json=True,
        )
    except DataRetrievalError as exc:
        raise router_upstream_error(prefix, f"{type(exc).__name__}: {exc}")

    raw_feats = (fc or {}).get("features", []) if isinstance(fc, dict) else []
    # Twin contract: a raw-empty navigate -> typed EMPTY; a raw-non-empty result
    # whose features filter to zero LineStrings -> honest header-only FGB (the
    # twin's _flowlines_to_fgb writes an empty GeoDataFrame in that case).
    if not raw_feats:
        raise router_empty_error(
            prefix,
            f"NLDI navigate returned zero flowlines for seed COMID={seed_comid} "
            f"direction={direction} distance_km={distance_km} (network terminus, "
            f"stub reach, or distance shorter than the next reach)",
            spec.empty_error_suffix,
        )
    feats: list[dict[str, Any]] = []
    for feat in raw_feats:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if not isinstance(geom, dict) or geom.get("type") != "LineString":
            continue
        props = feat.get("properties") or {}
        cid = props.get("nhdplus_comid") or props.get("comid") or feat.get("id")
        try:
            cid_int = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid_int = None
        feats.append(
            {"type": "Feature", "geometry": geom, "properties": {"nhdplus_comid": cid_int}}
        )

    if len(feats) > _NLDI_MAX_FLOWLINES:
        logger.warning(
            "router.dataretrieval[nldi]: %d flowlines > cap %d; truncating",
            len(feats), _NLDI_MAX_FLOWLINES,
        )
        feats = feats[:_NLDI_MAX_FLOWLINES]
    return feats


# --------------------------------------------------------------------------- #
# Dispatch.
# --------------------------------------------------------------------------- #

_SERVICES = {
    "wqp_water_quality": wqp_features,
    "nldi_navigate": nldi_features,
}


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch via ``dataretrieval`` and serialize to FGB (the ``fetch_fn`` body)."""
    delegate = (spec.ingest or {}).get("delegate") or {}
    service = delegate.get("service")
    builder = _SERVICES.get(service)
    if builder is None:
        raise router_input_error(
            spec.error_code_prefix, f"unknown dataretrieval service {service!r}", spec.input_error_suffix
        )
    features = builder(spec, params)
    return features_to_fgb_bytes(features, spec, params)
