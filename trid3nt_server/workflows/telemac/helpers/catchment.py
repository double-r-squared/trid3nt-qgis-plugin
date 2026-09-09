"""The catchment a storm is solved over: its outlet, its window, its nodes.

A rain-on-grid domain is a DELINEATED CATCHMENT, the terrain that drains to one
point. What lives here is what everything downstream reads off that domain: the
analysis window around the outlet, and the accepted mesh's own nodes."""

from __future__ import annotations

from typing import Any, Mapping

from trid3nt_server.workflows.runtime import Step
from trid3nt_server.workflows.shared.aoi import aoi_slug

from .errors import RainOnGridError

__all__ = ["AcquireCatchment", "acquire_catchment", "catchment_aoi", "mesh_nodes"]

_HELPERS = "trid3nt_server.workflows.telemac.helpers"


# Centred on the outlet rather than on a geocoded place: a place bbox names a TOWN
# and need not contain the UPSTREAM catchment.
def catchment_aoi(pour_point: tuple[float, float],
                  half_deg: float) -> tuple[float, float, float, float]:
    """The analysis AOI a catchment is delineated inside, centred on its OUTLET.

    The delineation truncates at the box edge, so this must OVER-cover."""
    lon, lat = float(pour_point[0]), float(pour_point[1])
    b = float(half_deg)
    return (max(lon - b, -180.0), max(lat - b, -90.0),
            min(lon + b, 180.0), min(lat + b, 90.0))


async def acquire_catchment(*, location: str | None, bbox: Any,
                            pour_point: Any, half_deg: float,
                            default_name: str = "watershed",
                            code_prefix: str = "TELEMAC_ROG") -> dict[str, Any]:
    """Resolve the outlet and the AOI the catchment is delineated INSIDE.

    The pour point comes first and the AOI derives from it; a ``bbox`` still wins."""
    from trid3nt_server.workflows.runtime import user_input

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

    Read off the display face, the one readable record of the file's numbering."""
    from trid3nt_server.workflows.mesh.shared.nodes import read_accepted_mesh_nodes

    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    uri = str(mesh.get("display_uri") or "")
    if not uri or not utm_epsg:
        raise RainOnGridError(
            "the accepted mesh carries no display face or no projected zone, so "
            "its nodes cannot be read; the catchment mesh ask builds both.",
            error_code="TELEMAC_ROG_MESH_NOT_ACCEPTED")
    return read_accepted_mesh_nodes(uri, utm_epsg=utm_epsg)
