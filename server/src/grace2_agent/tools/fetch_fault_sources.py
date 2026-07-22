"""``fetch_fault_sources`` atomic tool -- real active-fault seismic sources (task #199).

Fetches **real, harmonized** active-fault traces with their slip rates from the
GEM Global Active Faults Database (GAF). These are the seismic SOURCES a
physics-based PSHA needs: each fault carries a geographic trace plus the
kinematic attributes (net slip rate, dip, rake, seismogenic depth range) that
the OpenQuake deck builder turns into a moment-balanced truncated
Gutenberg-Richter magnitude-frequency distribution on a ``simpleFaultSource``.

This is the gap NATE hit: the OpenQuake worker only had a SYNTHETIC area source
(a uniform GR rectangle over the AOI), so a real-fault hazard map ("show the
seismic hazard along the San Andreas") fell back to a fabricated smear instead
of hazard that PEAKS ON the actual fault trace. ``fetch_fault_sources`` is the
real-source companion: a proven local SF run drove these records into a
fault-aligned hazard map (max 1.23 g, peaking ON the San Andreas trace).

**Data source** (free, public, no API key):

    PRIMARY -- GEM Global Active Faults, harmonized GeoJSON:
        https://raw.githubusercontent.com/GEMScienceTools/
            gem-global-active-faults/master/geojson/
            gem_active_faults_harmonized.geojson
        ~10.6 MB, 13696 faults worldwide. Cached for 30 days (the database is
        a versioned research artifact, not a live feed). One global file ->
        we cache the whole thing once, then filter to the AOI in-process.

**GEM property encoding** (IMPORTANT): the harmonized properties are STRINGS in
a ``'(best,min,max)'`` triple form, e.g. ``net_slip_rate='(15.15,10.49,19.18)'``
(mm/yr), ``average_dip='(90,,)'`` (deg, min/max omitted), ``average_rake``,
``upper_seis_depth``, ``lower_seis_depth`` (km). We parse the FIRST
(best-estimate) value of each triple. Plain numbers / lists are also handled.

**Geometry**: ``LineString`` or ``MultiLineString`` of ``[lon, lat]`` (sometimes
``[lon, lat, z]``) vertices -- the fault surface trace. A MultiLineString trace
is flattened to a single ordered vertex list.

**Filter**: keep a fault iff its trace intersects the requested AOI bbox AND it
has a positive net slip rate AND at least 2 trace vertices (a 1-point or
slip<=0 trace cannot drive a moment-balanced MFD).

**Honest degrade** (data-source fallback norm): an AOI with NO active faults is
NOT an error and NOT fabricated -- we return an empty ``faults`` list plus a
typed ``note`` ("no GEM active faults intersect this AOI ...") so the caller can
honestly tell the user the area has no mapped active faults (then fall back to
the synthetic area source if a hazard run is still wanted). The only hard error
is a malformed bbox (caller bug) or a genuine upstream failure with no cache.

**Output**: on a NON-empty fetch a ``FaultSourcesResult`` -- a vector
``LayerURI`` (task #207) that AUTO-RENDERS the fault traces on the map (red/
orange lines) AND carries the SOURCE-MODEL records the OpenQuake deck builder
consumes; the agent passes its ``faults`` list straight to
``render_fault_source_model_xml`` in the worker's ``job_ini``. On an EMPTY fetch
a plain ``dict`` with no layer (honesty gate). The records shape (the LayerURI's
``faults`` field / the dict's ``faults`` key)::

    {
      "catalog": "gem",
      "bbox": [minlon, minlat, maxlon, maxlat],
      "fault_count": <int>,
      "faults": [
        {
          "name": str,
          "geometry": [[lon, lat], ...],        # the trace, in-order
          "net_slip_rate_mm_yr": float,         # best estimate
          "dip_deg": float,
          "rake_deg": float,
          "upper_seis_depth_km": float,
          "lower_seis_depth_km": float,
          "slip_type": str | None,
          "catalog_name": str | None,           # source sub-catalog (e.g. UCERF3)
        },
        ...
      ],
      "note": str | None,                       # set only on empty-AOI degrade
      "source": "GEM Global Active Faults (harmonized)",
    }

Tier-1, no auth, ``supports_global_query=True`` (GEM GAF is worldwide).

FR-AS-11 typed-error surface; FR-DC-3 cache shim; data-source fallback norm.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

import httpx

from grace2_contracts.execution import LayerURI, LegendClass, LegendKey
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_fault_sources",
    "FaultSourcesResult",
    "estimate_payload_mb",
    "FaultSourcesError",
    "FaultSourcesInputError",
    "FaultSourcesUpstreamError",
    "GEM_GAF_URL",
    "FAULT_LINE_STYLE_PRESET",
    "first_num",
    "trace_coords",
    "faults_to_feature_collection",
    "_trace_hits_bbox",
    "_parse_fault_feature",
    "_filter_faults_to_bbox",
    "_fetch_gem_gaf_bytes",
]

logger = logging.getLogger("grace2_agent.tools.fetch_fault_sources")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class FaultSourcesError(RuntimeError):
    """Base class for fetch_fault_sources failures.

    ``error_code`` maps to the WebSocket A.6 error frame; ``retryable`` guides
    FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "FAULT_SOURCES_ERROR"
    retryable: bool = True


