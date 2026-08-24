"""Shared NLCD-derived overland roughness (Manning's n) resolution (law 9).

Several engines need a single overland-friction Manning's n to solve when the user
supplies none - SWMM's quasi-2D overland grid (``overland_manning_n``), GeoClaw's
bottom-friction coefficient (``manning_n``). Historically each fell back to an
invented constant (SWMM 0.03, GeoClaw 0.025) - a friction value with no basis in
the AOI's actual land cover, labeled but run on regardless. Law 9 forbids that: a
physics-consequential value with no real data source must REFUSE, not run on an
invention.

This module is the single resolution seam those engines share (it lives in
``workflows/shared`` so swmm / geoclaw import one derivation, not two). The real
source is the SAME version-pinned NLCD land-cover -> Manning's n table SFINCS
already builds its per-cell roughness grid from (``shared/manning.py`` +
``data/manning_mapping.csv``). Here it is reduced to a single representative scalar
for a whole AOI:

- ``area_weighted_manning`` -> the area-weighted mean of the per-class Manning's n
  over the AOI's NLCD cells (each class weighted by its cell count). This is the
  honest-simple reduction of a heterogeneous land cover to ONE bulk friction value:
  a screening estimate, NOT a per-cell roughness field (SFINCS builds that when the
  fidelity is needed). A single scalar over a whole AOI is a simplification, but it
  is DERIVED from the real land cover at the AOI, not invented.
- ``resolve_overland_manning`` -> the user -> NLCD-derived -> REFUSE ladder, with a
  ``SyntheticInput`` provenance entry the caller narrates under its own param name.

When the caller supplies a value it is used (``user``); when NLCD can serve, the
value is DERIVED (``derived``, source named, screening caveat stated); when NLCD
cannot serve (fetch fails, AOI outside CONUS/PR/USVI coverage, or all-nodata over
the window) the value is UNRESOLVED and its ``SyntheticInput`` carries
``basis="default_demo", consequence="physics"`` so the input-review gate REFUSES in
auto mode. The demo constants are gone - there is no invented value to fall back to.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from trid3nt_contracts.common import SyntheticInput

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.shared.manning import (
    MANNING_MAPPING_VERSION,
    ManningMappingError,
    load_manning_mapping,
)

logger = logging.getLogger("trid3nt_server.workflows.shared.roughness_resolve")

__all__ = [
    "area_weighted_manning",
    "nlcd_class_histogram",
    "ManningResolution",
    "resolve_overland_manning",
]

#: NLCD is a 30 m product; fetch it at native resolution for the class histogram
#: (a finer request would only resample the same classes - no added information).
NLCD_RESOLUTION_M: int = 30

#: NLCD nodata / background sentinels to drop from the histogram: 0 (background,
#: remapped to nodata by the fetcher), 255 and -9999 (typical raster nodata).
_NLCD_NODATA_SENTINELS: frozenset[int] = frozenset({0, 255, -9999})


def nlcd_class_histogram(landcover_uri: str) -> dict[int, int]:
    """Return ``{nlcd_class_int: cell_count}`` over a fetched NLCD raster.

    Reads band-1 via the shared boto3/GDAL reader (handles ``s3://`` / ``file://``
    / local uniformly, same seam ``aquifer_resolve.mean_valid_raster`` uses), drops
    the nodata sentinels + the raster's declared nodata, and counts cells per class.
    NEVER raises: any read failure returns an empty dict so the caller REFUSES (law
    9 - there is no demo default to fall back to).
    """
    try:
        import numpy as np
        import rasterio

        from trid3nt_server.tools.processing._gdal_runner import read_raster_bytes

        read_uri = (
            landcover_uri[len("file://"):]
            if landcover_uri.startswith("file://")
            else landcover_uri
        )
        data = read_raster_bytes(read_uri, on_error=lambda msg: RuntimeError(msg))
        with rasterio.MemoryFile(data) as mf:
            with mf.open() as src:
                arr = src.read(1)
                nodata = src.nodata
        flat = np.asarray(arr).ravel()
        classes, counts = np.unique(flat, return_counts=True)
        hist: dict[int, int] = {}
        for cls, cnt in zip(classes.tolist(), counts.tolist()):
            ci = int(cls)
            if ci in _NLCD_NODATA_SENTINELS:
                continue
            if nodata is not None and ci == int(nodata):
                continue
            hist[ci] = int(cnt)
        return hist
    except Exception as exc:  # noqa: BLE001 -- raster read is best-effort; refuse on failure
        logger.warning(
            "roughness: NLCD class-histogram read failed (non-fatal, will REFUSE - "
            "no demo default): %s",
            exc,
        )
        return {}


def area_weighted_manning(
    landcover_uri: str, mapping_csv: Any = None
) -> tuple[float | None, dict[str, Any]]:
    """Area-weighted mean Manning's n over an AOI's NLCD cells.

    Each NLCD class is mapped to its Manning's n via the version-pinned
    ``manning_mapping.csv`` (the SFINCS substrate) and weighted by its cell count:
    ``n_bar = sum(count_c * n_c) / sum(count_c)`` over the classes carrying a
    mapping. Returns ``(n_bar_or_None, meta)``; None when the raster is unreadable /
    all-nodata or no class maps (the caller REFUSES). ``meta`` carries the class
    fractions + the mapping version for loud provenance. NEVER raises.
    """
    meta: dict[str, Any] = {"mapping_version": MANNING_MAPPING_VERSION}
    hist = nlcd_class_histogram(landcover_uri)
    if not hist:
        meta["reason"] = "no valid NLCD land-cover cells over the AOI window"
        return None, meta
    try:
        mapping = load_manning_mapping(mapping_csv)
    except ManningMappingError as exc:
        meta["reason"] = f"Manning mapping load failed: {exc}"
        return None, meta

    total = 0
    weighted = 0.0
    fractions: dict[int, float] = {}
    all_cells = sum(hist.values())
    for cls, cnt in hist.items():
        n_c = mapping.get(cls)
        if n_c is None:
            logger.warning(
                "roughness: NLCD class %d has no Manning mapping (skipped in the "
                "area-weight); mapping version=%s",
                cls,
                MANNING_MAPPING_VERSION,
            )
            continue
        total += cnt
        weighted += cnt * float(n_c)
        fractions[cls] = round(cnt / all_cells, 4) if all_cells else 0.0
    if total == 0:
        meta["reason"] = "no NLCD class over the AOI carries a Manning mapping"
        return None, meta
    n_bar = weighted / total
    dominant = max(hist.items(), key=lambda kv: kv[1])[0]
    meta.update(
        {
            "manning_n": round(n_bar, 4),
            "class_fractions": fractions,
            "dominant_class": int(dominant),
            "n_classes": len(fractions),
        }
    )
    return round(n_bar, 4), meta


def _fetch_landcover_uri(bbox: Any) -> tuple[str | None, dict[str, Any]]:
    """Fetch NLCD over ``bbox`` and return ``(uri_or_None, meta)``. NEVER raises."""
    meta: dict[str, Any] = {}
    try:
        entry = TOOL_REGISTRY.get("fetch_landcover")
        if entry is None:
            meta["reason"] = "fetch_landcover not registered"
            return None, meta
        layer = entry.fn(bbox=list(bbox), resolution_m=NLCD_RESOLUTION_M)
        uri = (
            layer.get("uri") if isinstance(layer, dict)
            else getattr(layer, "uri", None)
        )
        vintage = (
            layer.get("nlcd_vintage_year") if isinstance(layer, dict)
            else getattr(layer, "nlcd_vintage_year", None)
        )
        if not uri:
            meta["reason"] = (
                "fetch_landcover returned no raster (AOI outside NLCD CONUS/PR/USVI "
                "coverage)"
            )
            return None, meta
        if vintage is not None:
            meta["nlcd_vintage_year"] = vintage
        return str(uri), meta
    except Exception as exc:  # noqa: BLE001 -- fetch is best-effort; refuse on failure
        meta["reason"] = f"NLCD fetch error: {exc}"
        logger.warning(
            "roughness: fetch_landcover failed (non-fatal, will REFUSE - no demo "
            "default): %s",
            exc,
        )
        return None, meta


@dataclass
class ManningResolution:
    """The resolved overland Manning's n + its machine-readable provenance.

    ``manning_n`` is None only when UNRESOLVED (NLCD could not serve and the caller
    supplied nothing) - in that case ``entry`` carries ``basis="default_demo",
    consequence="physics"`` so the input-review gate refuses in auto mode. When the
    resolution proceeds (user or derived), the value is real (never an invention).
    """

    manning_n: float | None
    source: str  # "user_supplied" | "nlcd_area_weighted" | "unresolved"
    entry: SyntheticInput
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        """True when the Manning's n is real (never an invented default)."""
        return self.manning_n is not None


