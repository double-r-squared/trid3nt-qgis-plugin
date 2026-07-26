"""``fetch_high_water_marks`` atomic tool -- USGS STN flood-event high-water marks.

Fetches surveyed HIGH-WATER MARKS (HWMs) from the USGS Short-Term Network
(STN) Flood Event Data Portal (``stn.wim.usgs.gov/STNServices``) as a point
FlatGeobuf: the physical evidence (debris line, seed line, mud line, stain
line) of a flood's PEAK water-surface elevation, surveyed after the event.
This is the canonical OBSERVED peak-stage truth for validating a modeled
max-flood-depth / max-WSE surface.

Each mark carries lat/lon, peak ELEVATION (``elev_ft``), its VERTICAL DATUM
(usually NAVD88 -- verify against the model datum before differencing), a
surveyor QUALITY rating (Excellent +/-0.05 ft ... VP > 0.40 ft), the HWM TYPE
(seed/debris/stain/mud line), and the flood environment. The returned layer's
envelope carries a QUALITY breakdown + honest caveats.

STN has NO server-side bbox filter, so this fetches by EVENT (when named) or
by the US STATE(S) the bbox overlaps, then clips to the AOI client-side.
``supports_global_query=False`` (US + territories only).
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_high_water_marks",
    "estimate_payload_mb",
    "HighWaterMarksLayerURI",
    "HwmError",
    "HwmInputError",
    "HwmEventNotFoundError",
    "HwmUpstreamError",
    "HwmNoMarksError",
    "_http_get",
    "_fetch_events",
    "_fetch_filtered_hwms",
    "_resolve_event_id",
    "_parse_hwm_records",
    "_build_flatgeobuf",
    "_states_overlapping_bbox",
    "EVENTS_URL",
    "FILTERED_HWMS_URL",
]

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers.hydrology.fetch_high_water_marks"
)


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class HwmError(RuntimeError):
    """Base class for fetch_high_water_marks failures."""

    error_code: str = "HWM_ERROR"
    retryable: bool = True


class HwmInputError(HwmError):
    """Bad inputs -- missing/malformed bbox, bbox outside the US, bad event."""

    error_code = "HWM_INPUT_ERROR"
    retryable = False


class HwmEventNotFoundError(HwmError):
    """The named ``event`` did not resolve to exactly one STN flood event."""

    error_code = "HWM_EVENT_NOT_FOUND"
    retryable = False


class HwmUpstreamError(HwmError):
    """STN request failed (network error, HTTP 5xx, unparseable body)."""

    error_code = "HWM_UPSTREAM_ERROR"
    retryable = True


class HwmNoMarksError(HwmError):
    """No high-water marks found in the AOI for the scope -- honest, not empty."""

    error_code = "HWM_NO_MARKS"
    retryable = False


# ---------------------------------------------------------------------------
# Result type -- LayerURI subclass carrying the quality/type envelope.
# ---------------------------------------------------------------------------


class HighWaterMarksLayerURI(LayerURI):
    """The HWM point ``LayerURI`` plus the survey-quality envelope.

    Extra fields beyond ``LayerURI``:

    - ``n_marks`` -- HWM count in the AOI.
    - ``event`` -- resolved flood-event name (or None for a state-scoped fetch).
    - ``quality_breakdown`` -- ``{quality_label: count}`` (surveyor accuracy).
    - ``type_breakdown`` -- ``{hwm_type: count}`` (seed/debris/stain/mud line).
    - ``datum_summary`` -- ``{vertical_datum: count}``.
    - ``observed_quantity`` -- the physical quantity the ``elev_ft`` observed
      field carries: ``"water_surface_elevation"`` (a WSE above the stated
      vertical datum, NOT a depth-above-ground). Stamped so
      ``extract_model_at_observations`` never silently pairs this WSE against a
      model DEPTH raster (the two need a ground-elevation conversion first).
    - ``caveats`` -- honest usage caveats (quality spread, datum, point-peak).
    - ``notes`` -- provenance detail.
    """

    n_marks: int = 0
    event: str | None = None
    quality_breakdown: dict[str, int] = {}
    type_breakdown: dict[str, int] = {}
    datum_summary: dict[str, int] = {}
    observed_quantity: str = "water_surface_elevation"
    caveats: list[str] = []
    notes: list[str] = []


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: STN flood-event list (event_id <-> event_name).
EVENTS_URL = "https://stn.wim.usgs.gov/STNServices/Events.json"

#: STN filtered high-water-mark query (Event / States filters; no bbox param).
FILTERED_HWMS_URL = "https://stn.wim.usgs.gov/STNServices/HWMs/FilteredHWMs.json"

_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

_HTTP_TIMEOUT = 90.0

#: Style token (categorical HWM points; publish_layer renders generically).
_STYLE_PRESET = "usgs_high_water_marks"

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


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_high_water_marks",
    ttl_class="semi-static-7d",
    source_class="usgs_stn_hwm",
    cacheable=True,
    supports_global_query=False,
)


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    event: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output FlatGeobuf size in MB.

    Each HWM is one Point with ~20 small scalar properties (~250 bytes). Even a
    large event in a metro AOI is a few hundred marks -> well under 1 MB.
    """
    n = 400 if event else 150
    if bbox is not None:
        try:
            w, s, e, n_ = bbox
            sq = max(0.0, e - w) * max(0.0, n_ - s)
            n = max(20, int(sq * 200))
        except (TypeError, ValueError):
            pass
    return max(0.001, n * 250 / 1_000_000.0)


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise HwmInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise HwmInputError(f"bbox contains non-numeric values: {bbox!r}") from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise HwmInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise HwmInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise HwmInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise HwmInputError(f"bbox is degenerate (min must be < max): {bbox!r}")
    return (west, south, east, north)


