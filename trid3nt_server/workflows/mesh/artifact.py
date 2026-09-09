"""Case-scoped mesh artifact record + discovery seam.

The facts a run reads to decide whether it can solve on a built mesh. They ride
two existing seams and never a parallel store: a case-keyed same-process stash,
and a ``mesh_artifact.json`` sidecar written beside the mesh objects."""

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
    """A computational mesh built into a case, and what it can be solved on."""

    mesh_id: str
    name: str
    mode: str  # the MESHER that built it: om2d | reg_grid
    display_uri: str  # s3:// display face (a .2dm mesh, or a cell-polygon vector)
    #: The per-solver geometry file; ``None`` when that format was not emitted.
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
    #: The segmented open/land boundary; ``{}`` for a fully-closed inland catchment.
    open_boundary_info: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None

    def unsolvable_reason(self) -> str | None:
        """WHY no solve can be staged on this mesh, or ``None`` when one can.

        A solve needs a geometry file and a bed, or a topology to fit one onto."""
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

    What the mesh has, never the edge that was asked for."""
    edges = ((art.probes if art is not None else None) or {}).get("edge_length_m")
    if not isinstance(edges, dict):
        return None
    try:
        value = float(edges.get("min"))
    except (TypeError, ValueError):
        return None
    return value if value > 0.0 else None


# --------------------------------------------------------------------------- #
# Same-process stash, keyed by case.
# --------------------------------------------------------------------------- #
_MAX_CASES: int = 64
_MAX_PER_CASE: int = 16
_CASE_MESH_ARTIFACTS: dict[str, list[MeshArtifact]] = {}


def stash_mesh_artifact(case_id: str | None, art: MeshArtifact) -> None:
    """Record a just-built mesh artifact under its case (FIFO-bounded).

    An empty ``case_id`` is dropped: nothing case-scoped could discover it."""
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
# Durable sidecar, co-located with the mesh objects.
# --------------------------------------------------------------------------- #
def sidecar_key_for_mesh_uri(mesh_uri: str) -> tuple[str, str] | None:
    """``s3://bucket/prefix/mesh.2dm`` -> ``(bucket, prefix/mesh_artifact.json)``.

    The mesh key with the basename swapped; ``None`` for a non-``s3://`` uri."""
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

    Best-effort: a write failure never fails a mesh build, and answers ``None``."""
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

    Best-effort: ``None`` on any miss."""
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

    ``case_id=None`` means the active turn's case."""
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


