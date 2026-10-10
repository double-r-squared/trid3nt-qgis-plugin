"""The SHARED op primitives: what we impose on a mesh that no library does.

This module IS a namespace - it rides along for every mesher, and an op names
one of these by its real ``def`` name. Both take the data class they are DEFINED
OVER, and neither carries a branch that compensates for the wrong one."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.tools.mesh.inputs import op_geometry, op_raster, raster_cell_m
from trid3nt_server.tools.mesh.meshers import (
    Mesh,
    MeshToolError,
    fetch_notes,
)

__all__ = ["set_bed", "set_boundary_roles"]

#: How the sampled surface is read between the raster's cell centres. ``nearest``
#: is the labeled default: it returns a value the grid actually holds, which is
#: what a bed has to be when the raster is already finer than the elements.
_INTERPOLATIONS = ("nearest", "bilinear")

#: The one conditioning a bed source is put through: the watershed delineator's pit/depression/flat chain. A basin delineated on a
#: filled surface over a raw-sink bed ponds in pits the routing does not believe in.
_PIT_FILL = "pit_fill"

#: How far past the mesh's own extent the bed is fetched, as a fraction of each
#: span. The mesh has nodes ON the extent's corners and a raster's rim rows carry
#: the warp's fill, so the grid has to reach past where the domain ends.
_BED_MARGIN_FRAC = 0.02
def set_bed(mesh: Mesh, source: Any, interp: str = "nearest",
            condition: str | None = None) -> Mesh:
    """Paint every node's elevation from the bed slot -> the mesh, bedded.

    ``source`` is ONE surface (fetcher name, object-store uri, raster layer, or a depth in metres below the free surface);
    a bed of several is composed BEFORE here by merge_rasters and fill_nodata. A node the surface has nothing for refuses."""
    import numpy as np

    if str(interp) not in _INTERPOLATIONS:
        raise MeshToolError(
            "MESH_OP_BAD_VALUE",
            f"set_bed reads a raster {list(_INTERPOLATIONS)}, not {interp!r}.")
    lonlat = _lonlat_nodes(mesh)
    box = _grown(_extent(lonlat))
    values, provenance, notes = _painted(source, lonlat, box, interp, condition,
                                         mesh.meta.get("resolution_m"))
    _reached(source, values, lonlat, provenance)
    painted = f"{provenance} at {values.size} nodes"
    _journal_bed(source, provenance, lonlat, values.size)
    return _with_meta(
        dataclasses.replace(mesh, bed=np.asarray(values, dtype=float)),
        bed_source=painted,
        bed_sources=[provenance],
        bed_notes=notes or None,
        # A bed STATED as a depth is counted from the free surface, so the mesh knows the elevation that surface stands at and a run needs no gauge.
        # A bed measured on a datum states nothing about the water.
        **({"free_surface_m": 0.0} if _stated_depth(source) else {}),
        synthetic_inputs=[
            *(mesh.meta.get("synthetic_inputs") or []),
            {"param": "mesh_bed", "value": painted, "basis": "fetched",
             "consequence": "physics", "real_source_if_any": painted,
             "note": "the elevation every node carries; a solver reads it as the "
                     "domain's bathymetry"}])


def _reached(source: Any, values: Any, lonlat: Any, provenance: str) -> None:
    """REFUSE a mesh with a node no value reached, naming how many and where."""
    import numpy as np

    blank = ~np.isfinite(np.asarray(values, dtype=float))
    if not int(blank.sum()):
        return
    at = np.asarray(lonlat, dtype=float)[blank]
    raise MeshToolError(
        "MESH_BED_UNPAINTED",
        f"{int(blank.sum())} of {np.asarray(values).size} nodes have no "
        f"elevation from {provenance}: they stand between "
        f"({at[:, 0].min():.5f}, {at[:, 1].min():.5f}) and "
        f"({at[:, 0].max():.5f}, {at[:, 1].max():.5f}), on cells no row of this "
        f"bed measured. {_feedback(source)} Name a surface that covers them, "
        "compose one with merge_rasters and fill_nodata and hand its layer to "
        "the bed, or state the depth this water body holds.")


def _feedback(source: Any) -> str:
    """The coverage feedback the bed surface STATES about itself, or ""."""
    from trid3nt_server.inputs.bed import bed as read_bed

    slot = read_bed(source, label="bed")
    return " ".join(str(note) for note in
                    getattr(getattr(slot, "source", None), "notes", None) or [])


def _journal_bed(source: Any, provenance: str, lonlat: Any, nodes: int) -> None:
    """What the run SAYS about the bed its nodes were painted from."""
    from trid3nt_server.workflows.runtime import journal_note

    shares = _node_shares(source, lonlat)
    journal_note(f"bed: {provenance} painted all {nodes} nodes."
                 if shares is None else
                 f"bed: {provenance} painted {nodes} nodes - "
                 + "; ".join(f"{label} reached {share * 100.0:.1f}% of them"
                             for label, share in shares) + ".")


def _node_shares(source: Any, lonlat: Any) -> list[tuple[str, float]] | None:
    """The share of the NODES each input of a composed bed painted (merge band 2 names each cell's winner), or ``None``."""
    import numpy as np
    import rasterio
    from rasterio.warp import transform as warp_transform

    from trid3nt_server.inputs.bed import RASTER, bed as read_bed
    from trid3nt_server.tools.derive._raster_layers import FILLED, sources_of

    slot = read_bed(source, label="bed")
    # A surface by bare name or address carries no band its producer named.
    if slot is None or slot.kind != RASTER or isinstance(slot.source, str):
        return None
    surface = slot.source
    with rasterio.open(str(op_raster(surface))) as src:
        if src.count < 2:
            return None
        pts = np.asarray(lonlat, dtype=float)
        xs, ys = warp_transform("EPSG:4326", src.crs, pts[:, 0].tolist(),
                                pts[:, 1].tolist())
        won = np.array([v[0] for v in src.sample(zip(xs, ys), indexes=2)])
    total = float(won.size) or 1.0
    names = sources_of(surface, won.astype("uint8"))
    return [(name, float((won == (FILLED if name == "filled" else code)).sum())
             / total) for code, name in enumerate(names)]


def _stated_depth(source: Any) -> bool:
    """Was this bed STATED as a depth below the free surface?"""
    from trid3nt_server.inputs.bed import DEPTH, bed as read_bed

    slot = read_bed(source, label="bed")
    return slot is not None and slot.kind == DEPTH


def _painted(source: Any, lonlat: Any, box: tuple[float, float, float, float],
             interp: str, condition: str | None, edge_m: Any = None
             ) -> tuple[Any, str, list[str]]:
    """One bed source sampled at the nodes -> ``(values, provenance, notes)``; a node it has nothing for is NaN."""
    import numpy as np

    from trid3nt_server.inputs.bed import DEPTH, bed as read_bed
    from trid3nt_server.tools.mesh.shared.nodes import sample_raster_at_nodes

    slot = read_bed(source, label="bed")
    if slot is None:
        raise MeshToolError(
            "MESH_BED_UNRESOLVED",
            "set_bed was given no source, so the mesh has no elevation to carry.")
    if slot.kind == DEPTH:
        # A DEPTH is stated below the free surface, and the surface a run opens
        # at is its own zero, so the bed it describes is a flat bottom at minus
        # that depth. Every node is covered, which is why it never falls back.
        depth = float(slot.depth_m or 0.0)
        return (np.full(np.asarray(lonlat).shape[0], -depth, dtype=float),
                f"stated depth {depth:g} m below the free surface", [])
    raster, provenance, notes = _bed_raster(slot.source, box)
    cell = raster_cell_m(raster)
    if edge_m is not None and float(edge_m) < cell:
        notes = [*notes, (
            f"the mesh's {float(edge_m):g} m edge is finer than the bed's "
            f"{cell:g} m cell: the nodes between its cells carry the values it "
            "was read at, not a measurement")]
    if condition:
        raster, provenance = _conditioned(raster, provenance, condition)
    values = sample_raster_at_nodes(str(raster), lonlat, interp=str(interp),
                                    fill_holes=False)
    return values, provenance, notes


def set_boundary_roles(mesh: Mesh, runs: Any = None, **roles: Any) -> Mesh:
    """Which CONTIGUOUS runs of the boundary carry which role -> the mesh, roled.

    ``runs`` are the domain slot's boundary runs; ``roles`` is ``{role: face | [face, ...]}``. Boundary nodes on a face's run take
    the role, the rest are wall: a transect is the run between the contour nodes nearest its ends, a point the run within the mean
    boundary edge, a ring the stretch it stands along. Several faces union per role; a node two faces claim, or a face no node lies on, refuses."""
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform

    from trid3nt_server.tools.mesh.shared.nodes import boundary_contours

    declared = _declared_faces(runs, roles)
    if not declared:
        return mesh
    if not mesh.has_cells:
        raise MeshToolError(
            "MESH_ROLES_UNSEGMENTABLE",
            "this mesh states no cells of its own - the engine realizes them - so "
            "it has no boundary walk for a role to name a run of.")
    contours = [[int(n) for n in loop]
                for loop in boundary_contours(mesh.cells) if len(loop)]
    if not contours:
        raise MeshToolError(
            "MESH_BOUNDARY_UNSEGMENTED",
            f"boundary roles {sorted(declared)} were declared but this mesh's "
            "boundary walk found no nodes to carry them.")
    points_m, utm_epsg = _metre_nodes(mesh)
    tr = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    faces = {role: [_transform(tr.transform, _shape(face)) for face in value]
             for role, value in declared.items()}
    xy = np.asarray(points_m, dtype=float)
    # The tolerance is measured off the mesh and gates the FACE, not its anchors: a triangulator cuts polygon corners by more than its sides.
    tolerance = _mean_boundary_edge_m(xy, contours)
    matched = _runs(xy, contours, faces, tolerance_m=tolerance)
    unmatched = [f"{role}[{i}]" for role, declared in faces.items()
                 for i in range(len(declared))
                 if not (matched.get(role) or [])[i:i + 1]
                 or not matched[role][i]]
    if unmatched:
        raise MeshToolError(
            "MESH_BOUNDARY_ROLE_UNMATCHED",
            f"no boundary node of this mesh lies within {tolerance:.1f} m of the "
            f"face declared for {unmatched}; the mesh and the face the "
            "chain measured describe different domains.")
    claimed: dict[int, str] = {}
    for role, runs in matched.items():
        for node in (n for run in runs for n in run):
            if claimed.setdefault(int(node), role) != role:
                raise MeshToolError(
                    "MESH_BOUNDARY_ROLE_CONFLICT",
                    f"boundary node {int(node)} is named by both "
                    f"{claimed[int(node)]!r} and {role!r}; a node carries one "
                    "boundary condition, so the declared faces overlap.")
    return _with_meta(
        mesh,
        # Once per node, in walk order: two sections of one role may meet at a
        # shared anchor, and a node listed twice would double the count a reader
        # reads as how much boundary carries the role.
        boundary_roles={**dict(mesh.meta.get("boundary_roles") or {}),
                        **{role: list(dict.fromkeys(
                            int(n) for run in runs for n in run))
                           for role, runs in matched.items()}},
        boundary_role_runs={**dict(mesh.meta.get("boundary_role_runs") or {}),
                            **{role: len(runs) for role, runs in matched.items()}})


def _declared_faces(runs: Any, roles: Mapping[str, Any]
                    ) -> dict[str, list[dict[str, Any]]]:
    """Every face this call prescribes, by role; a ``wall`` run prescribes nothing."""
    from trid3nt_server.inputs.boundary import boundary_runs, roles_from_runs

    out: dict[str, list[dict[str, Any]]] = {}
    for role, value in roles.items():
        out.setdefault(str(role), []).extend(_faces(str(role), value))
    for role, faces in roles_from_runs(boundary_runs(runs)).items():
        out.setdefault(role, []).extend(faces)
    return {role: faces for role, faces in out.items() if faces}


def bed_raster_of(source: Any, bbox: tuple[float, float, float, float]
                  ) -> Path | None:
    """The raster ``set_bed`` would paint from, staged; ``None`` for a stated depth."""
    from trid3nt_server.inputs.bed import bed as read_bed

    slot = None if _stated_depth(source) else read_bed(source, label="bed")
    return None if slot is None else _bed_raster(slot.source, bbox)[0]


def _bed_raster(source: Any, bbox: tuple[float, float, float, float]
                ) -> tuple[Path, str, list[str]]:
    """Stage the bed as a local EPSG:4326 raster -> ``(path, provenance, notes)``.

    EPSG:4326: the nodes are sampled in lon/lat, and a projected bed reads fill."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.inputs.geometry import source_uri

    name = str(source_uri(source) or "").strip()
    if not name:
        raise MeshToolError(
            "MESH_BED_UNRESOLVED",
            "set_bed was given no source, so the mesh has no elevation to carry.")
    if name in TOOL_REGISTRY:
        _refuse_undated_source(name)
        # The NAMED source only: a bed is TOPOBATHY and a standard DEM measures the water SURFACE, so substitutions are the DATA row's declaration.
        layer = TOOL_REGISTRY[name].fn(bbox=bbox, target_crs="EPSG:4326")
        return (op_raster(layer), _provenance(name, layer),
                fetch_notes(layer))
    return op_raster(source), f"bed raster supplied directly: {name}", []


def _fetcher_row(name: str) -> Any:
    """The declaration behind a registered fetcher, or None where none is served."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    return get_spec(name)


def _refuse_undated_source(name: str) -> None:
    """A ROW states its vertical datum, or it is not a bed.

    The bytes cannot be asked: only the dataset's own row states it."""
    spec = _fetcher_row(name)
    if spec is not None and not spec.vertical_datum:
        raise MeshToolError(
            "MESH_BED_DATUM_UNSTATED",
            f"{name} states no vertical datum, so what its elevations are "
            "counted from is unknown and the bed it would paint cannot be read "
            "against anything. State the datum on the row from the "
            "dataset's own documentation, or name a source that does.")


def _provenance(name: str, layer: Any) -> str:
    """What ACTUALLY painted the bed, and what its elevations are counted from.

    The datum and the acquisition instant ride with the name."""
    notes = fetch_notes(layer)
    painted = f"{name} ({'; '.join(notes)})" if notes else name
    facts = [fact for fact in (_datum(name), _acquired(layer))
             if fact]
    return f"{painted} [{'; '.join(facts)}]" if facts else painted


def _datum(name: str) -> str | None:
    spec = _fetcher_row(name)
    return f"datum {spec.vertical_datum}" if spec is not None \
        and spec.vertical_datum else None


def _acquired(layer: Any) -> str | None:
    """When the source was measured, where the layer states an instant."""
    for field in ("reference_time", "valid_from"):
        found = layer.get(field) if isinstance(layer, Mapping) \
            else getattr(layer, field, None)
        if found:
            return f"acquired {found}"
    return None


def _conditioned(raster: Path, provenance: str, condition: str) -> tuple[Path, str]:
    """The staged bed, put through the one conditioning chain a bed knows."""
    if str(condition) != _PIT_FILL:
        raise MeshToolError(
            "MESH_OP_BAD_VALUE",
            f"set_bed knows one conditioning, {_PIT_FILL!r}, not {condition!r}.")
    from trid3nt_server.tools.derive._hydrology_common import (
        write_conditioned_dem,
    )

    filled = raster.with_name(f"{raster.stem}_pit_filled.tif")
    write_conditioned_dem(str(raster), str(filled))
    return filled, f"{provenance} (pit-filled: the delineator's own chain)"


def _lonlat_nodes(mesh: Mesh) -> Any:
    """This mesh's nodes in lon/lat, whichever CRS its own points are in."""
    import numpy as np

    declared = mesh.meta.get("lonlat")
    if declared is not None:
        return np.asarray(declared, dtype=float)
    if str(mesh.crs_authid).upper() == "EPSG:4326":
        return np.asarray(mesh.points, dtype=float)
    from pyproj import Transformer

    epsg = int(str(mesh.crs_authid).split(":")[-1])
    pts = np.asarray(mesh.points, dtype=float)
    lon, lat = Transformer.from_crs(epsg, 4326, always_xy=True).transform(
        pts[:, 0], pts[:, 1])
    return np.column_stack([lon, lat])


def _metre_nodes(mesh: Mesh) -> tuple[Any, int]:
    """This mesh's nodes in METRES, and the zone they are in.

    A tolerance is a length, and a length in degrees weights the axes apart."""
    import numpy as np

    from trid3nt_server.tools.mesh.shared.nodes import reproject_nodes_to_utm

    authid = str(mesh.crs_authid).upper()
    if authid != "EPSG:4326":
        return np.asarray(mesh.points, dtype=float), int(authid.split(":")[-1])
    return reproject_nodes_to_utm(np.asarray(mesh.points, dtype=float))


def _extent(lonlat: Any) -> tuple[float, float, float, float]:
    import numpy as np

    pts = np.asarray(lonlat, dtype=float)
    return (float(pts[:, 0].min()), float(pts[:, 1].min()),
            float(pts[:, 0].max()), float(pts[:, 1].max()))


def _grown(box: tuple[float, float, float, float]
           ) -> tuple[float, float, float, float]:
    """The extent grown by :data:`_BED_MARGIN_FRAC` of its own span, in degrees."""
    dx = (box[2] - box[0]) * _BED_MARGIN_FRAC
    dy = (box[3] - box[1]) * _BED_MARGIN_FRAC
    return (box[0] - dx, box[1] - dy, box[2] + dx, box[3] + dy)


def _with_meta(mesh: Mesh, **meta: Any) -> Mesh:
    """``mesh`` carrying ``meta``; a None value states nothing rather than null."""
    carried = {**dict(mesh.meta),
               **{k: v for k, v in meta.items() if v is not None}}
    return dataclasses.replace(mesh, meta=carried)


def _faces(role: str, value: Any) -> list[dict[str, Any]]:
    """One declared role's faces as GeoJSON, whichever way they were declared.

    A sequence of coordinate pairs is ONE transect; anything else is faces."""
    if isinstance(value, (list, tuple)):
        if all(isinstance(item, (list, tuple)) and len(item) == 2
               and all(isinstance(c, (int, float)) for c in item)
               for item in value):
            coords = [[float(c[0]), float(c[1])] for c in value]
            if len(coords) < 2:
                raise MeshToolError(
                    "MESH_BOUNDARY_ROLE_INVALID",
                    f"boundary role {role!r} names {coords}, which is not a face: "
                    "a role is prescribed across a transect, so declare the two "
                    "ends of one (a section's face_start / face_end).")
            return [{"type": "LineString", "coordinates": coords}]
        if not value:
            raise MeshToolError(
                "MESH_BOUNDARY_ROLE_INVALID",
                f"boundary role {role!r} names no face at all; declare the "
                "stretch it is prescribed across.")
        return [face for item in value for face in _faces(role, item)]
    return [op_geometry(value)]


def _mean_boundary_edge_m(points_m: Any, contours: Any) -> float:
    """The mean length of this mesh's boundary edges, in metres."""
    import numpy as np

    xy = np.asarray(points_m, dtype=float)
    lengths = [float(np.hypot(*(xy[int(loop[i + 1])] - xy[int(loop[i])])))
               for loop in contours for i in range(len(loop) - 1)]
    return float(np.mean(lengths)) if lengths else 0.0


def _runs(points_utm: Any, contours: Any, faces_utm: Mapping[str, Any], *,
          tolerance_m: float) -> dict[str, list[list[int]]]:
    """``{role: [run, ...]}``, each run a stretch of ONE contour in walk order.

    One run per DECLARED FACE, in declared order; an unmatched face stays empty."""
    import numpy as np
    from shapely.geometry import Point

    rings = [[int(n) for n in ring] for ring in contours if len(ring)]
    if not rings or not faces_utm:
        return {}
    pts = np.asarray(points_utm, dtype=float)

    def distance(geometry: Any, node: int) -> float:
        return float(geometry.distance(Point(pts[node, 0], pts[node, 1])))

    def nearest(geometry: Any, ring: Any) -> tuple[int, float]:
        return min(((i, distance(geometry, node)) for i, node in enumerate(ring)),
                   key=lambda hit: hit[1])

    def run_for(face: Any) -> list[int]:
        # WHICH contour carries the face: the one holding the node nearest it. A
        # domain with an island has more than one, and a run that jumped between
        # them is a boundary no walk of the geometry produces.
        on, offset = min(((ring, nearest(face, ring)[1]) for ring in rings),
                         key=lambda hit: hit[1])
        if offset > float(tolerance_m):
            return []
        ends = list(getattr(face.boundary, "geoms", []))
        if len(ends) == 2:
            return _arc(on, nearest(ends[0], on)[0], nearest(ends[1], on)[0])
        return _window(on, nearest(face, on)[0],
                       lambda node: distance(face, node), float(tolerance_m))

    return {role: [run_for(face) for face in declared]
            for role, declared in faces_utm.items()}


def _arc(ring: Any, start: int, end: int) -> list[int]:
    """The shorter of the two ways round ``ring`` from ``start`` to ``end``.

    A transect cuts one end off the domain: the short way is the stretch."""
    size = len(ring)
    forward = (end - start) % size
    if 2 * forward <= size:
        return [ring[(start + step) % size] for step in range(forward + 1)]
    return [ring[(start - step) % size] for step in range(size - forward + 1)]


def _window(ring: Any, index: int, offset: Any, tolerance_m: float) -> list[int]:
    """The run of ``ring`` about ``index`` that stays within ``tolerance_m``.

    The walk STOPS at the first node past the tolerance and never resumes."""
    size = len(ring)
    sides: dict[int, list[int]] = {1: [], -1: []}
    for step in (1, -1):
        reach = step
        while (len(sides[1]) + len(sides[-1]) + 1 < size
               and offset(ring[(index + reach) % size]) <= tolerance_m):
            sides[step].append(ring[(index + reach) % size])
            reach += step
    return list(reversed(sides[-1])) + [ring[index]] + sides[1]