# ---------------------------------------------------------------------------
# HTTP + fetch seams (patched in offline tests).
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    """Plain HTTP GET. Raises ``HwmUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise HwmUpstreamError(
            f"USGS STN returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise HwmUpstreamError(f"Network error fetching {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HwmUpstreamError(f"Timed out after {timeout}s fetching {url}") from exc


def _fetch_events() -> list[dict[str, Any]]:
    """GET the STN event list. Raises ``HwmUpstreamError`` on a bad body."""
    raw = _http_get(EVENTS_URL)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HwmUpstreamError(f"STN Events response is not valid JSON: {exc}") from exc
    if not isinstance(obj, list):
        raise HwmUpstreamError("STN Events response was not a JSON array.")
    return obj


def _fetch_filtered_hwms(
    event_id: int | None, states: list[str] | None
) -> list[dict[str, Any]]:
    """GET FilteredHWMs for an event_id and/or a state list -> raw records."""
    params: list[tuple[str, str]] = []
    if event_id is not None:
        params.append(("Event", str(event_id)))
    if states:
        params.append(("States", ",".join(states)))
    url = FILTERED_HWMS_URL + ("?" + urllib.parse.urlencode(params) if params else "")
    raw = _http_get(url)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HwmUpstreamError(f"STN FilteredHWMs is not valid JSON: {exc}") from exc
    if not isinstance(obj, list):
        raise HwmUpstreamError("STN FilteredHWMs response was not a JSON array.")
    return obj


# ---------------------------------------------------------------------------
# Event resolution + spatial scoping.
# ---------------------------------------------------------------------------


def _resolve_event_id(event: str) -> tuple[int, str]:
    """Resolve a named event to ``(event_id, event_name)`` via a substring match.

    Raises ``HwmEventNotFoundError`` when zero or several events match (an
    ambiguous match lists the candidates so the caller can disambiguate).
    """
    needle = event.strip().lower()
    if not needle:
        raise HwmInputError("event was empty after trimming.")
    events = _fetch_events()
    exact = [e for e in events if str(e.get("event_name", "")).strip().lower() == needle]
    if len(exact) == 1:
        return int(exact[0]["event_id"]), str(exact[0]["event_name"])
    matches = [
        e for e in events if needle in str(e.get("event_name", "")).strip().lower()
    ]
    if len(matches) == 1:
        return int(matches[0]["event_id"]), str(matches[0]["event_name"])
    if not matches:
        raise HwmEventNotFoundError(
            f"no STN flood event matches event={event!r}. Example event names: "
            + ", ".join(sorted(str(e.get("event_name", "")) for e in events)[:8])
        )
    raise HwmEventNotFoundError(
        f"event={event!r} is ambiguous ({len(matches)} STN events match): "
        + ", ".join(sorted(str(m.get("event_name", "")) for m in matches)[:10])
        + ". Use a more specific event name."
    )


def _states_overlapping_bbox(bbox: tuple[float, float, float, float]) -> list[str]:
    """Return the 2-letter USPS codes of the US states the bbox overlaps."""
    from trid3nt_server.tools.fetchers.weather.fetch_asos_metar import (
        _STATE_BBOX,
        _bbox_overlaps_state,
    )

    return sorted(
        st for st, box in _STATE_BBOX.items() if _bbox_overlaps_state(bbox, box)
    )


# ---------------------------------------------------------------------------
# Record parsing + FlatGeobuf builder.
# ---------------------------------------------------------------------------


def _f(v: Any) -> float | None:
    try:
        fv = float(v)
        return fv if math.isfinite(fv) else None
    except (TypeError, ValueError):
        return None


def _parse_hwm_records(
    raw: list[dict[str, Any]], bbox: tuple[float, float, float, float]
) -> list[dict[str, Any]]:
    """Filter FilteredHWMs records to the bbox; normalize to point records.

    Keeps only records with a parseable HWM coordinate inside ``bbox``. A
    missing ``elev_ft`` is kept as ``None`` (honest -- never fabricated). Uses
    the inline ``*Name`` fields so no lookup-table round-trip is needed.
    """
    west, south, east, north = bbox
    out: list[dict[str, Any]] = []
    for r in raw:
        lat = _f(r.get("latitude"))
        lon = _f(r.get("longitude"))
        if lat is None or lon is None:
            continue
        if not (west <= lon <= east and south <= lat <= north):
            continue
        out.append(
            {
                "hwm_id": r.get("hwm_id"),
                "site_no": str(r.get("site_no") or ""),
                "lon": lon,
                "lat": lat,
                # QUANTITY STAMP: elev_ft is a WATER-SURFACE ELEVATION (above the
                # stated vertical_datum), NOT a depth above ground. Stamped onto
                # every feature so extract_model_at_observations can read it back
                # from the FGB and refuse to silently pair this WSE against a
                # model flood-DEPTH raster without a ground-elevation conversion.
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
            }
        )
    return out


def _build_flatgeobuf(records: list[dict[str, Any]]) -> bytes:
    """Serialize HWM records -> FlatGeobuf (Point, EPSG:4326)."""
    try:
        import geopandas as gpd
        from shapely.geometry import Point
    except ImportError as exc:  # noqa: BLE001
        raise HwmUpstreamError(f"geopandas / shapely not available: {exc}") from exc

    geoms = [Point(r["lon"], r["lat"]) for r in records]
    scalar_cols = [
        "hwm_id", "site_no", "quantity", "elev_ft", "height_above_gnd",
        "vertical_datum", "horizontal_datum", "quality", "quality_id",
        "hwm_type", "hwm_type_id", "hwm_environment", "event", "event_id",
        "state", "county", "waterbody", "survey_date", "stillwater",
        "hwm_label",
    ]
    data = {c: [r.get(c) for r in records] for c in scalar_cols}
    gdf = gpd.GeoDataFrame(data, geometry=geoms, crs="EPSG:4326")

    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_hwm_"
        ) as f:
            tmp = f.name
        gdf.to_file(tmp, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001
        raise HwmUpstreamError(
            f"FlatGeobuf write failed for {len(records)} HWMs: {exc}"
        ) from exc
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Quality / type / datum breakdown + extent for the envelope."""
    from collections import Counter

    q = Counter(r.get("quality") or "Unknown/Historical" for r in records)
    t = Counter(r.get("hwm_type") or "Unspecified" for r in records)
    d = Counter(r.get("vertical_datum") or "unspecified" for r in records)
    lons = [r["lon"] for r in records]
    lats = [r["lat"] for r in records]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    if west == east:
        west -= 0.02
        east += 0.02
    if south == north:
        south -= 0.02
        north += 0.02
    return {
        "quality_breakdown": dict(q),
        "type_breakdown": dict(t),
        "datum_summary": dict(d),
        "extent": (west, south, east, north),
    }


