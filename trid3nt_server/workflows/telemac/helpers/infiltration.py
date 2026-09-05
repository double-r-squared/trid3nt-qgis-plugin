"""The infiltration surface a rain-on-grid run reads: per-node CN2 and Manning n.

The field the engine's own SCS-CN model reads out of ``FORMATTED DATA FILE 2``,
sampled from land cover at the ACCEPTED mesh's own nodes. A declaration summons
it; nothing here decides what question is being asked.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_server.workflows.runtime import Step
from trid3nt_server.workflows.mesh.shared.nodes import (
    node_slopes_from_mesh,
    sample_raster_at_nodes,
)
from trid3nt_server.workflows.shared.layer_fields import layer_field

from .catchment import mesh_nodes

logger = logging.getLogger("trid3nt_server.workflows.telemac.helpers.infiltration")

__all__ = ["Infiltration", "node_infiltration_fields"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"


async def node_infiltration_fields(*, mesh: dict[str, Any],
                                   landcover: Any,
                                   curve_number: float | None,
                                   steep_slope_correction: bool,
                                   antecedent_moisture: Any) -> dict[str, Any]:
    """Per-node CN2 + Manning n, sampled from land cover at the mesh nodes.

    A uniform ``curve_number`` overrides the CN field and ONLY the CN field:
    roughness is a separate physical property, so every node still takes its
    land-cover Manning n.

    ``steep_slope_correction`` applies the Huang (2006) rational correction to the
    CN field HERE, before the file is written, because the branch that would have
    done it inside the engine is compiled off in the installed 9.0.0 build. The
    slopes come from the mesh's own piecewise-linear bed - the discretization the
    solver sees - never from a finer raster the run does not resolve.

    The nodes are the ACCEPTED mesh's own, read off the artifact's display face:
    the field is written against the numbering the geometry file carries, so a
    curve number lands on the node it was sampled for.
    """
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.cn_infiltration import (
        amc_condition_for, landcover_cn_manning, node_curve_numbers,
    )

    def _sample() -> tuple[list[float], list[float], list[int]]:
        from trid3nt_server.tools.cache import read_object_bytes_s3

        points_utm, cells, bed, points_lonlat = mesh_nodes(mesh)
        rundir = Path(tempfile.mkdtemp(prefix="telemac-rog-cn-"))
        uri = str(layer_field(landcover, "uri"))
        local = rundir / "landcover.tif"
        local.write_bytes(read_object_bytes_s3(uri) if uri.startswith("s3://")
                          else Path(uri).read_bytes())
        codes = [int(round(v)) for v in
                 sample_raster_at_nodes(local, points_lonlat)]
        slopes = (list(node_slopes_from_mesh(points_utm, cells, bed))
                  if steep_slope_correction else None)
        manning = [landcover_cn_manning(c)[1] for c in codes]
        cn2 = node_curve_numbers(codes, uniform_cn=curve_number,
                                 slopes_m_per_m=slopes,
                                 steep_slope_correction=bool(steep_slope_correction))
        return cn2, manning, codes

    node_cn2, node_manning, codes = await asyncio.to_thread(_sample)
    amc = amc_condition_for(antecedent_moisture)
    logger.info("rog infiltration: %d nodes, %d land-cover classes, AMC=%d, "
                "uniform_cn=%s, steep_slope=%s", len(codes), len(set(codes)), amc,
                curve_number, bool(steep_slope_correction))
    return {"node_cn2": node_cn2, "node_manning": node_manning,
            "amc_condition": int(amc), "curve_number": curve_number,
            "steep_slope_correction": bool(steep_slope_correction),
            "landcover_classes": sorted(set(codes)),
            "note": str(layer_field(landcover, "fallback_note") or "")}


class Infiltration:
    """The catchment's node fields, as a declared step."""

    @staticmethod
    def fields(*, mesh: Any, landcover: Any, curve_number: Any,
               steep_slope_correction: Any, antecedent_moisture: Any) -> Step:
        """Per-node curve numbers and Manning n - the infiltration surface."""
        return Step(runner=f"{_HELPERS}.infiltration.node_infiltration_fields",
                    stage="prep",
                    kwargs={"mesh": mesh, "landcover": landcover,
                            "curve_number": curve_number,
                            "steep_slope_correction": steep_slope_correction,
                            "antecedent_moisture": antecedent_moisture})
