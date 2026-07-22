"""``fetch_statsgo_soils`` atomic tool — STATSGO soils (job A11).

Wraps the USGS STATSGO COG collection (published to ScienceBase) through the
``pfdf.data.usgs.statsgo`` module. STATSGO is the State Soil Geographic
Database — a coarse-scale soils inventory that pfdf re-publishes as
30-meter Cloud-Optimized GeoTIFFs for the post-fire debris-flow (pfdf)
hazard assessment workflow. The two surfaced fields are:

    KFFACT  — soil KF-factor (erodibility, hydrologic soil group proxy)
    THICK   — soil thickness (centimeters)

These are the canonical STATSGO derivatives the pfdf debris-flow models
(M1 / M3 / M4 et al. from Staley et al. 2017 / Gartner et al. 2014) ask
for as catchment-aggregated covariates. KFFACT is also a stand-in for
hydrologic soil group (USDA HSG A-D) when running curve-number / runoff
analyses outside the wildfire context.

API surface (verified live 2026-06-09 against ScienceBase via pfdf 3.0.4):

    pfdf.data.usgs.statsgo.read(field, bounds, *, timeout=60)
        field   — "KFFACT" or "THICK"
        bounds  — pfdf.projection.BoundingBox (must carry a CRS)
        returns — pfdf.raster.Raster (30 m nominal CONUS coverage)

We wrap that call:

    1. Convert the agent's standard ``bbox`` 4-tuple (EPSG:4326) into a
       pfdf ``BoundingBox(left, bottom, right, top, crs=4326)``.
    2. Call ``statsgo.read`` for the requested field; let pfdf mosaic
       the underlying COG tiles.
    3. Persist the ``Raster`` to a temp GeoTIFF and reload as a COG
       through rioxarray for the agent-side cache.
    4. Route bytes through ``read_through`` with
       ``ttl_class="static-30d"``, ``source_class="statsgo_soils"``.

Tier-1 free (no auth, no API key). CONUS coverage; the underlying STATSGO
inventory does not extend to AK/HI/territories — ``supports_global_query``
remains False and a bbox outside CONUS raises an empty/upstream error.

FR-AS-11: typed exceptions ``STATSGOSoilsError`` + 3 sub-classes carry
``error_code`` + ``retryable`` for the agent retry/clarify surface.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any, Literal

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_statsgo_soils",
    "STATSGOSoilsError",
    "STATSGOSoilsInputError",
    "STATSGOSoilsUpstreamError",
    "STATSGOSoilsEmptyError",
    "estimate_payload_mb",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.soil.fetch_statsgo_soils")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class STATSGOSoilsError(RuntimeError):
    """Base class for fetch_statsgo_soils failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "STATSGO_SOILS_ERROR"
    retryable: bool = True


class STATSGOSoilsInputError(STATSGOSoilsError):
    """Bad inputs (malformed bbox, unsupported field name, bbox out of CONUS)."""

    error_code = "STATSGO_SOILS_INPUT_INVALID"
    retryable = False


class STATSGOSoilsUpstreamError(STATSGOSoilsError):
    """ScienceBase / pfdf download or COG materialization failure."""

    error_code = "STATSGO_SOILS_UPSTREAM_ERROR"
    retryable = True


class STATSGOSoilsEmptyError(STATSGOSoilsError):
    """Bounding box is inside CONUS envelope but returned no STATSGO pixels.

    Typical cause: bbox falls in open water, the Great Lakes, or a coastal
    pocket the STATSGO inventory does not cover.
    """

    error_code = "STATSGO_SOILS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Supported STATSGO fields. pfdf 3.0.4 surfaces exactly these two; new
#: releases may extend the set (PHYS, RUNOFF, etc.) — the validator below
#: re-derives the live allow-list lazily so a pfdf bump does not require a
#: code change here.
_DEFAULT_VALID_FIELDS: frozenset[str] = frozenset({"KFFACT", "THICK"})

#: CONUS envelope for the STATSGO COG collection (EPSG:4326). Outside this
#: envelope ScienceBase returns nothing and pfdf raises — we short-circuit
#: with a typed input error to spare the round-trip.
_CONUS_BBOX: tuple[float, float, float, float] = (-125.0, 24.0, -66.5, 49.5)

