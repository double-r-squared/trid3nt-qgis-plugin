"""``fetch_mobi`` atomic tool  --  NatureServe Map of Biodiversity Importance (MoBI).

Fetches the NatureServe Map of Biodiversity Importance (MoBI) for a bbox via
the Microsoft Planetary Computer (PC) STAC catalog. MoBI is the canonical
imperiled-species biodiversity-priority raster for the conterminous US: it maps
where concentrations of at-risk species (especially narrow-range endemics)
occur, the headline biodiversity layer in the SC-DNR-style conservation-priority
stack (``model_conservation_priority``).

Data source
===========

PC collection ``mobi`` ("MoBI: Map of Biodiversity Importance"):

    catalog: https://planetarycomputer.microsoft.com/api/stac/v1
    extent:  conterminous US, ~990 m GSD, single CONUS-wide item
    assets:  15 COG layers across three measures x five taxa groups:
      - SpeciesRichness_{All,Plants,Vertebrates,AquaticInverts,PollinatorInverts}
        (count of imperiled species)
      - RSR_{...}  (Range-Size Rarity  --  weighted by range size)
      - PWRSR_GAP12_SUM_{...}  (Protection-Weighted RSR  --  outside protected land)

The chosen layer asset is a single CONUS-wide Azure-Blob COG behind a SAS token;
this tool signs the href (``_pc_stac.sas_sign_href``), reads the asset warped to
EPSG:4326 and WINDOWED to the bbox through GDAL ``/vsicurl/`` (so a small AOI
materializes only its own clip of the CONUS raster), and re-emits a single-band
float32 COG with the ``mobi_biodiversity`` colormap.

Honesty (data-source fallback norm): MoBI is CONUS-only. A bbox outside CONUS
(or whose window is entirely nodata) raises a typed ``MoBIEmptyError``  --  never a
fabricated layer.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical ``(bbox,
layer)`` calls reuse the cached COG in the ``static-30d`` / ``mobi`` prefix.

Tier-1 free (no API key). Heavy emit-free sync raster work  --  registered in
``_ALWAYS_OFFLOAD_SYNC_TOOLS`` so it runs via ``asyncio.to_thread`` and never
stalls the WebSocket heartbeat.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from . import _pc_stac
from .cache import read_through

__all__ = [
    "fetch_mobi",
    "estimate_payload_mb",
    "MOBI_LAYERS",
    "MoBIError",
    "MoBIBboxError",
    "MoBILayerError",
    "MoBIEmptyError",
    "MoBIUpstreamError",
]

logger = logging.getLogger("grace2_agent.tools.fetch_mobi")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class MoBIError(RuntimeError):
    """Base class for fetch_mobi failures."""

    error_code = "MOBI_ERROR"
    retryable = True


class MoBIBboxError(MoBIError):
    """Malformed / out-of-range / degenerate bbox."""

    error_code = "MOBI_BBOX_INVALID"
    retryable = False


class MoBILayerError(MoBIError):
    """Unknown MoBI layer requested."""

    error_code = "MOBI_LAYER_INVALID"
    retryable = False


class MoBIEmptyError(MoBIError):
    """The bbox window is entirely nodata (outside CONUS coverage).

    Honest no-coverage signal (data-source fallback norm)  --  never fabricate.
    """

    error_code = "MOBI_EMPTY"
    retryable = False


class MoBIUpstreamError(MoBIError):
    """A PC STAC search / asset read / COG write failed."""

    error_code = "MOBI_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "mobi"

#: Caller-facing layer -> PC ``mobi`` item-asset key. The default
#: ``species_richness`` is the headline "imperiled-species richness" product the
#: kickoff asks for. The rarity-weighted variants are also exposed.
MOBI_LAYERS: dict[str, str] = {
    "species_richness": "SpeciesRichness_All",
    "species_richness_vertebrates": "SpeciesRichness_Vertebrates",
    "species_richness_plants": "SpeciesRichness_Plants",
    "range_size_rarity": "RSR_All",
    "protection_weighted_rsr": "PWRSR_GAP12_SUM_All",
}

_VALID_LAYERS = frozenset(MOBI_LAYERS.keys())

#: MoBI native GSD ~990 m; size the window grid accordingly (clamped).
_NATIVE_CELL_M = 990.0

#: MoBI CONUS coverage envelope (collection spatial extent). A bbox wholly
#: outside this fails fast before any network read.
_CONUS_BBOX: tuple[float, float, float, float] = (-130.24, 21.74, -63.66, 49.19)

_BBOX_DECIMALS = 6

#: Single style preset  --  a YlGn biodiversity ramp (low->high importance).
_STYLE_PRESET = "mobi_biodiversity"


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_mobi",
    ttl_class="static-30d",
    source_class="mobi",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate emitted MoBI COG size in MB.

    Single-band float32 at ~990 m is tiny even for a state-sized AOI; floor a
    conservative few MB so the warning gate never under-reports.
    """
    if bbox is None:
        return 2.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 2.0
    return max(0.25, sq_deg * 2.0)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise MoBIBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise MoBIBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise MoBIBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise MoBIBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise MoBIBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _bbox_intersects_conus(bbox: tuple[float, float, float, float]) -> bool:
    """True if ``bbox`` overlaps the MoBI CONUS coverage envelope at all."""
    w, s, e, n = bbox
    cw, cs, ce, cn = _CONUS_BBOX
    return not (e < cw or w > ce or n < cs or s > cn)


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Core: search MoBI -> windowed asset read -> single-band float32 COG bytes.
# ---------------------------------------------------------------------------


