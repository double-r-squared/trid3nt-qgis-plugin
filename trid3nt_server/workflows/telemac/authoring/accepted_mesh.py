"""What ANY accepted mesh hands the author: the files it was staged under, the
nodes and the bed it carries, the channel a boundary face cuts and whether its
outline is closed.

Every number is read from the mesh the acceptance produced, never beside it: a
mesh carrying none of it refuses by name rather than opening on a value nobody
measured."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from trid3nt_server.workflows.mesh.shared.nodes import read_accepted_mesh_nodes
from trid3nt_server.workflows.mesh.topology import read_topology
from ..errors import TelemacError

__all__ = ["BASIN_BOUNDARY", "BASIN_GEOMETRY", "HARBOUR_GEOMETRY",
           "boundary_uri", "face_section", "geometry_uri", "mesh_facts",
           "mesh_field", "mesh_missing", "mesh_nodes", "refuse_a_sealed_domain",
           "slug_of", "to_utm", "topology_of"]

BASIN_GEOMETRY = "basin.slf"


BASIN_BOUNDARY = "basin.cli"


#: The names the run directory holds an open-water domain's staged geometry
#: under - the decks' own GEOMETRY / BOUNDARY CONDITIONS statements. A harbour
#: stages the geometry alone: its boundary file is RESTAMPED as the incident wave
#: and is written beside the deck rather than staged from the mesh.
HARBOUR_GEOMETRY = "harbour.slf"


def mesh_field(mesh: Mapping[str, Any], name: str, *,
                missing: Callable[[str], Exception]) -> str:
    """One field of the ACCEPTED mesh's record, or the refusal that names it.

    Falling through would solve on a mesh nobody accepted, under its name."""
    uri = (mesh or {}).get(name)
    if not uri:
        raise missing(
            f"the mesh for this run carries no {name}, so the accepted mesh "
            f"cannot be staged (mesh record: {sorted((mesh or {}))}).")
    return str(uri)


def mesh_missing(message: str) -> Exception:
    return TelemacError(message, error_code="TELEMAC_MESH_NOT_ACCEPTED")


def mesh_nodes(mesh: Mapping[str, Any]) -> tuple[Any, Any]:
    """The accepted mesh's node coordinates and bed, read off its display face.

    The ``.2dm`` is the one readable record of the geometry file's numbering."""
    points, _cells, z, _lonlat = read_accepted_mesh_nodes(
        mesh_field(mesh, "display_uri", missing=mesh_missing))
    if points is None or z is None:
        raise TelemacError(
            "the accepted mesh's display face carries no nodes or no painted bed, "
            "so this run has no ground to measure an outflow stage over and "
            "nowhere to settle a source; a reach mesh recipe paints its bed with "
            "set_bed.", error_code="TELEMAC_MESH_BED_UNMEASURED")
    return points, z


def face_section(nodes: Sequence[int], node_xy: Any, bed: Any, *,
                  missing: Callable[[str], Exception]) -> list[list[float]]:
    """The channel a role's face cuts, as ``(offset, bed)`` pairs.

    A role is a contiguous run of the walk, so the offset is running chord."""
    import numpy as np

    xy = None if node_xy is None else np.asarray(node_xy, dtype=float)
    if xy is None or len(nodes) < 2 or max(nodes) >= xy.shape[0]:
        raise missing(
            f"a uniform-flow depth is derived over the channel the face cuts, "
            f"and that face names {len(nodes)} node(s) against "
            f"{0 if xy is None else xy.shape[0]} mesh coordinates; a mesh recipe "
            "names its faces with set_boundary_roles.")
    points = xy[list(nodes)]
    steps = np.hypot(*(points[1:] - points[:-1]).T)
    offsets = np.concatenate([[0.0], np.cumsum(steps)])
    # A node the bed left unpainted drops out of the section and takes no offset
    # with it: the survey has a hole in it, and closing the hole by shifting the
    # nodes past it would narrow a channel nobody re-measured.
    section = [[round(float(o), 3), round(float(z), 3)]
               for o, z in zip(offsets, bed[list(nodes)]) if np.isfinite(z)]
    if len(section) < 2:
        raise missing(f"the face carries {len(section)} painted node(s), which is "
                      "no section to derive a normal depth over.")
    return section


