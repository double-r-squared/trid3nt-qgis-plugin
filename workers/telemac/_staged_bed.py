"""Read the BED the run directory was staged with, at this build's node points.

The four open-water builders each carried their own copy of an HTTP fetch against
the NOAA NCEI mosaic. The bed now arrives as a staged file, so what is left is the
part that was ever the worker's business: sample the raster at the nodes it is
meshing. Off-coverage, nodata and sentinel values all read as NaN, which is what
every caller's dry/land treatment already expects.
"""

from __future__ import annotations

import os
from typing import Any

#: Basename the server's manifest stages the bed raster under.
STAGED_BED_FILENAME: str = "bed_source.tif"

#: Below this the value is a fill sentinel rather than a depth: no real bed on
#: Earth is 10 km below its datum.
_SENTINEL_FLOOR_M: float = -1.0e4


def staged_bed_path(data_dir: str) -> str:
    """Where the staged bed raster is, whether or not it arrived."""
    return os.path.join(data_dir, STAGED_BED_FILENAME)


def sample_staged_bed(lon: Any, lat: Any, data_dir: str) -> Any:
    """Bed elevation (m, positive up) at each ``lon``/``lat``, NaN off coverage.

    Raises ``FileNotFoundError`` when nothing was staged. A builder that meshes
    real geography turns that into its own typed bathy-unavailable error, because
    "the bed is missing" and "the bed has a hole here" are different answers and
    only the second one is a NaN.
    """
    import numpy as np
    import rasterio

    path = staged_bed_path(data_dir)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"no staged bed raster at {path}; the run directory was not staged "
            "with the bathymetry this domain is solved on.")
    pts = np.column_stack([np.asarray(lon, dtype=float).ravel(),
                           np.asarray(lat, dtype=float).ravel()])
    with rasterio.open(path) as src:
        samp = np.array(list(src.sample(pts)), dtype=float).ravel()
        nod = src.nodata
    if nod is not None:
        samp[samp == nod] = np.nan
    samp[~np.isfinite(samp)] = np.nan
    samp[samp < _SENTINEL_FLOOR_M] = np.nan
    return samp
