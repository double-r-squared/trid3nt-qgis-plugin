"""landcover hooks: NLCD through the MRLC WCS.

Two irreducible per-source steps, both PURE. ``pre_resolve`` normalizes the dataset
alias, parses the vintage year, and derives the auto-coarsen; ``envelope`` builds the
validation sidecar the downstream builder reads."""

# The auto-coarsen computes the effective resolution from the AOI size against the
# service's pixel budget and re-quantizes the bbox to that grid, all merged into params
# BEFORE read_through, so the effective resolution and quantized bbox enter the cache
# key: a bypassed gate never delivers a rung finer than the AOI honestly supports.
#
# The sidecar fields -- vintage year, dataset, source, effective and native resolution,
# whether it was downsampled and the note saying so -- live on the result SUBCLASS,
# because ``LayerURI`` is a frozen extra-forbid contract.

from __future__ import annotations

import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._fetch_common import round_bbox_to_resolution
from ..._router import hooks as _hooks
from ..._router.errors import router_input_error, router_upstream_error

__all__ = ["pre_resolve", "envelope"]

_DEFAULT_NLCD_DATASET = "nlcd_2021"
_NATIVE_RES_M = 30
_PIXEL_BUDGET = 4000  # max px/side the MRLC WCS server serves (auto-coarsen driver)


@_hooks.register_hook("landcover.pre_resolve")
def pre_resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Normalize the dataset, parse the vintage and auto-coarsen the resolution, PURE.
    Returns a params-merge carrying the resolved dataset, vintage year, effective
    resolution, re-quantized bbox and downsample flag, all entering the cache key."""
    sc = spec.error_code_prefix
    isfx = spec.input_error_suffix
    dataset = params.get("dataset") or _DEFAULT_NLCD_DATASET
    if not isinstance(dataset, str) or not dataset.strip():
        raise router_input_error(sc, f"dataset must be a non-empty string; got {dataset!r}", isfx)

    normalized = dataset.strip().lower()
    if normalized in ("nlcd", "nlcd_"):
        dataset = _DEFAULT_NLCD_DATASET

    if dataset.startswith("esa_worldcover_"):
        raise router_upstream_error(
            sc,
            "ESA WorldCover branch is not implemented in the v0.1 substrate "
            "(reserved for a follow-up job; opt into NLCD by passing "
            "dataset='nlcd_2021' / 'nlcd_2019').",
        )
    if not dataset.startswith("nlcd_"):
        raise router_input_error(
            sc,
            f"unsupported dataset={dataset!r}; allowed: 'nlcd' (default vintage, "
            f"currently {_DEFAULT_NLCD_DATASET!r}) or 'nlcd_YYYY' (Tier-1 CONUS), "
            "'esa_worldcover_' (opt-in, forward-looking - not implemented).",
            isfx,
        )
    try:
        vintage_year = int(dataset.split("_", 1)[1])
    except (IndexError, ValueError):
        raise router_input_error(
            sc,
            f"could not parse NLCD vintage year from dataset={dataset!r}; "
            "expected 'nlcd_YYYY' (e.g. 'nlcd_2021').",
            isfx,
        )

    bbox = [float(v) for v in params["bbox"]]
    requested_res = int(params.get("resolution_m") or _NATIVE_RES_M)
    effective_res = max(_NATIVE_RES_M, requested_res)

    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    long_axis_m = max(
        geod.inv(min_lon, mid_lat, max_lon, mid_lat)[2],
        geod.inv(min_lon, min_lat, min_lon, max_lat)[2],
    )
    budget_res = int(math.ceil(long_axis_m / _PIXEL_BUDGET))
    effective_res = max(effective_res, budget_res)
    downsampled = effective_res > _NATIVE_RES_M
    quantized = round_bbox_to_resolution(tuple(bbox), effective_res)

    return {
        "dataset": dataset,
        "vintage_year": vintage_year,
        "resolution_m": effective_res,
        "bbox": list(quantized),
        "downsampled": downsampled,
    }


@_hooks.register_hook("landcover.envelope")
def envelope(spec: SourceSpec, params: dict[str, Any], layer: Any, data: bytes | None) -> dict[str, Any]:
    """The Manning's-validation sidecar (-> LandcoverResult)."""
    vintage_year = int(params["vintage_year"])
    effective_res = int(params["resolution_m"])
    downsampled = bool(params.get("downsampled"))
    note = None
    if downsampled:
        note = (
            f"Landcover fetched at {effective_res} m (coarsened from {_NATIVE_RES_M} m native). "
            "NLCD class codes are preserved (nearest-neighbor resampling via WCS pixel grid). "
            "Category boundaries are approximate at this scale."
        )
    name = f"NLCD Land Cover ({vintage_year})" + (f" at {effective_res} m" if downsampled else "")
    return {
        "name": name,
        "nlcd_vintage_year": vintage_year,
        "dataset": str(params["dataset"]),
        "source": "mrlc-wcs",
        "effective_resolution_m": effective_res,
        "native_resolution_m": _NATIVE_RES_M,
        "downsampled": downsampled,
        "downsampling_note": note,
    }
