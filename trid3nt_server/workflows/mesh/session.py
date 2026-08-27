"""``MeshSession`` - the mesh under construction, and the recipe that IS its record.

A mesh is its spec plus an ordered chain of named edits, so the session keeps that
chain and journals it to ``mesh_recipe.jsonl`` beside the mesh files: the spec on
the first line, then one line per applied edit. Replaying the journal rebuilds the
mesh. An edit whose change lives in a hand-edited layer rather than in its inputs
is recorded with the layer's digest and ``replayable: false``, so a replay refuses
instead of quietly producing a different mesh.

The build is LAZY - nothing runs until a probe, a snapshot or an accept demands
the mesh - and ``restart`` truncates the chain back to the DECLARED prefix.
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.mesh.grid_geometry import M_PER_DEG_LAT
from trid3nt_server.workflows.mesh.artifact import (
    MeshArtifact,
    stash_mesh_artifact,
    write_mesh_artifact_sidecar,
)
from trid3nt_server.workflows.mesh.meshers import (
    Mesh,
    Mesher,
    MeshToolError,
    get_mesher,
    input_digest,
)
from trid3nt_server.workflows.mesh.tool import (
    DeclaredEdit,
    MeshDeclaration,
    MeshSpec,
    jsonable,
    bind_edit_inputs,
    validate_edit,
)

logger = logging.getLogger("trid3nt_server.workflows.mesh.session")

__all__ = ["MeshSession", "mesh_digest", "replay_recipe", "write_2dm"]

#: The SMS element tag for a cell of N nodes. A mesh of any other arity has no
#: display face here and says so rather than being silently reshaped.
_ELEMENT_TAG: Mapping[int, str] = {3: "E3T", 4: "E4Q"}


class MeshSession:
    """A mesh being built: edit, probe, snapshot, restart, accept."""

    def __init__(self, declaration: MeshDeclaration, *,
                 workdir: str | os.PathLike[str] | None = None,
                 case_id: str | None = None, name: str | None = None) -> None:
        self.declaration = declaration
        self.mesher: Mesher = get_mesher(declaration.spec.mesher)
        self.mesh_id = new_ulid()
        self.case_id = case_id
        self.name = name or f"{declaration.spec.mesher} mesh"
        self.workdir = Path(workdir) if workdir is not None else (
            Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"mesh-{self.mesh_id}")
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._declared: tuple[DeclaredEdit, ...] = tuple(declaration.edits)
        self._chain: list[DeclaredEdit] = list(self._declared)
        self._mesh: Mesh | None = None
        self._display: tuple[Path, str] | None = None

    # -- the mesh ---------------------------------------------------------- #
    @property
    def spec(self) -> MeshSpec:
        return self.declaration.spec

    @property
    def chain(self) -> tuple[DeclaredEdit, ...]:
        return tuple(self._chain)

    @property
    def mesh(self) -> Mesh:
        self._ensure_built()
        assert self._mesh is not None
        return self._mesh

    @property
    def recipe_path(self) -> Path:
        return self.workdir / "mesh_recipe.jsonl"

    def _ensure_built(self) -> None:
        if self._mesh is None:
            self._mesh = _build_chain(self.mesher, self.spec, self._chain)
            self._journal()

    # -- the loop ---------------------------------------------------------- #
    def edit(self, action: str, *values: Any, **inputs: Any) -> dict[str, Any]:
        """Apply one registered edit -> the rebuilt mesh's probes.

        Positional values bind to the action's declared inputs in declaration
        order, the same as on a declared edit.
        """
        self._ensure_built()
        act = self.mesher.action(action)
        bound = validate_edit(self.mesher.name, act.name,
                              bind_edit_inputs(self.mesher.name, act.name,
                                               values, inputs))
        self._mesh = act.apply(self._mesh, **dict(bound))
        self._chain.append(DeclaredEdit(act.name, bound))
        self._display = None
        self._journal()
        return self.probes()

    def restart(self) -> dict[str, Any]:
        """Truncate the chain back to the DECLARED prefix and rebuild -> probes."""
        self._chain = list(self._declared)
        self._mesh = None
        self._display = None
        self._ensure_built()
        return self.probes()

    def probes(self) -> dict[str, Any]:
        """The numeric facts a human or an agent judges the mesh on."""
        return _probes(self.mesh, [e.action for e in self._chain])

    def snapshot(self) -> LayerURI:
        """The mesh's DISPLAY face for the current chain, as an MDAL mesh layer."""
        _, uri = self._display_face()
        mesh = self.mesh
        return LayerURI(
            layer_id=f"mesh-{self.mesh_id}", name=f"Mesh: {self.name}",
            layer_type="mesh", uri=uri, style_preset="mesh_wireframe",
            role="primary", bbox=_lonlat_bbox(mesh), crs_authid=mesh.crs_authid)

    def accept(self) -> MeshArtifact:
        """Freeze the current mesh as a case artifact -> the :class:`MeshArtifact`."""
        mesh = self.mesh
        _, display_uri = self._display_face()
        slf_uri = self._selafin()
        recipe_uri = self._stage(self.recipe_path)
        has_bed = mesh.has_bed
        art = MeshArtifact(
            mesh_id=self.mesh_id, name=self.name, mode=self.mesher.name,
            display_uri=display_uri, slf_uri=slf_uri,
            crs_authid=mesh.crs_authid, has_bathymetry=has_bed,
            node_count=mesh.node_count, element_count=mesh.element_count,
            bbox=_lonlat_bbox(mesh),
            engine_compat=(["telemac"] if slf_uri and has_bed else []),
            utm_epsg=mesh.meta.get("utm_epsg"),
            provenance={"mesher": self.mesher.name,
                        "spec": self.spec.to_json(),
                        "edits": [e.action for e in self._chain]},
            recipe_uri=recipe_uri, case_id=self.case_id)
        stash_mesh_artifact(self.case_id, art)
        if str(display_uri).startswith("s3://"):
            write_mesh_artifact_sidecar(art, _s3_client())
        logger.info("build_mesh: %s mesh %s -> %d nodes %d elements %s (bed=%s)",
                    self.mesher.name, self.mesh_id, art.node_count,
                    art.element_count, art.crs_authid, has_bed)
        return art

    # -- files ------------------------------------------------------------- #
    def _display_face(self) -> tuple[Path, str]:
        if self._display is None:
            local = self.workdir / "mesh.2dm"
            local.write_text(write_2dm(self.mesh))
            self._display = (local, self._stage(local))
        return self._display

    def _selafin(self) -> str | None:
        """The TELEMAC geometry, when this mesh IS one: triangles with a real bed."""
        mesh = self.mesh
        if mesh.nodes_per_cell != 3 or not mesh.has_bed:
            return None
        from trid3nt_server.workflows.mesh.telemac_build import write_bottom_selafin

        local = self.workdir / "mesh.slf"
        write_bottom_selafin(str(local), mesh.points, mesh.cells, mesh.bed)
        return self._stage(local)

    def _stage(self, local: Path) -> str:
        """Upload ``local`` beside this mesh's other objects -> its uri.

        With no case cache bucket configured the file stays where it was written
        and the artifact points at it, which is what a headless direct call has.
        """
        bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
        if not bucket:
            return str(local)
        key = f"mesh/{self.mesh_id}/{local.name}"
        _s3_client().put_object(Bucket=bucket, Key=key, Body=local.read_bytes())
        return f"s3://{bucket}/{key}"

    # -- the recipe -------------------------------------------------------- #
    def recipe_lines(self) -> list[dict[str, Any]]:
        """The recipe as records: the spec, then one per edit in chain order."""
        return [{"spec": self.spec.to_json()},
                *(_edit_line(self.mesher, e) for e in self._chain)]

    def _journal(self) -> None:
        self.recipe_path.write_text(
            "".join(json.dumps(line) + "\n" for line in self.recipe_lines()))


