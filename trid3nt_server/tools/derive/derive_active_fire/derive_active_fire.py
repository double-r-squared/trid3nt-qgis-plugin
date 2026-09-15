"""``derive_active_fire``: an ABI layer's two window bands -> the burning pixels.

A sub-pixel flame dominates the shortwave window while the longwave window stays
near the ambient surface, and it is hot where its neighbours are not: the two
window tests screen for candidates and the shortwave contrast against the
neighbourhood decides which of them are burning. Thresholds and the contrast in
standard deviations are parameters; the defaults are stated at their lines.
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
    window bands are absent), ``ACTIVE_FIRE_INPUT_INVALID`` (a non-finite threshold
    or a negative contrast), ``ACTIVE_FIRE_NO_DETECTIONS`` (no candidate, or no
    candidate that stands out from its background), ``ACTIVE_FIRE_WRITE_FAILED``."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class ActiveFireLayerURI(LayerURI):
    """The detected pixels as points, with how many there are, how many candidates
    the window tests raised before the contrast test, the tests they passed and the
    strongest signal in the crop."""

    fire_pixel_count: int = 0
    candidate_cell_count: int = 0
    bt_shortwave_min_k: float = 0.0
    bt_split_min_k: float = 0.0
    background_sigma: float = 0.0
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

#: The contextual stage of the same MODIS Collection-6 algorithm, which is what
#: makes the pair of floors a detection rather than a screen: a candidate is fire
#: only where its shortwave brightness temperature exceeds that of the non-candidate
#: cells of its neighbourhood by this many standard deviations. Sunlit bare ground
#: is hot across the whole window at once, so it clears the floors and fails this
#: test; a flame is hot where its neighbours are not. The contrast is measured on
#: the shortwave window alone: the difference between the windows carries cloud,
#: which is cold in the longwave and would inflate the background's spread with a
#: signal that has nothing to do with burning.
_BACKGROUND_SIGMA = 3.0

#: The neighbourhood a candidate is measured against: a square window this many
#: cells across, clipped at the crop edge, in which the valid non-candidate cells
#: must be at least a quarter of what the window covers and at least six, the
#: published requirement for a background worth measuring. A candidate whose window
#: holds fewer - the interior of a scene that is candidate everywhere - has no
#: background to stand out from and is UNCLASSIFIABLE, which this tool reports as
#: not burning rather than as fire.
_BACKGROUND_WINDOW_CELLS = 15
_BACKGROUND_MIN_FRACTION = 0.25
_BACKGROUND_MIN_CELLS = 6

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


def _number(value: Any, fallback: float, name: str, unit: str,
            minimum: float | None = None) -> float:
    """A caller's number in its stated unit, or the published default."""
    import math

    if value is None:
        return float(fallback)
    try:
        resolved = float(value)
    except (TypeError, ValueError) as exc:
        raise ActiveFireError(
            "ACTIVE_FIRE_INPUT_INVALID",
            f"{name} must be {unit}; got {value!r}.") from exc
    if not math.isfinite(resolved) or (minimum is not None and resolved < minimum):
        raise ActiveFireError(
            "ACTIVE_FIRE_INPUT_INVALID",
            f"{name} must be a finite {unit}"
            + (f" of at least {minimum:g}" if minimum is not None else "")
            + f"; got {value!r}.")
    return resolved


def _window_sums(values: Any, radius: int) -> Any:
    """Sums over a square window ``2 * radius + 1`` cells across, centred on every
    cell and clipped at the edges, so a border cell sees the part of its window that
    exists."""
    import numpy as np

    array = np.asarray(values, dtype=np.float64)
    height, width = array.shape
    integral = np.pad(array, ((1, 0), (1, 0)))
    np.cumsum(integral, axis=0, out=integral)
    np.cumsum(integral, axis=1, out=integral)
    top = np.clip(np.arange(height) - radius, 0, height)[:, None]
    bottom = np.clip(np.arange(height) + radius + 1, 0, height)[:, None]
    left = np.clip(np.arange(width) - radius, 0, width)[None, :]
    right = np.clip(np.arange(width) + radius + 1, 0, width)[None, :]
    return (integral[bottom, right] - integral[top, right]
            - integral[bottom, left] + integral[top, left])


def _background_floor(values: Any, background: Any, counts: Any, radius: int,
                      sigma: float) -> Any:
    """The per-cell floor a candidate must clear: the mean of the background cells in
    its window plus ``sigma`` of their standard deviations. A window with no
    background cell yields a floor of ``inf``, which no candidate clears."""
    import numpy as np

    filled = np.where(background, values, 0.0)
    safe = np.where(counts > 0, counts, 1.0)
    mean = _window_sums(filled, radius) / safe
    variance = np.maximum(
        _window_sums(filled * filled, radius) / safe - mean * mean, 0.0)
    return np.where(counts > 0, mean + sigma * np.sqrt(variance), np.inf)


