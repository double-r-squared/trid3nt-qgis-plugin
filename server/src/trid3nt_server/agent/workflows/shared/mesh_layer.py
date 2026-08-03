"""Compatibility re-export -- the mesh-preview layer constructors now live in the
mesh authoring layer at :mod:`trid3nt_server.agent.mesh.mesh_preview`.

New code imports from the mesh layer directly. This shim keeps the existing
``trid3nt_server.agent.workflows.shared.mesh_layer`` import path working for the
current consumers (model_urban_flood_swmm / openquake model) and is scheduled
for deletion once M2 finishes re-pointing them (see docs/DELETION_LEDGER.md).

It mirrors the FULL module namespace (public + private helpers) so callers that
reach private symbols via this module path keep resolving.
"""

from __future__ import annotations

from trid3nt_server.agent.mesh import mesh_preview as _mesh_preview

globals().update(
    {k: v for k, v in vars(_mesh_preview).items() if not k.startswith("__")}
)