def _s3_client() -> Any:
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    return _get_s3_client()


# --------------------------------------------------------------------------- #
# Recipe replay.
# --------------------------------------------------------------------------- #
def _edit_line(mesher: Mesher, edit: DeclaredEdit) -> dict[str, Any]:
    act = mesher.action(edit.action)
    line: dict[str, Any] = {"edit": act.name}
    for name, declared in act.inputs.items():
        if name not in edit.inputs:
            continue
        value = edit.inputs[name]
        if declared.hashed:
            line[name] = input_digest(value)
            if isinstance(value, str):
                line["source"] = value
        else:
            line[name] = jsonable(value)
    if not act.replayable:
        line["replayable"] = False
    return line


def _build_chain(mesher: Mesher, spec: MeshSpec,
                 chain: Sequence[DeclaredEdit]) -> Mesh:
    mesh = mesher.build(dict(spec.fields))
    for edit in chain:
        act = mesher.action(edit.action)
        mesh = act.apply(mesh, **dict(edit.inputs))
    return mesh


def replay_recipe(source: str | os.PathLike[str] | Sequence[Mapping[str, Any]]) -> Mesh:
    """Rebuild a mesh from its recipe -> the :class:`Mesh` the recipe describes.

    A recorded hand-edit REFUSES: its change is in the layer's bytes, not in the
    recipe, so replaying it would return a different mesh under the same record.
    """
    if isinstance(source, (str, os.PathLike)):
        lines = [json.loads(ln) for ln in
                 Path(source).read_text().splitlines() if ln.strip()]
    else:
        lines = [dict(ln) for ln in source]
    if not lines or "spec" not in lines[0]:
        raise MeshToolError(
            "MESH_RECIPE_MALFORMED",
            "a mesh recipe starts with its spec line; this one does not.")
    spec = MeshSpec.from_json(lines[0]["spec"])
    mesher = get_mesher(spec.mesher)
    mesh = mesher.build(dict(spec.fields))
    for line in lines[1:]:
        act = mesher.action(line["edit"])
        if not act.replayable or line.get("replayable") is False:
            raise MeshToolError(
                "MESH_RECIPE_NOT_REPLAYABLE",
                f"edit {act.name!r} carries its change in an input this recipe can "
                f"only digest ({line.get(next(iter(act.inputs), ''), '')}); the mesh "
                "it produced cannot be rebuilt from the record. Rebuild from the "
                "prefix before it, or keep the accepted mesh.")
        inputs: dict[str, Any] = {}
        for name, declared in act.inputs.items():
            if declared.hashed:
                source_ref = line.get("source")
                if source_ref is None:
                    raise MeshToolError(
                        "MESH_RECIPE_MISSING_SOURCE",
                        f"edit {act.name!r} hashes {name!r} but the recipe records "
                        "no source to re-read it from.")
                inputs[name] = source_ref
            elif name in line:
                inputs[name] = line[name]
        bound = validate_edit(mesher.name, act.name, inputs)
        mesh = act.apply(mesh, **dict(bound))
    return mesh