@register_tool(
    _METADATA,
    # Reads only the layer it is handed and writes its own artifact.
    open_world_hint=False,
)
def derive_active_fire(
    layer: Any,
    bt_shortwave_min_k: float | None = None,
    bt_split_min_k: float | None = None,
    background_sigma: float | None = None,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> ActiveFireLayerURI:
    """Detect the ACTIVE FIRE pixels in a raw ABI layer: two window tests, then the
    background-contrast test that tells a flame from hot ground.

    Use this on a layer of raw ABI bands that carries band 7 (the 3.9 um shortwave
    window) and band 14 (the 11.2 um longwave window). A cell becomes a CANDIDATE
    when its shortwave brightness temperature is above an absolute floor AND its
    shortwave minus longwave difference is above a split floor: a sub-pixel flame
    saturates the shortwave window while the longwave window stays near the ambient
    surface. A candidate is a DETECTION only where it also stands out from the
    non-candidate cells of its neighbourhood, by ``background_sigma`` standard
    deviations of their shortwave brightness temperature - sunlit bare ground is hot
    across the whole neighbourhood at once, so it clears the two floors and fails
    the contrast test, while a fire is hot where its neighbours are not.

    Params:
        layer: the raw ABI layer to read - the layer a raw ABI band fetch returned,
            or its uri. It must carry bands 7 and 14; a layer without them is
            refused, naming the bands it does carry.
        bt_shortwave_min_k: the absolute 3.9 um brightness-temperature floor in
            kelvin. Default 310. Lower it to raise more candidates for the contrast
            test; raise it to screen harder before that test runs.
        bt_split_min_k: the floor on the 3.9 um minus 11.2 um difference in kelvin.
            Default 10. Raise it to demand an unambiguous shortwave excess.
        background_sigma: how far above the shortwave brightness temperature of its
            neighbourhood a candidate must sit, in standard deviations of the
            background cells. Default 3. Lower it (2) to catch weaker fires at the
            cost of false detections; raise it (4-5) over broken hot ground.

    Returns the detections as a point layer, one point at each detected cell centre
    carrying its two brightness temperatures and their difference, with the
    detection count, the candidate count and the tests on the layer. A crop where
    nothing is burning is refused - naming the strongest signal it found, and
    whether the candidates failed the floors or the contrast test - rather than
    returned as an empty layer.
    """
    import numpy as np

    def _fail(message: str) -> ActiveFireError:
        return ActiveFireError("ACTIVE_FIRE_LAYER_UNREADABLE", message)

    shortwave_min = _number(
        bt_shortwave_min_k, _BT_SHORTWAVE_MIN_K, "bt_shortwave_min_k",
        "a temperature in kelvin")
    split_min = _number(
        bt_split_min_k, _BT_SPLIT_MIN_K, "bt_split_min_k",
        "a temperature difference in kelvin")
    sigma = _number(
        background_sigma, _BACKGROUND_SIGMA, "background_sigma",
        "a count of standard deviations", minimum=0.0)

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
    candidate = valid & (shortwave >= np.float32(shortwave_min)) & (
        split >= np.float32(split_min))

    max_shortwave = float(np.nanmax(np.where(valid, shortwave, np.nan)))
    max_split = float(np.nanmax(split))
    if not candidate.any():
        raise ActiveFireError(
            "ACTIVE_FIRE_NO_DETECTIONS",
            f"no cell passes both window tests at "
            f"bt_shortwave_min_k={shortwave_min:g} K and "
            f"bt_split_min_k={split_min:g} K: the hottest cell in the crop reaches "
            f"{max_shortwave:.1f} K and the largest shortwave-minus-longwave "
            f"difference is {max_split:.1f} K. Nothing here is burning at that "
            "sensitivity.")

    radius = _BACKGROUND_WINDOW_CELLS // 2
    background = valid & ~candidate
    counts = _window_sums(background, radius)
    covered = _window_sums(np.ones_like(background, dtype=np.float64), radius)
    enough = (counts >= _BACKGROUND_MIN_CELLS) & (
        counts >= _BACKGROUND_MIN_FRACTION * covered)
    flagged = candidate & enough & (
        shortwave >= _background_floor(shortwave, background, counts, radius, sigma))
    candidate_count = int(candidate.sum())
    if not flagged.any():
        raise ActiveFireError(
            "ACTIVE_FIRE_NO_DETECTIONS",
            f"{candidate_count} cells pass both window tests but none stands out "
            f"from the shortwave background of its neighbourhood by "
            f"background_sigma={sigma:g}: the hottest cell "
            f"reaches {max_shortwave:.1f} K and the largest "
            f"shortwave-minus-longwave difference is {max_split:.1f} K, and the "
            "cells around them are just as hot. That is ground the sun has heated, "
            "not combustion.")

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
        "derive_active_fire: %d of %d candidates detected at %g K / %g K split and "
        "%g sigma over background (max %.1f K, max split %.1f K)",
        len(features), candidate_count, shortwave_min, split_min, sigma,
        max_shortwave, max_split)
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
        candidate_cell_count=candidate_count,
        bt_shortwave_min_k=shortwave_min,
        bt_split_min_k=split_min,
        background_sigma=sigma,
        max_bt_shortwave_k=round(max_shortwave, 2),
        max_bt_split_k=round(max_split, 2),
        cell_size_deg=round(cell, 6))
