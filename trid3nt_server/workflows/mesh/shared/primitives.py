"""The SHARED op primitives: what we impose on a mesh that no library does.

This module IS a namespace - it rides along for every mesher, and an op names
one of these by its real ``def`` name. Both take the data class they are DEFINED
OVER, and neither carries a branch that compensates for the wrong one."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.workflows.mesh.inputs import op_geometry, op_raster
from trid3nt_server.workflows.mesh.meshers import (
    Mesh,
    MeshToolError,
    fetch_activation_rows,
    fetch_fallback_note,
)

__all__ = ["set_bed", "set_boundary_roles"]

#: How the sampled surface is read between the raster's cell centres. ``nearest``
#: is the labeled default: it returns a value the grid actually holds, which is
#: what a bed has to be when the raster is already finer than the elements.
_INTERPOLATIONS = ("nearest", "bilinear")

#: The one conditioning a bed source is put through on the way in, named by the
#: ask: the watershed delineator's own pit/depression/flat chain. A catchment
#: whose basin was delineated on a filled surface but whose bed carries the raw
#: sinks ponds in pits the routing does not believe in, and the deepest water in
#: the run is then a terrain artifact.
_PIT_FILL = "pit_fill"

#: How far past the mesh's own extent the bed is fetched, as a fraction of each
#: span. The mesh has nodes ON the extent's corners and a raster's rim rows carry
#: the warp's fill, so the grid has to reach past where the domain ends.
_BED_MARGIN_FRAC = 0.02
def set_bed(mesh: Mesh, source: Any, interp: str = "nearest",
            condition: str | None = None) -> Mesh:
    """Paint every node's elevation from the bed slot -> the mesh, bedded.

    ``source`` is ONE surface - a raster fetcher's name, an object-store uri, a
    layer, a layer of soundings, or a depth in metres below the free surface. A
    bed built from a measurement over a wider surface is composed BEFORE here,
    by the merge derive, and arrives as the one layer it produced."""
    import numpy as np

    if str(interp) not in _INTERPOLATIONS:
        raise MeshToolError(
            "MESH_OP_BAD_VALUE",
            f"set_bed reads a raster {list(_INTERPOLATIONS)}, not {interp!r}.")
    lonlat = _lonlat_nodes(mesh)
    box = _grown(_extent(lonlat))
    cell_m = _node_spacing_m(mesh)
    values, provenance, note = _painted(source, lonlat, box, interp, condition,
                                        cell_m)
    unpainted = int((~np.isfinite(values)).sum())
    if unpainted:
        raise MeshToolError(
            "MESH_BED_UNPAINTED",
            f"{unpainted} of {values.size} nodes have no elevation from "
            f"{provenance}: the domain reaches past the surface it was given. "
            "Merge that surface with one that covers the rest, name a source "
            "that covers it whole, or state the depth this water body holds.")
    painted = f"{provenance} at {values.size} nodes"
    _journal_bed(source, provenance, lonlat, values.size)
    return _with_meta(
        dataclasses.replace(mesh, bed=np.asarray(values, dtype=float)),
        bed_source=painted,
        bed_sources=[provenance],
        bed_fallback_note=note,
        # A bed STATED as a depth is counted from the free surface itself, so
        # this mesh knows the elevation that surface stands at and a run over it
        # needs no gauge. A bed measured on a datum states nothing about the
        # water, and a run over it opens at the level somebody read.
        **({"free_surface_m": 0.0} if _stated_depth(source) else {}),
        synthetic_inputs=[
            *(mesh.meta.get("synthetic_inputs") or []),
            {"param": "mesh_bed", "value": painted, "basis": "fetched",
             "consequence": "physics", "real_source_if_any": painted,
             "note": "the elevation every node carries; a solver reads it as the "
                     "domain's bathymetry"}])


def _journal_bed(source: Any, provenance: str, lonlat: Any, nodes: int) -> None:
    """What the run SAYS about the bed its nodes were painted from.

    On the run journal and not in a log line: which dataset reached each node is
    part of the answer, and a composed bed is two datasets whose shares of the
    NODES a reader has to be able to see."""
    from trid3nt_server.workflows.runtime import journal_note

    shares = _node_shares(source, lonlat)
    journal_note(f"bed: {provenance} painted all {nodes} nodes."
                 if shares is None else
                 f"bed: {provenance} painted {nodes} nodes - the measurement "
                 f"reached {shares[0] * 100.0:.1f}% of them and the wider "
                 f"surface under it {shares[1] * 100.0:.1f}%.")


def _node_shares(source: Any, lonlat: Any) -> tuple[float, float] | None:
    """The share of the NODES each input of a composed bed painted, or ``None``.

    The merge writes which input won at each CELL; a node takes the cell it
    stands in, so the shares a run reports are the ones its own solve reads."""
    import numpy as np

    from trid3nt_server.inputs.bed import bed as read_bed
    from trid3nt_server.workflows.mesh.shared.nodes import sample_raster_at_nodes

    slot = read_bed(source, label="bed")
    uri = str(getattr(getattr(slot, "source", None), "provenance_uri", "") or "")
    if not uri:
        return None
    # STAGED the way every other raster this op reads is: the sidecar lives in
    # the object store, and a sampler handed its address reads it as a public URL.
    won = np.asarray(sample_raster_at_nodes(str(op_raster(uri)), lonlat,
                                            interp="nearest", fill_holes=False))
    total = float(won.size) or 1.0
    return (float((won == 0).sum()) / total, float((won == 1).sum()) / total)


def _stated_depth(source: Any) -> bool:
    """Was this bed STATED as a depth below the free surface?"""
    from trid3nt_server.inputs.bed import DEPTH, bed as read_bed

    slot = read_bed(source, label="bed")
    return slot is not None and slot.kind == DEPTH


def _painted(source: Any, lonlat: Any, box: tuple[float, float, float, float],
             interp: str, condition: str | None, cell_m: float
             ) -> tuple[Any, str, str | None]:
    """One bed source sampled at the nodes -> ``(values, provenance, note)``.

    A node the source has nothing for comes back NaN, which is what lets a
    second source paint it; a STATED DEPTH covers every node by construction."""
    import numpy as np

    from trid3nt_server.inputs.bed import DEPTH, POINTS, bed as read_bed
    from trid3nt_server.workflows.mesh.shared.nodes import sample_raster_at_nodes

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
                f"stated depth {depth:g} m below the free surface", None)
    if slot.kind == POINTS:
        raster, provenance = _interpolated_survey(slot.source, cell_m, slot)
        note = None
    else:
        raster, provenance, note = _bed_raster(slot.source, box)
    if condition:
        raster, provenance = _conditioned(raster, provenance, condition)
    values = sample_raster_at_nodes(str(raster), lonlat, interp=str(interp),
                                    fill_holes=False)
    return values, provenance, note


def _node_spacing_m(mesh: Mesh) -> float:
    """This mesh's own median element edge, in metres - the scale a bed has to
    resolve. A surface finer than the elements buys nothing, and one coarser
    loses the channel the elements were sized for."""
    import numpy as np

    if not mesh.has_cells:
        return 0.0
    xy, _epsg = _metre_nodes(mesh)
    cells = np.asarray(mesh.cells, dtype=int)
    lengths = []
    for a, b in ((0, 1), (1, 2), (2, 0)):
        lengths.append(np.hypot(*(xy[cells[:, b]] - xy[cells[:, a]]).T))
    return float(np.median(np.concatenate(lengths)))


def _interpolated_survey(layer: Any, cell_m: float, slot: Any) -> tuple[Any, str]:
    """A layer of SOUNDINGS through the grid that turns it into a surface.

    Interpolating scattered measurements between soundings is the BED slot's own
    rule, so it is reached at its own address in that slot rather than through a
    registered name. The surface it makes carries the survey's own zero, so it is
    read onto the run's frame here - the one hop a raster bed takes on the way
    into its slot, taken at the moment the surface exists."""
    from trid3nt_server.inputs.bed import elevations, survey_surface
    from trid3nt_server.inputs.geometry import source_uri

    if not cell_m:
        raise MeshToolError(
            "MESH_BED_SURVEY_UNSCALED",
            "this mesh states no cells of its own, so there is no element scale "
            "to interpolate the soundings at; supply a survey raster instead.")
    surface = survey_surface(points=layer, resolution_m=float(cell_m))
    surface = elevations(surface, frame=slot.frame, offset=slot.offset)
    return op_raster(surface), (f"a survey surface at {cell_m:.3g} m over the "
                                f"supplied soundings ({source_uri(layer)})")


def set_boundary_roles(mesh: Mesh, runs: Any = None, **roles: Any) -> Mesh:
    """Which CONTIGUOUS runs of the boundary carry which role -> the mesh, roled.

    ``runs`` is the BOUNDARY RUNS the domain slot carries - two points on the
    edge and a type each, from a template, a producer or the user's drawing -
    and stating none is an answer: a closed body's edge is solid wall whole.

    ``roles`` is ``{role: face}`` or ``{role: [face, ...]}`` - ``inflow``,
    ``outflow``, ``open``, ``rating_curve``, ``free_exit`` - each face a geometry
    the chain measured, or the two ends of one. Every boundary node on the run a
    face names takes that role; the rest are solid wall.

    A role is a RUN, not a node set: a declared TRANSECT names the run between
    the contour nodes nearest its two ends, a declared POINT the run standing
    within the mesh's own mean boundary edge of it, and a declared RING the whole
    stretch it stands along.

    SEVERAL FACES, ONE ROLE: each face lands its own run and the role carries
    their union, with the number of runs riding back on the mesh. A node two
    faces both claim refuses, and so does a face NO boundary node lies on."""
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform

    from trid3nt_server.workflows.mesh.shared.nodes import boundary_contours

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
    # The tolerance is measured off the mesh and gates the FACE, not its anchors:
    # a triangulator conforms to a polygon within an edge along its sides and
    # cuts its corners by more, so a tolerance on the two end anchors would
    # reject the very reach whose middle the boundary follows exactly.
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
    """Every face this call prescribes, by role: the runs and the named roles.

    A run of type ``wall`` prescribes nothing - the edge is already wall where
    nothing names it - so a body whose runs are all walls declares no face."""
    from trid3nt_server.inputs.boundary import boundary_runs, roles_from_runs

    out: dict[str, list[dict[str, Any]]] = {}
    for role, value in roles.items():
        out.setdefault(str(role), []).extend(_faces(str(role), value))
    for role, faces in roles_from_runs(boundary_runs(runs)).items():
        out.setdefault(role, []).extend(faces)
    return {role: faces for role, faces in out.items() if faces}


def _bed_raster(source: Any, bbox: tuple[float, float, float, float]
                ) -> tuple[Path, str, str | None]:
    """Stage the bed as a local EPSG:4326 raster -> ``(path, provenance, note)``.

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
        # The PRIMARY source and nothing else: no ladder rung is permitted from
        # here. A bed is TOPOBATHY - the channel bottom and the sea floor - and a
        # standard DEM measures the water SURFACE, so which substitutions a bed
        # tolerates is the DATA row's declaration; a rung this op permitted on
        # the author's behalf would be a cross-dataset bed nobody wrote down.
        layer = TOOL_REGISTRY[name].fn(bbox=bbox, target_crs="EPSG:4326")
        return (op_raster(layer), _provenance(name, layer),
                fetch_fallback_note(layer))
    return op_raster(source), f"bed raster supplied directly: {name}", None


