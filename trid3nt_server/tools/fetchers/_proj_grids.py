"""The offline vertical seam: NGVD 29, NAVD88 and EGM2008 heights converted per point on PROJ's grids.

A grid PROJ names for the ground a call stands over is downloaded WHOLE, once, into PROJ's
user-writable directory, and every later conversion reads it from disk. A point no grid covers
refuses by name: PROJ's ballpark steps answer such a point with a zero or with an unshifted
height, so they are switched off.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from ._fetch_common import FetchError
from ._router.transport import TransportError, get_bytes, get_client

logger = logging.getLogger(__name__)

__all__ = ["FRAMES", "DatumGridError", "DatumGridUnavailable", "NoGridCoverage",
           "geodetic_offset", "grids_used", "served_pairs"]

#: The frames the seam converts between, in the order the chain below runs them.
FRAMES = ("NGVD29", "NAVD88", "NAD83_2011_ELLIPSOID", "EGM2008")

#: The hops between neighbouring frames, each as the CRS pairs PROJ chains left to right.
#: NAD83(2011) -> WGS 84 (G1674) is a Helmert step PROJ's EGM2008 route replaces with a null
#: one, which leaves the answer a metre off, so the two ellipsoids are joined here by name.
_HOPS: tuple[tuple[tuple[str, str], ...], ...] = (
    (("EPSG:4269+7968", "EPSG:4269+5703"),),
    (("EPSG:6349", "EPSG:6319"),),
    (("EPSG:6319", "EPSG:9056"), ("EPSG:4979", "EPSG:9518")),
)


class DatumGridError(FetchError):
    """Base of the seam's refusals."""

    error_code = "DATUM_GRID_ERROR"
    retryable = False


class NoGridCoverage(DatumGridError):
    """No grid PROJ carries covers a point for this frame pair."""

    error_code = "DATUM_GRID_NO_COVERAGE"


class DatumGridUnavailable(DatumGridError):
    """A grid the point needs could not be fetched from the PROJ CDN."""

    error_code = "DATUM_GRID_UNAVAILABLE"


def geodetic_offset(from_frame: str, to_frame: str, lon: Any, lat: Any) -> Any:
    """Metres a zero on ``from_frame`` reads at on ``to_frame`` at each point, as a float or an array.

    ``lon`` and ``lat`` are degrees (scalars or equal-length arrays); a point no grid covers
    raises :class:`NoGridCoverage` naming the first such point, and a frame outside
    :data:`FRAMES` raises ``ValueError``."""
    import numpy as np

    xs = np.atleast_1d(np.asarray(lon, dtype=np.float64))
    ys = np.atleast_1d(np.asarray(lat, dtype=np.float64))
    height = np.zeros_like(xs)
    for index, forward in _route(from_frame, to_frame):
        height = _across(index, forward, xs, ys, height)
    if not np.all(np.isfinite(height)):
        miss = int(np.argmax(~np.isfinite(height)))
        raise NoGridCoverage(
            f"no PROJ grid covers {from_frame} -> {to_frame} at ({xs[miss]:.6f}, {ys[miss]:.6f}); "
            "the seam answers CONUS (GEOID18, VERTCON) and Alaska (GEOID12B) and refuses "
            "elsewhere rather than reading a zero offset.")
    return float(height[0]) if np.ndim(lon) == 0 and np.ndim(lat) == 0 else height


@lru_cache(maxsize=256)
def served_pairs(lon: float, lat: float) -> tuple[tuple[str, str], ...] | None:
    """The pairs of :data:`FRAMES`' geodetic set the grids convert between at this point, as ``(from, to)``.

    Message context for a refusal: a pair no grid covers is left out, and a
    grid the PROJ CDN could not give leaves the whole answer ``None``, so a
    reader never reads an outage as "nothing is served here"."""
    geodetic = (("NGVD29", "NAVD88"), ("NAVD88", "EGM2008"), ("NGVD29", "EGM2008"))
    served: list[tuple[str, str]] = []
    for pair in geodetic:
        try:
            geodetic_offset(*pair, lon, lat)
        except NoGridCoverage:
            continue
        except DatumGridUnavailable:
            return None
        served.append(pair)
    return tuple(served)