def _derived_caveat(meta: dict[str, Any]) -> str:
    dom = meta.get("dominant_class")
    ncls = meta.get("n_classes")
    return (
        f"DERIVED as the area-weighted mean of NLCD land-cover Manning's n over the "
        f"AOI ({ncls} classes, dominant class {dom}; manning_mapping v"
        f"{meta.get('mapping_version')}). SCREENING estimate - ONE bulk friction "
        "value for a heterogeneous AOI, NOT a per-cell roughness field. Supply an "
        "explicit Manning's n for a calibrated run."
    )


async def resolve_overland_manning(
    bbox: Any,
    user_manning: float | None,
    *,
    param_name: str,
    units: str | None = "s/m^(1/3)",
    allow_nlcd_derive: bool = True,
    mapping_csv: Any = None,
) -> ManningResolution:
    """Resolve an overland Manning's n over the AOI: user -> NLCD-derived -> REFUSE.

    - A caller-supplied value is used verbatim (``basis="user"``).
    - A missing value is DERIVED as the area-weighted mean of NLCD land-cover
      Manning's n over the AOI (``basis="derived"``, source named, screening caveat
      in the note). The NLCD fetch + raster read are offloaded to a thread (never
      block the event loop).
    - When NLCD cannot serve a still-missing value, it is UNRESOLVED: the
      ``SyntheticInput`` carries ``basis="default_demo", consequence="physics"``
      (value None) so the input-review gate REFUSES in auto mode (law 9 - no
      invented default). ``param_name`` lets each engine narrate under its own name
      (SWMM ``overland_manning_n`` / GeoClaw ``manning_n``).
    """
    if user_manning is not None:
        n = float(user_manning)
        return ManningResolution(
            manning_n=n,
            source="user_supplied",
            entry=SyntheticInput(
                param=param_name, value=round(n, 4), units=units,
                basis="user", consequence="physics", real_source_if_any=None,
                note="caller-supplied overland Manning's n.",
            ),
            meta={"source": "user_supplied"},
        )

    meta: dict[str, Any] = {}
    n_bar: float | None = None
    if allow_nlcd_derive:
        uri, fetch_meta = await asyncio.to_thread(_fetch_landcover_uri, bbox)
        meta.update(fetch_meta)
        if uri is not None:
            n_bar, aw_meta = await asyncio.to_thread(
                area_weighted_manning, uri, mapping_csv
            )
            meta.update(aw_meta)
    else:
        meta["reason"] = "NLCD derivation disabled by caller"

    if n_bar is not None and math.isfinite(n_bar) and n_bar > 0:
        return ManningResolution(
            manning_n=float(n_bar),
            source="nlcd_area_weighted",
            entry=SyntheticInput(
                param=param_name, value=round(float(n_bar), 4), units=units,
                basis="derived", consequence="physics",
                real_source_if_any="fetch_landcover (NLCD area-weighted Manning's n)",
                note="Overland roughness " + _derived_caveat(meta),
            ),
            meta=meta,
        )

    return ManningResolution(
        manning_n=None,
        source="unresolved",
        entry=SyntheticInput(
            param=param_name, value=None, units=units,
            basis="default_demo", consequence="physics", real_source_if_any=None,
            note=(
                "overland Manning's n is required and could not be resolved from NLCD "
                f"land cover at this AOI ({meta.get('reason', 'unavailable')}). No "
                f"invented default (law 9): supply {param_name} or run over an AOI with "
                "NLCD coverage."
            ),
        ),
        meta=meta,
    )
