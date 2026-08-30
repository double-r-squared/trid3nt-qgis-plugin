"""Case-scoped mesh artifact record + discovery seam.

A mesh ``build_mesh`` built is TWO things in one case:

  * a DISPLAY layer -- an MDAL-loadable ``.2dm`` (``layer_type="mesh"``) that
    lands in ``loaded_layers`` via the normal ``LayerURI`` auto-emit path, so a
    human sees the wireframe in QGIS; and
  * an ARTIFACT record -- the facts (format URIs, CRS, bathymetry, node/element
    counts, measured probes, open-boundary info) a run needs to decide "can I
    solve on this?" and, if the user accepts, to point its solver at it.

This module is the ARTIFACT half. It does NOT invent a parallel store: the facts
ride TWO existing seams so both a same-session run and a cold reopen can find them:

  * a module side-table keyed by ``case_id`` (mirrors ``publish_layer``'s
    ``_LAST_LEGEND_BY_URI`` stash) -- the fast, same-daemon-process path the
    build door reads when the mesh was built earlier in the SAME case; and
  * a durable ``mesh_artifact.json`` SIDECAR written next to the mesh objects in
    the cache bucket -- discoverable from any ``layer_type="mesh"`` row's ``uri``
    in a later session (its key is the mesh key with the basename swapped).

Whether a mesh is USABLE is the artifact's own question and it answers it:
:meth:`MeshArtifact.unsolvable_reason` reads the facts this record already
carries. WHICH KINDS a pipeline accepts is a different question with a different
owner - the template's ``Accepts`` row, read at the supply door - so the two are
never conflated into one engine table.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.mesh.artifact")

__all__ = [
    "MeshArtifact",
    "stash_mesh_artifact",
    "stashed_mesh_artifacts",
    "sidecar_key_for_mesh_uri",
    "write_mesh_artifact_sidecar",
    "read_mesh_artifact_sidecar",
    "find_case_mesh_artifacts",
    "measured_min_edge_m",
]


@dataclass
class MeshArtifact:
    """A computational mesh built into a case, and what it can be solved on.

    ``display_uri`` is the MDAL ``.2dm`` (``s3://``) the ``layer_type="mesh"`` row
    carries; ``slf_uri`` is the per-solver geometry file (``None`` when the format
    was not emitted). ``crs_authid`` is the mesh CRS (a projected UTM authid, e.g.
    ``"EPSG:32617"``); ``has_bathymetry`` is True
    when node elevations were sampled (a solve-ready bed). ``open_boundary_info``
    records the segmented open/land boundary (``{}`` for a fully-closed inland
    catchment). ``recipe_uri`` points at the ``mesh_recipe.jsonl`` this mesh was
    built from - spec plus ordered edits, the record a rebuild replays."""

    mesh_id: str
    name: str
    mode: str  # the MESHER that built it: om2d | reg_grid
    display_uri: str  # s3:// display face (a .2dm mesh, or a cell-polygon vector)
    slf_uri: str | None
    crs_authid: str
    has_bathymetry: bool
    node_count: int
    element_count: int
    bbox: tuple[float, float, float, float]
    #: The UTM zone the mesh's own coordinates are in; ``None`` when the mesh is not
    #: in a projected UTM CRS (a geographic lattice has no zone).
    utm_epsg: int | None = None
    #: The ``mesh_recipe.jsonl`` (spec + ordered edit chain) this mesh replays from.
    recipe_uri: str | None = None
    #: What was MEASURED on the accepted topology - counts, the edge-length band and
    #: its histogram, min angle, boundary segments, plus whatever the mesher measured
    #: about its own build. A consumer that needs the finest edge reads it here
    #: rather than re-deriving it from the ask, which is only what was requested.
    probes: dict[str, Any] = field(default_factory=dict)
    #: The TELEMAC boundary-conditions file written from THIS geometry's own
    #: boundary numbering; only valid against the ``slf_uri`` beside it.
    cli_uri: str | None = None
    #: The mesher's record of the ACCEPTED TOPOLOGY, for an engine whose solve
    #: needs more than a geometry file states. A SELAFIN says which nodes lie on a
    #: boundary and never which stretch of it is the inflow, so a mesh whose
    #: boundary carries roles ships them here beside the geometry.
    topology_uri: str | None = None
    outlet_lonlat: tuple[float, float] | None = None
    pour_point_lonlat: tuple[float, float] | None = None
    open_boundary_info: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None

    def unsolvable_reason(self) -> str | None:
        """WHY no solve can be staged on this mesh, or ``None`` when one can.

        A readiness property of the artifact, read off the facts it already
        carries: a shallow-water solve needs a geometry file and a bed under it.
        A mesh whose bed is fitted onto a staged topology at authoring time
        carries that bundle instead, and says so by carrying ``topology_uri``.

        This is not a compatibility CONTRACT - which kinds a pipeline accepts is
        the template's ``Accepts`` row and is checked at the supply door. This is
        the artifact answering for itself.
        """
        if not self.slf_uri:
            return (f"mesh {self.name!r} carries no SELAFIN geometry (it was "
                    f"built as mode={self.mode!r}), so there is no file a solve "
                    "could be staged from")
        if not self.has_bathymetry and not self.topology_uri:
            return (f"mesh {self.name!r} has no sampled bed and no staged "
                    "topology to fit one onto, so a shallow-water solve has no "
                    "ground to start from")
        return None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, doc: dict[str, Any]) -> "MeshArtifact":
        fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in (doc or {}).items() if k in fields}
        for key in ("bbox", "outlet_lonlat", "pour_point_lonlat"):
            if isinstance(clean.get(key), list):
                clean[key] = tuple(clean[key])
        return cls(**clean)


def measured_min_edge_m(art: MeshArtifact | None) -> float | None:
    """The SHORTEST edge measured on this mesh, in metres; ``None`` when unmeasured.

    A stability criterion is a statement about the mesh that exists, not about the
    edge somebody asked for - and gate-time refinement moves the two apart. A mesh
    whose cells the engine realizes has no edges here to measure, and says so by
    answering None rather than by quoting the ask back."""
    edges = ((art.probes if art is not None else None) or {}).get("edge_length_m")
    if not isinstance(edges, dict):
        return None
    try:
        value = float(edges.get("min"))
    except (TypeError, ValueError):
        return None
    return value if value > 0.0 else None


# --------------------------------------------------------------------------- #
# Same-process stash (case-keyed), mirroring publish_layer._LAST_LEGEND_BY_URI.
# --------------------------------------------------------------------------- #
_MAX_CASES: int = 64
_MAX_PER_CASE: int = 16
_CASE_MESH_ARTIFACTS: dict[str, list[MeshArtifact]] = {}


def stash_mesh_artifact(case_id: str | None, art: MeshArtifact) -> None:
    """Record a just-built mesh artifact under its case (FIFO-bounded).

    A ``None``/empty case_id is dropped (a headless direct-call with no case has
    no case-scoped consumer to discover it -- the sidecar still persists it)."""
    if not case_id:
        return
    bucket = _CASE_MESH_ARTIFACTS.setdefault(case_id, [])
    bucket.append(art)
    while len(bucket) > _MAX_PER_CASE:
        bucket.pop(0)
    while len(_CASE_MESH_ARTIFACTS) > _MAX_CASES:
        _CASE_MESH_ARTIFACTS.pop(next(iter(_CASE_MESH_ARTIFACTS)))


def stashed_mesh_artifacts(case_id: str | None) -> list[MeshArtifact]:
    """Same-process mesh artifacts for a case (most-recent last)."""
    if not case_id:
        return []
    return list(_CASE_MESH_ARTIFACTS.get(case_id, []))


# --------------------------------------------------------------------------- #
# Durable sidecar (co-located with the mesh objects; no parallel store).
# --------------------------------------------------------------------------- #
def sidecar_key_for_mesh_uri(mesh_uri: str) -> tuple[str, str] | None:
    """``s3://bucket/prefix/mesh.2dm`` -> ``(bucket, prefix/mesh_artifact.json)``.

    The sidecar is the mesh key with the basename swapped, so ANY mesh row's
    ``uri`` resolves its facts without a separate index. ``None`` for a
    non-``s3://`` uri."""
    if not mesh_uri.startswith("s3://"):
        return None
    rest = mesh_uri[len("s3://"):]
    slash = rest.find("/")
    if slash < 0:
        return None
    bucket = rest[:slash]
    key = rest[slash + 1:]
    prefix = key.rsplit("/", 1)[0] if "/" in key else ""
    sidecar = f"{prefix}/mesh_artifact.json" if prefix else "mesh_artifact.json"
    return bucket, sidecar


def write_mesh_artifact_sidecar(art: MeshArtifact, s3_client: Any) -> str | None:
    """Persist ``art`` as ``mesh_artifact.json`` beside its mesh objects.

    Best-effort: a staging failure NEVER fails a mesh build (the same-process
    stash still carries the facts for this session). Returns the sidecar
    ``s3://`` uri, or ``None`` if it could not be derived/written."""
    loc = sidecar_key_for_mesh_uri(art.display_uri)
    if loc is None:
        return None
    bucket, key = loc
    try:
        s3_client.put_object(
            Bucket=bucket, Key=key,
            Body=json.dumps(art.to_json(), indent=2).encode("utf-8"),
            ContentType="application/json")
    except Exception as exc:  # noqa: BLE001 -- sidecar is durability, not correctness
        logger.warning("mesh artifact sidecar write failed for %s: %s",
                       art.display_uri, exc)
        return None
    return f"s3://{bucket}/{key}"


def read_mesh_artifact_sidecar(mesh_uri: str, s3_client: Any) -> MeshArtifact | None:
    """Read the ``mesh_artifact.json`` sidecar for a mesh row's ``uri``.

    The cross-session discovery path: a ``layer_type="mesh"`` row persisted in a
    reopened case resolves its facts from the object store. Best-effort -> ``None``
    on any miss."""
    loc = sidecar_key_for_mesh_uri(mesh_uri)
    if loc is None:
        return None
    bucket, key = loc
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        doc = json.loads(obj["Body"].read().decode("utf-8"))
        return MeshArtifact.from_json(doc)
    except Exception as exc:  # noqa: BLE001 -- absent/unreadable sidecar
        logger.debug("mesh artifact sidecar read miss for %s: %s", mesh_uri, exc)
        return None


def find_case_mesh_artifacts(
    *, case_id: str | None = None, loaded_mesh_uris: list[str] | None = None,
    s3_client: Any = None,
) -> list[MeshArtifact]:
    """Discover mesh artifacts available in the active case (most-recent last).

    Stash-first (same daemon process), then, for any persisted
    ``layer_type="mesh"`` row uri NOT already covered by the stash, the durable
    sidecar. ``case_id`` defaults to the active turn's case; ``loaded_mesh_uris``
    are the mesh-row uris the caller read off the emitter (kept as a parameter so
    this module never imports the emitter)."""
    if case_id is None:
        try:
            from trid3nt_server.emission.pipeline_emitter import current_turn_case
            case_id = current_turn_case()
        except Exception:  # noqa: BLE001
            case_id = None

    out: list[MeshArtifact] = list(stashed_mesh_artifacts(case_id))
    covered = {a.display_uri for a in out}
    if loaded_mesh_uris and s3_client is not None:
        for uri in loaded_mesh_uris:
            if uri in covered:
                continue
            art = read_mesh_artifact_sidecar(uri, s3_client)
            if art is not None:
                out.append(art)
                covered.add(uri)
    return out