#: HTTP / ScienceBase timeout (seconds). STATSGO COG tiles are small
#: (~few MiB) so a 60 s upper bound is generous.
_DEFAULT_TIMEOUT_S = 60.0

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: Field-specific palette name surfaced as ``style_preset`` so the QGIS
#: server can colorize each layer correctly. KFFACT is a low-to-high
#: erodibility ramp; THICK is a depth ramp.
_STYLE_PRESET_BY_FIELD: dict[str, str] = {
    "KFFACT": "statsgo_kffact",
    "THICK": "statsgo_thick",
}

#: Field-specific units string surfaced on the ``LayerURI``.
_UNITS_BY_FIELD: dict[str, str | None] = {
    "KFFACT": None,           # dimensionless K-factor
    "THICK": "centimeters",
}


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------


_METADATA = AtomicToolMetadata(
    name="fetch_statsgo_soils",
    ttl_class="static-30d",
    source_class="statsgo_soils",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    field: str = "KFFACT",
    **_kw: Any,
) -> float:
    """Estimate emitted GeoTIFF size in MB.

    Empirical sizing for STATSGO COGs at 30 m resolution: a 1° × 1° bbox
    (~110 km × 110 km, ~3.6M cells) compresses to roughly 1.5 MB; a
    state-sized 5° × 5° bbox lands around 30 MB. Scales linearly with
    bbox area in square degrees; floor at 0.05 MB.
    """
    if bbox is None:
        return 1.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 1.0
    return max(0.05, sq_deg * 1.5)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``STATSGOSoilsInputError`` on degenerate / out-of-CONUS bbox."""
    if len(bbox) != 4:
        raise STATSGOSoilsInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise STATSGOSoilsInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise STATSGOSoilsInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise STATSGOSoilsInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise STATSGOSoilsInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    if max_lon < _CONUS_BBOX[0] or min_lon > _CONUS_BBOX[2]:
        raise STATSGOSoilsInputError(
            f"bbox {bbox} does not intersect STATSGO CONUS envelope {_CONUS_BBOX}; "
            "STATSGO does not cover Alaska / Hawaii / territories"
        )
    if max_lat < _CONUS_BBOX[1] or min_lat > _CONUS_BBOX[3]:
        raise STATSGOSoilsInputError(
            f"bbox {bbox} does not intersect STATSGO CONUS envelope {_CONUS_BBOX}; "
            "STATSGO does not cover Alaska / Hawaii / territories"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _normalize_field(field: str) -> str:
    """Upper-case the field name + validate against pfdf's live allow-list."""
    if not isinstance(field, str):
        raise STATSGOSoilsInputError(
            f"field must be a string; got {type(field).__name__}"
        )
    field = field.upper().strip()
    # Try to derive the live field set from pfdf; fall back to the static
    # default if pfdf is unavailable (test environment with stubbed pfdf).
    try:
        from pfdf.data.usgs import statsgo  # type: ignore[import-not-found]
        valid: frozenset[str] = frozenset(
            str(name).upper() for name in statsgo.fields().index
        )
    except Exception:  # noqa: BLE001 — pfdf unavailable or schema drift
        valid = _DEFAULT_VALID_FIELDS
    if field not in valid:
        raise STATSGOSoilsInputError(
            f"unknown STATSGO field={field!r}; allowed: {sorted(valid)}"
        )
    return field


# ---------------------------------------------------------------------------
# pfdf → COG bytes.
# ---------------------------------------------------------------------------


