"""``MeshSession`` - the mesh under construction, and the recipe that IS its state.

The session holds ONE recipe. Every change - appending an op, altering one by
index, removing one, resetting to the declaration - swaps that recipe and
regenerates the mesh WHOLESALE; nothing is patched incrementally, because a mesh
patched from a program that no longer describes it is a mesh nobody can rebuild.

The journal beside the mesh files (``mesh_recipe.jsonl``) is append-only and is
AUDIT, not state: the recipe as declared on the first line, then one line per
edit event carrying the recipe that event produced. Undo is editing the recipe
back; the one structured revert is :meth:`reset`.

A hand-edit is the one operation that is genuinely history rather than program:
the nodes were dragged, and no recipe produces that. It is ADOPTED and the mesh
is flagged - regen would break it - rather than pretended into the recipe.
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

from trid3nt_server.emission.mesh_display import mesh_display_path, write_2dm
from trid3nt_server.workflows.mesh.grid_geometry import M_PER_DEG_LAT
from trid3nt_server.workflows.mesh.artifact import (
    MeshArtifact,
    stash_mesh_artifact,
    write_mesh_artifact_sidecar,
)
from trid3nt_server.workflows.mesh.meshers import (
    Mesh,
    Mesher,
    MeshOp,
    MeshToolError,
    get_mesher,
    input_digest,
)
from trid3nt_server.workflows.mesh.recipe import MeshRecipe

logger = logging.getLogger("trid3nt_server.workflows.mesh.session")

__all__ = ["MeshSession", "mesh_digest", "replay_recipe"]


class MeshSession:
    """A mesh being built: edit the recipe, probe, reset, accept."""

    def __init__(self, recipe: MeshRecipe, *,
                 workdir: str | os.PathLike[str] | None = None,
                 case_id: str | None = None, name: str | None = None) -> None:
        self.declared = recipe
        self.recipe = recipe
        self.mesher: Mesher = get_mesher(recipe.mesher)
        self.mesh_id = new_ulid()
        self.case_id = case_id
        self.name = name or f"{recipe.mesher} mesh"
        self.workdir = Path(workdir) if workdir is not None else (
            Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"mesh-{self.mesh_id}")
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._mesh: Mesh | None = None
        self._display: tuple[Path, str] | None = None
        self._events: list[dict[str, Any]] = []
        #: Why this mesh cannot be regenerated from its recipe, once a hand-edit
        #: has replaced the topology the recipe produces.
        self.regen_note: str | None = None

    # -- the mesh ---------------------------------------------------------- #
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
            self._mesh = _build(self.mesher, self.recipe)
            self._journal()

    def _regenerate(self, event: dict[str, Any]) -> dict[str, Any]:
        """Swap the recipe and rebuild the whole mesh -> the new probes."""
        if self.regen_note is not None:
            raise MeshToolError(
                "MESH_REGEN_WOULD_DISCARD_HAND_EDIT", self.regen_note)
        self._mesh = None
        self._display = None
        self._events.append(event)
        self._ensure_built()
        return self.probes()

    # -- editing the recipe ------------------------------------------------ #
    def append_op(self, op: MeshOp) -> dict[str, Any]:
        """Add one op to the end of the recipe and regenerate -> the probes."""
        self.recipe = self.recipe.appending(op)
        return self._regenerate({"event": "append", "op": repr(op)})

    def alter_op(self, index: int, op: MeshOp) -> dict[str, Any]:
        """Replace the op at ``index`` and regenerate -> the probes."""
        self.recipe = self.recipe.altering(int(index), op)
        return self._regenerate(
            {"event": "alter", "index": int(index), "op": repr(op)})

    def remove_op(self, index: int) -> dict[str, Any]:
        """Drop the op at ``index`` and regenerate -> the probes."""
        dropped = repr(self.recipe.ops[int(index)]) if 0 <= int(index) < len(
            self.recipe.ops) else None
        self.recipe = self.recipe.without(int(index))
        return self._regenerate(
            {"event": "remove", "index": int(index), "op": dropped})

    def set_params(self, **params: Any) -> dict[str, Any]:
        """Replace the recipe's agnostic params and regenerate -> the probes."""
        self.recipe = self.recipe.with_params(**params)
        return self._regenerate({"event": "params", **{
            k: str(v) for k, v in params.items()}})

    def reset(self) -> dict[str, Any]:
        """Put the recipe back to the DECLARATION and rebuild -> the probes.

        The one structured revert. What the template declared survives;
        everything appended, altered or removed at this gate does not.
        """
        self.recipe = self.declared
        self.regen_note = None
        return self._regenerate({"event": "reset"})

    def adopt_layer(self, layer: str) -> dict[str, Any]:
        """Adopt a hand-edited ``.2dm`` as this mesh -> the probes.

        HISTORY, not program. The change lives in the layer's bytes, so the
        recipe cannot produce it: the mesh is flagged instead, and any later
        recipe edit refuses rather than silently throwing the hand-edit away.
        """
        import dataclasses

        from trid3nt_server.workflows.mesh.shared.nodes import read_2dm_mesh

        mesh = self.mesh
        if not mesh.has_cells:
            raise MeshToolError(
                "MESH_ADOPT_NOT_STAGEABLE",
                "this mesh states no cells of its own - the engine realizes them "
                "- so an adopted layer cannot be reconciled with what a solve "
                "would be staged from.")
        points, cells, z = read_2dm_mesh(str(layer))
        # A .2dm always carries a node z column, so the edited layer cannot say
        # whether a bed was ever painted; the mesh it replaces is what knows. The
        # per-solver files and the probes described the cells the edit replaced,
        # so they are dropped rather than carried onto a different topology.
        carried = {k: v for k, v in mesh.meta.items()
                   if k not in ("files", "probes", "lonlat")}
        self._mesh = dataclasses.replace(
            mesh, points=points, cells=cells,
            bed=(z if mesh.has_bed else None), meta=carried)
        self._display = None
        self.regen_note = (
            f"this mesh was hand-edited at the gate (layer digest "
            f"{input_digest(layer)}); its topology is not what the recipe "
            "produces, so regenerating would discard the edit. Accept it, or "
            "reset to the declared recipe and start again.")
        self._events.append({"event": "adopt", "source": str(layer),
                             "digest": input_digest(layer),
                             "replayable": False})
        self._journal()
        return self.probes()

    # -- the loop ---------------------------------------------------------- #
    def probes(self) -> dict[str, Any]:
        """The numeric facts a human or an agent judges the mesh on."""
        return _probes(self.mesh, self.recipe)

    def snapshot(self) -> LayerURI:
        """The mesh's DISPLAY face for the current recipe, as a map layer.

        A node/cell mesh is an MDAL ``.2dm``; a mesh whose cells the engine
        re-realizes carries the display face its own mesher wrote, and the row
        names the type that file actually is.
        """
        _, uri = self._display_face()
        mesh = self.mesh
        return LayerURI(
            layer_id=f"mesh-{self.mesh_id}", name=f"Mesh: {self.name}",
            layer_type=("mesh" if mesh.has_cells else "vector"), uri=uri,
            style_preset="mesh_wireframe", role="primary",
            bbox=_lonlat_bbox(mesh), crs_authid=mesh.crs_authid,
            synthetic_inputs=_synthetic_inputs(mesh),
            fallback_note=mesh.meta.get("fallback_note"))

    def accept(self) -> MeshArtifact:
        """Freeze the current mesh as a case artifact -> the :class:`MeshArtifact`.

        The RECIPE is frozen onto the artifact as its provenance: what a rebuild
        would run, beside the facts only the MESHER knows - what painted its bed,
        which stretch it opened - which ride in on the mesh's own ``meta``.
        """
        mesh = self.mesh
        declared = dict(mesh.meta.get("artifact") or {})
        # The counts are already read through the mesh's own properties; passing
        # them again would name one field twice.
        declared.pop("node_count", None)
        declared.pop("element_count", None)
        _, display_uri = self._display_face()
        slf_uri, cli_uri = self._telemac_pair()
        recipe_uri = self._stage(self.recipe_path)
        has_bed = mesh.has_bed
        declared.pop("engine_compat", None)
        provenance = {"mesher": self.mesher.name,
                      "recipe": self.recipe.to_json(),
                      "deterministic": self.mesher.deterministic,
                      # WHAT ACTUALLY PAINTED THE BED, not what the recipe asked
                      # for: the ladder rung that served is the only statement a
                      # reader downstream can tell a coarse global relief from the
                      # surveyed topobathy by, and the op that painted it is the
                      # only thing that knows.
                      **({"bed_source": str(mesh.meta["bed_source"])}
                         if mesh.meta.get("bed_source") else {}),
                      **({"regen_note": self.regen_note}
                         if self.regen_note else {}),
                      **dict(declared.pop("provenance", None) or {})}
        art = MeshArtifact(
            mesh_id=self.mesh_id, name=self.name, mode=self.mesher.name,
            display_uri=display_uri, slf_uri=slf_uri,
            crs_authid=mesh.crs_authid, has_bathymetry=has_bed,
            node_count=mesh.node_count, element_count=mesh.element_count,
            bbox=_lonlat_bbox(mesh),
            utm_epsg=mesh.meta.get("utm_epsg"), provenance=provenance,
            probes=self.probes(),
            recipe_uri=recipe_uri, case_id=self.case_id,
            **({"cli_uri": cli_uri} if cli_uri else {}),
            **self._staged_files(mesh), **declared)
        stash_mesh_artifact(self.case_id, art)
        if str(display_uri).startswith("s3://"):
            write_mesh_artifact_sidecar(art, _s3_client())
        logger.info("build_mesh: %s mesh %s -> %d nodes %d elements %s (bed=%s)",
                    self.mesher.name, self.mesh_id, art.node_count,
                    art.element_count, art.crs_authid, has_bed)
        return art

    # -- files ------------------------------------------------------------- #
    def _display_face(self) -> tuple[Path, str]:
        """The ``.2dm`` this mesh renders as, written and staged ONCE per build.

        Not a cache of STATE - the recipe is that, and it regenerates wholesale.
        This memoizes a WRITE: the display face is asked for by every present, by
        the snapshot and twice more by accept, and each miss is a file write plus
        an object-store put. It is dropped on every regeneration and on an
        adopted layer, so it can never answer for a mesh that no longer exists.
        """
        if self._display is None:
            declared = mesh_display_path(self.mesh)
            local = Path(declared) if declared else self.workdir / "mesh.2dm"
            if declared is None:
                local.write_text(write_2dm(self.mesh))
            self._display = (local, self._stage(local))
        return self._display

    def _telemac_pair(self) -> tuple[str | None, str | None]:
        """The TELEMAC geometry AND its ``.cli``, when this mesh IS one.

        The two are ONE artifact - the ``.cli`` rows are ordered by the geometry's
        own IPOBO - so they are written together by the shared telapy driver
        rather than by a byte layout this file would have to maintain. A mesher
        that already wrote its own pair keeps it; nothing is re-derived.
        """
        mesh = self.mesh
        files = dict(mesh.meta.get("files") or {})
        declared = files.get("slf_uri")
        if declared:
            return self._stage(Path(declared)), None
        if mesh.nodes_per_cell != 3 or not mesh.has_bed:
            return None, None
        from trid3nt_server.workflows.mesh.shared.selafin_cli import write_telemac_pair

        written = write_telemac_pair(
            self.workdir, x=mesh.points[:, 0], y=mesh.points[:, 1],
            cells=mesh.cells, bed=mesh.bed, title=f"TRID3NT {self.mesher.name}")
        cli = written["cli"]
        return self._stage(written["geo_slf"]), (
            self._stage(cli) if not files.get("cli_uri") else None)

    def _staged_files(self, mesh: Mesh) -> dict[str, Any]:
        """Stage the per-solver files the mesher wrote.

        ``meta["files"]`` maps an artifact URI field to a LOCAL path; each one lands
        beside this mesh's other objects, so a mesher's own output is never a second
        store.
        """
        out: dict[str, Any] = {}
        for name, local in dict(mesh.meta.get("files") or {}).items():
            if name in ("slf_uri", "display_uri") or not local:
                continue
            out[name] = self._stage(Path(local))
        return out

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

    # -- the journal ------------------------------------------------------- #
    def recipe_lines(self) -> list[dict[str, Any]]:
        """The journal as records: the declaration, then one per edit event.

        A mesher whose library does not reproduce itself says so on the first
        line, so a replay is read as an equivalent rebuild rather than as a
        promise of the same mesh.
        """
        head: dict[str, Any] = {"recipe": self.declared.to_json()}
        if not self.mesher.deterministic:
            head["determinism"] = False
        lines = [head]
        for event in self._events:
            lines.append({**event, "recipe": self.recipe.to_json()})
        return lines

    def _journal(self) -> None:
        self.recipe_path.write_text(
            "".join(json.dumps(line) + "\n" for line in self.recipe_lines()))