class FaultSourcesInputError(FaultSourcesError):
    """Invalid inputs -- malformed bbox / unknown catalog. Not retryable."""

    error_code = "FAULT_SOURCES_INPUT_ERROR"
    retryable = False


class FaultSourcesUpstreamError(FaultSourcesError):
    """GEM GAF download failed and no cache was available. Retryable."""

    error_code = "FAULT_SOURCES_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: GEM Global Active Faults, harmonized GeoJSON (worldwide; ~10.6 MB, 13696
#: faults). Versioned research artifact -> 30-day cache.
GEM_GAF_URL = (
    "https://raw.githubusercontent.com/GEMScienceTools/"
    "gem-global-active-faults/master/geojson/"
    "gem_active_faults_harmonized.geojson"
)

#: Polite User-Agent (matches sibling tools).
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

#: The download is ~10.6 MB across the public CDN; allow a generous timeout.
_HTTP_TIMEOUT = 120.0

#: Supported source catalogs (only GEM today; kept open for future catalogs).
_VALID_CATALOGS = ("gem",)

#: Provenance label, mirrored on both the dict and LayerURI return shapes.
_SOURCE_LABEL = "GEM Global Active Faults (harmonized)"

#: Style-preset label for the surfaced fault-trace vector. Semantic name (kept
#: in lockstep with ``model_seismic_hazard_scenario.FAULT_LINE_STYLE_PRESET``):
#: the web renders an unknown LINE preset in its geometry-family colour so the
#: traces draw as a distinct line. NATE: faults read as red/orange lines.
FAULT_LINE_STYLE_PRESET = "fault_line"


# ---------------------------------------------------------------------------
# Result type -- a renderable fault-trace ``LayerURI`` that ALSO carries the
# source-model records (task #207 auto-publish fix).
#
# Before this, ``fetch_fault_sources`` returned a bare ``dict``, so the
# ``emit_tool_call`` ``add_loaded_layer`` gate -- which fires only on an
# ``isinstance(result, LayerURI)`` return -- never surfaced the fetched fault
# lines. The model then HALLUCINATED "the fault traces are now displayed" with
# nothing on the map. ``FaultSourcesResult`` subclasses ``LayerURI`` (mirrors
# ``fetch_topobathy.TopobathyResult``): the gate auto-renders the fault traces
# as a vector line layer, while the OpenQuake deck builder + the in-process
# ``resolve_fault_sources`` consumer still read the kinematic records off the
# extra fields. The HONESTY GATE is preserved upstream: a zero-fault AOI returns
# the plain empty dict (NOT this type), so no layer is ever fabricated.
# ---------------------------------------------------------------------------


class FaultSourcesResult(LayerURI):
    """A fault-trace vector ``LayerURI`` carrying the GEM source-model records.

    Extra fields beyond ``LayerURI``:

    - ``catalog`` -- the source catalog ("gem").
    - ``fault_count`` -- number of fault-source records (== ``len(faults)``).
    - ``faults`` -- the kinematic source records (geometry trace + slip rate +
      dip / rake / seismogenic-depth band) the OpenQuake deck builder turns into
      ``simpleFaultSource`` sources. This is the SAME list the legacy dict shape
      carried under ``faults``.
    - ``source`` -- provenance string.
    - ``note`` -- always ``None`` on this (non-empty, rendered) path; the empty
      AOI degrade returns a plain dict with a populated ``note`` instead.
    """

    catalog: str = "gem"
    fault_count: int = 0
    faults: list[dict[str, Any]] = []
    source: str = _SOURCE_LABEL
    note: str | None = None


