"""``derive_active_fire``: an ABI layer's two window bands -> the burning pixels.

A sub-pixel flame dominates the shortwave window while the longwave window stays
near the ambient surface, so the pair of them is a SCREEN for combustion rather
than a verdict on it: hot bare ground can clear both at this cell size. Both
thresholds are parameters; the defaults are stated at the lines that hold them.
Nothing here fetches - the bands arrive on the layer.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive._abi_layer import read_bands
from trid3nt_server.tools.derive._hydrology_common import (
    HydrologyUpstreamError,
    _write_geojson,
)

__all__ = ["ActiveFireError", "ActiveFireLayerURI", "derive_active_fire"]

logger = logging.getLogger(
    "trid3nt_server.tools.derive.derive_active_fire.derive_active_fire")


class ActiveFireError(RuntimeError):
    """A typed refusal: ``ACTIVE_FIRE_LAYER_UNREADABLE`` (no layer, no uri, or the
    window bands are absent), ``ACTIVE_FIRE_INPUT_INVALID`` (a non-finite
    threshold), ``ACTIVE_FIRE_NO_DETECTIONS`` (nothing in the crop passes both
    tests), ``ACTIVE_FIRE_WRITE_FAILED``."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ActiveFireLayerURI(LayerURI):
    """The flagged pixels as points, with how many there are, the two thresholds
    they passed and the strongest signal in the crop."""

    fire_pixel_count: int = 0
    bt_shortwave_min_k: float = 0.0
    bt_split_min_k: float = 0.0
    max_bt_shortwave_k: float | None = None
    max_bt_split_k: float | None = None
    cell_size_deg: float = 0.0


#: The two window bands the discriminator reads: the 3.9 um shortwave window, which
#: a sub-pixel flame saturates, and the 11.2 um longwave window, which tracks the
#: ambient surface temperature of the whole cell.
_SHORTWAVE_BAND = 7
_LONGWAVE_BAND = 14

#: The published daytime potential-fire-pixel thresholds of the MODIS Collection-6
#: contextual algorithm, whose 3.9 um and 11 um channels these two ABI bands stand
#: in for: the shortwave brightness temperature must exceed 310 K and the shortwave
#: minus longwave difference must exceed 10 K. Both are parameters because the ABI
#: cell is coarser than the cell those thresholds were published over, so a warm
#: bare-ground scene may need the shortwave floor raised.
_BT_SHORTWAVE_MIN_K = 310.0
_BT_SPLIT_MIN_K = 10.0

#: Detections are points because that is what a detection IS: one flagged cell with
#: the temperatures it was flagged on, in the same data class - and so the same
#: shape on the map and the same input downstream - as every other satellite
#: active-fire detection the product carries.
_STYLE = {"kind": "reference", "geometry": "point"}

