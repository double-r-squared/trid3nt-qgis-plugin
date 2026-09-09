"""usgs_stn_hwm: USGS STN flood high-water marks through pygeohydro.

The library owns the service -- the query-parameter set, the request, the decode, the
geo-referencing of each mark -- and four things it does not own ride here."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks.hyriver import hyriver_call

__all__ = ["delegate", "resolve_build", "resolve_parse", "envelope"]

#: STN flood-event list (event_id <-> event_name); the client publishes no equivalent.
EVENTS_URL = "https://stn.wim.usgs.gov/STNServices/Events.json"

_CAVEATS = [
    "HWM elevation accuracy varies by surveyor QUALITY rating (Excellent "
    "+/-0.05 ft, Good +/-0.10 ft, Fair +/-0.20 ft, Poor +/-0.40 ft, VP > "
    "0.40 ft). Filter by quality before treating a mark as calibration truth.",
    "Marks rated 'Unknown/Historical' carry NO stated accuracy -- do not use "
    "them as an absolute peak-elevation reference.",
    "VERTICAL DATUM: each mark states its vdatum (usually NAVD88). Confirm it "
    "matches your model's vertical datum before differencing -- an unreconciled "
    "NAVD88 vs NGVD29 mismatch silently corrupts every residual.",
    "An HWM is POINT evidence of the flood's PEAK stage, not a continuous "
    "water surface; peak marks across a reach need not be simultaneous.",
]

# Generous (~0.5 deg pad) per-state bboxes -- self-contained (a hook must not
# import router internals).
_STATE_BBOX: dict[str, tuple[float, float, float, float]] = {
    "AL": (-88.6, 30.1, -84.8, 35.0), "AK": (-180.0, 51.2, -129.9, 71.4),
    "AZ": (-114.9, 31.3, -109.0, 37.0), "AR": (-94.7, 33.0, -89.6, 36.5),
    "CA": (-124.5, 32.5, -114.1, 42.0), "CO": (-109.1, 36.9, -102.0, 41.0),
    "CT": (-73.7, 40.9, -71.7, 42.1), "DE": (-75.8, 38.4, -75.0, 39.9),
    "FL": (-87.7, 24.4, -79.9, 31.0), "GA": (-85.6, 30.4, -80.8, 35.0),
    "HI": (-160.3, 18.9, -154.8, 22.2), "ID": (-117.3, 41.9, -111.0, 49.0),
    "IL": (-91.5, 36.9, -87.0, 42.5), "IN": (-88.1, 37.7, -84.7, 41.8),
    "IA": (-96.7, 40.4, -90.1, 43.5), "KS": (-102.1, 36.9, -94.6, 40.0),
    "KY": (-89.6, 36.5, -81.9, 39.1), "LA": (-94.1, 28.9, -88.8, 33.0),
    "ME": (-71.1, 43.0, -66.9, 47.5), "MD": (-79.5, 37.9, -75.0, 39.7),
    "MA": (-73.5, 41.2, -69.9, 42.9), "MI": (-90.5, 41.7, -82.4, 48.3),
    "MN": (-97.2, 43.5, -89.5, 49.4), "MS": (-91.7, 30.2, -88.1, 35.0),
    "MO": (-95.8, 35.9, -89.1, 40.6), "MT": (-116.1, 44.4, -104.0, 49.0),
    "NE": (-104.1, 40.0, -95.3, 43.0), "NV": (-120.0, 35.0, -114.0, 42.0),
    "NH": (-72.6, 42.7, -70.6, 45.3), "NJ": (-75.6, 38.9, -73.9, 41.4),
    "NM": (-109.1, 31.3, -103.0, 37.0), "NY": (-79.8, 40.5, -71.8, 45.0),
    "NC": (-84.3, 33.8, -75.4, 36.6), "ND": (-104.1, 45.9, -96.6, 49.0),
    "OH": (-84.8, 38.4, -80.5, 42.3), "OK": (-103.0, 33.6, -94.4, 37.0),
    "OR": (-124.7, 41.9, -116.5, 46.3), "PA": (-80.5, 39.7, -74.7, 42.3),
    "RI": (-71.9, 41.1, -71.1, 42.0), "SC": (-83.4, 32.0, -78.5, 35.2),
    "SD": (-104.1, 42.5, -96.4, 45.9), "TN": (-90.3, 34.9, -81.6, 36.7),
    "TX": (-106.6, 25.8, -93.5, 36.5), "UT": (-114.1, 37.0, -109.0, 42.0),
    "VT": (-73.4, 42.7, -71.5, 45.0), "VA": (-83.7, 36.5, -75.3, 39.5),
    "WA": (-124.8, 45.5, -116.9, 49.0), "WV": (-82.7, 37.2, -77.7, 40.6),
    "WI": (-92.9, 42.5, -86.8, 47.1), "WY": (-111.1, 40.9, -104.0, 45.0),
    "DC": (-77.2, 38.8, -76.9, 39.0), "PR": (-67.3, 17.9, -65.2, 18.6),
    "VI": (-65.1, 17.7, -64.6, 18.4), "GU": (144.6, 13.2, 145.0, 13.7),
}


def _f(v: Any) -> float | None:
    import math
    try:
        fv = float(v)
        return fv if math.isfinite(fv) else None
    except (TypeError, ValueError):
        return None


def _states_overlapping_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    w1, s1, e1, n1 = bbox
    return sorted(
        st for st, (w2, s2, e2, n2) in _STATE_BBOX.items()
        if not (e1 < w2 or w1 > e2 or n1 < s2 or s1 > n2)
    )


# --------------------------------------------------------------------------- #
# PHASE R -- resolve a named event -> event_id (skip when no event named).
# --------------------------------------------------------------------------- #


@_hooks.register_hook("usgs_stn_hwm.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """GET the STN event list ONLY when a named event needs resolution."""
    event = params.get("event")
    if event is None or not str(event).strip():
        return []
    return [_hooks.RequestPlan(url=EVENTS_URL, headers={"User-Agent": spec.auth.user_agent})]


@_hooks.register_hook("usgs_stn_hwm.resolve_parse")
def resolve_parse(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> dict[str, Any]:
    """Resolve the named event to ``{event_id, event_name}`` (substring match)."""
    sc = spec.error_code_prefix
    needle = str(params.get("event") or "").strip().lower()
    if not needle:
        raise router_input_error(sc, "event was empty after trimming.", spec.input_error_suffix)
    raw = bodies[0] if bodies else b""
    try:
        events = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"STN Events response is not valid JSON: {exc}")
    if not isinstance(events, list):
        raise router_upstream_error(sc, "STN Events response was not a JSON array.")

    exact = [e for e in events if str(e.get("event_name", "")).strip().lower() == needle]
    if len(exact) == 1:
        return {"event_id": int(exact[0]["event_id"]), "event_name": str(exact[0]["event_name"])}
    matches = [e for e in events if needle in str(e.get("event_name", "")).strip().lower()]
    if len(matches) == 1:
        return {"event_id": int(matches[0]["event_id"]), "event_name": str(matches[0]["event_name"])}
    if not matches:
        raise router_input_error(
            sc,
            f"no STN flood event matches event={params.get('event')!r}. Example event names: "
            + ", ".join(sorted(str(e.get("event_name", "")) for e in events)[:8]),
            "EVENT_NOT_FOUND",
        )
    raise router_input_error(
        sc,
        f"event={params.get('event')!r} is ambiguous ({len(matches)} STN events match): "
        + ", ".join(sorted(str(m.get("event_name", "")) for m in matches)[:10])
        + ". Use a more specific event name.",
        "EVENT_NOT_FOUND",
    )


# --------------------------------------------------------------------------- #
# MAIN FETCH -- the library read, scoped by event or by overlapping state.
# --------------------------------------------------------------------------- #


def _query_params(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """The STN filter: the Event when one resolved, else the overlapping States. After
    the bbox clip a State filter cannot add an in-AOI mark to an event-scoped query, so
    the two are exclusive."""
    bbox = tuple(float(v) for v in params["bbox"])
    event_id = params.get("event_id")
    if event_id is not None:
        return {"Event": str(int(event_id))}
    states = _states_overlapping_bbox(bbox)  # type: ignore[arg-type]
    if not states:
        raise router_input_error(
            spec.error_code_prefix,
            f"bbox={tuple(round(v, 3) for v in bbox)} does not overlap any US "
            "state, and no event was named. USGS STN covers the US + territories "
            "only; pass a US AOI or a named flood event.",
            spec.input_error_suffix,
        )
    return {"States": ",".join(states)}


@_hooks.register_hook("usgs_stn_hwm.delegate")
def delegate(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Fetch the filtered marks, clip to the bbox, stamp the WSE quantity per mark."""
    from pygeohydro import STNFloodEventData

    sc = spec.error_code_prefix
    west, south, east, north = (float(v) for v in params["bbox"])
    query = _query_params(spec, params)
    records = hyriver_call(
        spec,
        f"pygeohydro.STNFloodEventData.get_filtered_data(hwms, {query})",
        STNFloodEventData.get_filtered_data,
        "hwms",
        query_params=query,
        as_list=True,
    )

    features: list[dict[str, Any]] = []
    for r in records:
        lat, lon = _f(r.get("latitude")), _f(r.get("longitude"))
        if lat is None or lon is None:
            continue
        if not (west <= lon <= east and south <= lat <= north):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "hwm_id": r.get("hwm_id"),
                    "site_no": str(r.get("site_no") or ""),
                    # QUANTITY STAMP: elev_ft is a WATER-SURFACE ELEVATION (above
                    # the stated vertical_datum), NOT a depth above ground.
                    "quantity": "water_surface_elevation",
                    "elev_ft": _f(r.get("elev_ft")),
                    "height_above_gnd": _f(r.get("height_above_gnd")),
                    "vertical_datum": str(r.get("verticalDatumName") or "") or None,
                    "horizontal_datum": str(r.get("horizontalDatumName") or "") or None,
                    "quality": str(r.get("hwmQualityName") or "") or None,
                    "quality_id": r.get("hwm_quality_id"),
                    "hwm_type": str(r.get("hwmTypeName") or "") or None,
                    "hwm_type_id": r.get("hwm_type_id"),
                    "hwm_environment": str(r.get("hwm_environment") or "") or None,
                    "event": str(r.get("eventName") or "") or None,
                    "event_id": r.get("event_id"),
                    "state": str(r.get("stateName") or "") or None,
                    "county": str(r.get("countyName") or "") or None,
                    "waterbody": str(r.get("waterbody") or "") or None,
                    "survey_date": str(r.get("survey_date") or "") or None,
                    "stillwater": _f(r.get("stillwater")),
                    "hwm_label": str(r.get("hwm_label") or "") or None,
                },
            }
        )

    if not features:
        scope = ", ".join(f"{k}={v}" for k, v in query.items())
        raise router_empty_error(
            sc,
            f"No USGS STN high-water marks found inside the AOI for {scope}. "
            "Either no flood was surveyed here or the marks fall outside the "
            "bbox; widen the AOI or pick a surveyed flood event.",
            "NO_MARKS",
        )
    return features


