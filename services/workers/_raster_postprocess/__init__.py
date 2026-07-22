"""Shared, GPL-free NetCDF -> COG raster postprocess for the Batch workers.

This package is the worker-side LIFT of the pure ``numpy`` / ``scipy`` /
``rasterio`` / ``pyproj`` / ``xarray`` postprocess code that used to run on the
always-on agent box after a SFINCS Batch solve finished (job-0042
``postprocess_flood`` + sprint-17 ``postprocess_waves``). Moving it INTO the
Batch worker — which already has the raw ``sfincs_map.nc`` local, the geo stack
in-image, and a big c7i box to parallelize frames — is the scale-to-zero island
pattern: the agent collapses to "read a thin manifest, build the TiTiler URL,
register + persist".

Hard constraints honoured here (per the spike design
``reports/design/worker_side_postprocess_spike.md``):

  * GPL-FREE — never imports ``cht_sfincs`` (GPL-3.0). Face coordinates are read
    STRAIGHT off the NetCDF (``mesh2d_face_x``/``_y`` or computed from the node
    coords + ``mesh2d_face_nodes`` connectivity).
  * AGENT-IMPORT-FREE — never imports ``trid3nt_server.*`` (no
    ``..tools.solver._get_s3_client`` / ``..tools.cache.storage_scheme``). The
    upload helper takes a worker-local boto3 client / creds.
  * Pure + unit-testable — the modules below run against a synthetic NetCDF with
    no Batch, no S3, no cht.

Modules:
  * :mod:`cog`            orientation guards, face rasterize (scipy griddata),
                          COG+overviews encode (rasterio COG driver), CRS
                          round-trip verify.
  * :mod:`sfincs_reader`  open ``sfincs_map.nc`` (xarray), peak + per-timestep
                          depth/wave field extraction, frame subsample, the
                          empty-field honesty gate.
  * :mod:`band_stats`     precompute min/max/percentiles + categorical/RGBA
                          flags per COG so the agent skips reading the COG.
  * :mod:`upload`         worker-local boto3 ``put_object`` (no agent client).
  * :mod:`manifest`       the typed ``publish_manifest.json`` writer (plain dict).
  * :mod:`postprocess`    the orchestrator the worker entrypoints call: run the
                          shared postprocess on a LOCAL ``sfincs_map.nc``, encode
                          frames in parallel (bounded ProcessPool), upload to the
                          deterministic keys, and return the manifest dict +
                          honesty-gate status.
"""

from __future__ import annotations

from .manifest import MANIFEST_SCHEMA_VERSION, MANIFEST_FILENAME

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
]