def _s3_client() -> Any:
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    return _get_s3_client()


# --------------------------------------------------------------------------- #
# Building and replaying.
# --------------------------------------------------------------------------- #
def _build(mesher: Mesher, recipe: MeshRecipe) -> Mesh:
    """The whole mesh, from the whole recipe. There is no incremental path."""
    unbound = recipe.unbound
    if unbound:
        raise MeshToolError(
            "MESH_RECIPE_UNBOUND",
            f"{sorted(unbound)} are late-bound reads rather than values, so this "
            "mesh cannot be built: bind the declaration against a resolved sheet "
            "before demanding the mesh.")
    return mesher.build(recipe)


def replay_recipe(source: str | os.PathLike[str] | Sequence[Mapping[str, Any]]
                  ) -> Mesh:
    """Rebuild a mesh from its journal -> the :class:`Mesh` its recipe describes.

    The LAST recipe the journal records is the one that produced the mesh, which
    is what a replay runs. A recorded hand-edit REFUSES: its change is in the
    layer's bytes, not in the recipe, so replaying would return a different mesh
    under the same record.
    """
    if isinstance(source, (str, os.PathLike)):
        lines = [json.loads(ln) for ln in
                 Path(source).read_text().splitlines() if ln.strip()]
    else:
        lines = [dict(ln) for ln in source]
    if not lines or "recipe" not in lines[0]:
        raise MeshToolError(
            "MESH_RECIPE_MALFORMED",
            "a mesh journal starts with the recipe it was declared from; this "
            "one does not.")
    adopted = [ln for ln in lines if ln.get("event") == "adopt"]
    if adopted:
        raise MeshToolError(
            "MESH_RECIPE_NOT_REPLAYABLE",
            f"this mesh was hand-edited at the gate ({adopted[-1].get('source')}, "
            f"digest {adopted[-1].get('digest')}); the topology it carries is not "
            "what its recipe produces, so it cannot be rebuilt from the record. "
            "Keep the accepted mesh, or rebuild from the recipe and edit again.")
    recipe = MeshRecipe.from_json(lines[-1]["recipe"])
    return _build(get_mesher(recipe.mesher), recipe)