def _source_row(name: str) -> Any:
    """The declaration behind a registered fetcher, or None where none is served."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    return get_spec(name)


def _refuse_undated_source(name: str) -> None:
    """A SOURCE ROW states its vertical datum, or it is not a bed.

    The bytes cannot be asked: only the dataset's own row states it."""
    spec = _source_row(name)
    if spec is not None and not spec.vertical_datum:
        raise MeshToolError(
            "MESH_BED_DATUM_UNSTATED",
            f"{name} states no vertical datum, so what its elevations are "
            "counted from is unknown and the bed it would paint cannot be read "
            "against anything. State the datum on the source row from the "
            "dataset's own documentation, or name a source that does.")


def _provenance(name: str, layer: Any) -> str:
    """What ACTUALLY painted the bed, and what its elevations are counted from.

    The datum, the acquisition instant and the native cell ride with the name."""
    rows = fetch_activation_rows(layer)
    note = fetch_fallback_note(layer)
    if rows:
        painted = f"{name}: " + ", ".join(
            f"{rung} {coverage * 100:.0f}%" for rung, coverage in rows)
    elif note:
        painted = f"{name} ({note})"
    else:
        painted = f"{name} (source UNMEASURED: the fetch reported no activation rows)"
    facts = [fact for fact in (_datum(name), _acquired(layer), _native_cell(name))
             if fact]
    return f"{painted} [{'; '.join(facts)}]" if facts else painted


def _datum(name: str) -> str | None:
    spec = _source_row(name)
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


def _native_cell(name: str) -> str | None:
    spec = _source_row(name)
    for decl in (getattr(spec, "resolution_declarations", ()) or ()):
        if decl.native_hint:
            return f"native {decl.native_hint}"
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

    from trid3nt_server.workflows.mesh.shared.nodes import reproject_nodes_to_utm

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