def grids_used(from_frame: str, to_frame: str, lon: float, lat: float) -> list[str]:
    """The grid file names PROJ reads for this pair at this point, in the order it applies them."""
    names: list[str] = []
    for index, _forward in _route(from_frame, to_frame):
        for pair in _HOPS[index]:
            best = _group(pair, (lon, lat, lon, lat)).transformers[0]
            for part in best.definition.split():
                if part.startswith("grids="):
                    names += [n for n in part[6:].split(",") if n not in names]
    return names


def _route(from_frame: str, to_frame: str) -> list[tuple[int, bool]]:
    """The hops from one frame to the other as ``(hop index, left to right)``."""
    source, target = FRAMES.index(_frame(from_frame)), FRAMES.index(_frame(to_frame))
    if source <= target:
        return [(i, True) for i in range(source, target)]
    return [(i, False) for i in range(source - 1, target - 1, -1)]


def _frame(name: str) -> str:
    frame = str(name).strip().upper()
    if frame not in FRAMES:
        raise ValueError(f"{name!r} is not a frame the seam converts: {list(FRAMES)}")
    return frame


def _across(index: int, forward: bool, xs: Any, ys: Any, height: Any) -> Any:
    """``height`` carried across hop ``index``, left to right when ``forward``."""
    import numpy as np
    from pyproj import Transformer
    from pyproj.exceptions import ProjError

    pairs = _HOPS[index] if forward else [(b, a) for a, b in reversed(_HOPS[index])]
    bounds = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
    for pair in pairs:
        _ensure(pair, bounds)
        try:
            mover = Transformer.from_crs(pair[0], pair[1], always_xy=True, allow_ballpark=False)
        except ProjError as exc:
            raise NoGridCoverage(
                f"PROJ carries no grid operation for {pair[0]} -> {pair[1]} over {bounds}") from exc
        if xs.size == 1:
            height = np.array([mover.transform(float(xs[0]), float(ys[0]), float(height[0]))[2]])
        else:
            _x, _y, height = mover.transform(xs, ys, height)
    return height


def _group(pair: tuple[str, str], bounds: tuple[float, float, float, float]) -> Any:
    from pyproj.aoi import AreaOfInterest
    from pyproj.transformer import TransformerGroup

    return TransformerGroup(pair[0], pair[1], always_xy=True, allow_ballpark=False,
                            area_of_interest=AreaOfInterest(*bounds))


def _ensure(pair: tuple[str, str], bounds: tuple[float, float, float, float]) -> None:
    """Download, once, the grids of the best operation PROJ lacks for this pair over ``bounds``."""
    for _ in range(len(_HOPS) * 4):
        group = _group(pair, bounds)
        if group.best_available or not group.unavailable_operations:
            return
        missing = [g for g in group.unavailable_operations[0].grids if not g.available]
        if not missing:
            return
        for grid in missing:
            _download(grid.short_name, grid.url)


def _download(name: str, url: str) -> None:
    import os
    from pathlib import Path

    from pyproj.datadir import get_user_data_dir

    if not url or not url.startswith("https://"):
        raise DatumGridUnavailable(f"PROJ names no download for the grid {name!r}")
    target = Path(get_user_data_dir(True)) / name
    logger.info("proj grid: fetching %s once into %s", url, target.parent)
    try:
        body, _type, _final = get_bytes(get_client(), url)
    except TransportError as exc:
        raise DatumGridUnavailable(
            f"the PROJ grid {name} could not be fetched from {url}: {exc}") from exc
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(body)
    os.replace(partial, target)
    # PROJ remembers a grid it failed to find; resetting the search path makes it look again.
    from pyproj.datadir import get_data_dir, set_data_dir

    set_data_dir(get_data_dir())