def to_utm(source: Any, utm_epsg: int) -> Any:
    """A lon/lat geometry source -> its shapely geometry in the mesh's metres."""
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform, unary_union

    from trid3nt_server.inputs.geometry import (
        flatten_geometries, read_geometry_doc,
    )

    geometry = unary_union([_shape(g)
                            for g in flatten_geometries(read_geometry_doc(source))])
    tr = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    return _transform(tr.transform, geometry)


def slug_of(name: str) -> str:
    """A name as the run prefix and the layer titles spell it."""
    return "".join(c if c.isalnum() else "_" for c in str(name).lower()).strip("_")


def mesh_facts(mesh: Mapping[str, Any], *,
                missing: Callable[[str], Exception]) -> dict[str, Any]:
    """The accepted mesh's own record, as every open-water sheet reads it.

    One reader: a harbour and a basin differ in what they DO with the mesh."""
    artifact = mesh.get("artifact")
    utm_epsg = int(getattr(artifact, "utm_epsg", 0) or 0)
    if not utm_epsg:
        raise missing(
            "the accepted mesh names no projected zone, so nothing solved on it "
            "can be georeferenced.")
    probes = dict(getattr(artifact, "probes", None) or {})
    edges = dict(probes.get("edge_length_m") or {})
    provenance = dict(mesh.get("provenance") or {})
    name = str(getattr(artifact, "name", None) or "domain")
    return {
        "mesh_name": name,
        "mesh_id": mesh.get("mesh_id"),
        "utm_epsg": utm_epsg,
        "mesh_node_count": int(mesh.get("node_count") or 0),
        "mesh_element_count": int(mesh.get("element_count") or 0),
        "mesh_size_m": float(edges.get("median") or mesh.get("min_edge_m") or 0.0),
        "mesh_edge_min_m": float(edges.get("min") or 0.0),
        "mesh_edge_max_m": float(edges.get("max") or 0.0),
        "bed_source": str(provenance.get("bed_source") or "staged"),
        "lonlat_bounds": [float(v)
                          for v in (getattr(artifact, "bbox", None) or ())],
    }


def refuse_a_sealed_domain(topology: Mapping[str, Any], *, deck: str,
                           open_depth_threshold_m: float) -> None:
    """A prescribed sea state needs an edge to enter the domain through.

    Every deck that imposes one states the depth a boundary stretch has to reach
    for it to be designated open; a mesh where none does is sealed, and the
    solver runs it green with every wave answer zero."""
    if topology["roles"].get("open"):
        return
    raise TelemacError(
        f"{deck}: the accepted mesh designates no liquid boundary, so a "
        f"prescribed sea state has no edge to enter the domain through - no "
        f"boundary stretch reaches {open_depth_threshold_m:g} m, the depth this "
        f"deck states an open edge at, and {topology['states']}. Ask over a "
        f"window whose seaward edge reaches that depth, or state "
        f"open_depth_threshold_m at a depth this window does reach.",
        error_code="TELEMAC_MESH_CLOSED")


def geometry_uri(mesh: Mapping[str, Any], *,
                 missing: Callable[[str], Exception]) -> str:
    """Where the accepted mesh's own geometry stands, for the box to be given."""
    return mesh_field(mesh, "slf_uri", missing=missing)


def boundary_uri(mesh: Mapping[str, Any], *,
                 missing: Callable[[str], Exception]) -> str:
    """Where the accepted mesh's own boundary file stands."""
    return mesh_field(mesh, "cli_uri", missing=missing)


def topology_of(mesh: Mapping[str, Any], *,
                missing: Callable[[str], Exception]) -> Mapping[str, Any]:
    """The walk the acceptance wrote over this mesh's boundary.

    Every stage that asks which face carries what asks it here, so the bundle is
    read under ONE name rather than by each stage spelling the record's key."""
    return read_topology(mesh_field(mesh, "topology_uri", missing=missing))
