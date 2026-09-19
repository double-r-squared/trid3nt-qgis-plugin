"""``derive_active_fire``: an ABI layer's window and cloud bands -> the burning pixels.

A sub-pixel flame dominates the shortwave window while the longwave window stays
near the ambient surface, and it is hot where its neighbours are not: two window
tests screen for candidates and the published contextual tests, measured against
the CLOUD-FREE cells of the neighbourhood, decide which of them are burning. A
candidate whose neighbourhood holds too little cloud-free ground is unclassifiable
and says so. Nothing here fetches - the bands arrive on the layer.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive import DeriveError
from trid3nt_server.tools.derive._abi_layer import read_bands
from trid3nt_server.tools.derive._hydrology_common import (
    HydrologyUpstreamError,
    _write_geojson,
)

__all__ = ["ActiveFireError", "ActiveFireLayerURI", "derive_active_fire"]

logger = logging.getLogger(
    "trid3nt_server.tools.derive.derive_active_fire.derive_active_fire")


class ActiveFireError(DeriveError):
    """A typed refusal: ``ACTIVE_FIRE_LAYER_UNREADABLE`` (no layer, no uri, or a band
    the tests need is absent), ``ACTIVE_FIRE_INPUT_INVALID`` (a non-finite threshold
    or a negative contrast), ``ACTIVE_FIRE_NO_DETECTIONS`` (no candidate, or no
    candidate that stands out from its background), ``ACTIVE_FIRE_UNCLASSIFIABLE``
    (candidates, but not one of them has a background to be measured against),
    ``ACTIVE_FIRE_WRITE_FAILED``."""


class ActiveFireLayerURI(LayerURI):
    """The detected pixels as points, with how many there are, how many candidates
    the window tests raised, how many of those had no background to be judged
    against, how much of the crop the cloud test set aside, the tests they passed
    and the strongest signal in the crop."""

    fire_pixel_count: int = 0
    candidate_cell_count: int = 0
    unclassifiable_cell_count: int = 0
    cloud_fraction: float = 0.0
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

#: The three bands the cloud test reads. The MODIS contextual algorithm states that
#: test over its 0.65 um and 0.86 um reflectances and its 12 um brightness
#: temperature; on ABI those channels are bands 2, 3 and 15, so this derive asks the
#: layer for five bands rather than the two window bands alone.
_RED_BAND = 2
_NIR_BAND = 3
_DIRTY_LONGWAVE_BAND = 15
_BANDS_READ = (_RED_BAND, _NIR_BAND, _SHORTWAVE_BAND, _LONGWAVE_BAND,
               _DIRTY_LONGWAVE_BAND)

#: The daytime cloud test of the MODIS contextual fire algorithm (Giglio,
#: Descloitres, Justice and Kaufman 2003, Remote Sensing of Environment 87:273-282,
#: section 2.3; carried unchanged into Collection 6, Giglio, Schroeder and Justice
#: 2016, Remote Sensing of Environment 178:31-41): a cell is cloud where the sum of
#: its 0.65 um and 0.86 um reflectances exceeds 0.9, OR its 12 um brightness
#: temperature is below 265 K, OR that sum exceeds 0.7 while that temperature is
#: below 285 K. Cloud is neither fire nor background - it is warm in the shortwave
#: from reflected sunlight and cold in the longwave, so leaving it in a neighbourhood
#: drags the background down and makes ordinary sunlit ground stand out from it.
_CLOUD_REFLECTANCE_SUM = 0.9
_CLOUD_LONGWAVE_K = 265.0
_CLOUD_WARM_REFLECTANCE_SUM = 0.7
_CLOUD_WARM_LONGWAVE_K = 285.0

#: The published daytime potential-fire-pixel thresholds of the same algorithm, whose
#: 3.9 um and 11 um channels these two ABI bands stand in for: the shortwave
#: brightness temperature must exceed 310 K and the shortwave minus longwave
#: difference must exceed 10 K. Both are parameters because the ABI cell is coarser
#: than the cell those thresholds were published over, so a warm bare-ground scene
#: may need the shortwave floor raised.
_BT_SHORTWAVE_MIN_K = 310.0
_BT_SPLIT_MIN_K = 10.0

#: The contextual stage, which is what makes the pair of floors a detection rather
#: than a screen. Three published daytime conditions must hold at once against the
#: cloud-free background of the candidate's neighbourhood: the shortwave-minus-
#: longwave difference exceeds the background's by 3.5 of its mean absolute
#: deviations AND by 6 K outright, and the shortwave brightness temperature exceeds
#: the background's by ``background_sigma`` of them. Sunlit bare ground is hot across the
#: whole window at once, so it clears the floors and fails these; a flame is hot
#: where its neighbours are not.
#: The algorithm's fourth daytime condition - the longwave brightness temperature
#: within 4 K of one deviation above the background's - is NOT taken here. It was
#: written for a 1 km cell, where a fire lifts the whole cell's longwave; in a 2 km
#: ABI cell a sub-pixel flame does not, and terrain that is genuinely cool in the
#: longwave carries the fire below the floor. Measured over a confirmed fire it
#: rejects more than half the cells the other three keep, and over a scene with no
#: fire in it, it rejects nothing they had not already rejected.
_BACKGROUND_SIGMA = 3.0
_SPLIT_DEVIATIONS = 3.5
_SPLIT_MARGIN_K = 6.0

#: The neighbourhood a candidate is measured against: a square window this many cells
#: across, clipped at the crop edge, in which the valid, cloud-free, non-candidate
#: cells must be at least a quarter of what the window covers and at least six, the
#: published requirement for a background worth measuring. A candidate whose window
#: holds fewer - the interior of a scene that is candidate or cloud everywhere - has
#: no background to stand out from and is UNCLASSIFIABLE: this tool reports it as
#: neither burning nor clear. A window the crop edge clips is one-sided: a candidate
#: within the radius of that edge is measured only against what lies inward of it.
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


def _cloud_cells(red: Any, nir: Any, dirty_longwave: Any) -> Any:
    """The cells the published cloud test masks out.

    An unobserved reflectance counts as zero, which is what it physically is after
    dark and which collapses the expression onto the published night-time test - the
    12 um threshold alone."""
    import numpy as np

    reflectance = np.where(
        np.isfinite(red) & np.isfinite(nir), np.nan_to_num(red + nir), 0.0)
    return np.isfinite(dirty_longwave) & (
        (reflectance > _CLOUD_REFLECTANCE_SUM)
        | (dirty_longwave < _CLOUD_LONGWAVE_K)
        | ((reflectance > _CLOUD_WARM_REFLECTANCE_SUM)
           & (dirty_longwave < _CLOUD_WARM_LONGWAVE_K)))


def _background_stats(values: Any, background: Any, counts: Any,
                      radius: int) -> tuple[Any, Any]:
    """Per cell: the mean of the background cells in its window, and their mean
    absolute deviation about that mean. The deviation is the published statistic -
    the contextual thresholds are stated in mean absolute deviations, and over a
    background as skewed as warm ground the standard deviation is a different and
    stricter number. A window with no background cell yields zero for both, which
    only reaches a comparison the background-size test has already rejected."""
    import numpy as np

    safe = np.where(counts > 0, counts, 1.0)
    mean = _window_sums(np.where(background, values, 0.0), radius) / safe
    height, width = mean.shape
    padded = np.full((height + 2 * radius, width + 2 * radius), np.nan)
    padded[radius:radius + height, radius:radius + width] = np.where(
        background, values, np.nan)
    total = np.zeros_like(mean)
    span = 2 * radius + 1
    for row in range(span):
        for col in range(span):
            total += np.nan_to_num(
                np.abs(padded[row:row + height, col:col + width] - mean), nan=0.0)
    return mean, total / safe


def _stands_above(values: Any, background: Any, counts: Any, radius: int,
                  deviations: float, margin_k: float) -> Any:
    """Where a cell exceeds its background's mean by ``deviations`` of that
    background's mean absolute deviation plus ``margin_k`` kelvin."""
    mean, deviation = _background_stats(values, background, counts, radius)
    return values > mean + deviations * deviation + margin_k


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
    contextual tests against the cloud-free neighbourhood that tell a flame from hot
    ground.

    Use this on a layer of raw ABI bands carrying band 7 (the 3.9 um shortwave
    window), band 14 (the 11.2 um longwave window) and bands 2, 3 and 15 (0.64 um,
    0.86 um and 12.3 um), which the cloud test needs - fetch ``[2, 3, 7, 14, 15]``.
    A cell becomes a CANDIDATE when it is not cloud AND its shortwave brightness
    temperature is above an absolute floor AND its shortwave minus longwave
    difference is above a split floor: a sub-pixel flame saturates the shortwave
    window while the longwave window stays near the ambient surface. A candidate is
    a DETECTION only where it also stands out from the cloud-free, non-candidate
    cells of its neighbourhood - in that difference and in its shortwave brightness
    temperature at once. Sunlit bare ground is hot across the whole neighbourhood at
    once, so it clears the two floors and fails the contrast; a fire is hot where
    its neighbours are not.

    Params:
        layer: the raw ABI layer to read - the layer a raw ABI band fetch returned,
            or its uri. It must carry bands 2, 3, 7, 14 and 15; a layer without them
            is refused, naming the bands it does carry.
        bt_shortwave_min_k: the absolute 3.9 um brightness-temperature floor in
            kelvin. Default 310. Lower it to raise more candidates for the contrast
            tests; raise it to screen harder before they run.
        bt_split_min_k: the floor on the 3.9 um minus 11.2 um difference in kelvin.
            Default 10. Raise it to demand an unambiguous shortwave excess.
        background_sigma: how far above the shortwave brightness temperature of its
            neighbourhood a candidate must sit, in mean absolute deviations of the
            background cells. Default 3. Lower it (2) to catch weaker fires at the
            cost of false detections; raise it (4-5) over broken hot ground.

    Returns the detections as a point layer, one point at each detected cell centre
    carrying its two brightness temperatures and their difference, with the
    detection count, the candidate count, how many candidates had too little
    cloud-free background to be judged, the cloud fraction of the crop and the tests
    on the layer. A crop where nothing is burning is refused - naming the strongest
    signal it found, and whether the candidates failed the floors or the contrast -
    rather than returned as an empty layer, and a crop where no candidate has a
    background at all is refused as UNCLASSIFIABLE rather than as clear.
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
        "a count of mean absolute deviations", minimum=0.0)

    bands, grid = read_bands(layer, _BANDS_READ, on_error=_fail)
    shortwave = bands[_SHORTWAVE_BAND]
    longwave = bands[_LONGWAVE_BAND]
    valid = np.isfinite(shortwave) & np.isfinite(longwave)
    if not valid.any():
        raise ActiveFireError(
            "ACTIVE_FIRE_LAYER_UNREADABLE",
            "the two window bands carry no valid pixel anywhere in the crop, so "
            "there is nothing to test.")
    cloud = valid & _cloud_cells(
        bands[_RED_BAND], bands[_NIR_BAND], bands[_DIRTY_LONGWAVE_BAND])
    cloud_fraction = float(cloud.sum()) / float(valid.sum())
    split = np.where(valid, shortwave - longwave, np.float32(np.nan))
    candidate = valid & ~cloud & (shortwave >= np.float32(shortwave_min)) & (
        split >= np.float32(split_min))

    max_shortwave = float(np.nanmax(np.where(valid, shortwave, np.nan)))
    max_split = float(np.nanmax(split))
    if not candidate.any():
        raise ActiveFireError(
            "ACTIVE_FIRE_NO_DETECTIONS",
            f"no cloud-free cell passes both window tests at "
            f"bt_shortwave_min_k={shortwave_min:g} K and "
            f"bt_split_min_k={split_min:g} K: the hottest cell in the crop reaches "
            f"{max_shortwave:.1f} K and the largest shortwave-minus-longwave "
            f"difference is {max_split:.1f} K, and the cloud test covers "
            f"{cloud_fraction:.0%} of it. Nothing here is burning at that "
            "sensitivity.")

    radius = _BACKGROUND_WINDOW_CELLS // 2
    background = valid & ~candidate & ~cloud
    counts = _window_sums(background, radius)
    covered = _window_sums(np.ones_like(background, dtype=np.float64), radius)
    enough = (counts >= _BACKGROUND_MIN_CELLS) & (
        counts >= _BACKGROUND_MIN_FRACTION * covered)
    judged = candidate & enough
    flagged = (
        judged
        & _stands_above(split, background, counts, radius, _SPLIT_DEVIATIONS, 0.0)
        & _stands_above(split, background, counts, radius, 0.0, _SPLIT_MARGIN_K)
        & _stands_above(shortwave, background, counts, radius, sigma, 0.0))
    candidate_count = int(candidate.sum())
    unclassifiable_count = candidate_count - int(judged.sum())
    if not flagged.any():
        if not judged.any():
            raise ActiveFireError(
                "ACTIVE_FIRE_UNCLASSIFIABLE",
                f"{candidate_count} cells pass both window tests and not one of "
                f"them can be judged: every neighbourhood holds fewer than "
                f"{_BACKGROUND_MIN_CELLS} cloud-free non-candidate cells, or fewer "
                f"than a {_BACKGROUND_MIN_FRACTION:.0%} share of one, so there is "
                "no background to measure the contrast against. Whether anything "
                "here is burning is UNKNOWN - this crop is not evidence that it is "
                "clear. Widen the crop so ordinary ground enters each "
                "neighbourhood, or raise bt_shortwave_min_k so fewer cells are "
                "candidates.")
        raise ActiveFireError(
            "ACTIVE_FIRE_NO_DETECTIONS",
            f"{candidate_count} cells pass both window tests but none stands out "
            f"from the cloud-free background of its neighbourhood at "
            f"background_sigma={sigma:g}: the hottest cell reaches "
            f"{max_shortwave:.1f} K and the largest shortwave-minus-longwave "
            f"difference is {max_split:.1f} K, and the cells around them are just "
            "as hot. That is ground the sun has heated, not combustion."
            + (f" A further {unclassifiable_count} had too little cloud-free "
               "background to be judged at all." if unclassifiable_count else ""))

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
        "%g deviations over a cloud-free background (%d unclassifiable, cloud %.1f%%,"
        " max %.1f K, max split %.1f K)",
        len(features), candidate_count, shortwave_min, split_min, sigma,
        unclassifiable_count, 100.0 * cloud_fraction, max_shortwave, max_split)
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
        unclassifiable_cell_count=unclassifiable_count,
        cloud_fraction=round(cloud_fraction, 4),
        bt_shortwave_min_k=shortwave_min,
        bt_split_min_k=split_min,
        background_sigma=sigma,
        max_bt_shortwave_k=round(max_shortwave, 2),
        max_bt_split_k=round(max_split, 2),
        cell_size_deg=round(cell, 6))
