"""Case-scoped mesh artifact record + discovery seam.

A mesh built by the standalone ``generate_mesh`` tool is TWO things in one case:

  * a DISPLAY layer -- an MDAL-loadable ``.2dm`` (``layer_type="mesh"``) that
    lands in ``loaded_layers`` via the normal ``LayerURI`` auto-emit path, so a
    human sees the wireframe in QGIS; and
  * an ARTIFACT record -- the engine-compat facts (format URIs, CRS, bathymetry,
    node/element counts, open-boundary info) a model template needs to decide
    "can I solve on this?" and, if the user accepts, to point its solver at it.

This module is the ARTIFACT half. It does NOT invent a parallel store: the facts
ride TWO existing seams so both a same-session run and a cold reopen can find them:

  * a module side-table keyed by ``case_id`` (mirrors ``publish_layer``'s
    ``_LAST_LEGEND_BY_URI`` stash) -- the fast, same-daemon-process path the
    precondition gate uses when the mesh was built earlier in the SAME case; and
  * a durable ``mesh_artifact.json`` SIDECAR written next to the mesh objects in
    the cache bucket -- discoverable from any ``layer_type="mesh"`` row's ``uri``
    in a later session (its key is the mesh key with the basename swapped).

``mesh_compatible_with_engine`` is the honest gatekeeper: it answers whether a
given engine can actually consume this mesh (TELEMAC needs a bathymetric SELAFIN;
SCHISM an hgrid; SWAN a fort.14) and, on a mismatch, WHY -- so the consuming
template can decline loudly instead of force-fitting.
"""

from __future__ import annotations

import json
import logging
import os
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
    "mesh_compatible_with_engine",
    "open_boundary_node_count",
    "materialize_hecras_mesh_inputs",
    "ENGINE_MESH_REQUIREMENTS",
    "HECRAS_INPUT_KEYS",
]


@dataclass
class MeshArtifact:
    """A computational mesh built into a case, plus its engine-compat facts.

    ``display_uri`` is the MDAL ``.2dm`` (``s3://``) the ``layer_type="mesh"`` row
    carries; ``slf_uri`` / ``gr3_uri`` / ``fort14_uri`` are the per-solver geometry
    files (``None`` when a format was not emitted). ``crs_authid`` is the mesh CRS
    (a projected UTM authid, e.g. ``"EPSG:32617"``); ``has_bathymetry`` is True
    when node elevations were sampled (a solve-ready bed). ``open_boundary_info``
    records the segmented open/land boundary (``{}`` for a fully-closed inland
    catchment). ``engine_compat`` lists the engines whose REQUIRED geometry this
    mesh actually carries (see :data:`ENGINE_MESH_REQUIREMENTS`)."""

    mesh_id: str
    name: str
    mode: str  # "watershed" | "coastal_water_edge" | "hecras_rog"
    display_uri: str  # s3:// display face (a .2dm mesh, or a cell-polygon vector)
    slf_uri: str | None
    utm_epsg: int
    crs_authid: str
    has_bathymetry: bool
    node_count: int
    element_count: int
    bbox: tuple[float, float, float, float]
    engine_compat: list[str] = field(default_factory=list)
    gr3_uri: str | None = None
    fort14_uri: str | None = None
    outlet_lonlat: tuple[float, float] | None = None
    pour_point_lonlat: tuple[float, float] | None = None
    open_boundary_info: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    case_id: str | None = None
    #: HEC-RAS RoG mesh (mode ``hecras_rog``): the PORTABLE AUTHORING INPUTS the 2025
    #: managed engine re-realizes the graded cell mesh from -- a ``{key: s3-uri}`` map
    #: over :data:`HECRAS_INPUT_KEYS`. The engine's ``MeshFactory.TryCreateMesh`` is
    #: DETERMINISTIC on identical seeds (verified byte-identical over independent
    #: realizations), so re-realizing on consume reproduces exactly the cell mesh NATE
    #: inspected -- no realized geometry need be stored for the solve.
    hecras_inputs: dict[str, str] = field(default_factory=dict)
    channel_target_size_m: float | None = None
    background_size_m: float | None = None
    #: True iff the in-container meshprobe realized the cells (``TryCreateMesh`` ok,
    #: HEC's <= 8-sides-per-cell validation passed). A mesh that never realized cleanly
    #: is not offered for consumption.
    cells_validated: bool = False

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