# ---------------------------------------------------------------------------
# Top-level fetch (primary -> fallback -> honest typed error).
# ---------------------------------------------------------------------------


def _fetch_hwm_bytes(
    bbox: tuple[float, float, float, float],
    event_id: int | None,
    states: list[str],
) -> bytes:
    """Event-scoped fetch (primary) -> state-scoped fetch (fallback) -> FGB bytes.

    Raises ``HwmNoMarksError`` when both paths yield zero marks inside the AOI.
    """
    records: list[dict[str, Any]] = []
    if event_id is not None:
        raw = _fetch_filtered_hwms(event_id=event_id, states=None)
        records = _parse_hwm_records(raw, bbox)
        logger.info(
            "fetch_high_water_marks: event=%s -> %d mark(s) in AOI (of %d fetched)",
            event_id, len(records), len(raw),
        )

    if not records and states:
        # Fallback (or the no-event primary): fetch by overlapping state(s).
        raw = _fetch_filtered_hwms(event_id=event_id, states=states)
        records = _parse_hwm_records(raw, bbox)
        logger.info(
            "fetch_high_water_marks: states=%s -> %d mark(s) in AOI (of %d fetched)",
            states, len(records), len(raw),
        )

    if not records:
        scope = f"event_id={event_id}" if event_id is not None else f"states={states}"
        raise HwmNoMarksError(
            f"No USGS STN high-water marks found inside the AOI for {scope}. "
            "Either no flood was surveyed here or the marks fall outside the "
            "bbox; widen the AOI or pick a surveyed flood event."
        )
    return _build_flatgeobuf(records)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    open_world_hint=True,
)
def fetch_high_water_marks(
    bbox: tuple[float, float, float, float] | None = None,
    event: str | None = None,
    **_extra_ignored: Any,
) -> HighWaterMarksLayerURI:
    """Fetch surveyed USGS STN flood high-water marks (observed PEAK stage) as points.

    Retrieves post-flood HIGH-WATER MARKS from the USGS Short-Term Network
    (STN) Flood Event Data Portal: surveyed debris/seed/stain/mud lines marking
    a flood's PEAK water-surface elevation. This is the canonical OBSERVED
    peak-stage truth for validating a modeled max-flood-depth / max-WSE
    surface. Each mark carries its peak ``elev_ft``, VERTICAL DATUM (usually
    NAVD88), a surveyor QUALITY rating, and the mark TYPE.

    **When to use:**
    - Validate a modeled peak flood surface: pair these marks with the model
      via ``extract_model_at_observations`` then ``compute_skill_metrics``.
    - "high water marks for Hurricane Michael near Panama City", "surveyed
      flood peak elevations here", "USGS HWMs for this flood".

    **When NOT to use:**
    - Real-time / continuous gauge stage or discharge ->
      ``fetch_usgs_nwis_gauges`` (this is post-event point survey, not a
      time-series).
    - Regulatory flood ZONES -> ``fetch_fema_nfhl_zones``; modeled flood DEPTH
      -> the SFINCS / SWMM engines.
    - A satellite-derived flood EXTENT polygon/raster ->
      ``fetch_flood_extent_observation``.
    - Non-US floods -> unsupported (STN is US + territories only).

    **Parameters:**
    - ``bbox``: REQUIRED ``(west, south, east, north)`` in EPSG:4326 (the AOI;
      marks are clipped to it client-side).
    - ``event``: OPTIONAL flood-event name (e.g. ``"2018 Michael"``,
      ``"2017 Harvey"``). When given it scopes the fetch to that event; when
      omitted the fetch covers all events over the AOI's state(s).

    **Returns:** ``HighWaterMarksLayerURI`` -- a FlatGeobuf point layer
    (EPSG:4326) named ``"USGS high-water marks (<n>)"``. Per-mark properties:
    ``hwm_id``, ``site_no``, ``elev_ft`` (peak elevation; null if unreported),
    ``vertical_datum``, ``quality`` (surveyor rating), ``hwm_type``,
    ``hwm_environment``, ``event``, ``state``/``county``, ``waterbody``,
    ``survey_date``. Envelope: ``n_marks``, ``event``, ``quality_breakdown``,
    ``type_breakdown``, ``datum_summary``, ``caveats``, ``notes``.

    **Fallback (data-source fallback norm):** an event-scoped query is the
    primary; if it yields zero marks in the AOI (or no event was named) the
    tool fetches by the overlapping US state(s) and clips. If BOTH yield zero,
    ``HwmNoMarksError`` is raised -- never an empty success layer.

    **Errors (FR-AS-11):** ``HwmInputError`` (missing/bad bbox, AOI outside the
    US); ``HwmEventNotFoundError`` (named event unresolvable/ambiguous);
    ``HwmUpstreamError`` (STN network/HTTP/parse failure);
    ``HwmNoMarksError`` (no marks in the AOI).

    Cross-tool dependencies:
        - Downstream: ``extract_model_at_observations`` (pairs these marks with
          a model raster) -> ``compute_skill_metrics``.
        - Upstream: ``geocode_location`` / ``fetch_administrative_boundaries``
          (derive the AOI bbox).
        - Source: USGS STN (stn.wim.usgs.gov/STNServices).
    """
    bbox_t = _validate_bbox(bbox)

    resolved_event_id: int | None = None
    resolved_event_name: str | None = None
    if event is not None and str(event).strip():
        resolved_event_id, resolved_event_name = _resolve_event_id(str(event))

    states = _states_overlapping_bbox(bbox_t)
    if resolved_event_id is None and not states:
        raise HwmInputError(
            f"bbox={tuple(round(v, 3) for v in bbox_t)} does not overlap any US "
            "state, and no event was named. USGS STN covers the US + territories "
            "only; pass a US AOI or a named flood event."
        )

    params: dict[str, Any] = {
        "bbox": [round(v, 6) for v in bbox_t],
        "event_id": resolved_event_id,
        "states": states if resolved_event_id is None else None,
    }

    def _fetch_bytes() -> bytes:
        return _fetch_hwm_bytes(bbox_t, resolved_event_id, states)

    result = read_through(
        metadata=_METADATA, params=params, ext="fgb", fetch_fn=_fetch_bytes
    )
    assert result.uri is not None, "fetch_high_water_marks is cacheable; uri required"

    # Summarize from the (hit-or-miss) FGB bytes so the envelope is correct on
    # a cache HIT too (read_through returns .data on both paths).
    records = _records_from_fgb(result.data)
    summary = _summarize(records)

    n_marks = len(records)
    caveats = list(_CAVEATS)
    n_hist = summary["quality_breakdown"].get("Unknown/Historical", 0)
    if n_hist:
        caveats.append(
            f"{n_hist} of {n_marks} mark(s) are 'Unknown/Historical' quality "
            "(no stated accuracy)."
        )
    datums = [d for d in summary["datum_summary"] if d != "unspecified"]
    if len(datums) > 1:
        caveats.append(
            f"Marks span multiple vertical datums {sorted(datums)}; reconcile "
            "to ONE datum before comparing against a model."
        )

    notes = [
        f"USGS STN FilteredHWMs: {n_marks} mark(s) in the AOI"
        + (f" for event {resolved_event_name!r}." if resolved_event_name else
           f" across state(s) {states}."),
        "Observed quantity is WATER-SURFACE ELEVATION (elev_ft, above the "
        "stated vertical_datum) -- stamped on every feature as "
        "quantity='water_surface_elevation'. It is NOT a depth above ground; "
        "pair it against a model flood-DEPTH raster only via "
        "extract_model_at_observations (which converts WSE->depth with a DEM).",
    ]

    return HighWaterMarksLayerURI(
        layer_id=f"usgs-hwm-{_seed(params)}",
        name=f"USGS high-water marks ({n_marks})",
        layer_type="vector",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units="ft (peak elevation; see vertical_datum)",
        bbox=summary["extent"],
        n_marks=n_marks,
        event=resolved_event_name,
        quality_breakdown=summary["quality_breakdown"],
        type_breakdown=summary["type_breakdown"],
        datum_summary=summary["datum_summary"],
        observed_quantity="water_surface_elevation",
        caveats=caveats,
        notes=notes,
    )


def _records_from_fgb(data: bytes) -> list[dict[str, Any]]:
    """Read an FGB byte payload back into simple property dicts (for the envelope)."""
    import geopandas as gpd

    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_hwm_rd_"
        ) as f:
            tmp = f.name
            f.write(data)
        gdf = gpd.read_file(tmp)
    except Exception as exc:  # noqa: BLE001
        raise HwmUpstreamError(f"could not re-read cached HWM FGB: {exc}") from exc
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
                "lon": float(row.geometry.x),
                "lat": float(row.geometry.y),
                "quality": row.get("quality"),
                "hwm_type": row.get("hwm_type"),
                "vertical_datum": row.get("vertical_datum"),
            }
        )
    return recs


def _seed(params: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:8]