def _fetch_mobi_cog_bytes(
    bbox: tuple[float, float, float, float],
    layer: str,
) -> bytes:
    """Window the MoBI ``layer`` to ``bbox`` and return a single-band float32 COG.

    Raises:
        ``MoBIEmptyError``: the window is entirely nodata (outside CONUS).
        ``MoBIUpstreamError``: search / read / write failure.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    asset_key = MOBI_LAYERS[layer]

    try:
        item = _pc_stac.search_least_cloudy_item(
            collection=_COLLECTION,
            bbox=bbox,
            datetime_range=None,
            max_cloud_cover=None,
            sort_by_cloud=False,
        )
    except _pc_stac.PCStacNoItemsError as exc:
        raise MoBIEmptyError(
            f"no MoBI coverage for bbox={bbox} (MoBI is conterminous-US only): {exc}"
        ) from exc
    except _pc_stac.PCStacError as exc:
        raise MoBIUpstreamError(f"MoBI STAC search failed: {exc}") from exc

    assets = getattr(item, "assets", {}) or {}
    if asset_key not in assets:
        raise MoBIUpstreamError(
            f"MoBI item {getattr(item, 'id', '?')} missing asset {asset_key!r} "
            f"(have {sorted(assets)[:8]}...)"
        )

    signed = _pc_stac.sas_sign_href(assets[asset_key].href, _COLLECTION)
    vsicurl = "/vsicurl/" + signed

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)
    transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )

    try:
        with rasterio.Env(**_pc_stac.VSICURL_ENV_KW):
            with rasterio.open(vsicurl) as src:
                src_nodata = src.nodata
                dst = np.full((height_px, width_px), np.nan, dtype="float32")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                    src_nodata=src_nodata,
                    dst_nodata=float("nan"),
                )
    except Exception as exc:  # noqa: BLE001
        raise MoBIUpstreamError(
            f"MoBI window read failed for bbox={bbox} layer={layer}: {exc}"
        ) from exc

    valid = np.isfinite(dst)
    # MoBI nodata is 0 / negative outside coverage; treat <=0 as no-data for the
    # importance products (richness/RSR are strictly positive where mapped).
    valid &= dst > 0.0
    if not valid.any():
        raise MoBIEmptyError(
            f"MoBI window for bbox={bbox} layer={layer} is entirely nodata "
            "(bbox likely outside conterminous-US coverage)."
        )
    dst = np.where(valid, dst, np.nan).astype("float32")

    # Re-emit as a single-band float32 COG (NaN nodata, LZW) in EPSG:4326.
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="grace2_mobi_"
        ) as f:
            tmp_path = f.name
        profile = dict(
            driver="COG",
            dtype="float32",
            count=1,
            height=height_px,
            width=width_px,
            crs="EPSG:4326",
            transform=transform,
            nodata=float("nan"),
            compress="LZW",
        )
        with rasterio.open(tmp_path, "w", **profile) as out:
            out.write(dst, 1)
        with open(tmp_path, "rb") as fh:
            cog_bytes = fh.read()
    except Exception as exc:  # noqa: BLE001
        raise MoBIUpstreamError(f"MoBI COG write failed for bbox={bbox}: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    logger.info(
        "fetch_mobi: layer=%s asset=%s bbox=%s -> %d-byte COG (%dx%d, valid=%d)",
        layer,
        asset_key,
        bbox,
        len(cog_bytes),
        width_px,
        height_px,
        int(valid.sum()),
    )
    return cog_bytes


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (PC STAC public API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_mobi(
    bbox: tuple[float, float, float, float],
    layer: str = "species_richness",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch NatureServe Map of Biodiversity Importance (MoBI) for a US bbox.

    **What it does:** Reads the NatureServe MoBI imperiled-species
    biodiversity-priority raster (via the Microsoft Planetary Computer ``mobi``
    collection) windowed to ``bbox``, and returns a single-band float32 COG
    with a YlGn (low->high importance) ramp. The default ``species_richness``
    layer is the count of imperiled species per cell  --  high values flag
    biodiversity hotspots / conservation-priority areas.

    MoBI is the headline biodiversity layer in the conservation-priority stack:
    it shows WHERE at-risk-species concentrations are, complementing the actual
    occurrence points (``fetch_gbif_occurrences``) and threatened ranges
    (``fetch_iucn_red_list_range``).

    **When to use:**
    - User asks for biodiversity importance / imperiled-species richness /
      conservation-priority areas for a US region.
    - As the biodiversity layer in ``model_conservation_priority``.

    **When NOT to use:**
    - Outside the conterminous US (MoBI is CONUS-only)  --  a no-coverage result
      is an honest typed error, not a fabricated layer.
    - Individual species occurrences (use ``fetch_gbif_occurrences`` /
      ``fetch_inaturalist_observations``) or species RANGES (use
      ``fetch_iucn_red_list_range``).
    - Protected-area boundaries (use ``fetch_wdpa_protected_areas``).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. CONUS-only.
    - ``layer`` (str, default ``"species_richness"``): one of
      ``species_richness`` (imperiled-species count, the headline product),
      ``species_richness_vertebrates``, ``species_richness_plants``,
      ``range_size_rarity`` (range-size-weighted), ``protection_weighted_rsr``
      (RSR outside protected land).

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="primary"``,
    ``units="imperiled-species count"`` for richness layers) pointing at a
    single-band float32 COG in the ``static-30d``/``mobi`` cache prefix.
    ``style_preset="mobi_biodiversity"`` (YlGn ramp).

    **Data source:** NatureServe Map of Biodiversity Importance via the
    Microsoft Planetary Computer STAC (``mobi`` collection).

    FR-CE-8: routed through ``read_through`` so identical ``(bbox, layer)``
    calls reuse the cached COG.
    """
    if layer not in _VALID_LAYERS:
        raise MoBILayerError(
            f"unknown MoBI layer={layer!r}; allowed: {sorted(_VALID_LAYERS)}"
        )
    _validate_bbox(bbox)
    if not _bbox_intersects_conus(bbox):
        raise MoBIEmptyError(
            f"bbox={bbox} does not intersect MoBI conterminous-US coverage "
            f"{_CONUS_BBOX}; MoBI is a US-only biodiversity product."
        )

    q_bbox = _round_bbox(bbox)
    params = {"bbox": list(q_bbox), "layer": layer, "collection": _COLLECTION}

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_mobi_cog_bytes(q_bbox, layer),
    )
    assert result.uri is not None, (
        "fetch_mobi is cacheable; uri must be set by read_through"
    )

    units = (
        "imperiled-species count" if layer.startswith("species_richness") else None
    )
    label = {
        "species_richness": "All species",
        "species_richness_vertebrates": "Vertebrates",
        "species_richness_plants": "Plants",
        "range_size_rarity": "Range-Size Rarity",
        "protection_weighted_rsr": "Protection-Weighted RSR",
    }.get(layer, layer)

    return LayerURI(
        layer_id=(
            f"mobi-{layer}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=f"MoBI Biodiversity Importance - {label}",
        layer_type="raster",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units=units,
    )