# --------------------------------------------------------------------------- #
# Display face + probes.
# --------------------------------------------------------------------------- #
def write_2dm(mesh: Mesh) -> str:
    """Write the mesh as an SMS ``.2dm`` string MDAL opens as a mesh layer.

    A bed-less mesh writes a zero node column because the format requires one;
    the artifact's ``has_bathymetry`` is what says whether an elevation was ever
    sampled.
    """
    tag = _ELEMENT_TAG.get(mesh.nodes_per_cell)
    if tag is None:
        raise MeshToolError(
            "MESH_DISPLAY_UNSUPPORTED_CELL",
            f"a {mesh.nodes_per_cell}-node cell has no .2dm element tag "
            f"(supported: {sorted(_ELEMENT_TAG)}).")
    pts = np.asarray(mesh.points, dtype=float)
    cells = np.asarray(mesh.cells, dtype=np.int64) + 1
    bed = (np.zeros(pts.shape[0], dtype=float) if mesh.bed is None
           else np.asarray(mesh.bed, dtype=float))
    lines = ["MESH2D"]
    for i, row in enumerate(cells, start=1):
        lines.append(f"{tag} {i} " + " ".join(str(int(v)) for v in row) + " 1")
    for i, (x, y) in enumerate(pts, start=1):
        lines.append(f"ND {i} {x:.6f} {y:.6f} {float(bed[i - 1]):.6f}")
    return "\n".join(lines) + "\n"


def mesh_digest(mesh: Mesh) -> str:
    """``sha256:<hex>`` over the mesh's display text - one number per geometry."""
    import hashlib

    return "sha256:" + hashlib.sha256(write_2dm(mesh).encode("utf-8")).hexdigest()


