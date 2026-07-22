"""TRID3NT ELMFIRE wildfire-spread worker package (FIRE track).

FIRE-1 delivered the pinned container (``Dockerfile``, image
``trid3nt/elmfire:dev``, release 2025.0526) with tutorial 01 + verification
case 01 reproduced in-container.

FIRE-2 (this package's ``deck_builder``) delivers the deterministic input
deck builder: AOI + ignition(s) + scenario weather + input raster paths ->
a run-ready ELMFIRE case directory (all rasters warped onto ONE identical
EPSG:5070 30 m grid, constant weather rasters, adj/phi, a rendered
``elmfire.data`` namelist mirroring the tutorial-01 keys the proven
container consumed, and a ``deck_manifest.json`` with grid + checksums).

FIRE-3 (dispatch + composer) and FIRE-4 (ECR/Batch infra) build on this.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