# --------------------------------------------------------------------------- #
# POST-EMIT ENVELOPE -- quality/type/datum breakdown + caveats/notes.
# --------------------------------------------------------------------------- #


def _records_from_fgb(data: bytes) -> list[dict[str, Any]]:
    import os
    import tempfile

    import geopandas as gpd

    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False, prefix="trid3nt_hwm_env_") as f:
            tmp = f.name
            f.write(data)
        gdf = gpd.read_file(tmp)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    recs: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        recs.append(
            {
                "quality": row.get("quality"),
                "hwm_type": row.get("hwm_type"),
                "vertical_datum": row.get("vertical_datum"),
            }
        )
    return recs


@_hooks.register_hook("usgs_stn_hwm.envelope")
def envelope(
    spec: SourceSpec, params: dict[str, Any], layer: Any, data: bytes | None
) -> dict[str, Any]:
    """Compute the HWM survey-quality envelope from the produced FGB bytes."""
    recs = _records_from_fgb(data) if data else []
    n_marks = len(recs)
    quality = dict(Counter(r.get("quality") or "Unknown/Historical" for r in recs))
    types = dict(Counter(r.get("hwm_type") or "Unspecified" for r in recs))
    datum = dict(Counter(r.get("vertical_datum") or "unspecified" for r in recs))

    caveats = list(_CAVEATS)
    n_hist = quality.get("Unknown/Historical", 0)
    if n_hist:
        caveats.append(
            f"{n_hist} of {n_marks} mark(s) are 'Unknown/Historical' quality "
            "(no stated accuracy)."
        )
    datums = [d for d in datum if d != "unspecified"]
    if len(datums) > 1:
        caveats.append(
            f"Marks span multiple vertical datums {sorted(datums)}; reconcile "
            "to ONE datum before comparing against a model."
        )

    event_name = params.get("event_name")
    if event_name:
        scope_note = f" for event {event_name!r}."
    else:
        bbox = tuple(float(v) for v in params["bbox"])
        scope_note = f" across state(s) {_states_overlapping_bbox(bbox)}."  # type: ignore[arg-type]
    notes = [
        f"USGS STN FilteredHWMs: {n_marks} mark(s) in the AOI" + scope_note,
        "Observed quantity is WATER-SURFACE ELEVATION (elev_ft, above the "
        "stated vertical_datum) -- stamped on every feature as "
        "quantity='water_surface_elevation'. It is NOT a depth above ground; "
        "pair it against a model flood-DEPTH raster only via "
        "extract_model_at_observations (which converts WSE->depth with a DEM).",
    ]

    return {
        "name": f"USGS high-water marks ({n_marks})",
        "units": "ft (peak elevation; see vertical_datum)",
        "n_marks": n_marks,
        "event": event_name,
        "quality_breakdown": quality,
        "type_breakdown": types,
        "datum_summary": datum,
        "observed_quantity": "water_surface_elevation",
        "caveats": caveats,
        "notes": notes,
    }