_METADATA = AtomicToolMetadata(
    name="derive_active_fire",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _threshold(value: Any, fallback: float, name: str) -> float:
    """A threshold in kelvin: the caller's value, or the published default."""
    import math

    if value is None:
        return float(fallback)
    try:
        resolved = float(value)
    except (TypeError, ValueError) as exc:
        raise ActiveFireError(
            "ACTIVE_FIRE_INPUT_INVALID",
            f"{name} must be a temperature in kelvin; got {value!r}.") from exc
    if not math.isfinite(resolved):
        raise ActiveFireError(
            "ACTIVE_FIRE_INPUT_INVALID",
            f"{name} must be a finite temperature in kelvin; got {value!r}.")
    return resolved


@register_tool(
    _METADATA,
    # Reads only the layer it is handed and writes its own artifact.
    open_world_hint=False,
)
def derive_active_fire(
    layer: Any,
    bt_shortwave_min_k: float | None = None,
    bt_split_min_k: float | None = None,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> ActiveFireLayerURI:
    """Flag the ACTIVE FIRE pixels in a raw ABI layer by the split-window test.

    Use this on a layer of raw ABI bands that carries band 7 (the 3.9 um shortwave
    window) and band 14 (the 11.2 um longwave window) to SCREEN for cells that may
    be burning. A cell is flagged when its shortwave brightness temperature is above
    an absolute floor AND its shortwave minus longwave difference is above a split
    floor: a sub-pixel flame saturates the shortwave window while the longwave
    window stays near the ambient surface.

    READ THE RESULT AS A SCREEN, NOT A DETECTION. At the 2 km ABI cell the two
    published floors are cleared by hot sunlit bare ground as well as by fire: over
    arid terrain in the afternoon they flag the whole crop. Raise
    ``bt_shortwave_min_k`` for such a scene, read the flagged cells against the
    temperatures they carry, and use a dedicated detection product
    (``fetch_firms_active_fire``) when the question is whether a fire EXISTS rather
    than where the hot cells are in this frame.

    Params:
        layer: the raw ABI layer to read - the layer a raw ABI band fetch returned,
            or its uri. It must carry bands 7 and 14; a layer without them is
            refused, naming the bands it does carry.
        bt_shortwave_min_k: the absolute 3.9 um brightness-temperature floor in
            kelvin. Default 310. Raise it (315-325) over hot bare ground; lower it
            to catch smaller or cooler fires at the cost of false flags.
        bt_split_min_k: the floor on the 3.9 um minus 11.2 um difference in kelvin.
            Default 10. Raise it (15-20) to demand an unambiguous fire signal.

    Returns the flagged cells as a point layer, one point at each flagged cell
    centre carrying its two brightness temperatures and their difference, with the
    detection count and the thresholds on the layer. A crop where nothing passes
    both tests is refused, naming the strongest signal it did find, rather than
    returned as an empty layer.
    """
    import numpy as np

    def _fail(message: str) -> ActiveFireError:
        return ActiveFireError("ACTIVE_FIRE_LAYER_UNREADABLE", message)

    shortwave_min = _threshold(
        bt_shortwave_min_k, _BT_SHORTWAVE_MIN_K, "bt_shortwave_min_k")
    split_min = _threshold(bt_split_min_k, _BT_SPLIT_MIN_K, "bt_split_min_k")

    bands, grid = read_bands(layer, (_SHORTWAVE_BAND, _LONGWAVE_BAND), on_error=_fail)
    shortwave = bands[_SHORTWAVE_BAND]
    longwave = bands[_LONGWAVE_BAND]
    valid = np.isfinite(shortwave) & np.isfinite(longwave)
    if not valid.any():
        raise ActiveFireError(
            "ACTIVE_FIRE_LAYER_UNREADABLE",
            "the two window bands carry no valid pixel anywhere in the crop, so "
            "there is nothing to test.")
    split = np.where(valid, shortwave - longwave, np.float32(np.nan))
    flagged = valid & (shortwave >= np.float32(shortwave_min)) & (
        split >= np.float32(split_min))

    max_shortwave = float(np.nanmax(np.where(valid, shortwave, np.nan)))
    max_split = float(np.nanmax(split))
    if not flagged.any():
        raise ActiveFireError(
            "ACTIVE_FIRE_NO_DETECTIONS",
            f"no cell passes both tests at bt_shortwave_min_k={shortwave_min:g} K "
            f"and bt_split_min_k={split_min:g} K: the hottest cell in the crop "
            f"reaches {max_shortwave:.1f} K and the largest shortwave-minus-longwave "
            f"difference is {max_split:.1f} K. Nothing here is burning at that "
            "sensitivity.")

    transform = grid["transform"]
    rows, cols = np.nonzero(flagged)
    features = []
    for row, col in zip(rows.tolist(), cols.tolist()):
        lon, lat = transform * (col + 0.5, row + 0.5)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
            "properties": {
                "bt_shortwave_k": round(float(shortwave[row, col]), 2),
                "bt_longwave_k": round(float(longwave[row, col]), 2),
                "bt_split_k": round(float(split[row, col]), 2),
            },
        })

    seed = uuid.uuid4().hex[:8]
    try:
        uri = _write_geojson(
            {"type": "FeatureCollection", "features": features},
            "active_fire", seed, _output_dir)
    except HydrologyUpstreamError as exc:
        raise ActiveFireError(
            "ACTIVE_FIRE_WRITE_FAILED",
            f"the detections could not be written: {exc}") from exc

    lons = [f["geometry"]["coordinates"][0] for f in features]
    lats = [f["geometry"]["coordinates"][1] for f in features]
    cell = abs(float(transform.a))
    logger.info(
        "derive_active_fire: %d cells flagged at %g K / %g K split (max %.1f K, "
        "max split %.1f K)",
        len(features), shortwave_min, split_min, max_shortwave, max_split)
    return ActiveFireLayerURI(
        layer_id=f"active-fire-{seed}",
        name=f"Active fire detections ({len(features)} cells)",
        layer_type="vector",
        uri=uri,
        style=_STYLE,
        role="primary",
        units="K",
        crs_authid=str(grid["crs"]) if grid["crs"] else "EPSG:4326",
        bbox=(min(lons) - cell / 2.0, min(lats) - cell / 2.0,
              max(lons) + cell / 2.0, max(lats) + cell / 2.0),
        fire_pixel_count=len(features),
        bt_shortwave_min_k=shortwave_min,
        bt_split_min_k=split_min,
        max_bt_shortwave_k=round(max_shortwave, 2),
        max_bt_split_k=round(max_split, 2),
        cell_size_deg=round(cell, 6))