#: A mesh carries SCHISM open-boundary segmentation iff its ``open_boundary_info``
#: names a designated seaward side AND a positive open-node count -- the forcing
#: boundary a barotropic/baroclinic SCHISM solve applies tides/T-S at. An inland
#: catchment (``open_boundary_info == {}``) is fully closed and has none.
def open_boundary_node_count(art: MeshArtifact) -> int:
    """Number of designated open-boundary nodes on the mesh (0 = fully closed)."""
    try:
        return int((art.open_boundary_info or {}).get("open_node_count", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


#: Engine -> the mesh format + facts that engine's solver REQUIRES. A mesh is
#: compatible with an engine iff it carries the named format URI and (when
#: ``needs_bathymetry``) a sampled bed and (when ``needs_open_boundary``) a
#: designated seaward open boundary. ``unstructured_unsupported`` marks an engine
#: whose worker cannot consume ANY user mesh (a regular-grid-only solver). Open-set:
#: templates adopt the same gate by reading their rows here, no new gate code.
ENGINE_MESH_REQUIREMENTS: dict[str, dict[str, Any]] = {
    # TELEMAC-2D geometry is SELAFIN with a BOTTOM node field (the bed the
    # shallow-water solve needs); rain-on-grid + river-dye consume it.
    "telemac": {"uri_field": "slf_uri", "needs_bathymetry": True,
                "format": "SELAFIN (.slf, BOTTOM)"},
    # SCHISM reads an hgrid.gr3 with depths AND open/land boundary segmentation:
    # bare bathymetry is not enough, the solve needs a designated seaward open
    # boundary to force tides / T-S at. A generate_mesh WATERSHED mesh is an
    # inland closed catchment (no open boundary) -> honestly declined; a COASTAL
    # mesh built with an open_boundary_side carries one.
    "schism": {"uri_field": "gr3_uri", "needs_bathymetry": True,
               "needs_open_boundary": True, "format": "SCHISM hgrid (.gr3)"},
    # The SWAN worker is REGULAR-GRID ONLY (CGRID REGULAR + INPGRID BOTTOM +
    # bottom.bot sampled from a DEM). It has no unstructured (fort.14) path, so it
    # cannot consume a user mesh at all -- the honest answer is always False.
    "swan": {"unstructured_unsupported": True,
             "reason": ("the SWAN worker is REGULAR-GRID (CGRID REGULAR + "
                        "bottom.bot); it has no unstructured-mesh (fort.14) path, "
                        "so it cannot consume a user-supplied mesh")},
    # The HEC-RAS 2025 managed engine realizes its graded cell mesh INSIDE the project
    # from a graded seed cloud + channel breaklines over a local-SI terrain (it has no
    # single geometry file); so its "geometry" is a BUNDLE of authoring inputs the
    # engine re-realizes on consume (deterministic). Compatible iff the bundle carries
    # the seeds + breaklines + local terrain + frame AND the cell mesh validated in the
    # meshprobe. rain-on-grid only.
    "hecras": {"bundle": True, "needs_validated": True,
               "format": ("HEC-RAS RoG authoring bundle (graded seeds + channel "
                          "breaklines + local terrain frame)")},
}

#: The keys a ``hecras`` mesh's :attr:`MeshArtifact.hecras_inputs` bundle carries. The
#: first four are REQUIRED to re-realize + solve; ``catchment``/``flowlines`` are the
#: modeled-domain provenance (the metrics mask + the channel network it graded toward).
HECRAS_INPUT_KEYS: tuple[str, ...] = (
    "seeds", "breaklines", "local_dem", "prep_json", "catchment", "flowlines")
_HECRAS_REQUIRED_KEYS: tuple[str, ...] = ("seeds", "breaklines", "local_dem", "prep_json")


def mesh_compatible_with_engine(
    art: MeshArtifact, engine: str
) -> tuple[bool, str]:
    """Can ``engine`` solve on ``art``? -> ``(ok, reason)``.

    ``reason`` names the missing requirement on a mismatch so the consuming
    template can DECLINE LOUDLY (never force-fit a mesh an engine cannot read).
    An unknown engine is treated as incompatible (honest, not permissive)."""
    req = ENGINE_MESH_REQUIREMENTS.get(str(engine).lower())
    if req is None:
        return False, f"no mesh-compatibility rule registered for engine {engine!r}"
    if req.get("unstructured_unsupported"):
        return False, str(req.get("reason") or f"{engine} cannot consume a user mesh")
    if req.get("bundle"):
        # HEC-RAS RoG bundle: check the authoring inputs are all present + the cells
        # realized, rather than a single geometry-file field.
        missing = [k for k in _HECRAS_REQUIRED_KEYS if not (art.hecras_inputs or {}).get(k)]
        if missing:
            return False, (
                f"mesh {art.name!r} is not a {req['format']} (missing "
                f"{', '.join(missing)}; built as mode={art.mode!r})")
        if req.get("needs_validated") and not art.cells_validated:
            return False, (
                f"mesh {art.name!r} never realized a valid cell mesh (the meshprobe "
                "did not confirm <= 8 sides/cell), so it is not solve-ready")
        return True, "compatible"
    uri = getattr(art, str(req["uri_field"]), None)
    if not uri:
        return False, (
            f"mesh {art.name!r} carries no {req['format']} geometry that "
            f"{engine} requires (this mesh was built as mode={art.mode!r})")
    if req.get("needs_bathymetry") and not art.has_bathymetry:
        return False, (
            f"mesh {art.name!r} has no sampled bathymetry; {engine} needs a "
            "bed-carrying geometry")
    if req.get("needs_open_boundary") and open_boundary_node_count(art) <= 0:
        return False, (
            f"mesh {art.name!r} has no designated open (seaward) boundary; "
            f"{engine} needs an open-boundary segmentation to force at (build a "
            "coastal mesh with an open_boundary_side to use it here)")
    return True, "compatible"


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


def materialize_hecras_mesh_inputs(
    art: MeshArtifact, dst_dir: str, s3_client: Any
) -> dict[str, str]:
    """Download a ``hecras`` mesh's authoring bundle to ``dst_dir`` -> ``{key: path}``.

    The accepted-gate consume path: the 2025 engine re-realizes the graded cell mesh
    from these staged inputs (seeds + breaklines + the local terrain frame), so the
    solve reproduces exactly the mesh NATE inspected -- no fresh delineation, no
    re-seeding. Missing OPTIONAL keys are skipped; a missing REQUIRED key raises."""
    import os as _os

    out: dict[str, str] = {}
    dst = dst_dir
    _os.makedirs(dst, exist_ok=True)
    for key in HECRAS_INPUT_KEYS:
        uri = (art.hecras_inputs or {}).get(key)
        if not uri:
            if key in _HECRAS_REQUIRED_KEYS:
                raise ValueError(
                    f"hecras mesh {art.name!r} bundle is missing required input {key!r}")
            continue
        rest = str(uri)[len("s3://"):]
        bucket, dkey = rest.split("/", 1)
        local = _os.path.join(dst, _os.path.basename(dkey))
        s3_client.download_file(bucket, dkey, local)
        out[key] = local
    return out
