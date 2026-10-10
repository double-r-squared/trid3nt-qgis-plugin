"""The declared MESH: a template's ``tool.build_mesh`` ask, built under the gate
or taken from the keep.

The RECIPE travels WHOLE - its mesher, its kind, its extent, its size word and
every op in declared order. A built mesh is kept under a digest of the CONTENT
it is built from, so a run that changes nothing the mesher reads reuses it."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from trid3nt_server.tools.mesh.artifact import MeshArtifact, measured_min_edge_m

logger = logging.getLogger("trid3nt_server.tools.mesh.step")

__all__ = ["GATE_LABEL", "build_declared_mesh", "keep_mesh", "mesh_key",
           "mesh_record"]

#: The label the mesh gate's card carries. It names the ASK - the mesh this run
#: is about to solve on - never the template that demanded it.
GATE_LABEL = "build_mesh"

#: The store collection a built mesh is kept in, by its content key.
_KEPT = "kept_meshes"

#: What names a thing rather than holding it: a produced layer's name carries a
#: random seed, so two fetches of one surface differ here and nowhere else.
_NAMING = frozenset({"name", "label", "layer_id", "title"})


async def build_declared_mesh(*, mesh: dict[str, Any], name: Any = None,
                              supplied: Any = None,
                              tool: Any = None,
                              input_mode: str | None = None,
                              fresh: bool = False) -> dict[str, Any]:
    """The mesh a solve runs on -> the accepted mesh's record, with its ``key``.

    A SUPPLIED mesh is adopted whole; one KEPT under this recipe's content key
    is reused unless ``fresh``; otherwise one is built, gated in the run's mode."""
    from trid3nt_server.render.pipeline_emitter import current_turn_case
    from trid3nt_server.tools.mesh.gate import gate_mesh_build
    from trid3nt_server.tools.mesh.session import MeshSession
    from trid3nt_server.tools.mesh.tool import (
        recipe_from_plan_value,
        supplied_mesh_artifact,
    )
    from trid3nt_server.workflows.runtime import journal_note

    art = None
    if supplied:
        art = await asyncio.to_thread(supplied_mesh_artifact, supplied,
                                      tool_name=str(tool or ""))
    if art is not None:
        logger.info("mesh supplied: %s -> %d nodes / %d elements",
                    art.mesh_id, art.node_count, art.element_count)
        return mesh_record(art)
    key = await asyncio.to_thread(mesh_key, mesh)
    kept = await _kept(key)
    if kept is not None:
        art, run = kept
        builder = run or "that did not reach its solve"
        if fresh:
            journal_note(f"a mesh is kept under content key {key}, built by run "
                         f"{builder}; restart_clean builds it again")
        else:
            journal_note(f"the mesh kept under content key {key} is reused, "
                         f"built by run {builder}: nothing the mesher reads has "
                         "changed")
            return {**mesh_record(art), "key": key, "reused": True,
                    "built_by": run}
    recipe = recipe_from_plan_value(mesh)
    session = await asyncio.to_thread(
        MeshSession, recipe, case_id=current_turn_case(),
        name=_session_name(name, recipe.mesher))
    art = await gate_mesh_build(session, tool_name=GATE_LABEL,
                                input_mode=input_mode)
    logger.info("mesh accepted: %s -> %d nodes / %d elements, min edge %s m",
                art.mesh_id, art.node_count, art.element_count,
                measured_min_edge_m(art))
    await asyncio.to_thread(_mesh_coverage, recipe.extent, art)
    return {**mesh_record(art), "key": key}


