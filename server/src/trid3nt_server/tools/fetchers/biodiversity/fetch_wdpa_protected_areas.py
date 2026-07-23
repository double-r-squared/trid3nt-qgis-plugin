"""``fetch_wdpa_protected_areas`` atomic tool — WDPA polygon fetcher.
"""

from __future__ import annotations

import io
import json
import logging
import math
from typing import Any

import httpx

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = ["fetch_wdpa_protected_areas"]

logger = logging.getLogger("trid3nt_server.tools.fetchers.biodiversity.fetch_wdpa_protected_areas")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class WDPAError(RuntimeError):
    """Base class for fetch_wdpa_protected_areas failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "WDPA_ERROR"
    retryable: bool = True


class WDPAUpstreamError(WDPAError):
    """WDPA ArcGIS REST query failed (network, HTTP, or parse error)."""

    error_code = "WDPA_UPSTREAM_ERROR"
    retryable = True


class WDPABboxError(WDPAError):
    """The bbox failed validation (degenerate, out of range, non-finite)."""

    error_code = "WDPA_BBOX_INVALID"
    retryable = False


class WDPADesignationError(WDPAError):
    """A ``designation_filter`` entry is malformed or an unknown designation.

    Raised by ``_normalize_designation_filter`` when an entry is not a
    non-empty string, or when an entry resolves to neither a known
    ``desig_eng`` value nor a known alias. This makes the "goes18 vs goes-18"
    class of silent-mismatch fail LOUD with a list of accepted forms rather
    than silently filtering every feature out and returning a 0-feature
    FlatGeobuf (the OQ-0089-DESIGNATION-FILTER-SEMANTICS hazard).
    """

    error_code = "WDPA_DESIGNATION_INVALID"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_WDPA_BASE = (
    "https://services5.arcgis.com/Mj0hjvkNtV7NRhA7/ArcGIS/rest/services/"
    "WDPA_v0/FeatureServer/1/query"
)

# WDPA OutFields we keep (live schema field names, lowercase). ``name_eng`` is
# the human-readable site name, ``desig_eng`` is the designation string used
# by ``designation_filter``, ``iucn_cat`` is the IUCN protected-area category,
# ``status`` / ``status_yr`` carry status + year of designation, ``site_id``
# is the stable WDPA identifier. ``outFields=*`` rejects with HTTP 400 when
# combined with a spatial filter, so we enumerate.
_WDPA_OUT_FIELDS = "name_eng,desig_eng,iucn_cat,status,status_yr,site_id"

#: The DESIG_ENG field name in the live schema (lowercase). Used by the
#: client-side designation_filter.
_WDPA_DESIG_FIELD = "desig_eng"

#: The NAME field name in the live schema (lowercase).
_WDPA_NAME_FIELD = "name_eng"


# ---------------------------------------------------------------------------
# Designation vocabulary + alias table (OQ-0089-DESIGNATION-FILTER-SEMANTICS).
#
# The WDPA ``desig_eng`` field is a free-text English phrase (e.g.
# "National Park", "National Wildlife Refuge"), NOT a short code. Humans and
# LLM prompts spell these many ways: case variants ("national park"), plurals
# ("National Parks"), and abbreviations ("NP", "NWR"). An exact byte-match
# against the live field silently filters EVERY feature out and returns a
# valid 0-feature FlatGeobuf with no error - the same "goes18 vs goes-18"
# silent-mismatch class. We normalize every accepted spelling to the EXACT
# ``desig_eng`` token the filter compares against, case/whitespace/plural
# insensitive, and raise ``WDPADesignationError`` listing the accepted forms
# when an entry resolves to nothing known.
#
# ``_CANONICAL_DESIGNATIONS`` is the curated set of the most common US/global
# ``desig_eng`` values (their EXACT live casing). It is deliberately not
# exhaustive - the WDPA corpus carries thousands of national designations -
# so an unknown-but-plausible designation is reported with the accepted set
# rather than guessed. Comparison is always casefolded, so any casing of a
# listed designation is accepted even though only the canonical form is shown.
# ---------------------------------------------------------------------------

#: Curated canonical ``desig_eng`` values (exact live casing). Comparison is
#: casefolded, so the canonical form here is only used for error messages and
#: the cache-key normalization.
_CANONICAL_DESIGNATIONS: tuple[str, ...] = (
    "National Park",
    "National Wildlife Refuge",
    "National Preserve",
    "National Monument",
    "National Forest",
    "National Recreation Area",
    "National Seashore",
    "National Lakeshore",
    "National Conservation Area",
    "National Marine Sanctuary",
    "National Estuarine Research Reserve",
    "Wilderness Area",
    "State Park",
    "State Forest",
    "State Wildlife Management Area",
    "State Wildlife Area",
    "Wildlife Management Area",
    "Wildlife Sanctuary",
    "Nature Reserve",
    "Marine Protected Area",
    "Habitat/Species Management Area",
    "Protected Landscape/Seascape",
    "Ramsar Site, Wetland of International Importance",
    "World Heritage Site (natural or mixed)",
    "UNESCO-MAB Biosphere Reserve",
    "Area of Outstanding Natural Beauty",
    "Site of Special Scientific Interest",
    "Special Area of Conservation (Habitats Directive)",
    "Special Protection Area (Birds Directive)",
    "Conservation Area",
    "Game Reserve",
    "Forest Reserve",
)

#: Casefold -> canonical mapping for the curated vocabulary. The key is the
#: casefolded designation (so any casing matches); the value is the EXACT live
#: ``desig_eng`` token. Built once at import time.
_DESIG_CANONICAL_BY_FOLD: dict[str, str] = {
    d.casefold(): d for d in _CANONICAL_DESIGNATIONS
}

#: Alias -> canonical mapping for the common human/LLM shorthands and plural
#: forms. Keys are casefolded so "np", "NP", and "N.P." normalize the same
#: (dots/whitespace are stripped before lookup; see ``_fold_designation``).
#: Values MUST appear in ``_CANONICAL_DESIGNATIONS`` so a single source of
#: truth governs the accepted output tokens.
_DESIG_ALIASES: dict[str, str] = {
    # Abbreviations.
    "np": "National Park",
    "nps": "National Park",
    "nwr": "National Wildlife Refuge",
    "nm": "National Monument",
    "nf": "National Forest",
    "nra": "National Recreation Area",
    "nms": "National Marine Sanctuary",
    "nerr": "National Estuarine Research Reserve",
    "wma": "Wildlife Management Area",
    "mpa": "Marine Protected Area",
    "sssi": "Site of Special Scientific Interest",
    "aonb": "Area of Outstanding Natural Beauty",
    "sac": "Special Area of Conservation (Habitats Directive)",
    "spa": "Special Protection Area (Birds Directive)",
    # Plurals (casefolded; singularization is also attempted generically in
    # ``_normalize_one_designation`` for any trailing-"s" form).
    "national parks": "National Park",
    "national wildlife refuges": "National Wildlife Refuge",
    "national monuments": "National Monument",
    "national forests": "National Forest",
    "national preserves": "National Preserve",
    "wilderness areas": "Wilderness Area",
    "state parks": "State Park",
    "nature reserves": "Nature Reserve",
    "marine protected areas": "Marine Protected Area",
    "wildlife management areas": "Wildlife Management Area",
    # Common phrasings.
    "biosphere reserve": "UNESCO-MAB Biosphere Reserve",
    "ramsar site": "Ramsar Site, Wetland of International Importance",
    "ramsar": "Ramsar Site, Wetland of International Importance",
    "world heritage site": "World Heritage Site (natural or mixed)",
    "world heritage": "World Heritage Site (natural or mixed)",
}


def _fold_designation(value: str) -> str:
    """Casefold + collapse whitespace + strip dots for tolerant lookup.

    Mirrors the ``goes-18`` lesson: normalize the human/LLM token to a single
    canonical key before any membership test so case, internal-whitespace, and
    abbreviation-dot variants ("N.P.", "n p", "National  Park") all collapse
    to one comparison key.
    """
    return " ".join(value.replace(".", " ").split()).casefold()


def _normalize_one_designation(value: str) -> str:
    """Resolve one human/LLM designation spelling to its EXACT ``desig_eng`` token.

    Resolution order (all case/whitespace insensitive):
    1. exact canonical vocabulary (any casing),
    2. alias table (abbreviations, plurals, common phrasings),
    3. generic trailing-"s" singularization against the canonical vocabulary.

    Raises ``WDPADesignationError`` (non-retryable) listing the accepted forms
    when nothing matches, so a typo fails LOUD rather than silently filtering
    every feature out.
    """
    if not isinstance(value, str):
        raise WDPADesignationError(
            f"designation_filter entries must be str; got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped:
        raise WDPADesignationError(
            "designation_filter entries must be non-empty strings; got an "
            "empty/whitespace value"
        )

    fold = _fold_designation(stripped)
    if fold in _DESIG_CANONICAL_BY_FOLD:
        return _DESIG_CANONICAL_BY_FOLD[fold]
    if fold in _DESIG_ALIASES:
        return _DESIG_ALIASES[fold]
    # Acronyms written with dots ("N.P.") fold to "n p"; collapse the internal
    # whitespace and retry the alias table so "N.P." == "NP" == "np".
    fold_nospace = fold.replace(" ", "")
    if fold_nospace in _DESIG_ALIASES:
        return _DESIG_ALIASES[fold_nospace]
    # Generic plural -> singular: "national parks" -> "national park".
    if fold.endswith("s") and fold[:-1] in _DESIG_CANONICAL_BY_FOLD:
        return _DESIG_CANONICAL_BY_FOLD[fold[:-1]]

    accepted = ", ".join(_CANONICAL_DESIGNATIONS)
    raise WDPADesignationError(
        f"designation_filter entry {value!r} is not a known WDPA designation "
        f"or alias. Accepted designations (case/plural/abbreviation "
        f"insensitive): {accepted}. Accepted aliases include: "
        f"{', '.join(sorted(_DESIG_ALIASES))}. "
        "Pass designation_filter=None to keep all designations."
    )


def _normalize_designation_filter(
    designation_filter: list[str] | None,
) -> list[str] | None:
    """Normalize a designation_filter list to canonical ``desig_eng`` tokens.

    Maps every accepted spelling to its EXACT live token, de-duplicates, and
    sorts for cache-key stability. ``None`` / empty-list mean "no filter" and
    return ``None``. Raises ``WDPADesignationError`` on a malformed or unknown
    entry (data-source-fallback norm: fail LOUD, never build a filter that
    silently matches nothing).
    """
    if not designation_filter:
        return None
    if not isinstance(designation_filter, list):
        raise WDPADesignationError(
            f"designation_filter must be a list[str] or None; "
            f"got {type(designation_filter).__name__}"
        )
    canonical = {_normalize_one_designation(d) for d in designation_filter}
    return sorted(canonical)

# Page size. WDPA's FeatureServer default cap is 2000 — request that
# explicitly so server-side defaults do not surprise us.
_PAGE_SIZE = 2000

# Per-request timeout. WDPA's ArcGIS REST cluster can be slow under load —
# the kickoff allots 60s.
_REQUEST_TIMEOUT = 60.0

# Safety cap on pagination iterations. 50 * 2000 = 100k features. A bbox
# returning more than that is almost certainly an unintentional global
# query; fail loudly rather than silently paginate forever.
_MAX_PAGES = 50

# User-Agent — UNEP-WCMC's terms ask for identifying agents.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_wdpa_protected_areas",
    ttl_class="static-30d",
    source_class="wdpa",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``WDPABboxError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise WDPABboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise WDPABboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise WDPABboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise WDPABboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise WDPABboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1m) for cache-key stability.

    Matching the audit.md cache-key spec: bbox-rounded-6dp + sorted
    designation_filter tuple.
    """
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_envelope(bbox: tuple[float, float, float, float]) -> str:
    """Format a bbox as an ArcGIS ``geometryType=esriGeometryEnvelope`` string.

    ArcGIS REST envelope format is the literal ``xmin,ymin,xmax,ymax`` —
    no JSON wrapping when ``geometryType=esriGeometryEnvelope`` is set.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return f"{min_lon},{min_lat},{max_lon},{max_lat}"


# ---------------------------------------------------------------------------
# WDPA HTTP fetch.
# ---------------------------------------------------------------------------


def _wdpa_query_one_page(
    bbox: tuple[float, float, float, float],
    offset: int,
) -> dict[str, Any]:
    """Fetch one page of the WDPA FeatureServer query, returning parsed GeoJSON.

    Returns the parsed response dict (the FeatureServer wraps GeoJSON in a
    standard envelope: ``{"type": "FeatureCollection", "features": [...],
    "exceededTransferLimit": bool}``).
    """
    params = {
        "where": "1=1",
        "geometry": _bbox_to_envelope(bbox),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outFields": _WDPA_OUT_FIELDS,
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(_PAGE_SIZE),
        "resultOffset": str(offset),
    }
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            resp = client.get(
                _WDPA_BASE,
                params=params,
                headers={"User-Agent": _USER_AGENT},
            )
    except httpx.RequestError as exc:
        raise WDPAUpstreamError(
            f"WDPA query failed (network) offset={offset}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise WDPAUpstreamError(
            f"WDPA query returned HTTP {resp.status_code} offset={offset}: "
            f"{resp.text[:200]}"
        )

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise WDPAUpstreamError(
            f"WDPA returned non-JSON body offset={offset}: {exc}"
        ) from exc

    # ArcGIS REST surfaces errors inside a 200 envelope: {"error": {...}}.
    if isinstance(payload, dict) and "error" in payload:
        err = payload["error"]
        raise WDPAUpstreamError(
            f"WDPA query returned error envelope offset={offset}: {err}"
        )

    return payload


def _fetch_wdpa_features(
    bbox: tuple[float, float, float, float],
    designation_filter: list[str] | None,
) -> list[dict[str, Any]]:
    """Fetch all features in the bbox, paginating as needed.

    Applies ``designation_filter`` client-side after fetch.
    Returns a list of GeoJSON Feature dicts (possibly empty).
    """
    all_features: list[dict[str, Any]] = []
    offset = 0

    for page_idx in range(_MAX_PAGES):
        payload = _wdpa_query_one_page(bbox, offset)
        page_features = payload.get("features", []) or []
        all_features.extend(page_features)

        logger.info(
            "fetch_wdpa_protected_areas: page %d offset=%d -> %d feature(s) "
            "(total so far: %d)",
            page_idx,
            offset,
            len(page_features),
            len(all_features),
        )

        # WDPA tells us if more is available via exceededTransferLimit.
        # Some ArcGIS mirrors put this at the top of the GeoJSON envelope;
        # others nest it under "properties". Check both.
        more = bool(
            payload.get("exceededTransferLimit")
            or (payload.get("properties") or {}).get("exceededTransferLimit")
        )
        if not more:
            break
        if len(page_features) == 0:
            # Defensive: server says "more" but returned 0; avoid infinite loop.
            break
        offset += len(page_features)
    else:
        raise WDPAUpstreamError(
            f"WDPA pagination exceeded {_MAX_PAGES} pages for bbox={bbox}; "
            "bbox is probably too large — reduce bbox extent."
        )

    # Client-side designation filter (lowercase field name per the live schema).
    # ``designation_filter`` arrives already canonicalized to exact ``desig_eng``
    # tokens by ``_normalize_designation_filter``; we casefold BOTH sides of the
    # membership test so the comparison is robust to any live-casing drift and a
    # canonical token never silently misses on a case variant
    # (OQ-0089-DESIGNATION-FILTER-SEMANTICS; same class as "goes18 vs goes-18").
    if designation_filter:
        filter_set = {d.casefold() for d in designation_filter}
        filtered = [
            f
            for f in all_features
            if str((f.get("properties") or {}).get(_WDPA_DESIG_FIELD, "")).casefold()
            in filter_set
        ]
        logger.info(
            "fetch_wdpa_protected_areas: designation_filter=%s reduced %d -> %d",
            designation_filter,
            len(all_features),
            len(filtered),
        )
        # Honest degrade: if the bbox had protected areas but the filter
        # eliminated ALL of them, do NOT return a silent 0-feature FlatGeobuf.
        # Fail LOUD listing the designations actually present so the caller can
        # correct the filter (data-source-fallback norm).
        if all_features and not filtered:
            present = sorted(
                {
                    str((f.get("properties") or {}).get(_WDPA_DESIG_FIELD, "")).strip()
                    for f in all_features
                    if (f.get("properties") or {}).get(_WDPA_DESIG_FIELD)
                }
            )
            raise WDPADesignationError(
                f"designation_filter {designation_filter} matched 0 of "
                f"{len(all_features)} protected area(s) in the bbox. "
                f"Designations actually present here: {present}. "
                "Adjust designation_filter to one of these, or pass "
                "designation_filter=None to keep all."
            )
        all_features = filtered

    return all_features


# ---------------------------------------------------------------------------
# Features -> FlatGeobuf bytes.
# ---------------------------------------------------------------------------


def _features_to_flatgeobuf(features: list[dict[str, Any]]) -> bytes:
    """Convert a list of GeoJSON Features to FlatGeobuf bytes via geopandas.

    An empty feature list is returned as an empty FlatGeobuf (still valid
    bytes) — callers (and the cache shim) treat that as a successful
    "no-features-in-bbox" response per the audit.md "Empty bbox over open
    water → 0 features without error" test.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise WDPAUpstreamError(
            f"geopandas not available for FlatGeobuf encode: {exc}"
        ) from exc

    if not features:
        # Empty geodataframe with the WDPA schema columns (lowercase field
        # names matching the live FeatureServer schema).
        empty_gdf = gpd.GeoDataFrame(
            {
                "name_eng": [],
                "desig_eng": [],
                "iucn_cat": [],
                "status": [],
                "status_yr": [],
                "site_id": [],
                "geometry": [],
            },
            crs="EPSG:4326",
        )
        buf = io.BytesIO()
        import tempfile
        import os as _os

        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
            tmp_path = tf.name
        try:
            empty_gdf.to_file(tmp_path, driver="FlatGeobuf", engine="pyogrio")
            with open(tmp_path, "rb") as f:
                return f.read()
        except Exception as exc:  # noqa: BLE001
            raise WDPAUpstreamError(
                f"failed to write empty FlatGeobuf: {exc}"
            ) from exc
        finally:
            try:
                _os.unlink(tmp_path)
            except OSError:
                pass

    # Build a FeatureCollection and let geopandas parse it.
    fc = {"type": "FeatureCollection", "features": features}
    try:
        gdf = gpd.GeoDataFrame.from_features(fc, crs="EPSG:4326")
    except Exception as exc:  # noqa: BLE001
        raise WDPAUpstreamError(
            f"geopandas could not parse WDPA features: {exc}"
        ) from exc

    import os as _os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tmp_path = tf.name
    try:
        gdf.to_file(tmp_path, driver="FlatGeobuf", engine="pyogrio")
        with open(tmp_path, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001
        raise WDPAUpstreamError(
            f"failed to write FlatGeobuf: {exc}"
        ) from exc
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Fetch function — builds the bytes callable for read_through.
# ---------------------------------------------------------------------------


def _fetch_wdpa_bytes(
    bbox: tuple[float, float, float, float],
    designation_filter: list[str] | None,
) -> bytes:
    """Download WDPA features, filter, and serialize to FlatGeobuf bytes."""
    features = _fetch_wdpa_features(bbox, designation_filter)
    return _features_to_flatgeobuf(features)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_wdpa_protected_areas(
    bbox: tuple[float, float, float, float],
    designation_filter: list[str] | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch World Database on Protected Areas (WDPA) polygons clipped to a bbox.

    **What it does:** Queries the UNEP-WCMC WDPA ArcGIS REST FeatureServer
    (``services5.arcgis.com/Mj0hjvkNtV7NRhA7``) with a spatial envelope filter,
    paginates all matching protected-area polygons into a FlatGeobuf, and
    optionally filters by designation type client-side. Global coverage,
    monthly WDPA releases, cached ``static-30d``. No API key required.

    **When to use:**
    - Agent needs protected-area boundaries for a study area — e.g. overlay
      National Parks or National Wildlife Refuges on a flood risk surface.
    - Workflow must compute the fraction of a hazard footprint that intersects
      protected lands (conservation-impact analysis).
    - User asks about biodiversity context inside vs outside protected status.
    - Filtering ``fetch_gbif_occurrences`` or ``fetch_inaturalist_observations``
      results by protected/unprotected designation.

    **When NOT to use:**
    - Parcel-level land ownership or cadastral boundaries (WDPA covers
      conservation designations only; use county assessor data for parcels).
    - Private conservation easements not registered with UNEP-WCMC.
    - Tribal lands (use TIGER AIANNH or a BIA dataset).
    - Single-point inside/outside test (fetch the bbox once, test locally).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
      Example: ``(-82.0, 25.0, -80.0, 26.5)`` for Everglades region.
    - ``designation_filter`` (list[str] or None): list of designations to
      retain, e.g. ``["National Park", "National Wildlife Refuge"]``. Each
      entry is NORMALIZED to the exact live ``desig_eng`` token, so case
      variants ("national park"), plurals ("National Parks"), and common
      abbreviations ("NP", "NWR") all resolve. An unknown designation raises
      ``WDPADesignationError`` (non-retryable) listing the accepted forms
      rather than silently filtering everything out. ``None`` returns all
      designations. Filter is applied client-side after the spatial fetch.

    **Returns:**
    ``LayerURI(layer_type="vector", role="context", units=None)`` pointing at a
    FlatGeobuf with fields: ``name_eng``, ``desig_eng``, ``iucn_cat``,
    ``status``, ``status_yr``, ``site_id``. Empty bbox over open water returns
    a valid 0-feature FlatGeobuf (not an error).

    **Cross-tool dependencies:**
    - Pairs with: ``fetch_gbif_occurrences``, ``fetch_inaturalist_observations``
      (conservation layer context).
    - Upstream of: ``compute_zonal_statistics`` for inside/outside protected
      area summaries.
    - Complemented by: ``fetch_administrative_boundaries`` for jurisdictional
      boundary overlay.
    """
    _validate_bbox(bbox)

    # Quantize bbox to 6dp for cache-key stability (audit.md spec).
    q_bbox = _round_bbox_to_6dp(bbox)

    # Normalize designation_filter to EXACT live ``desig_eng`` tokens (resolves
    # case variants / plurals / abbreviations like "national park", "National
    # Parks", "NP" -> "National Park") and raise WDPADesignationError on an
    # unknown/malformed entry. This is the "goes18 vs goes-18" guard: a typo
    # fails LOUD with the accepted forms instead of silently filtering every
    # feature out. Normalization also dedupes + sorts for cache-key stability.
    df_normalized: list[str] | None = _normalize_designation_filter(
        designation_filter
    )

    params = {
        "bbox": list(q_bbox),
        "designation_filter": df_normalized,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_wdpa_bytes(q_bbox, df_normalized),
    )
    assert result.uri is not None, (
        "fetch_wdpa_protected_areas is cacheable; uri must be set by read_through"
    )

    # Layer name encodes the filter so multiple WDPA layers in the same panel
    # are distinguishable.
    if df_normalized:
        filter_label = " (" + ", ".join(df_normalized) + ")"
    else:
        filter_label = ""
    name = f"Protected Areas - WDPA{filter_label}"

    return LayerURI(
        layer_id=f"wdpa-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=name,
        layer_type="vector",
        uri=result.uri,
        style_preset="wdpa_protected_areas",
        role="context",
        units=None,
    )