# --------------------------------------------------------------------------- #
# Display face + probes.
# --------------------------------------------------------------------------- #
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


def _area_km2(points: Any, cells: Any, scale: tuple[float, float]) -> float:
    """The MESHED domain's own area, summed over its cells, in km2.

    Measured on the accepted topology rather than on the polygon the ask was cut
    from: a catchment's runoff coefficient divides by the area the solve actually
    covered, and the two differ by whatever the triangulation trimmed.
    """
    xy = points * np.asarray(scale, dtype=float)
    k = int(cells.shape[1])
    origin = xy[cells[:, 0]]
    twice = np.zeros(cells.shape[0], dtype=float)
    for i in range(1, k - 1):
        a = xy[cells[:, i]] - origin
        b = xy[cells[:, i + 1]] - origin
        twice += a[:, 0] * b[:, 1] - b[:, 0] * a[:, 1]
    return float(np.abs(twice).sum() / 2.0 / 1.0e6)


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


def _synthetic_inputs(mesh: Mesh) -> list[Any]:
    """The mesher's own input rows for the layer, built from what it declared.

    A build that substituted a dataset or read a real source says so ON the layer;
    a mesher that declared nothing contributes nothing rather than a manufactured
    row.
    """
    from trid3nt_contracts.common import SyntheticInput

    return [SyntheticInput(**dict(row))
            for row in (mesh.meta.get("synthetic_inputs") or [])]


def _probes(mesh: Mesh, recipe: MeshRecipe) -> dict[str, Any]:
    if not mesh.has_cells:
        # The cells are the engine's to realize from the staged authoring inputs,
        # so every edge-derived probe would be a statement about a topology this
        # process never saw. What it can measure is what it reports.
        return {
            "node_count": mesh.node_count,
            "element_count": mesh.element_count,
            "nodes_per_cell": 0,
            "crs_authid": mesh.crs_authid,
            "has_bed": mesh.has_bed,
            "cells_realized_by_engine": True,
            **dict(mesh.meta.get("probes") or {}),
            "ops": recipe.numbered(),
        }
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
        "area_km2": _area_km2(pts, cells, scale),
        "boundary_edges": int(boundary.shape[0]),
        "boundary_nodes": int(np.unique(boundary).size),
        "boundary_loops": _boundary_loops(boundary),
        # What the MESHER measured about its own build - island count, how much of
        # the mapped water the domain covered, which boundary it opened. Nothing
        # here can be recomputed from nodes and cells alone.
        **dict(mesh.meta.get("probes") or {}),
        "ops": recipe.numbered(),
    }
