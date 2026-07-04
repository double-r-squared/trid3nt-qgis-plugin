"""Shared, agent-import-free MODFLOW postprocess runners (Batch worker).

The worker-side LIFT of the per-archetype rasterize/vectorize that used to run
on the always-on agent box after a MODFLOW solve. Moving it INTO the
``grace2-modflow`` Batch worker (which already has the raw outputs local, the
flopy/numpy/rasterio stack in-image, and a tear-down Spot box) is the
scale-to-zero island pattern (mirrors the SFINCS ``_raster_postprocess`` split):
the agent collapses to "read a thin manifest, publish the COG, register the layer".

Hard constraints (mirroring ``_raster_postprocess``):

  * AGENT-IMPORT-FREE -- never imports ``grace2_agent.*``. The COG write is
    rasterio-only; the manifest reuses the engine-agnostic
    ``services.workers._raster_postprocess`` helpers.
  * Pure + unit-testable -- runs against synthetic deck outputs with no Batch/S3.

Modules:
  * :mod:`postprocess`  all postprocess runners:
      - run_plume_postprocess            (spill/contamination path)
      - run_drawdown_postprocess         (sustainable_yield archetype)
      - run_mounding_postprocess         (MAR archetype)
      - run_dewatering_postprocess       (mine_dewatering archetype)
      - run_budget_partition_postprocess (regional_water_budget archetype)
      - run_asr_postprocess              (ASR archetype)
      - run_wetland_hydroperiod_postprocess (wetland_hydroperiod archetype)
"""

from __future__ import annotations

from .postprocess import (
    _ARCHETYPE_POSTPROCESS_RUNNERS,
    GWF_CBC_FILENAME,
    GWF_HDS_FILENAME,
    GWT_UCN_FILENAME,
    PLUME_DETECTION_FLOOR_MGL,
    PLUME_STYLE_PRESET,
    ModflowPostprocessResult,
    run_asr_postprocess,
    run_budget_partition_postprocess,
    run_dewatering_postprocess,
    run_drawdown_postprocess,
    run_mounding_postprocess,
    run_plume_postprocess,
    run_wetland_hydroperiod_postprocess,
)

__all__ = [
    "_ARCHETYPE_POSTPROCESS_RUNNERS",
    "GWF_CBC_FILENAME",
    "GWF_HDS_FILENAME",
    "GWT_UCN_FILENAME",
    "PLUME_DETECTION_FLOOR_MGL",
    "PLUME_STYLE_PRESET",
    "ModflowPostprocessResult",
    "run_asr_postprocess",
    "run_budget_partition_postprocess",
    "run_dewatering_postprocess",
    "run_drawdown_postprocess",
    "run_mounding_postprocess",
    "run_plume_postprocess",
    "run_wetland_hydroperiod_postprocess",
]
