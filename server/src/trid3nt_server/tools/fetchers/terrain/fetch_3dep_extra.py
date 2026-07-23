"""``fetch_3dep_extra`` atomic tool — USGS 3DEP non-default DEM paths (job A11).
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
    "fetch_3dep_extra",
    "ThreeDEPExtraError",
    "ThreeDEPExtraInputError",
    "ThreeDEPExtraUpstreamError",
    "ThreeDEPExtraEmptyError",
    "estimate_payload_mb",
    "SUPPORTED_RESOLUTIONS",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.terrain.fetch_3dep_extra")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class ThreeDEPExtraError(RuntimeError):
    """Base class for fetch_3dep_extra failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry/clarify/fallback logic.
    """

    error_code: str = "THREE_DEP_EXTRA_ERROR"
    retryable: bool = True


class ThreeDEPExtraInputError(ThreeDEPExtraError):
    """Bad inputs (bbox shape, unsupported resolution, bad max_tiles)."""

    error_code = "THREE_DEP_EXTRA_INPUT_INVALID"
    retryable = False


class ThreeDEPExtraUpstreamError(ThreeDEPExtraError):
    """TNM tile discovery / download / COG materialization failure."""

    error_code = "THREE_DEP_EXTRA_UPSTREAM_ERROR"
    retryable = True


class ThreeDEPExtraEmptyError(ThreeDEPExtraError):
    """Requested resolution has no 3DEP tiles covering the bbox.

    Common cause: requesting 1 m or 1/9 arc-second outside a 3DEP LiDAR
    project footprint, or requesting 2 arc-second / 5 meter outside
    Alaska.
    """

    error_code = "THREE_DEP_EXTRA_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: The four non-default 3DEP resolutions this tool serves. ``fetch_dem``
#: already covers ``"1/3 arc-second"`` (~10 m); we deliberately exclude
#: it from this tool's allow-list so the agent picks the right entry
#: point (one tool, one resolution flavor).
SUPPORTED_RESOLUTIONS: tuple[str, ...] = (
    "1 arc-second",
    "1/9 arc-second",
    "1 meter",
    "2 arc-second",
    "5 meter",
)

#: US envelope including AK + HI + territories. The 1/9, 1 m, 2-arc-sec,
#: and 5 m datasets have sparse coverage within this envelope; the live
#: TNM query is the authoritative coverage check (we don't pre-screen
#: more tightly than this).
_US_BBOX: tuple[float, float, float, float] = (-180.0, 13.0, -65.0, 72.0)

#: pfdf's default tile cap. We expose it as a tool parameter so a
#: careful caller can raise it for a large-bbox 1 m mosaic.
_DEFAULT_MAX_TILES = 10
_MAX_MAX_TILES = 500   # pfdf's hard upper bound

#: Default ScienceBase / TNM timeout in seconds.
_DEFAULT_TIMEOUT_S = 120.0

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: Resolution → human-friendly metres-per-pixel for the LayerURI name.
_RESOLUTION_METERS_HINT: dict[str, str] = {
    "1 arc-second": "~30 m",
    "1/9 arc-second": "~3 m",
    "1 meter": "1 m",
    "2 arc-second": "~60 m (AK)",
    "5 meter": "5 m (AK)",
}

#: Resolution → style preset name. All paths share the same continuous
#: DEM ramp; the resolution is differentiated by the layer name.
_STYLE_PRESET = "continuous_dem"


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------


_METADATA = AtomicToolMetadata(
    name="fetch_3dep_extra",
    ttl_class="static-30d",
    source_class="3dep_extra",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    # Deterministic auto-publish opt-OUT (NATE 2026-06-26): the 3DEP-extra
    # derivative grids (role="input") are pure intermediates that feed terrain
    # analysis / solver setup, not standalone products the user asks to view.
    auto_publish=False,
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    resolution: str = "1 arc-second",
    **_kw: Any,
) -> float:
    """Estimate emitted COG size in MB.

    Rough empirical sizing (LZW-compressed COG, ~50-70% of raw):

    - 1 arc-second (30 m)   →  ~5 MB / sq-deg
    - 1/9 arc-second (3 m)  → ~500 MB / sq-deg
    - 1 meter               → ~5000 MB / sq-deg (rarely > 0.01 sq-deg)
    - 2 arc-second (60 m)   →  ~1 MB / sq-deg
    - 5 meter               → ~200 MB / sq-deg

    The estimator scales linearly with bbox area in square degrees and
    is intentionally conservative (over-estimates) so the Wave-1.5
    payload-warning gate fires before users dispatch huge LiDAR mosaics.
    """
    if bbox is None:
        return 5.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 5.0
    per_sq_deg = {
        "1 arc-second": 5.0,
        "1/9 arc-second": 500.0,
        "1 meter": 5000.0,
        "2 arc-second": 1.0,
        "5 meter": 200.0,
    }.get(resolution, 50.0)
    return max(0.05, sq_deg * per_sq_deg)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise ThreeDEPExtraInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise ThreeDEPExtraInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise ThreeDEPExtraInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise ThreeDEPExtraInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ThreeDEPExtraInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    west, south, east, north = _US_BBOX
    if max_lon < west or min_lon > east:
        raise ThreeDEPExtraInputError(
            f"bbox {bbox} does not intersect US envelope {_US_BBOX}; "
            "3DEP is US-only"
        )
    if max_lat < south or min_lat > north:
        raise ThreeDEPExtraInputError(
            f"bbox {bbox} does not intersect US envelope {_US_BBOX}; "
            "3DEP is US-only"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# pfdf → COG bytes.
# ---------------------------------------------------------------------------


def _fetch_3dep_dem_bytes(
    bbox: tuple[float, float, float, float],
    resolution: str,
    max_tiles: int,
    timeout_s: float,
) -> bytes:
    """Download a 3DEP raster through pfdf and serialize as a COG."""
    try:
        from pfdf.data.usgs.tnm import dem  # type: ignore[import-not-found]
        from pfdf.projection import BoundingBox  # type: ignore[import-not-found]
        import rioxarray  # noqa: F401 — registers .rio accessor
    except Exception as exc:  # noqa: BLE001
        raise ThreeDEPExtraUpstreamError(
            f"pfdf / rioxarray unavailable: {exc}"
        ) from exc

    min_lon, min_lat, max_lon, max_lat = bbox
    pfdf_bbox = BoundingBox(min_lon, min_lat, max_lon, max_lat, crs=4326)

    try:
        raster = dem.read(
            pfdf_bbox,
            resolution=resolution,
            max_tiles=max_tiles,
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        # pfdf raises ``NoTNMProductsError`` when the resolution has zero
        # coverage in the bbox; surface that as the empty error so the
        # agent can fall back / clarify rather than retry.
        if "noproducts" in msg.replace(" ", "") or "no tnm products" in msg:
            raise ThreeDEPExtraEmptyError(
                f"3DEP {resolution} has no TNM products covering bbox={bbox}; "
                "try a different resolution or expand the bbox"
            ) from exc
        if "too many" in msg or "tile" in msg and "limit" in msg:
            raise ThreeDEPExtraInputError(
                f"3DEP {resolution} request would exceed max_tiles={max_tiles} "
                f"for bbox={bbox}; raise max_tiles or shrink the bbox: {exc}"
            ) from exc
        raise ThreeDEPExtraUpstreamError(
            f"pfdf.data.usgs.tnm.dem.read failed for resolution={resolution} "
            f"bbox={bbox}: {exc}"
        ) from exc

    tmp_in: str | None = None
    tmp_cog: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_3dep_in_"
        ) as f:
            tmp_in = f.name
        try:
            raster.save(tmp_in, overwrite=True)
        except Exception as exc:  # noqa: BLE001
            raise ThreeDEPExtraUpstreamError(
                f"pfdf Raster.save failed for resolution={resolution}: {exc}"
            ) from exc

        import rioxarray as rxr

        try:
            da = rxr.open_rasterio(tmp_in, masked=True).squeeze(drop=True)
        except Exception as exc:  # noqa: BLE001
            raise ThreeDEPExtraUpstreamError(
                f"rioxarray.open_rasterio failed for staged 3DEP file: {exc}"
            ) from exc

        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_3dep_cog_"
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
            raise ThreeDEPExtraUpstreamError(
                f"COG write failed for resolution={resolution}: {exc}"
            ) from exc

        with open(tmp_cog, "rb") as f:
            cog_bytes = f.read()
        logger.info(
            "fetch_3dep_extra: resolution=%s bbox=%s -> %d bytes",
            resolution, bbox, len(cog_bytes),
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
def fetch_3dep_extra(
    bbox: tuple[float, float, float, float],
    resolution: Literal[
        "1 arc-second", "1/9 arc-second", "1 meter", "2 arc-second", "5 meter",
    ] = "1 arc-second",
    max_tiles: int = _DEFAULT_MAX_TILES,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch a non-default 3DEP DEM resolution for a US bbox.

    What it does:
        Pulls a USGS 3DEP elevation tile mosaic at one of five non-default
        resolutions — 1 arc-second (~30 m), 1/9 arc-second (~3 m), 1 m,
        2 arc-second (~60 m, AK only), or 5 m (AK only) — through pfdf's
        ``tnm.dem.read`` wrapper, mosaics intersecting tiles, and saves
        a single-band Cloud-Optimized GeoTIFF to the shared cache.

    When to use:
        - User asks for elevation at a resolution that ``fetch_dem`` does
          NOT serve — anything other than the 10 m / 30 m default. Common
          patterns: "I want 3-meter elevation", "give me the 1 m LiDAR DEM",
          "elevation in Alaska at 5 m".
        - Post-fire debris-flow workflow asks for the 1/9 arc-second
          channel-scale DEM (pfdf's recommended path for stream
          delineation in burn perimeters).
        - High-resolution slope / aspect / hillshade derivatives over a
          small bbox where the 1 m LiDAR mosaic is available.
        - Alaska work where the standard 1/3 arc-second tile tree is
          sparse — fall back to 2 arc-second or 5 m.

    When NOT to use:
        - DO NOT use for the default 10 m / 30 m DEM — use ``fetch_dem``
          (the canonical ``fetch_dem`` covers 1/3 arc-second and an
          aliased 30 m via ``py3dep`` and is the right default).
        - DO NOT use outside the US — 3DEP coverage is US-only; the
          input validator raises ``ThreeDEPExtraInputError`` for
          out-of-US bboxes. Use a future ``fetch_copernicus_dem`` for
          global coverage.
        - DO NOT use for bathymetry — 3DEP is a land-surface DEM.
        - DO NOT request 1 meter or 1/9 arc-second over a > ~10 km × 10 km
          bbox without raising ``max_tiles`` — pfdf will refuse with a
          ``ThreeDEPExtraInputError`` covering the tile-count limit.

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
            4-float tuple, lon/lat ordered min-then-max. Must intersect
            the US envelope ``(-180, 13, -65, 72)``. For 1 m / 1/9
            arc-second keep the bbox small (≤0.05° on a side typical) —
            larger bboxes hit ``max_tiles`` quickly. Example for Fort
            Myers at 1 arc-second: ``(-82.0, 26.4, -81.7, 26.7)``.
        resolution: One of:
            - ``"1 arc-second"`` (default, ~30 m, CONUS+OUS)
            - ``"1/9 arc-second"`` (~3 m, sparse CONUS LiDAR coverage)
            - ``"1 meter"`` (UTM-zoned LiDAR; small bbox required)
            - ``"2 arc-second"`` (~60 m, Alaska only)
            - ``"5 meter"`` (Alaska LiDAR mosaic).
        max_tiles: Maximum number of TNM tiles allowed to intersect
            the bbox. Default 10 (pfdf's default). Range [1, 500] —
            raise for larger 1 m / 1/9-arc-second mosaics; pfdf raises
            if the live count exceeds this.
        timeout_s: ScienceBase / TNM connect-and-read timeout in seconds.
            Defaults to 120. Multi-tile mosaics can take a while at high
            resolution.

    Returns:
        ``LayerURI`` pointing at a single-band COG in the cache bucket
        ``s3://trid3nt-cache/cache/static-30d/3dep_extra/<key>.tif``.
        ``layer_type="raster"``, ``role="input"``,
        ``style_preset="continuous_dem"``, ``units="meters"`` (NAVD88 or
        local vertical datum depending on the resolution path; for v0.1
        we report ``meters`` and surface CRS-level metadata in the COG
        header rather than the LayerURI). Downstream tools consume the
        COG as elevation input for slope / aspect / hillshade / pfdf
        debris-flow watershed delineation.

    Cross-tool dependencies:
        - Composes WITH: ``compute_slope`` / ``compute_aspect`` /
          ``compute_hillshade`` / ``compute_colored_relief`` (terrain
          derivatives); ``clip_raster_to_polygon`` (clip to a watershed
          / burn perimeter); pfdf debris-flow workflows that depend on
          the 1/9 arc-second channel DEM; ``publish_layer`` (render via
          the ``continuous_dem`` QML).
        - Sibling: ``fetch_dem`` — the canonical 10 m / 30 m path. Use
          fetch_dem unless you specifically need one of the five
          resolutions in ``SUPPORTED_RESOLUTIONS``.
        - Upstream source: USGS 3DEP TNM tile tree via
          ``pfdf.data.usgs.tnm.dem``.

    Cache: ``ttl_class="static-30d"``, ``source_class="3dep_extra"``.
    3DEP tiles are archival; the 30-day bucket amortizes well.

    Errors:
        - ``ThreeDEPExtraInputError``: bad bbox / unsupported resolution
          / out-of-US bbox / too many tiles (retryable=False).
        - ``ThreeDEPExtraUpstreamError``: TNM 5xx / network error / COG
          materialization failure (retryable=True).
        - ``ThreeDEPExtraEmptyError``: resolution has no coverage in the
          bbox (1 m / 1/9 arc-second outside LiDAR project footprints,
          2 arc-second / 5 m outside Alaska) (retryable=False).

    Tier-1 free. No API key. ``supports_global_query=False``.
    """
    if not isinstance(bbox, tuple):
        try:
            bbox = tuple(bbox)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ThreeDEPExtraInputError(
                f"bbox must be a 4-tuple or list; got {type(bbox).__name__}"
            ) from exc
    _validate_bbox(bbox)  # type: ignore[arg-type]
    q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    if resolution not in SUPPORTED_RESOLUTIONS:
        raise ThreeDEPExtraInputError(
            f"unknown resolution={resolution!r}; allowed: "
            f"{list(SUPPORTED_RESOLUTIONS)} "
            "(use fetch_dem for the default 1/3 arc-second / 30 m paths)"
        )

    try:
        mt = int(max_tiles)
    except (TypeError, ValueError) as exc:
        raise ThreeDEPExtraInputError(
            f"max_tiles must be an integer; got {max_tiles!r}"
        ) from exc
    if not (1 <= mt <= _MAX_MAX_TILES):
        raise ThreeDEPExtraInputError(
            f"max_tiles must be in [1, {_MAX_MAX_TILES}]; got {mt}"
        )

    try:
        t_s = float(timeout_s)
    except (TypeError, ValueError) as exc:
        raise ThreeDEPExtraInputError(
            f"timeout_s must be a finite number; got {timeout_s!r}"
        ) from exc
    if not math.isfinite(t_s) or t_s <= 0:
        raise ThreeDEPExtraInputError(
            f"timeout_s must be > 0 and finite; got {t_s!r}"
        )

    params: dict[str, Any] = {
        "bbox": list(q_bbox),
        "resolution": resolution,
        "max_tiles": mt,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_3dep_dem_bytes(q_bbox, resolution, mt, t_s),
    )
    assert result.uri is not None, (
        "fetch_3dep_extra is cacheable; uri must be set by read_through"
    )

    pretty = _RESOLUTION_METERS_HINT.get(resolution, resolution)
    layer_id_res = (
        resolution.replace(" ", "-")
        .replace("/", "")
        .lower()
    )
    return LayerURI(
        layer_id=(
            f"3dep-extra-{layer_id_res}-"
            f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=(
            f"USGS 3DEP DEM ({resolution}, {pretty}) — bbox "
            f"({q_bbox[0]:.2f},{q_bbox[1]:.2f},{q_bbox[2]:.2f},{q_bbox[3]:.2f})"
        ),
        layer_type="raster",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="input",
        units="meters",
    )
