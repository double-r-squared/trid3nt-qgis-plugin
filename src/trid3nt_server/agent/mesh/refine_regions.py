"""Per-region mesh sizing from drawn ``refine_region`` polygons.

The mesh authoring layer's GENERATE-stage refinement input. A drawn
``refine_region`` polygon carries an optional ``target_size_m`` (finer than the
base mesh); this module turns the parsed regions
(:attr:`~trid3nt_server.agent.mesh.spatial_roles.DrawnRoles.refine_regions`)
into ONE data-only :class:`MeshSizingSpec` the paradigm-native generators apply:

  * MODFLOW DISV / gridgen (worker-side ``services/workers/modflow/disv_grid``):
    each region -> a gridgen ``add_refinement_features(polygon, "polygon",
    level)`` at the quadtree ``refine_level`` computed here.
  * TELEMAC gmsh (worker-side): the target sizes computed here ride through as a
    sizing spec the worker's mesh-size field consumes (data-only pass-through --
    the server never runs gmsh).

Server/worker split: this component runs SERVER-side (pure stdlib math, no gmsh,
no gridgen binary) and emits the SPEC; each worker applies it behind the Docker
COPY-services/workers-only boundary. Refinement level maps a target cell size to
a quadtree subdivision depth: level ``L`` halves the base cell ``L`` times, so
``target ~= base / 2**L`` -> ``L = round(log2(base / target))`` clamped to
``[0, max_level]``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RegionSizing",
    "MeshSizingSpec",
    "refine_level_for",
    "mesh_sizing_from_refine_regions",
]


def refine_level_for(
    base_size_m: float, target_size_m: float, *, max_level: int = 5
) -> int:
    """Quadtree refinement depth taking a base cell to ``target_size_m`` or finer.

    ``level L`` halves the base cell ``L`` times (``target ~= base / 2**L``). We
    take ``ceil(log2(base/target))`` so the refined cell is AT LEAST as fine as
    requested (a coarser target than base -> level 0), clamped to
    ``[0, max_level]`` (the gridgen practical ceiling; a deeper level explodes
    the cell count).
    """
    if not (base_size_m > 0) or not math.isfinite(base_size_m):
        raise ValueError(f"base_size_m must be positive/finite, got {base_size_m!r}")
    if not (target_size_m > 0) or not math.isfinite(target_size_m):
        raise ValueError(f"target_size_m must be positive/finite, got {target_size_m!r}")
    if target_size_m >= base_size_m:
        return 0
    level = math.ceil(math.log2(base_size_m / target_size_m))
    return max(0, min(int(max_level), int(level)))


@dataclass(frozen=True)
class RegionSizing:
    """One refinement region resolved to a target size + a quadtree level.

    Fields:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` of the region.
        polygon: the raw GeoJSON polygon Feature (for gridgen/gmsh geometry).
        target_size_m: the resolved finer cell size for this region.
        refine_level: the quadtree subdivision depth (gridgen) reaching it.
    """

    bbox: tuple[float, float, float, float]
    polygon: dict[str, Any]
    target_size_m: float
    refine_level: int


@dataclass(frozen=True)
class MeshSizingSpec:
    """A base mesh size + per-region refinements (data-only, worker-applied).

    ``regions`` is empty when nothing was drawn -> the generator runs at the
    uniform ``base_size_m`` (no behavior change). ``max_refine_level`` is the
    deepest level across all regions (a convenience for the generator's gridgen
    call).
    """

    base_size_m: float
    regions: list[RegionSizing] = field(default_factory=list)

    @property
    def max_refine_level(self) -> int:
        return max((r.refine_level for r in self.regions), default=0)

    @property
    def is_uniform(self) -> bool:
        return not self.regions


def mesh_sizing_from_refine_regions(
    refine_regions: list[dict[str, Any]],
    base_size_m: float,
    *,
    default_target_size_m: float | None = None,
    max_level: int = 5,
) -> MeshSizingSpec:
    """Build a :class:`MeshSizingSpec` from parsed ``refine_region`` entries.

    Args:
        refine_regions: the ``DrawnRoles.refine_regions`` list -- each entry is
            ``{"polygon", "target_size_m": float|None, "bbox": (..4..)}``.
        base_size_m: the uniform base mesh cell size (metres).
        default_target_size_m: the target applied to a region that carried no
            explicit ``target_size_m``. When ``None`` we default to half the
            base cell (one refinement level) so a bare drawn region still
            refines visibly.
        max_level: quadtree depth ceiling per region.

    Returns:
        A :class:`MeshSizingSpec`. Regions whose resolved target is coarser than
        or equal to the base collapse to ``refine_level == 0`` (kept, so the
        generator still records the drawn intent, but no subdivision happens).
    """
    if not (base_size_m > 0) or not math.isfinite(base_size_m):
        raise ValueError(f"base_size_m must be positive/finite, got {base_size_m!r}")

    fallback = (
        float(default_target_size_m)
        if default_target_size_m is not None
        else base_size_m / 2.0
    )

    regions: list[RegionSizing] = []
    for entry in refine_regions:
        target = entry.get("target_size_m")
        target_size_m = float(target) if target is not None else fallback
        level = refine_level_for(base_size_m, target_size_m, max_level=max_level)
        regions.append(
            RegionSizing(
                bbox=tuple(entry["bbox"]),  # type: ignore[arg-type]
                polygon=entry["polygon"],
                target_size_m=target_size_m,
                refine_level=level,
            )
        )
    return MeshSizingSpec(base_size_m=float(base_size_m), regions=regions)
