"""Compatibility re-export -- the SWMM raster-cell mesh builder now lives in the
mesh authoring layer at :mod:`trid3nt_server.agent.mesh.raster_cell_mesh`.

New code imports from the mesh layer directly. This shim keeps the existing
``trid3nt_server.agent.workflows.swmm.swmm_mesh_builder`` import path working for
the current consumers (run_swmm / postprocess / solver / tests) and is scheduled
for deletion once M2 finishes re-pointing them (see docs/DELETION_LEDGER.md).

It mirrors the FULL module namespace (public + private helpers) so callers and
tests that reach private symbols (``_read_and_resample_dem``, ``_cell_node`` ...)
via this module path keep resolving.
"""

from __future__ import annotations

from trid3nt_server.agent.mesh import raster_cell_mesh as _raster_cell_mesh

globals().update(
    {k: v for k, v in vars(_raster_cell_mesh).items() if not k.startswith("__")}
)