def _metre_scale(mesh: Mesh) -> tuple[float, float]:
    """Node-coordinate units -> metres, per axis.

    A geographic mesh converts at its own mean latitude with the local
    equirectangular scale; a projected mesh is already in metres.
    """
    if str(mesh.crs_authid).upper() != "EPSG:4326":
        return 1.0, 1.0
    lat = float(np.asarray(mesh.points, dtype=float)[:, 1].mean())
    return (M_PER_DEG_LAT * max(0.01, math.cos(math.radians(lat))), M_PER_DEG_LAT)


def _lonlat_bbox(mesh: Mesh) -> tuple[float, float, float, float]:
    declared = mesh.meta.get("lonlat_bbox")
    if declared is not None:
        return tuple(float(v) for v in declared)  # type: ignore[return-value]
    if str(mesh.crs_authid).upper() != "EPSG:4326":
        raise MeshToolError(
            "MESH_NO_LONLAT_BBOX",
            f"a mesh in {mesh.crs_authid} cannot state its lon/lat extent; the "
            "mesher must carry a lonlat_bbox in the mesh meta.")
    pts = np.asarray(mesh.points, dtype=float)
    return (float(pts[:, 0].min()), float(pts[:, 1].min()),
            float(pts[:, 0].max()), float(pts[:, 1].max()))


def _unique_edges(cells: Any) -> tuple[Any, Any]:
    k = int(cells.shape[1])
    pairs = np.vstack([cells[:, [i, (i + 1) % k]] for i in range(k)])
    return np.unique(np.sort(pairs, axis=1), axis=0, return_counts=True)


def _min_angle_deg(points: Any, cells: Any, scale: tuple[float, float]) -> float:
    xy = points * np.asarray(scale, dtype=float)
    k = int(cells.shape[1])
    smallest = 180.0
    for i in range(k):
        here = xy[cells[:, i]]
        back = xy[cells[:, (i - 1) % k]] - here
        fwd = xy[cells[:, (i + 1) % k]] - here
        norms = np.linalg.norm(back, axis=1) * np.linalg.norm(fwd, axis=1)
        good = norms > 0.0
        if not good.any():
            continue
        cosine = np.clip((back * fwd).sum(axis=1)[good] / norms[good], -1.0, 1.0)
        smallest = min(smallest, float(np.degrees(np.arccos(cosine)).min()))
    return smallest


def _boundary_loops(boundary: Any) -> int:
    adjacency: dict[int, list[int]] = {}
    for a, b in boundary:
        adjacency.setdefault(int(a), []).append(int(b))
        adjacency.setdefault(int(b), []).append(int(a))
    seen: set[int] = set()
    loops = 0
    for node in adjacency:
        if node in seen:
            continue
        loops += 1
        stack = [node]
        while stack:
            here = stack.pop()
            if here in seen:
                continue
            seen.add(here)
            stack.extend(n for n in adjacency[here] if n not in seen)
    return loops


def _probes(mesh: Mesh, chain: Sequence[str]) -> dict[str, Any]:
    pts = np.asarray(mesh.points, dtype=float)
    cells = np.asarray(mesh.cells, dtype=np.int64)
    scale = _metre_scale(mesh)
    edges, counts = _unique_edges(cells)
    delta = (pts[edges[:, 0]] - pts[edges[:, 1]]) * np.asarray(scale, dtype=float)
    lengths = np.hypot(delta[:, 0], delta[:, 1])
    hist, bin_edges = np.histogram(lengths, bins=10)
    boundary = edges[counts == 1]
    return {
        "node_count": mesh.node_count,
        "element_count": mesh.element_count,
        "nodes_per_cell": mesh.nodes_per_cell,
        "crs_authid": mesh.crs_authid,
        "has_bed": mesh.has_bed,
        "edge_length_m": {
            "min": float(lengths.min()), "max": float(lengths.max()),
            "mean": float(lengths.mean()),
            "histogram": {"counts": [int(c) for c in hist],
                          "bin_edges": [float(b) for b in bin_edges]},
        },
        "min_angle_deg": _min_angle_deg(pts, cells, scale),
        "boundary_edges": int(boundary.shape[0]),
        "boundary_nodes": int(np.unique(boundary).size),
        "boundary_loops": _boundary_loops(boundary),
        "edits_applied": list(chain),
    }