def _fetch_statsgo_field_bytes(
    bbox: tuple[float, float, float, float],
    field: str,
    timeout_s: float,
) -> bytes:
    """Download a STATSGO field through pfdf + serialize as a COG.

    Raises ``STATSGOSoilsUpstreamError`` on any pfdf / ScienceBase failure;
    ``STATSGOSoilsEmptyError`` when the bbox is inside CONUS but pfdf
    returns an empty raster (open water etc.).
    """
    try:
        from pfdf.data.usgs import statsgo  # type: ignore[import-not-found]
        from pfdf.projection import BoundingBox  # type: ignore[import-not-found]
        import rioxarray  # noqa: F401 — registers .rio accessor
    except Exception as exc:  # noqa: BLE001
        raise STATSGOSoilsUpstreamError(
            f"pfdf / rioxarray unavailable: {exc}"
        ) from exc

    min_lon, min_lat, max_lon, max_lat = bbox
    pfdf_bbox = BoundingBox(min_lon, min_lat, max_lon, max_lat, crs=4326)

    try:
        raster = statsgo.read(field, pfdf_bbox, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 — re-raise as typed
        raise STATSGOSoilsUpstreamError(
            f"pfdf.data.usgs.statsgo.read failed for field={field} bbox={bbox}: {exc}"
        ) from exc

    # Persist to a temp GeoTIFF; pfdf's Raster.save uses rasterio under the
    # hood. We re-open with rioxarray and re-write as a COG so the cache
    # artifact has the canonical COG signature the QGIS Server tile pipeline
    # expects (matches the fetch_dem path).
    tmp_in: str | None = None
    tmp_cog: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_statsgo_in_"
        ) as f:
            tmp_in = f.name
        try:
            raster.save(tmp_in, overwrite=True)
        except Exception as exc:  # noqa: BLE001
            raise STATSGOSoilsUpstreamError(
                f"pfdf Raster.save failed for field={field}: {exc}"
            ) from exc

        import rioxarray as rxr  # local import for typing

        try:
            da = rxr.open_rasterio(tmp_in, masked=True).squeeze(drop=True)
        except Exception as exc:  # noqa: BLE001
            raise STATSGOSoilsUpstreamError(
                f"rioxarray.open_rasterio failed for staged STATSGO file: {exc}"
            ) from exc

        # Empty / all-nodata check (open water bbox inside CONUS).
        try:
            import numpy as np
            arr = np.asarray(da.values)
            if arr.size == 0 or bool(np.all(np.isnan(arr))) or arr.shape == (0,):
                raise STATSGOSoilsEmptyError(
                    f"STATSGO field={field} bbox={bbox} returned no pixels "
                    f"(likely open water or outside STATSGO coverage)"
                )
        except STATSGOSoilsEmptyError:
            raise
        except Exception:
            # Numpy check best-effort; pass through to COG write below.
            pass

        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_statsgo_cog_"
        ) as f:
            tmp_cog = f.name
        try:
            da.rio.to_raster(
                tmp_cog,
                driver="COG",
                compress="LZW",
                BIGTIFF="IF_SAFER",
            )
        except Exception as exc:  # noqa: BLE001
            raise STATSGOSoilsUpstreamError(
                f"COG write failed for field={field}: {exc}"
            ) from exc

        with open(tmp_cog, "rb") as f:
            cog_bytes = f.read()
        logger.info(
            "fetch_statsgo_soils: field=%s bbox=%s -> %d bytes",
            field, bbox, len(cog_bytes),
        )
        return cog_bytes
    finally:
        for p in (tmp_in, tmp_cog):
            if p is not None:
                try:
                    os.unlink(p)
                except OSError:
                    pass


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
def fetch_statsgo_soils(
    bbox: tuple[float, float, float, float],
    field: Literal["KFFACT", "THICK"] = "KFFACT",
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch a STATSGO soils raster (KFFACT or THICK) for a CONUS bbox.

    What it does:
        Pulls the requested STATSGO field from the USGS ScienceBase COG
        collection through the ``pfdf.data.usgs.statsgo.read`` Python
        wrapper. KFFACT is the soil K-factor (erodibility / hydrologic-
        soil-group proxy); THICK is the soil thickness in centimeters.
        Output is a 30-meter Cloud-Optimized GeoTIFF clipped to the
        requested bbox and rewritten through the shared cache.

    When to use:
        - User asks for soil erodibility, soil hydrologic-group / runoff
          curve-number derivation, or soil-thickness context in a CONUS
          area ("what's the soil K-factor in this watershed?", "give me
          soil thickness over Camp Fire footprint").
        - Post-fire debris-flow workflow (M1 / M3 / M4 logistic models
          from Staley et al. 2017) needs catchment-aggregated soil
          covariates. Pair with ``compute_zonal_statistics`` over a
          burn-perimeter or watershed polygon.
        - Hydrologic / runoff modeling needs a CONUS-wide soil substrate
          when SSURGO is too detailed or unavailable for the bbox.

    When NOT to use:
        - DO NOT use for fine-scale (1:24,000) county-level soils — use
          SSURGO (no atomic tool yet; add in a future job) which is the
          high-resolution sibling of STATSGO.
        - DO NOT use outside CONUS — STATSGO does not cover Alaska,
          Hawaii, Puerto Rico, or other territories; the input validator
          raises ``STATSGOSoilsInputError`` on out-of-CONUS bbox.
        - DO NOT use for global soils — use SoilGrids (future tool); for
          curve numbers use ``fetch_gcn250_curve_numbers`` (global SCS CN
          raster) which already encodes hydrologic soil group + landcover.
        - DO NOT use for organic-soil / peat depth — STATSGO THICK is the
          generic soil-profile thickness, not a peatland-specific layer.

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
            4-float tuple, lon/lat ordered min-then-max on each axis. Must
            intersect ``(-125, 24, -66.5, 49.5)`` CONUS envelope or
            ``STATSGOSoilsInputError`` is raised. Recommended scale:
            watershed / county sized (≤1° × 1°) — larger bboxes still
            work but emit MB-scale COGs; the Wave-1.5 payload-warning gate
            uses ``estimate_payload_mb`` to flag big requests.
            Example for Fort Myers watershed: ``(-82.0, 26.4, -81.7, 26.7)``.
        field: One of:
            - ``"KFFACT"`` (default): soil K-factor, USDA RUSLE erodibility
              (dimensionless, ~0.02 - 0.7). Proxy for hydrologic soil group
              A-D (low K = sandy/well-drained, high K = silty/runoff-prone).
            - ``"THICK"``: total soil-profile thickness in centimeters
              (~25 - 200 cm typical).
        timeout_s: ScienceBase server connect-and-read timeout in seconds.
            Defaults to 60.0 (matches pfdf default). Reduce for impatient
            callers; raise (or set None upstream) only for known-slow links.

    Returns:
        ``LayerURI`` pointing at a single-band COG in the cache bucket
        ``s3://trid3nt-cache/cache/static-30d/statsgo_soils/<key>.tif``.
        ``layer_type="raster"``, ``role="input"``,
        ``style_preset="statsgo_kffact"`` or ``"statsgo_thick"``,
        ``units="centimeters"`` for THICK and ``None`` for KFFACT.
        Downstream tools consume the COG as a catchment-aggregation input
        (typically via ``compute_zonal_statistics`` against a watershed
        polygon from ``fetch_administrative_boundaries`` /
        ``fetch_nhdplus_nldi_navigate`` basin).

    Cross-tool dependencies (FR-TA-3):
        - Composes WITH: ``compute_zonal_statistics`` (catchment-mean
          KFFACT for pfdf debris-flow scoring); ``clip_raster_to_polygon``
          (clip to a burn perimeter / watershed); ``publish_layer`` (map
          display via the ``statsgo_*`` QML preset).
        - Sibling: ``fetch_gcn250_curve_numbers`` (global SCS CN raster —
          overlaps semantically with KFFACT-as-HSG-proxy but is global +
          AMC-tiered; prefer GCN250 when leaving CONUS).
        - Upstream source: USGS ScienceBase STATSGO COG collection via
          ``pfdf.data.usgs.statsgo``.

    Cache: ``ttl_class="static-30d"``, ``source_class="statsgo_soils"``.
    STATSGO is a regulatory / archival dataset; the 30-day bucket
    amortizes well across typical agent sessions.

    Errors (FR-AS-11 typed-error surface):
        - ``STATSGOSoilsInputError``: bad bbox / unsupported field / bbox
          outside CONUS (retryable=False).
        - ``STATSGOSoilsUpstreamError``: ScienceBase 5xx / network error /
          COG materialization failure (retryable=True).
        - ``STATSGOSoilsEmptyError``: bbox inside CONUS but the STATSGO
          raster has no data there (open water / Great Lakes pocket)
          (retryable=False).

    Tier-1 free. No API key. ``supports_global_query=False``.
    """
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise STATSGOSoilsInputError(
                f"bbox must be a 4-tuple or list; got {type(bbox).__name__}"
            ) from exc
    _validate_bbox(bbox)  # type: ignore[arg-type]
    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    normalized_field = _normalize_field(field)

    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError) as exc:
        raise STATSGOSoilsInputError(
            f"timeout_s must be a finite number; got {timeout_s!r}"
        ) from exc
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise STATSGOSoilsInputError(
            f"timeout_s must be > 0 and finite; got {timeout_s!r}"
        )

    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "field": normalized_field,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_statsgo_field_bytes(q_bbox, normalized_field, timeout_s),
    )
    assert result.uri is not None, (
        "fetch_statsgo_soils is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"statsgo-{normalized_field.lower()}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=(
            f"STATSGO {normalized_field} — bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="raster",
        uri=result.uri,
        style_preset=_STYLE_PRESET_BY_FIELD[normalized_field],
        role="input",
        units=_UNITS_BY_FIELD[normalized_field],
    )
