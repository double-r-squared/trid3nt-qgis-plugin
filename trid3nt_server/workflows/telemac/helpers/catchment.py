"""The catchment a storm is solved over: its outlet, its window, its nodes.

A rain-on-grid domain is a DELINEATED CATCHMENT - the terrain that drains to one
point - and the delineation is a chained tool while the triangulation is the one
mesh step. What lives here is the pair of facts everything downstream reads off
that domain: where the analysis window around the outlet is, and what the
accepted mesh's own nodes are.
"""

from __future__ import annotations

from typing import Any, Mapping

from trid3nt_server.workflows.lib import Step
from trid3nt_server.workflows.shared.aoi import aoi_slug

from .errors import RainOnGridError

__all__ = ["AcquireCatchment", "acquire_catchment", "catchment_aoi", "mesh_nodes"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"


def catchment_aoi(pour_point: tuple[float, float],
                  half_deg: float) -> tuple[float, float, float, float]:
    """The analysis AOI a catchment is delineated inside, centred on its OUTLET.

    Centred on the outlet rather than on a geocoded place, because a place bbox
    names a TOWN and need not contain the UPSTREAM catchment. The delineation
    truncates at the box edge, so this must OVER-cover.
    """
    lon, lat = float(pour_point[0]), float(pour_point[1])
    b = float(half_deg)
    return (max(lon - b, -180.0), max(lat - b, -90.0),
            min(lon + b, 180.0), min(lat + b, 90.0))


async def acquire_catchment(*, location: str | None, bbox: Any,
                            pour_point: Any, half_deg: float,
                            default_name: str = "watershed",
                            code_prefix: str = "TELEMAC_ROG") -> dict[str, Any]:
    """Resolve the outlet and the AOI the catchment is delineated INSIDE.

    Order matters and is the opposite of every other domain here: the POUR POINT
    comes first and the AOI is derived FROM it, because a geocoded place bbox
    names a town and need not contain the upstream catchment. The live bug was
    'Otto, NC' clipping the Coweeta basin mid-hillslope into a 20-cell sliver.

    An explicit ``bbox`` still wins - it is the user's own extent, and squaring a
    different one off around the outlet would model a domain nobody asked for. A
    ``location`` names the run and nothing else here; the catchment's shape is the
    terrain's answer, not the geocoder's.
    """
    from trid3nt_server.workflows.lib import user_input

    point = user_input.lonlat_point(pour_point, label="pour_point",
                                    code=f"{code_prefix}_PARAMS_INVALID")
    if point is None:
        # Unreachable through the plan (the draw gate refuses first), and stated
        # anyway: an outlet decides the entire catchment, so a missing one is a
        # refusal rather than a centroid nobody chose.
        raise RainOnGridError(
            "the catchment has no pour point, and an outlet is never invented: "
            "the point decides which basin is modelled at all.",
            error_code=f"{code_prefix}_PARAMS_INCOMPLETE")

    extent = (tuple(float(v) for v in bbox) if bbox is not None
              else catchment_aoi(point, half_deg))
    name = str(location).strip() if (location and str(location).strip()) \
        else default_name
    return {"bbox": extent, "name": name,
            "slug": aoi_slug(name, default=default_name),
            "pour_point": [point[0], point[1]],
            "aoi_basis": "user bbox" if bbox is not None else
                         f"a +-{float(half_deg):g} deg buffer around the outlet"}


def AcquireCatchment(*, location: Any, bbox: Any, pour_point: Any,  # noqa: N802
                     half_deg: float, default_name: str = "watershed",
                     code_prefix: str = "TELEMAC_ROG") -> Step:
    """Outlet + AOI -> the modelled world. Refines the domain for everything after."""
    return Step(runner=f"{_HELPERS}.catchment.acquire_catchment", stage="acquire",
                kwargs={"location": location, "bbox": bbox, "pour_point": pour_point,
                        "half_deg": half_deg, "default_name": default_name,
                        "code_prefix": code_prefix}).overrides_domain()


def mesh_nodes(mesh: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    """The accepted catchment mesh's nodes, or the refusal that names what is missing.

    Read off the artifact's display face, which is the one readable record of the
    node numbering the geometry file carries: a curve number sampled here lands on
    the node the solve holds it at.
    """
    from trid3nt_server.workflows.mesh.shared.nodes import read_accepted_mesh_nodes

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    uri = str(mesh.get("display_uri") or "")
    if not uri or not utm_epsg:
        raise RainOnGridError(
            "the accepted mesh carries no display face or no projected zone, so "
            "its nodes cannot be read; the catchment mesh ask builds both.",
            error_code="TELEMAC_ROG_MESH_NOT_ACCEPTED")
    return read_accepted_mesh_nodes(uri, utm_epsg=utm_epsg)