# ---------------------------------------------------------------------------
# AtomicToolMetadata -- registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_fault_sources",
    ttl_class="static-30d",
    source_class="gem_active_faults",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: Any = None,
    catalog: str = "gem",
    **_kw: Any,
) -> float:
    """Estimate the returned-payload size (MB).

    The fetcher returns a small JSON record set (a handful to a few dozen
    faults per AOI), NOT the 10.6 MB source file. Cap the estimate at a small
    constant so the chat payload-warning banner never fires for this tool.
    """
    return 0.2


# ---------------------------------------------------------------------------
# GEM-property parsing helpers (ported from /tmp/oq_realfault_e2e.py).
# ---------------------------------------------------------------------------


def first_num(v: Any, default: float | None = None) -> float | None:
    """Take the FIRST (best-estimate) value of a GEM property.

    GEM harmonized fields are strings like ``'(15.15,10.49,19.18)'`` (best,
    min, max) or ``'(38,,)'`` (best only). This also tolerates plain numbers
    and lists. Returns ``default`` when the value is missing/blank/unparseable.
    """
    if v is None:
        return default
    if isinstance(v, bool):  # guard: bool is an int subclass
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (list, tuple)):
        return float(v[0]) if v and v[0] not in (None, "") else default
    if isinstance(v, str):
        head = v.strip().lstrip("(").split(",")[0].strip()
        try:
            return float(head)
        except ValueError:
            return default
    return default


def trace_coords(geometry: dict[str, Any] | None) -> list[list[float]]:
    """Flatten a fault geometry to an ordered ``[[lon, lat], ...]`` vertex list.

    Handles ``LineString`` and ``MultiLineString`` (the only shapes GEM GAF
    uses). A 3rd ``z`` ordinate, when present, is dropped -- the trace is a
    2-D surface line. Anything else yields an empty list.
    """
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if not coords:
        return []
    if gtype == "LineString":
        pts = coords
    elif gtype == "MultiLineString":
        pts = [p for line in coords for p in line]
    else:
        return []
    out: list[list[float]] = []
    for p in pts:
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            out.append([float(p[0]), float(p[1])])
    return out