def mesh_key(recipe: Mapping[str, Any]) -> str:
    """The CONTENT key of a bound recipe: the domain's geometry, the bytes of every file
    the mesher reads, the resolution, the mesher and its own ops. A file enters by what it holds, never by its name or uri."""
    blob = json.dumps(_content(recipe), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _content(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return {"bytes": _file_digest(value)} if _is_file(value) else value
    if isinstance(value, Mapping):
        return {str(k): _content(v) for k, v in value.items()
                if str(k) not in _NAMING}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_content(v) for v in value]
    fields = getattr(type(value), "__dataclass_fields__", None)
    if fields:
        return _content({name: getattr(value, name) for name in fields
                         if name != "uri"})
    uri = getattr(value, "uri", None)
    if isinstance(uri, str) and _is_file(uri):
        return {"bytes": _file_digest(uri)}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _content(dump(mode="json"))
    return {"is": f"{type(value).__module__}.{type(value).__name__}"}


def _is_file(text: str) -> bool:
    return text.startswith("s3://") or ("://" not in text and os.path.isfile(text))


def _file_digest(uri: str) -> str:
    """What a file HOLDS, as a digest of its bytes."""
    digest = hashlib.sha256()
    if uri.startswith("s3://"):
        from trid3nt_server.store import objects as storage

        bucket, _, key = uri[len("s3://"):].partition("/")
        body = storage.client().get_object(Bucket=bucket, Key=key)["Body"]
        for chunk in iter(lambda: body.read(1 << 20), b""):
            digest.update(chunk)
    else:
        with open(uri, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()


async def keep_mesh(record: Mapping[str, Any], run: str | None = None) -> None:
    """Keep the mesh ``record`` holds under its content key, with the run that
    built it; a supplied mesh has no key, and a reused one keeps its builder."""
    from trid3nt_server.store.cases import DEFAULT_DATABASE, FileMCPClient

    key, art = record.get("key"), record.get("artifact")
    if not key or art is None or record.get("reused"):
        return
    await FileMCPClient().call_tool("update-one", {
        "database": DEFAULT_DATABASE, "collection": _KEPT,
        "filter": {"_id": key},
        "update": {"$set": {"_id": key, "mesh": art.to_json(), "run": run,
                            "kept_at": datetime.now(timezone.utc).isoformat()}},
        "upsert": True})


async def _kept(key: str) -> tuple[MeshArtifact, str | None] | None:
    """The mesh kept under ``key`` and the run that built it, or ``None`` where
    nothing is kept or a file it names is gone from the store."""
    from trid3nt_server.store.cases import DEFAULT_DATABASE, FileMCPClient

    found = await FileMCPClient().call_tool("find-one", {
        "database": DEFAULT_DATABASE, "collection": _KEPT,
        "filter": {"_id": key}})
    doc = (found or {}).get("document") if isinstance(found, dict) else None
    if not doc:
        return None
    art = MeshArtifact.from_json(doc.get("mesh") or {})
    files = [art.display_uri, *(art.engine_files or {}).values()]
    live = await asyncio.to_thread(lambda: all(_live(uri) for uri in files if uri))
    if not live:
        logger.info("the mesh kept under %s names a file that is gone; it is "
                    "built again", key)
        return None
    return art, doc.get("run")


def _live(uri: str) -> bool:
    if uri.startswith("s3://"):
        from trid3nt_server.store import objects as storage

        bucket, _, key = uri[len("s3://"):].partition("/")
        try:
            storage.client().head_object(Bucket=bucket, Key=key)
        except Exception:  # noqa: BLE001 - a file the store cannot show is not reused
            return False
        return True
    return "://" in uri or os.path.exists(uri)


def _mesh_coverage(extent: Any, art: Any) -> None:
    """Say how much of the domain's own centerline the built cells actually hold; a drawn outline has none, so nothing is said."""
    from trid3nt_server.inputs.geometry import covered_fraction
    from trid3nt_server.workflows.runtime import journal_note

    line = (getattr(extent, "companions", None) or {}).get("centerline")
    if not line or not art.display_uri:
        return
    try:
        measured = covered_fraction(line, art.display_uri,
                                    within_epsg=art.utm_epsg)
    except Exception as exc:  # noqa: BLE001 - an unmeasurable overlap is not a build fault
        logger.info("mesh coverage not measured (%s)", exc)
        return
    journal_note(measured["note"])


def mesh_record(art: Any) -> dict[str, Any]:
    """One accepted mesh, as every consumer of the mesh step reads it."""
    return {
        "artifact": art,
        "mesh_id": art.mesh_id,
        "engine_files": dict(art.engine_files or {}),
        "boundary_roles": dict(art.boundary_roles or {}),
        "display_uri": art.display_uri,
        "recipe_uri": art.recipe_uri,
        "node_count": art.node_count,
        "element_count": art.element_count,
        "min_edge_m": measured_min_edge_m(art),
        "provenance": dict(art.provenance or {}),
    }


def _session_name(name: Any, mesher: str) -> str:
    """What the gate card calls this mesh: the step's own name, else the mesher's.

    A mapping ``name`` is read for its ``name``/``slug`` key, never stringified."""
    if isinstance(name, dict):
        name = name.get("name") or name.get("slug")
    text = str(name or "").strip()
    return f"{text} mesh" if text else f"{mesher} mesh"