def _trace_hits_bbox(
    pts: list[list[float]], bbox: tuple[float, float, float, float]
) -> bool:
    """True iff any trace vertex falls inside the AOI bbox.

    Faithful to the proven local run: an intersection test on the trace
    vertices (a fault whose trace passes through the AOI has a vertex inside
    it at GAF's vertex density). ``bbox`` is ``(minlon, minlat, maxlon, maxlat)``.
    """
    minlon, minlat, maxlon, maxlat = bbox
    return any(
        minlon <= p[0] <= maxlon and minlat <= p[1] <= maxlat for p in pts
    )


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    """Coerce + validate the AOI bbox to ``(minlon, minlat, maxlon, maxlat)``."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise FaultSourcesInputError(
            f"bbox must be [min_lon, min_lat, max_lon, max_lat]; got {bbox!r}"
        )
    try:
        minlon, minlat, maxlon, maxlat = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise FaultSourcesInputError(
            f"bbox values must be numeric; got {bbox!r}"
        ) from exc
    if not (minlon < maxlon and minlat < maxlat):
        raise FaultSourcesInputError(
            f"bbox must satisfy min<max on both axes; got {bbox!r}"
        )
    if not (-180.0 <= minlon <= 180.0 and -180.0 <= maxlon <= 180.0):
        raise FaultSourcesInputError(f"longitudes out of range in {bbox!r}")
    if not (-90.0 <= minlat <= 90.0 and -90.0 <= maxlat <= 90.0):
        raise FaultSourcesInputError(f"latitudes out of range in {bbox!r}")
    return (minlon, minlat, maxlon, maxlat)


def _parse_fault_feature(feature: dict[str, Any]) -> dict[str, Any] | None:
    """Parse one GEM GAF feature into a fault-source record.

    Returns ``None`` (skip) when the fault has no usable slip rate or fewer
    than 2 trace vertices -- those cannot drive a moment-balanced MFD.
    """
    if not isinstance(feature, dict):
        return None
    props = feature.get("properties") or {}
    pts = trace_coords(feature.get("geometry"))
    # Require >=2 DISTINCT vertices: a degenerate (all-coincident) trace has zero
    # haversine length, which is the only realistic way a fetched fault could pass
    # here yet fail the worker's length/moment-balance render gate. Dropping it
    # keeps the composer's real-fault stamp in lockstep with what the worker builds
    # (honesty floor), without importing the worker module agent-side.
    if len({(round(p[0], 6), round(p[1], 6)) for p in pts}) < 2:
        return None

    slip = first_num(props.get("net_slip_rate"))
    if slip is None or slip <= 0:
        return None

    # GEM defaults match the proven local run: vertical strike-slip, full
    # seismogenic depth when the field is blank.
    dip = first_num(props.get("average_dip"), 90.0)
    rake = first_num(props.get("average_rake"), 180.0)
    usd = first_num(props.get("upper_seis_depth"), 0.0)
    lsd = first_num(props.get("lower_seis_depth"), 12.0)
    if lsd is None or usd is None or lsd <= usd:
        # Degenerate / missing depth range -> default a 12 km seismogenic band.
        usd = usd if usd is not None else 0.0
        lsd = usd + 12.0

    slip_type = props.get("slip_type")
    catalog_name = props.get("catalog_name")
    return {
        "name": str(props.get("name") or "fault"),
        "geometry": pts,
        "net_slip_rate_mm_yr": float(slip),
        "dip_deg": float(dip),
        "rake_deg": float(rake),
        "upper_seis_depth_km": float(usd),
        "lower_seis_depth_km": float(lsd),
        "slip_type": str(slip_type) if slip_type else None,
        "catalog_name": str(catalog_name) if catalog_name else None,
    }


def _filter_faults_to_bbox(
    features: list[dict[str, Any]], bbox: tuple[float, float, float, float]
) -> list[dict[str, Any]]:
    """Parse + bbox-filter the GAF feature list into fault-source records."""
    out: list[dict[str, Any]] = []
    for feature in features:
        pts = trace_coords(feature.get("geometry") if isinstance(feature, dict) else None)
        if not _trace_hits_bbox(pts, bbox):
            continue
        record = _parse_fault_feature(feature)
        if record is not None:
            out.append(record)
    return out


# ---------------------------------------------------------------------------
# Fault records -> renderable GeoJSON FeatureCollection (auto-publish path).
# ---------------------------------------------------------------------------


def faults_to_feature_collection(
    faults: list[dict[str, Any]],
) -> dict[str, Any]:
    """Serialize fault-source records to a GeoJSON ``FeatureCollection``.

    One ``LineString`` ``Feature`` per fault (``coordinates`` = the record's
    ``[[lon, lat], ...]`` trace), carrying the click-inspect ``properties``
    ``name`` / ``net_slip_rate_mm_yr`` / ``dip_deg`` / ``rake_deg`` /
    ``upper_seis_depth_km`` / ``lower_seis_depth_km`` / ``slip_type`` (plus
    ``catalog_name`` when present). Records whose trace has fewer than 2 vertices
    are SKIPPED (a degenerate trace is not a drawable line) -- this mirrors the
    fetcher's own >=2-distinct-vertex gate, so in practice every fetched record
    yields a feature.

    Pure dict work (no I/O, no reproject -- the traces are already EPSG:4326
    lon/lat). Returns a valid (possibly empty) FeatureCollection.
    """
    features: list[dict[str, Any]] = []
    for rec in faults or []:
        coords = rec.get("geometry") or []
        line: list[list[float]] = []
        for p in coords:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                line.append([float(p[0]), float(p[1])])
        if len(line) < 2:
            continue
        props: dict[str, Any] = {
            "name": str(rec.get("name") or "fault"),
            "net_slip_rate_mm_yr": rec.get("net_slip_rate_mm_yr"),
            "dip_deg": rec.get("dip_deg"),
            "rake_deg": rec.get("rake_deg"),
            "upper_seis_depth_km": rec.get("upper_seis_depth_km"),
            "lower_seis_depth_km": rec.get("lower_seis_depth_km"),
            "slip_type": rec.get("slip_type"),
        }
        if rec.get("catalog_name"):
            props["catalog_name"] = str(rec.get("catalog_name"))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": line},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Upstream fetch (miss-path fetcher passed to read_through).
# ---------------------------------------------------------------------------


def _fetch_gem_gaf_bytes() -> bytes:
    """Download the GEM GAF harmonized GeoJSON (the whole worldwide file).

    Passed to ``read_through`` as the cache-miss fetcher: the 30-day cache
    means this hits the public CDN at most once per month. Raises a typed
    upstream error on any HTTP / network failure.
    """
    try:
        resp = httpx.get(
            GEM_GAF_URL,
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise FaultSourcesUpstreamError(
            f"GEM Global Active Faults download failed: {exc}"
        ) from exc
    data = resp.content
    if not data:
        raise FaultSourcesUpstreamError(
            "GEM Global Active Faults download returned an empty body"
        )
    return data


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=True,
    payload_mb_estimator_name="estimate_payload_mb",
    # Annotations: readOnlyHint=True (read-only), openWorldHint=True (external
    # public endpoint), destructiveHint=False, idempotentHint=True (cache shim
    # deduplicates).
    open_world_hint=True,
)
def fetch_fault_sources(
    bbox: list[float] | tuple[float, float, float, float],
    *,
    catalog: str = "gem",
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer; kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Fetch REAL active-fault seismic sources (traces + slip rates) for an AOI.

    Retrieves harmonized active-fault traces from the GEM Global Active Faults
    Database and returns the kinematic source records the OpenQuake deck builder
    turns into physics-based ``simpleFaultSource`` seismic sources (each with a
    moment-balanced truncated Gutenberg-Richter MFD derived from the fault's
    slip rate). This is the REAL-SOURCE path for PSHA -- hazard that peaks ON the
    actual fault trace, not a synthetic uniform-rate rectangle over the AOI.

    When to use:
        - The user asks for seismic/earthquake hazard "along" a named fault or
          in a tectonically active area ("seismic hazard along the San Andreas",
          "earthquake PSHA for the SF Bay", "PGA map near the Hayward fault").
        - You are about to build an OpenQuake classical-PSHA deck and want REAL
          fault sources instead of the synthetic AOI area source.
        - The user wants to know which active faults pass through an area and
          how fast they slip.

    When NOT to use:
        - Historical earthquake CATALOGS / observed events -- this is the fault
          SOURCE model (where future ruptures nucleate), not a record of past
          quakes.
        - Ground-shaking outputs / hazard rasters -- those are produced by the
          OpenQuake run that CONSUMES these sources, not by this fetcher.
        - Tsunami / surge / flood hazard -- unrelated engines.

    Parameters:
        bbox: ``[min_lon, min_lat, max_lon, max_lat]`` in EPSG:4326. Required.
            Example (SF peninsula): ``[-122.55, 37.45, -122.15, 37.90]``.
        catalog: source catalog. Only ``"gem"`` (GEM Global Active Faults) is
            supported today.

    Returns:
        On a NON-empty fetch: a ``FaultSourcesResult`` -- a renderable vector
        ``LayerURI`` (the fault traces AUTO-RENDER on the map as red/orange lines;
        do NOT call ``publish_layer`` on it) that ALSO carries the source-model
        records on its ``faults`` field (each ``{name, geometry (lon/lat trace),
        net_slip_rate_mm_yr, dip_deg, rake_deg, upper_seis_depth_km,
        lower_seis_depth_km, slip_type, catalog_name}``), plus ``catalog``,
        ``fault_count``, and ``source``. Pass ``.faults`` straight to the worker's
        ``render_fault_source_model_xml``.

        On an EMPTY fetch: a plain ``dict`` with ``fault_count=0``, an empty
        ``faults`` list, and a typed ``note`` -- and NO layer (see below).

    Honest degrade (data-source fallback norm):
        An AOI with NO mapped active faults returns an EMPTY ``faults`` list and
        a typed ``note`` -- it is NOT an error, the tool NEVER fabricates a fault,
        and (the honesty gate) NO map layer is produced, so a "fault lines
        displayed" claim is never grounded when nothing was drawn. The caller can
        honestly report "no mapped active faults here" (and fall back to the
        synthetic area source if a run is still wanted).

    Raises:
        FaultSourcesInputError: malformed bbox or unknown catalog (caller bug).
        FaultSourcesUpstreamError: GEM GAF download failed and no cache exists.
    """
    cat = str(catalog or "gem").strip().lower()
    if cat not in _VALID_CATALOGS:
        raise FaultSourcesInputError(
            f"unknown catalog {catalog!r}; supported: {_VALID_CATALOGS}"
        )
    q_bbox = _validate_bbox(bbox)

    # The cache key keys on the source FILE (one worldwide GeoJSON), NOT the
    # AOI -- the whole database is cached once and every AOI filters the same
    # cached bytes in-process. So the params dict is a stable constant.
    params = {"file": "gem_active_faults_harmonized"}
    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="geojson",
        fetch_fn=_fetch_gem_gaf_bytes,
    )

    try:
        collection = json.loads(result.data)
    except (ValueError, TypeError) as exc:
        raise FaultSourcesUpstreamError(
            f"GEM Global Active Faults payload was not valid GeoJSON: {exc}"
        ) from exc
    features = collection.get("features") if isinstance(collection, dict) else None
    if not isinstance(features, list):
        raise FaultSourcesUpstreamError(
            "GEM Global Active Faults payload had no 'features' array"
        )

    faults = _filter_faults_to_bbox(features, q_bbox)

    # HONESTY GATE: a zero-fault AOI is NOT an error and is NEVER given a layer
    # (the hallucination fix -- the model can only claim "displayed" when a real
    # LayerURI grounds the narration). Return the plain empty dict + typed note.
    if not faults:
        note = (
            "No GEM active faults intersect this AOI. The area has no mapped "
            "active-fault sources; a fault-based PSHA cannot be built here "
            "(fall back to the synthetic area source if a hazard run is still "
            "wanted)."
        )
        logger.info(
            "fetch_fault_sources: no active faults in AOI bbox=%s (honest "
            "empty degrade -- no layer)",
            list(q_bbox),
        )
        return {
            "catalog": cat,
            "bbox": list(q_bbox),
            "fault_count": 0,
            "faults": [],
            "note": note,
            "source": _SOURCE_LABEL,
        }

    # Non-empty: serialize the fault traces to a GeoJSON FeatureCollection,
    # persist it through the SAME cache read-through the sibling geometry
    # fetchers use (fetch_roads_osm / fetch_administrative_boundaries), and
    # return a vector ``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer``
    # gate AUTO-RENDERS the fault lines (no separate publish_layer step). The
    # cache key is the AOI vector params (distinct from the global-file params
    # above), so this is a second, AOI-scoped cache entry.
    fc = faults_to_feature_collection(faults)
    n_features = len(fc.get("features") or [])
    if n_features <= 0:
        # Defensive: every fetched record has >=2 distinct vertices, so this is
        # unreachable in practice. If it ever fires, degrade honestly to data-
        # only (no fabricated layer) rather than emit an empty vector.
        logger.warning(
            "fetch_fault_sources: %d fault(s) yielded no drawable trace for "
            "bbox=%s; returning data-only (no layer)",
            len(faults),
            list(q_bbox),
        )
        return {
            "catalog": cat,
            "bbox": list(q_bbox),
            "fault_count": len(faults),
            "faults": faults,
            "note": None,
            "source": _SOURCE_LABEL,
        }

    fc_bytes = json.dumps(fc).encode("utf-8")
    vector_params = {
        "vector": "fault_traces",
        "bbox": list(q_bbox),
        "catalog": cat,
    }
    vector = read_through(
        metadata=_METADATA,
        params=vector_params,
        ext="geojson",
        fetch_fn=lambda: fc_bytes,
    )
    assert vector.uri is not None, (
        "fetch_fault_sources is cacheable; the vector uri must be set"
    )

    logger.info(
        "fetch_fault_sources: %d active fault(s) -> %d trace feature(s) in AOI "
        "bbox=%s (gaf cache hit=%s, vector uri=%s)",
        len(faults),
        n_features,
        list(q_bbox),
        result.hit,
        vector.uri,
    )

    plural = "trace" if n_features == 1 else "traces"
    return FaultSourcesResult(
        layer_id=f"fault-sources-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"Active fault {plural} ({n_features})",
        layer_type="vector",
        uri=vector.uri,
        style_preset=FAULT_LINE_STYLE_PRESET,
        role="context",
        units=None,
        bbox=q_bbox,
        legend=LegendKey(
            kind="categorical",
            classes=[
                LegendClass(
                    value="fault",
                    color="#FF6A00",
                    label="Active fault trace",
                )
            ],
            label="Active faults (GEM)",
        ),
        catalog=cat,
        fault_count=len(faults),
        faults=faults,
        source=_SOURCE_LABEL,
        note=None,
    )
