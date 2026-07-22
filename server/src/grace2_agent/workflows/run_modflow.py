"""MODFLOW 6 deck-build + submit orchestration (sprint-13 Stage 2, job-0227).

The MODFLOW analogue of the SFINCS ``build_sfincs_model`` + ``run_solver`` +
``wait_for_completion`` chain (job-0041 / job-0042). One module owns three
things for the groundwater-contamination ("spill") engine:

  1. **Deck build + S3 staging** (``build_and_stage_modflow_deck``). Calls the
     engine's ``services/workers/modflow/gwt_adapter.build_modflow_deck`` (a
     FloPy GWF+GWT deck builder, FROZEN under job-0221), reorganises the FLAT
     deck FloPy writes into the ``gwf/`` + ``gwt/`` subdirectory layout the
     solver entrypoint reconstructs (design-doc § 6 / entrypoint.py), composes
     the worker-contract ``manifest.json`` (populating the OQ-MOD-3
     ``model_crs`` field), and uploads the deck + manifest to the cache bucket.

  2. **Solver submit** (``submit_modflow_run``). GCP Cloud Workflows is
     decommissioned. Two gated lanes: the GENERIC AWS Batch seam (the shared
     ``tools.solver.run_solver(solver='modflow', ...)`` per-job autoscaled
     submit) when ``is_batch_mode()`` (``GRACE2_SOLVER_BACKEND=aws-batch`` + a
     resolvable ``GRACE2_AWS_BATCH_JOB_DEF_MODFLOW``); otherwise - the DEFAULT,
     inert-until-flipped fallback - the shared local-exec solver backend
     (``tools.solver.launch_local_solver``) with the MODFLOW local-exec spec
     (the ``mf6`` binary on the box). Either way returns the schema-owned
     ``ExecutionHandle`` whose ``workflow_name`` pins the backend
     (``aws-batch`` / ``local-exec``) for ``wait_for_completion`` (the
     Invariant-8 cancellation seam).

  3. **LOCAL EXECUTION MODE** (``GRACE2_MODFLOW_LOCAL=1``). The foreground dev
     seam: run the staged deck against a locally-downloaded ``mf6`` binary
     in a scratch dir, parse ``mfsim.lst`` for the convergence marker (the same
     authoritative signal the entrypoint uses), and write a ``completion.json``
     so the downstream postprocess reads it identically to the supervised path.
     This is BOTH the dev/test seam AND the live-evidence path on a box with no
     docker daemon (mirrors the SFINCS smoke-harness fallback).

CRITICAL handoff fixes (Stage-1 Open Questions, resolved here):

  * **gwf/ + gwt/ subdir layout.** FloPy writes the deck FLAT (all files in the
    sim root) and references package files (``gwf_model.dis``) relative to the
    simulation CWD - NOT relative to the model namefile. So simply moving model
    namefiles into ``gwf/``/``gwt/`` and rewriting only the ``mfsim.nam`` model
    references is INSUFFICIENT: mf6 then can't find ``gwf_model.dis`` in the
    root. ``_reorganize_into_subdirs`` therefore (a) moves ``gwf_model.*`` →
    ``gwf/`` and ``gwt_model.*`` → ``gwt/``, (b) rewrites ``mfsim.nam`` model +
    ims paths to the subdir, AND (c) rewrites EACH model namefile's package
    references to the subdir prefix. The simulation roots (``mfsim.nam``,
    ``mfsim.tdis``, ``gwfgwt.exg``) stay flat. Verified against mf6 6.5.0:
    Normal termination of simulation. OUTPUT files (``gwt_model.ucn``,
    ``gwf_model.hds``, ``*.cbc``) land at the scratch ROOT because the OC
    ``FILEOUT`` records use bare filenames resolved against CWD - so the
    manifest ``outputs`` globs reference root paths + a recursive ``**`` net.

  * **model_crs in the manifest.** The entrypoint echoes ``manifest["model_crs"]``
    into ``completion.json``; the postprocess step needs it to reproject the
    concentration grid from the deck's UTM grid to EPSG:4326. We read it off the
    ``DeckManifest.model_crs`` field (e.g. ``"EPSG:32617"``) the engine adapter
    populated and write it into the manifest.

Determinism boundary (Invariant 1 / 2): no LLM call anywhere in this module.
Deck build is deterministic FloPy; submission is a thin Cloud Workflows call;
the local path is a subprocess run of the mf6 binary. Progress emission goes
through the active ``PipelineEmitter`` (job-0035 seam) exactly like the SFINCS
path - a wall-clock-keyed ramp, never an estimate.

Cancellation (Invariant 8): the cloud path returns an ``ExecutionHandle`` whose
``workflows_execution_id`` ``wait_for_completion`` (tools/solver.py) cancels on
the WS ``cancel`` chain. The local path is a foreground subprocess - cancel
terminates the process group.

AWS local backend (job-0292b, sprint-14-aws)
--------------------------------------------

``GRACE2_SOLVER_BACKEND=local-docker`` (the job-0291 seam) routes MODFLOW
through the solver module's shared local machinery instead of Cloud Workflows:

  * ``build_and_stage_modflow_deck`` becomes scheme-aware: under
    ``GRACE2_STORAGE_BACKEND=s3`` the deck + manifest upload to
    ``s3://$GRACE2_CACHE_BUCKET/modflow/<run_id>/`` via **boto3** (the
    job-0289 s3fs-anonymous lesson; shared ``tools.solver`` S3 client seam).
    The manifest keeps the LEGACY ``gs_uri`` field NAME with ``s3://`` VALUES
    - staging resolves by scheme (job-0291 convention). The default ``gs://``
    fsspec path is byte-identical.
  * ``submit_modflow_run`` dispatches to ``tools.solver.launch_local_solver``
    with a MODFLOW ``LocalSolverSpec``: stage the deck back down from S3 into
    ``$GRACE2_RUNS_DIR/<run_id>/``, launch the **mf6 binary directly**
    (``exec_kind="exec"`` - no public MODFLOW image exists; the instance
    carries the same SHA-pinned USGS 6.5.0 static binary the GCP Dockerfile
    installs, resolved via ``$GRACE2_MF6_BIN``), supervisor uploads outputs +
    the EXACT ``services/workers/modflow/entrypoint.py`` completion.json
    (``converged`` / ``model_crs`` / ``mf6_stdout_uri`` / ``mf6_stderr_uri``)
    to ``s3://$GRACE2_RUNS_BUCKET/<run_id>/``. The spec's ``classify_exit``
    reproduces the entrypoint's mfsim.lst convergence guard verbatim.
  * The SFINCS-shared ``wait_for_completion`` polls the S3 completion object
    (the handle's ``workflow_name="local-exec"`` pins the backend); cancel =
    process-group kill ≤30 s (Invariant 8).

``GRACE2_MODFLOW_LOCAL`` (the foreground dev seam) is independent and must
stay UNSET on the AWS deployment - the backend seam, not local mode, owns the
AWS path.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from grace2_contracts import new_ulid
from grace2_contracts.execution import ExecutionHandle
from grace2_contracts.modflow_contracts import MODFLOWRunArgs

logger = logging.getLogger("grace2_agent.workflows.run_modflow")

__all__ = [
    "MODFLOWWorkflowError",
    "DeckStaging",
    "build_and_stage_modflow_deck",
    "submit_modflow_run",
    "run_modflow_local",
    "is_local_mode",
    "register_modflow_solver",
    "MODFLOW_SOLVER_NAME",
    "MODFLOW_WORKFLOW_NAME",
    "set_cache_bucket",
    "set_runs_bucket",
    "set_mf6_binary",
    "build_modflow_deck",  # re-exported adapter alias (engine, job-0221)
    # Heavy-compute offload (reports/design/heavy-compute-offload-2026-07-02.md).
    "compose_and_upload_modflow_build_spec",
    "read_modflow_build_manifest",
    # Archetype offload (GRACE2_MODFLOW_ARCHETYPE_OFFLOAD).
    "read_modflow_archetype_manifest",
]


# --------------------------------------------------------------------------- #
# Constants / configuration
# --------------------------------------------------------------------------- #

#: Cloud Workflows orchestrator name (infra/modflow.tf). The agent submits a
#: ``{run_id, manifest_uri}`` execution against it; cancellation propagates to
#: the running Cloud Run Job (Invariant 8).
MODFLOW_WORKFLOW_NAME: str = "grace-2-modflow-orchestrator"

#: The registry key + ``ExecutionHandle.solver`` tag for the groundwater engine.
#: Mirrors ``run_swmm.SWMM_SOLVER_NAME`` - its PRESENCE in
#: ``tools.solver.SOLVER_WORKFLOW_REGISTRY`` is what gates ``run_solver(
#: solver='modflow', ...)`` dispatch (an absent key raises
#: ``SolverNotRegisteredError``). Registered at import via
#: ``register_modflow_solver()`` exactly like SWMM.
MODFLOW_SOLVER_NAME: str = "modflow"

#: Concentration floor (mg/L) below which a cell is NOT counted as plume. The
#: postprocess module owns plume metrics; this constant is mirrored there for
#: the manifest outputs glob comment only.
PLUME_FLOOR_MGL: float = 0.001

#: The MF6 concentration output stem the GWT OC package writes (gwt_adapter
#: registers ``gwt_model.ucn``). Recursive glob captures it wherever it lands.
GWT_UCN_FILENAME: str = "gwt_model.ucn"

#: Convergence markers - IDENTICAL to the entrypoint's authoritative signal so
#: the local path classifies a run the same way the container does.
CONVERGENCE_FAILURE_MARKER = "FAILED TO MEET SOLVER CONVERGENCE CRITERIA"
NORMAL_TERMINATION_MARKER = "Normal termination of simulation"


# --------------------------------------------------------------------------- #
# Cross-package adapter import (engine, job-0221, FROZEN)
# --------------------------------------------------------------------------- #
#
# ``gwt_adapter`` lives in ``services/workers/modflow/`` which is NOT an
# importable package (no ``__init__`` on ``services/workers/``). We add the
# modflow worker dir to ``sys.path`` lazily so the agent service boots even in
# environments where flopy is absent - the import only resolves when a MODFLOW
# tool is actually invoked. This mirrors the SFINCS pattern of containing the
# heavy geoscience deps behind the tool body rather than at module import.

_MODFLOW_WORKER_DIR = (
    Path(__file__).resolve().parents[4] / "services" / "workers" / "modflow"
)


def _import_gwt_adapter() -> Any:
    """Import and return the engine's ``gwt_adapter`` module (job-0221).

    Adds the modflow worker dir to ``sys.path`` if needed. Raises
    ``MODFLOWWorkflowError`` with a typed code if the import fails so the
    agent surface renders a useful message instead of an opaque ImportError.
    """
    worker_dir = str(_MODFLOW_WORKER_DIR)
    if worker_dir not in sys.path:
        sys.path.insert(0, worker_dir)
    try:
        import gwt_adapter  # type: ignore[import-not-found]

        return gwt_adapter
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_ADAPTER_IMPORT_FAILED",
            message=(
                f"could not import gwt_adapter from {worker_dir}: {exc}; "
                "flopy must be installed and services/workers/modflow present."
            ),
        ) from exc


def build_modflow_deck(*args: Any, **kwargs: Any) -> Any:
    """Thin re-export of the engine adapter's ``build_modflow_deck``.

    Importing the adapter lazily (it pulls in flopy). Returns the engine's
    typed ``DeckManifest``. Kept as a module-level callable so tests and the
    Case-2 composer (job-0228) can monkeypatch the build at this seam.
    """
    return _import_gwt_adapter().build_modflow_deck(*args, **kwargs)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class MODFLOWWorkflowError(RuntimeError):
    """Raised on any deck-build / staging / submit / local-run failure.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a
    typed error frame. Codes:

    - ``MODFLOW_ADAPTER_IMPORT_FAILED`` - gwt_adapter / flopy not importable.
    - ``MODFLOW_DECK_BUILD_FAILED`` - the FloPy deck build raised.
    - ``MODFLOW_DECK_STAGE_FAILED`` - subdir reorg / manifest / upload failed.
    - ``MODFLOW_DISPATCH_FAILED`` - the local-exec solver dispatch failed.
    - ``MODFLOW_LOCAL_RUN_FAILED`` - local mf6 subprocess failed to launch or
      the binary could not be located.
    - ``MODFLOW_SOLVER_DIVERGED`` - mf6 ran but the list file reports a
      convergence failure (or no normal-termination marker).
    """

    error_code: str = "MODFLOW_WORKFLOW_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# DI seams (mirror tools/solver.py)
# --------------------------------------------------------------------------- #

_CACHE_BUCKET: str | None = None
_RUNS_BUCKET: str | None = None
_MF6_BINARY: str | None = None


def set_cache_bucket(name: str | None) -> None:
    """Override the cache bucket the deck is staged into. ``None`` → env default."""
    global _CACHE_BUCKET
    _CACHE_BUCKET = name


def set_runs_bucket(name: str | None) -> None:
    """Override the runs bucket (local-mode completion.json target). ``None`` → env."""
    global _RUNS_BUCKET
    _RUNS_BUCKET = name


def set_mf6_binary(path: str | None) -> None:
    """Override the local mf6 binary path. ``None`` → ``$GRACE2_MF6_BIN``/``mf6``."""
    global _MF6_BINARY
    _MF6_BINARY = path


def _cache_bucket() -> str:
    if _CACHE_BUCKET is not None:
        return _CACHE_BUCKET
    # GCP decommissioned: AWS S3 cache bucket default (prod overrides via env).
    return os.environ.get("GRACE2_CACHE_BUCKET", "grace2-hazard-cache-226996537797")


def _runs_bucket() -> str:
    if _RUNS_BUCKET is not None:
        return _RUNS_BUCKET
    # GCP decommissioned: AWS S3 runs bucket default (prod overrides via env).
    return os.environ.get("GRACE2_RUNS_BUCKET", "grace2-hazard-runs-226996537797")


def _mf6_binary() -> str:
    if _MF6_BINARY is not None:
        return _MF6_BINARY
    return os.environ.get("GRACE2_MF6_BIN", "mf6")


def is_local_mode() -> bool:
    """True when ``GRACE2_MODFLOW_LOCAL`` is set to a truthy value.

    The dev/test/live-evidence seam: when set, ``run_modflow_job`` runs the
    deck against a local mf6 binary instead of dispatching Cloud Workflows.
    """
    return os.environ.get("GRACE2_MODFLOW_LOCAL", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def register_modflow_solver() -> None:
    """Register ``'modflow'`` in ``tools.solver.SOLVER_WORKFLOW_REGISTRY``.

    Mirrors ``run_swmm.register_swmm_solver`` (and the SFINCS registration): the
    registry maps the solver name to a workflow/dispatch sentinel; ``run_solver``
    only requires the KEY to be PRESENT to dispatch (an absent key raises
    ``SolverNotRegisteredError``). The backend seam then routes the run to the
    AWS Batch submit (default ``aws-batch``) or the local launcher. We seed the
    ``LOCAL_EXEC_WORKFLOW_NAME`` sentinel as the value (exactly what SWMM seeds)
    - the value is only a default tag; the per-call handle pins the real backend.
    Idempotent (``setdefault``) - safe to call at import.
    """
    from ..tools.solver import LOCAL_EXEC_WORKFLOW_NAME, SOLVER_WORKFLOW_REGISTRY

    SOLVER_WORKFLOW_REGISTRY.setdefault(MODFLOW_SOLVER_NAME, LOCAL_EXEC_WORKFLOW_NAME)


# Register at import so ``run_solver(solver='modflow')`` is wired wherever this
# module is imported (the composer + the tool wrapper both import it) - exactly
# mirroring run_swmm's import-time ``register_swmm_solver()`` call.
register_modflow_solver()


# --------------------------------------------------------------------------- #
# Deck reorganisation: FLAT FloPy output -> gwf/ + gwt/ subdir layout
# --------------------------------------------------------------------------- #


def _reorganize_into_subdirs(
    flat_dir: Path, dest_dir: Path, gwf_name: str, gwt_name: str
) -> list[str]:
    """Copy the flat FloPy deck into the entrypoint's gwf/ + gwt/ subdir layout.

    The entrypoint reconstructs ``inputs[].dest`` relative paths in a scratch
    dir and runs mf6 from the scratch ROOT (where ``mfsim.nam`` sits). For mf6
    to resolve every file, THREE rewrites are required (see module docstring):

      1. Move ``{gwf_name}.*`` → ``gwf/`` and ``{gwt_name}.*`` → ``gwt/``;
         leave ``mfsim.*`` and ``*.exg`` at the root.
      2. Rewrite ``mfsim.nam``: model namefile refs + ims refs get the subdir
         prefix.
      3. Rewrite EACH model namefile (``gwf/{gwf_name}.nam``,
         ``gwt/{gwt_name}.nam``): every package-file token gets the subdir
         prefix (package files are resolved relative to CWD, not the namefile).

    Returns the sorted list of relative dest paths (manifest ``inputs[].dest``).
    """
    gwf_sub = "gwf"
    gwt_sub = "gwt"
    (dest_dir / gwf_sub).mkdir(parents=True, exist_ok=True)
    (dest_dir / gwt_sub).mkdir(parents=True, exist_ok=True)

    dest_rel: list[str] = []
    for src in sorted(flat_dir.iterdir()):
        if not src.is_file():
            continue
        fname = src.name
        if fname.startswith(f"{gwf_name}."):
            rel = f"{gwf_sub}/{fname}"
        elif fname.startswith(f"{gwt_name}."):
            rel = f"{gwt_sub}/{fname}"
        else:
            # mfsim.nam, mfsim.tdis, gwfgwt.exg - stay at root.
            rel = fname
        shutil.copy2(src, dest_dir / rel)
        dest_rel.append(rel)

    # --- 2. Rewrite mfsim.nam model + ims references to subdir paths.
    mfsim = dest_dir / "mfsim.nam"
    if mfsim.exists():
        text = mfsim.read_text()
        text = re.sub(
            rf"(gwf6\s+){re.escape(gwf_name)}\.nam",
            rf"\1{gwf_sub}/{gwf_name}.nam",
            text,
        )
        text = re.sub(
            rf"(gwt6\s+){re.escape(gwt_name)}\.nam",
            rf"\1{gwt_sub}/{gwt_name}.nam",
            text,
        )
        text = re.sub(
            rf"(ims6\s+){re.escape(gwf_name)}\.ims",
            rf"\1{gwf_sub}/{gwf_name}.ims",
            text,
        )
        text = re.sub(
            rf"(ims6\s+){re.escape(gwt_name)}\.ims",
            rf"\1{gwt_sub}/{gwt_name}.ims",
            text,
        )
        mfsim.write_text(text)

    # --- 3. Rewrite each model namefile's package-file references.
    for sub, stem in ((gwf_sub, gwf_name), (gwt_sub, gwt_name)):
        nam = dest_dir / sub / f"{stem}.nam"
        if not nam.exists():
            continue
        text = nam.read_text()
        # Package lines look like "  DIS6  gwf_model.dis  dis". Prefix the
        # filename token (the one matching "<stem>.<ext>") with the subdir.
        text = re.sub(
            rf"(\s)({re.escape(stem)}\.[A-Za-z0-9_]+)(\s)",
            rf"\1{sub}/\2\3",
            text,
        )
        nam.write_text(text)

    return sorted(dest_rel)


# --------------------------------------------------------------------------- #
# River-geometry resolution (sprint-17 J9) - FGB/GeoJSON URI -> lon/lat vertices
# --------------------------------------------------------------------------- #


def _read_vector_bytes(uri: str) -> bytes:
    """Read a vector artifact's bytes from s3:// / gs:// / file:// / local path."""
    if uri.startswith("s3://"):
        from ..tools.cache import read_object_bytes_s3

        return read_object_bytes_s3(uri)
    if uri.startswith("gs://"):
        import fsspec  # type: ignore[import-not-found]

        with fsspec.open(uri, "rb") as fh:  # type: ignore[no-untyped-call]
            return fh.read()
    path = uri.replace("file://", "")
    return Path(path).read_bytes()


def resolve_river_polyline_lonlat(
    river_geometry_uri: str,
    *,
    max_vertices: int = 200,
) -> list[tuple[float, float]]:
    """Resolve a river-geometry artifact (FGB/GeoJSON) to ``(lon, lat)`` vertices.

    Reads the vector artifact (the ``fetch_river_geometry`` FlatGeobuf, or a
    GeoJSON), reprojects to EPSG:4326, picks the LONGEST flowline (the main
    reach to drape onto the grid), and returns its vertices as ``(lon, lat)``
    in path order, downsampled to at most ``max_vertices`` points (the draping
    sub-step sampler fills the gaps, so a coarse polyline still touches every
    crossed cell).

    Raises:
        MODFLOWWorkflowError("MODFLOW_RIVER_GEOMETRY_FAILED"): the artifact
            could not be read / had no usable LineString geometry.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import LineString, MultiLineString  # type: ignore[import-not-found]
        from shapely.ops import linemerge  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_RIVER_GEOMETRY_FAILED",
            message=f"geopandas/shapely not importable for river draping: {exc}",
            details={"river_geometry_uri": river_geometry_uri},
        ) from exc

    suffix = ".fgb" if not river_geometry_uri.lower().endswith(
        (".json", ".geojson")
    ) else ".geojson"
    tmp = Path(tempfile.mkdtemp(prefix="riv-geom-")) / f"river{suffix}"
    try:
        tmp.write_bytes(_read_vector_bytes(river_geometry_uri))
        gdf = gpd.read_file(str(tmp), engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_RIVER_GEOMETRY_FAILED",
            message=f"could not read river geometry from {river_geometry_uri}: {exc}",
            details={"river_geometry_uri": river_geometry_uri},
        ) from exc

    try:
        gdf = gdf[gdf.geometry.notna()]
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif str(gdf.crs).upper() not in {"EPSG:4326", "WGS84"}:
            gdf = gdf.to_crs("EPSG:4326")
    except Exception:  # noqa: BLE001
        pass

    # Collect all (Multi)LineString parts, merge, pick the longest line.
    lines: list = []
    for geom in gdf.geometry:
        if geom is None:
            continue
        if isinstance(geom, LineString):
            lines.append(geom)
        elif isinstance(geom, MultiLineString):
            lines.extend(list(geom.geoms))
    if not lines:
        raise MODFLOWWorkflowError(
            "MODFLOW_RIVER_GEOMETRY_FAILED",
            message=f"river geometry {river_geometry_uri} has no LineString features",
            details={"river_geometry_uri": river_geometry_uri},
        )
    try:
        merged = linemerge(lines)
        if isinstance(merged, MultiLineString):
            longest = max(merged.geoms, key=lambda ln: ln.length)
        else:
            longest = merged
    except Exception:  # noqa: BLE001 - fall back to the single longest raw line
        longest = max(lines, key=lambda ln: ln.length)

    coords = [(float(x), float(y)) for (x, y) in longest.coords]
    if len(coords) > max_vertices:
        step = max(1, len(coords) // max_vertices)
        coords = coords[::step]
        if coords[-1] != (float(longest.coords[-1][0]), float(longest.coords[-1][1])):
            coords.append(
                (float(longest.coords[-1][0]), float(longest.coords[-1][1]))
            )
    if len(coords) < 2:
        raise MODFLOWWorkflowError(
            "MODFLOW_RIVER_GEOMETRY_FAILED",
            message=f"river geometry {river_geometry_uri} reduced to < 2 vertices",
            details={"river_geometry_uri": river_geometry_uri},
        )
    return coords


# --------------------------------------------------------------------------- #
# Deck build + GCS staging
# --------------------------------------------------------------------------- #


@dataclass
class DeckStaging:
    """The result of building + staging a MODFLOW deck.

    Carries the staging URIs the cloud submit path needs plus the local deck
    dir + model_crs the local-run + postprocess paths read. Every field is a
    typed value a downstream step consumes - no prose-for-number.
    """

    run_id: str
    manifest_uri: str  # gs://.../modflow/<run_id>/manifest.json (cloud)
    deck_base_uri: str  # gs://.../modflow/<run_id>/   (deck files prefix)
    local_deck_dir: str  # the on-disk subdir-organised deck (local run reads this)
    model_crs: str  # e.g. "EPSG:32617" - postprocess reprojection key (OQ-MOD-3)
    gwf_name: str
    gwt_name: str
    spill_lat: float
    spill_lon: float
    output_globs: list[str]
    manifest_inputs: list[dict[str, str]] = field(default_factory=list)
    # sprint-17 J9 river-coupling: True iff a RIV package was written (the tool
    # wrapper then runs postprocess_river_seepage in addition to the plume).
    river_coupled: bool = False
    river_cell_count: int = 0
    # sprint-18 Wave-1 archetype: None = the spill/seepage GWF+GWT deck; the
    # three GWF-only archetypes carry their name + the field the archetype tool
    # reads to pick the right postprocess (drawdown / dewatering / budget).
    archetype: str | None = None
    gwt_present: bool = True
    drain_cell_count: int = 0
    well_lat: float = 0.0
    well_lon: float = 0.0


def _compose_manifest(
    deck_base_uri: str,
    dest_rel: list[str],
    model_crs: str,
    output_globs: list[str],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Compose the worker-contract manifest.json (entrypoint design-doc § 6).

    Schema (entrypoint.py docstring):
        {"inputs": [{"gs_uri", "dest"}, ...],
         "mf6_args": [],
         "model_crs": "EPSG:...",      # OQ-MOD-3 - REQUIRED here
         "outputs": ["gwt_model.ucn", "*.lst", "**/*.lst", ...]}
    """
    inputs: list[dict[str, str]] = []
    for rel in dest_rel:
        inputs.append({"gs_uri": deck_base_uri + rel, "dest": rel})
    manifest = {
        "inputs": inputs,
        "mf6_args": [],
        "model_crs": model_crs,  # the CRITICAL Stage-1 handoff field (OQ-MOD-3)
        "outputs": output_globs,
    }
    return manifest, inputs


def build_and_stage_modflow_deck(
    run_args: MODFLOWRunArgs,
    *,
    run_id: str | None = None,
    workdir: str | Path | None = None,
    stage_to_gcs: bool = True,
) -> DeckStaging:
    """Build the MF6 GWF+GWT deck and stage it for the solver entrypoint.

    Steps:
      1. ``build_modflow_deck(...)`` (engine adapter) - FLAT FloPy deck.
      2. ``_reorganize_into_subdirs`` - gwf/ + gwt/ layout + path rewrites.
      3. ``_compose_manifest`` - worker-contract manifest.json incl. model_crs.
      4. (cloud) upload deck + manifest to the cache bucket via fsspec[gcs];
         (local / GCS unavailable) keep the on-disk deck + a local manifest.

    Args:
        run_args: the confirmed ``MODFLOWRunArgs`` forcing parameters.
        run_id: optional ULID; minted if absent.
        workdir: optional base dir for the deck build (a temp dir otherwise).
        stage_to_gcs: when False, skip the GCS upload (local-only path).

    Returns:
        ``DeckStaging`` with the staging URIs + local deck dir + model_crs.

    Raises:
        MODFLOWWorkflowError: any build / reorg / stage step failed.
    """
    rid = run_id or new_ulid()

    # The base dir for both the FLAT build and the subdir-organised deck. We
    # keep it OUTSIDE a TemporaryDirectory context so the local-run path can
    # read it after this function returns; cleanup is the caller's (the tool
    # wrapper) responsibility - it deletes the dir after postprocess.
    base = Path(workdir) if workdir is not None else Path(
        tempfile.mkdtemp(prefix=f"modflow-{rid}-")
    )
    flat_dir = base / "flat"
    deck_dir = base / "deck"
    flat_dir.mkdir(parents=True, exist_ok=True)
    deck_dir.mkdir(parents=True, exist_ok=True)

    # --- 1a. River-coupling (sprint-17 J9): resolve the polyline -------------
    # When run_args carries a river_geometry_uri, read the flowline into the
    # (lon, lat) vertices the adapter drapes onto the grid. A resolution failure
    # is typed + fatal (the user asked for a river-coupled run) rather than a
    # silent fall-through to a plain spill deck.
    river_kwargs: dict[str, Any] = {}
    river_uri = getattr(run_args, "river_geometry_uri", None)
    if river_uri:
        polyline = resolve_river_polyline_lonlat(river_uri)
        river_kwargs = dict(
            river_polyline_lonlat=polyline,
            river_stage_m=getattr(run_args, "river_stage_m", None),
            river_stage_depth_m=getattr(run_args, "river_stage_depth_m", None),
            streambed_conductance_m2_day=getattr(
                run_args, "streambed_conductance_m2_day", None
            ),
            along_river_source=bool(getattr(run_args, "along_river_source", False)),
        )

    # --- 1a'. Archetype branch (sprint-18 Wave-1): thread the per-archetype
    # forcing into the adapter's GWF-only archetype dispatch. ``archetype is
    # None`` => the kwargs stay empty and the spill/seepage deck is byte-
    # identical. The adapter raises a ValueError when a required per-archetype
    # field is missing (e.g. a sustainable_yield run with no well) -- the
    # composer-level honesty gate is the FIRST line, this is the engine backstop.
    archetype_kwargs: dict[str, Any] = {}
    archetype = getattr(run_args, "archetype", None)
    if archetype is not None:
        archetype_kwargs = dict(
            archetype=archetype,
            well_location_latlon=getattr(run_args, "well_location_latlon", None),
            pumping_rate_m3_day=getattr(run_args, "pumping_rate_m3_day", None),
            aquifer_sy=getattr(run_args, "aquifer_sy", None),
            aquifer_ss=getattr(run_args, "aquifer_ss", None),
            sim_years=getattr(run_args, "sim_years", None),
            n_periods=getattr(run_args, "n_periods", None),
            pit_footprint_lonlat=getattr(run_args, "pit_footprint_lonlat", None),
            drain_elevation_m=getattr(run_args, "drain_elevation_m", None),
            drain_conductance_m2_day=getattr(
                run_args, "drain_conductance_m2_day", None
            ),
            well_pumping_rate_m3_day=getattr(
                run_args, "well_pumping_rate_m3_day", None
            ),
            zone_partition=getattr(run_args, "zone_partition", None),
            # --- Wave-2 archetype fields (sprint-18 Wave-2) ---
            # MAR (managed aquifer recharge -> RCH mounding)
            basin_footprint_lonlat=getattr(run_args, "basin_footprint_lonlat", None),
            infiltration_rate_m_day=getattr(run_args, "infiltration_rate_m_day", None),
            recharge_months=getattr(run_args, "recharge_months", None),
            # ASR (aquifer storage & recovery)
            injection_rate_m3_day=getattr(run_args, "injection_rate_m3_day", None),
            recovery_rate_m3_day=getattr(run_args, "recovery_rate_m3_day", None),
            injection_months=getattr(run_args, "injection_months", None),
            recovery_months=getattr(run_args, "recovery_months", None),
            n_cycles=getattr(run_args, "n_cycles", None),
            # wetland_hydroperiod (RCH-schedule + EVT seasonal water-table range)
            wetland_footprint_lonlat=getattr(
                run_args, "wetland_footprint_lonlat", None
            ),
            recharge_schedule_m_day=getattr(
                run_args, "recharge_schedule_m_day", None
            ),
            et_surface_m=getattr(run_args, "et_surface_m", None),
            et_max_rate_m_day=getattr(run_args, "et_max_rate_m_day", None),
            et_extinction_depth_m=getattr(run_args, "et_extinction_depth_m", None),
            specific_yield=getattr(run_args, "specific_yield", None),
            # --- module wave: stream_depletion SFR forcing (demo-defaulted) --- #
            # The river polyline itself is resolved into ``river_kwargs`` above
            # (from run_args.river_geometry_uri); these four are the SFR-specific
            # demo forcing fields the adapter drapes onto the reaches.
            river_inflow_m3_s=getattr(run_args, "river_inflow_m3_s", None),
            river_width_m=getattr(run_args, "river_width_m", None),
            streambed_k_m_day=getattr(run_args, "streambed_k_m_day", None),
            manning_n=getattr(run_args, "manning_n", None),
            # --- module wave: land_subsidence CSUB forcing (demo-defaulted) --- #
            # The four CSUB interbed/storage demo-default overrides the adapter
            # drapes onto the pumped footprint; ignored unless the archetype is
            # "land_subsidence".
            csub_ssv_inelastic_m=getattr(run_args, "csub_ssv_inelastic_m", None),
            csub_sse_elastic_m=getattr(run_args, "csub_sse_elastic_m", None),
            csub_interbed_thick_frac=getattr(
                run_args, "csub_interbed_thick_frac", None
            ),
            csub_cg_ske_m=getattr(run_args, "csub_cg_ske_m", None),
        )

    # --- 1b. advanced-physics overrides (levers STEP 3) ---------------------
    # Validate + resolve the run_args.advanced_physics dict against the per-engine
    # PHYSICS_REGISTRY. None => {} (byte-identical conservative-tracer deck). A
    # bad key / out-of-range value raises a typed PhysicsRegistryError surfaced as
    # MODFLOW_PHYSICS_INVALID (the user/LLM gets an honest correction, never a
    # silently-wrong deck). The resolved delta is echoed for narration.
    from .physics_registry import (
        PhysicsRegistryError,
        applied_physics_delta,
        validate_and_resolve_physics,
    )

    try:
        resolved_physics = validate_and_resolve_physics(
            "modflow", getattr(run_args, "advanced_physics", None)
        )
    except PhysicsRegistryError as exc:
        raise MODFLOWWorkflowError(
            "MODFLOW_PHYSICS_INVALID",
            message=f"invalid advanced_physics: {exc}",
            details={"run_id": rid, "engine": "modflow", "key": getattr(exc, "key", None)},
        ) from exc
    if resolved_physics:
        logger.info(
            "run_modflow advanced_physics applied run_id=%s delta=%s",
            rid,
            applied_physics_delta("modflow", resolved_physics),
        )

    # --- 1. Build the FLAT deck via the engine adapter ----------------------
    try:
        manifest_obj = build_modflow_deck(
            spill_location_latlon=run_args.spill_location_latlon,
            contaminant=run_args.contaminant,
            release_rate_kg_s=run_args.release_rate_kg_s,
            duration_days=run_args.duration_days,
            aquifer_k_ms=run_args.aquifer_k_ms,
            porosity=run_args.porosity,
            workdir=str(flat_dir),
            write=True,
            advanced_physics=resolved_physics or None,
            **river_kwargs,
            **archetype_kwargs,
        )
    except MODFLOWWorkflowError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_DECK_BUILD_FAILED",
            message=f"build_modflow_deck failed: {exc}",
            details={"run_id": rid, "run_args": run_args.model_dump()},
        ) from exc

    model_crs = manifest_obj.model_crs
    gwf_name = manifest_obj.gwf_name
    gwt_name = manifest_obj.gwt_name
    river_coupled = bool(getattr(manifest_obj, "river_coupled", False))
    river_cell_count = int(getattr(manifest_obj, "river_cell_count", 0))
    # sprint-18 Wave-1: a GWF-only archetype deck (sustainable_yield /
    # mine_dewatering / regional_water_budget) carries no GWT model (gwt_name="")
    # and writes head + cbc only (no UCN concentration).
    archetype = getattr(manifest_obj, "archetype", None)
    gwt_present = bool(getattr(manifest_obj, "gwt_present", True))

    # --- 2. Reorganise FLAT -> gwf/ + gwt/ subdir layout --------------------
    # A GWF-only archetype deck has no gwt model namefile; the reorg's gwt6/ims
    # rewrites no-op on its empty gwt_name, so the same reorg is reused (the gwt/
    # subdir stays empty). The flat FloPy GWF-only deck references package files
    # relative to CWD exactly like the GWF half of the spill deck.
    try:
        dest_rel = _reorganize_into_subdirs(flat_dir, deck_dir, gwf_name, gwt_name)
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_DECK_STAGE_FAILED",
            message=f"subdir reorganisation failed: {exc}",
            details={"run_id": rid, "flat_dir": str(flat_dir)},
        ) from exc

    # --- 3. Compose the manifest --------------------------------------------
    # Outputs land at the scratch ROOT (OC FILEOUT bare filenames resolve to
    # CWD), but a recursive ``**`` net is belt-and-suspenders in case a future
    # adapter writes them under a subdir. The GWF head + cbc are written by
    # EVERY archetype (the GWF-only archetypes have no UCN; the UCN glob simply
    # matches nothing for them, which is harmless). Always capture the list
    # files + head + cbc; the UCN glob covers the spill/seepage path.
    output_globs = [
        GWT_UCN_FILENAME,
        f"{gwf_name}.hds",
        f"{gwf_name}.cbc",
        "*.cbc",
        "*.hds",
        "*.lst",
        "mfsim.lst",
        f"**/{GWT_UCN_FILENAME}",
        "**/*.lst",
        # module wave: stream_depletion SFR outputs (the postprocess parses the
        # obs csv; the .stg/.bud are belt-and-suspenders binary outputs).
        f"{gwf_name}.sfr.obs.csv",
        f"{gwf_name}.sfr.stg",
        f"{gwf_name}.sfr.bud",
        "*.sfr.obs.csv",
        "*.sfr.stg",
        "*.sfr.bud",
        # module wave: land_subsidence CSUB outputs (the postprocess reads the
        # z-displacement grid + hds + obs csv; the compaction grid is a
        # belt-and-suspenders binary output).
        f"{gwf_name}.csub.zdisp.bin",
        f"{gwf_name}.csub.compaction.bin",
        f"{gwf_name}.csub.obs.csv",
        "*.csub.zdisp.bin",
        "*.csub.compaction.bin",
        "*.csub.obs.csv",
    ]
    # job-0292b: scheme-aware deck prefix. ``cache.storage_scheme()`` returns
    # ``"gs"`` by default (byte-identical pre-job-0292b URI) and ``"s3"``
    # under GRACE2_STORAGE_BACKEND=s3 - the manifest's input VALUES then carry
    # s3:// so the local-backend staging resolves them by scheme (the field
    # NAME stays the legacy ``gs_uri``, job-0291 convention).
    from ..tools.cache import storage_scheme

    deck_base_uri = f"{storage_scheme()}://{_cache_bucket()}/modflow/{rid}/"
    manifest_uri = deck_base_uri + "manifest.json"
    manifest, manifest_inputs = _compose_manifest(
        deck_base_uri, dest_rel, model_crs, output_globs
    )
    manifest_local = deck_dir / "manifest.json"
    manifest_local.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # --- 4. Stage to the object store (cloud / AWS-local-backend path) ------
    if stage_to_gcs and not is_local_mode():
        if deck_base_uri.startswith("s3://"):
            # job-0292b: boto3 (NOT fsspec/s3fs - the job-0289 anonymous-
            # credentials lesson) through the solver module's shared S3
            # client seam, mirroring sfincs_builder's deck upload.
            try:
                from ..tools.solver import _get_s3_client

                s3 = _get_s3_client()
                bucket, _, base_key = (
                    deck_base_uri[len("s3://"):].rstrip("/").partition("/")
                )
                for rel in dest_rel:
                    with (deck_dir / rel).open("rb") as fh:
                        s3.put_object(
                            Bucket=bucket, Key=f"{base_key}/{rel}", Body=fh
                        )
                with manifest_local.open("rb") as fh:
                    s3.put_object(
                        Bucket=bucket,
                        Key=f"{base_key}/manifest.json",
                        Body=fh,
                        ContentType="application/json",
                    )
                logger.info(
                    "staged MODFLOW deck (%d files) + manifest to %s (boto3)",
                    len(dest_rel),
                    deck_base_uri,
                )
            except Exception as exc:  # noqa: BLE001
                raise MODFLOWWorkflowError(
                    "MODFLOW_DECK_STAGE_FAILED",
                    message=f"S3 upload of deck/manifest failed: {exc}",
                    details={"run_id": rid, "deck_base_uri": deck_base_uri},
                ) from exc
        else:
            try:
                import fsspec  # type: ignore[import-not-found]

                fs = fsspec.filesystem("gcs")
                for rel in dest_rel:
                    fs.put(str(deck_dir / rel), deck_base_uri + rel)
                fs.put(str(manifest_local), manifest_uri)
                logger.info(
                    "staged MODFLOW deck (%d files) + manifest to %s",
                    len(dest_rel),
                    deck_base_uri,
                )
            except Exception as exc:  # noqa: BLE001
                raise MODFLOWWorkflowError(
                    "MODFLOW_DECK_STAGE_FAILED",
                    message=f"GCS upload of deck/manifest failed: {exc}",
                    details={"run_id": rid, "deck_base_uri": deck_base_uri},
                ) from exc

    return DeckStaging(
        run_id=rid,
        manifest_uri=manifest_uri,
        deck_base_uri=deck_base_uri,
        local_deck_dir=str(deck_dir),
        model_crs=model_crs,
        gwf_name=gwf_name,
        gwt_name=gwt_name,
        spill_lat=float(manifest_obj.spill_lat),
        spill_lon=float(manifest_obj.spill_lon),
        output_globs=output_globs,
        manifest_inputs=manifest_inputs,
        river_coupled=river_coupled,
        river_cell_count=river_cell_count,
        archetype=archetype,
        gwt_present=gwt_present,
        drain_cell_count=int(getattr(manifest_obj, "drain_cell_count", 0)),
        well_lat=float(getattr(manifest_obj, "well_lat", 0.0)),
        well_lon=float(getattr(manifest_obj, "well_lon", 0.0)),
    )


# --------------------------------------------------------------------------- #
# Heavy-compute offload (reports/design/heavy-compute-offload-2026-07-02.md).
#
# Move the FloPy deck BUILD + the UCN -> plume-COG POSTPROCESS off the always-on
# agent onto the grace2-modflow tear-down Batch worker (the MODFLOW analogue of
# the SFINCS pluvial reference, commit ce1ba9d). Gated OFF by default
# (``GRACE2_MODFLOW_BUILD_OFFLOAD`` unset) so live behavior is BYTE-IDENTICAL to
# the legacy in-agent build+postprocess until NATE rebuilds+deploys the
# grace2-modflow image (now carrying pyproj + the shared substrate) and flips the
# flag. Mirrors ``model_flood_scenario._sfincs_build_offload_enabled`` +
# ``_compose_and_upload_flood_build_spec``.
# --------------------------------------------------------------------------- #


def _run_args_to_deck_kwargs(run_args: MODFLOWRunArgs) -> dict[str, Any]:
    """Assemble the ``build_modflow_deck`` kwargs for the worker job_spec.

    Reproduces ``build_and_stage_modflow_deck``'s river-geometry resolution +
    archetype threading + advanced-physics resolution so the WORKER's
    ``build_modflow_deck`` call is identical to the in-agent one. Every value is
    JSON-serializable (tuples round-trip to lists; ``build_modflow_deck`` accepts
    list-or-tuple for every coordinate field). A resolution/validation failure
    raises the SAME typed ``MODFLOWWorkflowError`` the in-agent path raises.
    """
    from .physics_registry import (
        PhysicsRegistryError,
        validate_and_resolve_physics,
    )

    kwargs: dict[str, Any] = {
        "spill_location_latlon": list(run_args.spill_location_latlon),
        "contaminant": run_args.contaminant,
        "release_rate_kg_s": float(run_args.release_rate_kg_s),
        "duration_days": float(run_args.duration_days),
        "aquifer_k_ms": float(run_args.aquifer_k_ms),
        "porosity": float(run_args.porosity),
    }

    # --- River-coupling (sprint-17 J9): resolve the flowline to lon/lat verts. --
    river_uri = getattr(run_args, "river_geometry_uri", None)
    if river_uri:
        kwargs["river_polyline_lonlat"] = [
            list(v) for v in resolve_river_polyline_lonlat(river_uri)
        ]
        for name in (
            "river_stage_m",
            "river_stage_depth_m",
            "streambed_conductance_m2_day",
        ):
            val = getattr(run_args, name, None)
            if val is not None:
                kwargs[name] = val
        kwargs["along_river_source"] = bool(
            getattr(run_args, "along_river_source", False)
        )

    # --- Archetype threading (sprint-18): thread every present per-archetype field.
    archetype = getattr(run_args, "archetype", None)
    if archetype is not None:
        kwargs["archetype"] = archetype
        for name in (
            "well_location_latlon",
            "pumping_rate_m3_day",
            "aquifer_sy",
            "aquifer_ss",
            "sim_years",
            "n_periods",
            "pit_footprint_lonlat",
            "drain_elevation_m",
            "drain_conductance_m2_day",
            "well_pumping_rate_m3_day",
            "zone_partition",
            "basin_footprint_lonlat",
            "infiltration_rate_m_day",
            "recharge_months",
            "injection_rate_m3_day",
            "recovery_rate_m3_day",
            "injection_months",
            "recovery_months",
            "n_cycles",
            "wetland_footprint_lonlat",
            "recharge_schedule_m_day",
            "et_surface_m",
            "et_max_rate_m_day",
            "et_extinction_depth_m",
            "specific_yield",
            # module wave: stream_depletion SFR forcing (demo-defaulted).
            "river_inflow_m3_s",
            "river_width_m",
            "streambed_k_m_day",
            "manning_n",
            # module wave: land_subsidence CSUB forcing (demo-defaulted).
            "csub_ssv_inelastic_m",
            "csub_sse_elastic_m",
            "csub_interbed_thick_frac",
            "csub_cg_ske_m",
        ):
            val = getattr(run_args, name, None)
            if val is not None:
                # Coordinate lists carry tuples; normalize to plain lists for JSON.
                if isinstance(val, (list, tuple)) and val and isinstance(
                    val[0], (list, tuple)
                ):
                    kwargs[name] = [list(x) for x in val]
                elif isinstance(val, tuple):
                    kwargs[name] = list(val)
                else:
                    kwargs[name] = val

    # --- advanced-physics overrides (resolved; None -> omitted). ----------------
    try:
        resolved_physics = validate_and_resolve_physics(
            "modflow", getattr(run_args, "advanced_physics", None)
        )
    except PhysicsRegistryError as exc:
        raise MODFLOWWorkflowError(
            "MODFLOW_PHYSICS_INVALID",
            message=f"invalid advanced_physics: {exc}",
            details={"engine": "modflow", "key": getattr(exc, "key", None)},
        ) from exc
    if resolved_physics:
        kwargs["advanced_physics"] = resolved_physics

    return kwargs


def compose_and_upload_modflow_build_spec(
    run_args: MODFLOWRunArgs,
    *,
    run_id: str | None = None,
    compute_class: str = "standard",
) -> str:
    """Compose + upload the MODFLOW BUILD job_spec; return its s3:// URI.

    Replaces the in-agent ``build_and_stage_modflow_deck`` on the offload path:
    resolves the ``build_modflow_deck`` kwargs (river/archetype/physics), wraps
    them in the ``_modflow_build`` job_spec schema, and uploads it to the cache
    bucket. The DECK is NEVER built here (the worker builds it) — only the small
    JSON spec is written. Requires an S3 storage backend (the Batch worker reads
    the spec from S3).

    Raises ``MODFLOWWorkflowError`` (kwargs resolution / storage-backend / upload).
    """
    from ..tools.cache import storage_scheme

    rid = run_id or new_ulid()
    deck_kwargs = _run_args_to_deck_kwargs(run_args)

    scheme = storage_scheme()
    if scheme != "s3":
        raise MODFLOWWorkflowError(
            "MODFLOW_DISPATCH_FAILED",
            message=(
                "The MODFLOW build offload requires an S3 storage backend so the "
                "Batch worker can read the job_spec (GRACE2_STORAGE_BACKEND=s3). "
                "Staying inert."
            ),
            details={"run_id": rid},
        )
    cache_bucket = _cache_bucket()
    spec_id = new_ulid()
    base_prefix = f"cache/static-30d/modflow_build/{spec_id}/"
    job_spec_uri = f"s3://{cache_bucket}/{base_prefix}modflow_build_spec.json"

    job_spec: dict[str, Any] = {
        "schema_version": 1,
        "engine": "modflow",
        "spec_id": spec_id,
        "run_args": deck_kwargs,
        "options": {"compute_class": compute_class},
    }
    payload = json.dumps(job_spec, indent=2).encode("utf-8")
    try:
        from ..tools.solver import _get_s3_client

        s3 = _get_s3_client()
        s3_bucket, _, key = job_spec_uri[len("s3://"):].partition("/")
        s3.put_object(
            Bucket=s3_bucket, Key=key, Body=payload, ContentType="application/json"
        )
    except MODFLOWWorkflowError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_DISPATCH_FAILED",
            message=f"failed to upload the MODFLOW build job_spec to {job_spec_uri}: {exc}",
            details={"run_id": rid, "job_spec_uri": job_spec_uri},
        ) from exc

    logger.info(
        "MODFLOW build offload: composed job_spec -> %s (archetype=%s)",
        job_spec_uri,
        deck_kwargs.get("archetype"),
    )
    return job_spec_uri


def _read_object_text(uri: str) -> str:
    """Read a small text object (the publish_manifest.json) by scheme."""
    if uri.startswith("s3://"):
        from ..tools.cache import read_object_bytes_s3

        return read_object_bytes_s3(uri).decode("utf-8")
    if uri.startswith("gs://"):
        import fsspec  # type: ignore[import-not-found]

        with fsspec.open(uri, "rb") as fh:  # type: ignore[no-untyped-call]
            return fh.read().decode("utf-8")
    return Path(uri.replace("file://", "")).read_text(encoding="utf-8")


def read_modflow_build_manifest(
    run_result: Any,
    *,
    publish: bool = True,
) -> Any:
    """Read the worker ``publish_manifest.json`` -> ``PlumeLayerURI`` (register-only).

    The offload tail: after the combined build+solve+postprocess Batch job
    succeeds, the worker has already rasterized the UCN into a plume COG + written
    the publish manifest. The agent becomes register-only: read the thin manifest,
    (optionally) publish the bare COG to a TiTiler tile URL, and return the typed
    ``PlumeLayerURI`` carrying the worker-computed metrics (Invariant 1 — the agent
    narrates the worker's numbers, never invents them).

    Raises ``MODFLOWWorkflowError`` when the manifest is missing/unparseable or the
    worker's honesty gate flagged an empty plume (``MODFLOW_PLUME_EMPTY``).
    """
    from grace2_contracts.modflow_contracts import PlumeLayerURI

    run_id = getattr(run_result, "run_id", None)
    prefix = getattr(run_result, "output_uri", None) or (
        f"s3://{_runs_bucket()}/{run_id}/"
    )
    manifest_uri = prefix.rstrip("/") + "/publish_manifest.json"
    try:
        manifest = json.loads(_read_object_text(manifest_uri))
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_PLUME_OUTPUT_MISSING",
            message=f"could not read publish_manifest.json from {manifest_uri}: {exc}",
            details={"run_id": run_id, "manifest_uri": manifest_uri},
        ) from exc

    if manifest.get("status") != "ok":
        raise MODFLOWWorkflowError(
            manifest.get("error_code") or "MODFLOW_PLUME_EMPTY",
            message=(
                "MODFLOW build+solve worker reported a non-ok postprocess "
                f"(status={manifest.get('status')!r}, "
                f"error_code={manifest.get('error_code')!r})"
            ),
            details={"run_id": run_id},
        )

    layers = manifest.get("layers") or []
    if not layers:
        raise MODFLOWWorkflowError(
            "MODFLOW_PLUME_EMPTY",
            message="publish_manifest.json carried no layers (empty plume)",
            details={"run_id": run_id},
        )
    layer = layers[0]
    metrics = layer.get("metrics") or manifest.get("metrics") or {}
    cog_uri = layer.get("cog_uri")
    bbox = layer.get("bbox")

    final_uri = cog_uri
    if publish and isinstance(cog_uri, str) and (
        cog_uri.startswith("s3://") or cog_uri.startswith("gs://")
    ):
        try:
            from ..tools.publish_layer import publish_layer

            wms_url = publish_layer(
                layer_uri=cog_uri,
                layer_id=layer.get("layer_id_stem", f"plume-concentration-{run_id}"),
                style_preset=layer.get("style_preset", "continuous_plume_concentration"),
            )
            if wms_url:
                final_uri = wms_url
        except Exception as exc:  # noqa: BLE001 — non-fatal (COG URI survives)
            logger.warning("publish_layer failed for offload plume: %s", exc)

    return PlumeLayerURI(
        layer_id=layer.get("layer_id_stem", f"plume-concentration-{run_id}"),
        name=layer.get("name", "Contaminant Plume (peak concentration)"),
        layer_type="raster",
        uri=final_uri,
        style_preset=layer.get("style_preset", "continuous_plume_concentration"),
        role=layer.get("role", "primary"),
        units=layer.get("units", "mg/L"),
        bbox=tuple(bbox) if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None,
        max_concentration_mgl=float(metrics.get("max_concentration_mgl", 0.0)),
        plume_area_km2=float(metrics.get("plume_area_km2", 0.0)),
    )


# --------------------------------------------------------------------------- #
# ARCHETYPE OFFLOAD GATE + MANIFEST READER
# (GRACE2_MODFLOW_ARCHETYPE_OFFLOAD, default OFF, independent of the spill gate)
# --------------------------------------------------------------------------- #


def read_modflow_archetype_manifest(
    run_result: Any,
    archetype: str,
    *,
    publish: bool = True,
) -> Any:
    """Read the worker ``publish_manifest.json`` -> archetype LayerURI (register-only).

    The offload tail for archetype runs: after the combined build+solve+postprocess
    Batch job succeeds, the worker has already rasterized the outputs into a COG
    + written the publish manifest. The agent becomes register-only: read the thin
    manifest, (optionally) publish the bare COG to a TiTiler tile URL, and return
    the typed archetype LayerURI carrying the worker-computed metrics (Invariant 1).

    Dispatches to the correct LayerURI subtype by ``archetype``:
      sustainable_yield        -> DrawdownLayerURI
      mine_dewatering          -> DewaterLayerURI
      regional_water_budget    -> BudgetPartitionLayerURI
      MAR                      -> MoundingLayerURI
      ASR                      -> ASRLayerURI
      wetland_hydroperiod      -> HydroperiodLayerURI

    Raises ``MODFLOWWorkflowError`` when the manifest is missing/unparseable or
    the worker's honesty gate flagged an empty result.
    """
    from grace2_contracts.modflow_contracts import (
        ASRLayerURI,
        BudgetPartitionLayerURI,
        DewaterLayerURI,
        DrawdownLayerURI,
        HydroperiodLayerURI,
        MoundingLayerURI,
    )

    run_id = getattr(run_result, "run_id", None)
    prefix = getattr(run_result, "output_uri", None) or (
        f"s3://{_runs_bucket()}/{run_id}/"
    )
    manifest_uri = prefix.rstrip("/") + "/publish_manifest.json"
    try:
        manifest = json.loads(_read_object_text(manifest_uri))
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_ARCHETYPE_OUTPUT_MISSING",
            message=(
                f"could not read publish_manifest.json from {manifest_uri}: {exc}"
            ),
            details={"run_id": run_id, "manifest_uri": manifest_uri},
        ) from exc

    if manifest.get("status") != "ok":
        raise MODFLOWWorkflowError(
            manifest.get("error_code") or "MODFLOW_ARCHETYPE_EMPTY_RESULT",
            message=(
                "MODFLOW archetype worker reported a non-ok postprocess "
                f"(status={manifest.get('status')!r}, "
                f"error_code={manifest.get('error_code')!r})"
            ),
            details={"run_id": run_id, "archetype": archetype},
        )

    layers = manifest.get("layers") or []
    if not layers:
        raise MODFLOWWorkflowError(
            "MODFLOW_ARCHETYPE_EMPTY_RESULT",
            message=f"publish_manifest.json carried no layers (archetype={archetype!r})",
            details={"run_id": run_id},
        )
    layer = layers[0]
    metrics = layer.get("metrics") or manifest.get("metrics") or {}
    cog_uri = layer.get("cog_uri")
    bbox = layer.get("bbox")
    bbox_tuple = (
        tuple(bbox) if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None
    )

    final_uri: str = cog_uri or ""
    if publish and isinstance(cog_uri, str) and (
        cog_uri.startswith("s3://") or cog_uri.startswith("gs://")
    ):
        try:
            from ..tools.publish_layer import publish_layer

            wms_url = publish_layer(
                layer_uri=cog_uri,
                layer_id=layer.get("layer_id_stem", f"{archetype}-{run_id}"),
                style_preset=layer.get("style_preset", "continuous_head_m"),
            )
            if wms_url:
                final_uri = wms_url
        except Exception as exc:  # noqa: BLE001 -- non-fatal (COG URI survives)
            logger.warning(
                "publish_layer failed for offload archetype %s: %s", archetype, exc
            )

    # Shared kwargs for all typed LayerURIs.
    common: dict[str, Any] = {
        "layer_id": layer.get("layer_id_stem", f"{archetype}-{run_id}"),
        "name": layer.get("name", archetype),
        "layer_type": layer.get("layer_type", "raster"),
        "uri": final_uri,
        "style_preset": layer.get("style_preset", "continuous_head_m"),
        "role": layer.get("role", "primary"),
        "units": layer.get("units", "m"),
        "bbox": bbox_tuple,
    }

    if archetype == "sustainable_yield":
        return DrawdownLayerURI(
            **common,
            max_drawdown_m=max(0.0, float(metrics.get("max_drawdown_m", 0.0))),
            head_decline_timeseries=metrics.get("head_decline_timeseries"),
        )
    if archetype == "mine_dewatering":
        return DewaterLayerURI(
            **common,
            dewatering_rate_m3_day=max(0.0, float(metrics.get("dewatering_rate_m3_day", 0.0))),
            drain_cell_count=int(metrics.get("drain_cell_count", 0)),
        )
    if archetype == "regional_water_budget":
        return BudgetPartitionLayerURI(
            **common,
            budget_partition_m3_day=metrics.get("budget_partition_m3_day") or {},
        )
    if archetype == "MAR":
        return MoundingLayerURI(
            **common,
            max_mounding_m=max(0.0, float(metrics.get("max_mounding_m", 0.0))),
            recharged_volume_m3=metrics.get("recharged_volume_m3"),
        )
    if archetype == "ASR":
        return ASRLayerURI(
            **common,
            recovery_efficiency=metrics.get("recovery_efficiency"),
            head_timeseries=metrics.get("head_timeseries"),
        )
    if archetype == "wetland_hydroperiod":
        return HydroperiodLayerURI(
            **common,
            seasonal_head_range_m=max(0.0, float(metrics.get("seasonal_head_range_m", 0.0))),
            head_timeseries=metrics.get("head_timeseries"),
        )
    # Should never reach here if gate guards PRT/saltwater correctly.
    raise MODFLOWWorkflowError(
        "MODFLOW_ARCHETYPE_UNKNOWN",
        message=(
            f"read_modflow_archetype_manifest called with unrecognised archetype "
            f"{archetype!r} -- this archetype is not offloadable"
        ),
        details={"run_id": run_id, "archetype": archetype},
    )


# --------------------------------------------------------------------------- #
# Solver dispatch (mirror of tools/solver.run_solver - local-exec backend)
# --------------------------------------------------------------------------- #


def _modflow_local_spec(staging: DeckStaging) -> Any:
    """Build the MODFLOW ``LocalSolverSpec`` for the shared local backend.

    job-0292b solver-binary decision: **image-less local-exec** - there is no
    maintained public MODFLOW docker image (the GCP Dockerfile itself built
    from python:3.11-slim + the SHA-pinned USGS 6.5.0 static binary), so the
    simplest contract-preserving path runs the same pinned ``mf6`` binary
    directly on the instance (``$GRACE2_MF6_BIN``, the existing env
    convention). ``classify_exit`` reproduces the MODFLOW entrypoint's
    exit-code resolution verbatim (list file authoritative - design doc § 8)
    and supplies the entrypoint-schema ``converged`` + ``model_crs``
    completion fields.
    """
    from ..tools.solver import LOCAL_EXEC_WORKFLOW_NAME, LocalSolverSpec

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        return [_mf6_binary(), *args]

    def classify_exit(
        rundir: Path, exit_code: int
    ) -> tuple[str, int, str | None, dict[str, Any]]:
        converged, conv_note = _check_convergence(rundir)
        if exit_code != 0:
            status: str = "error"
            error: str | None = f"mf6 exited with non-zero code {exit_code}"
        elif not converged:
            status, exit_code = "error", 2
            error = conv_note or "solver_diverged"
        else:
            status, exit_code, error = "ok", 0, None
        return (
            status,
            exit_code,
            error,
            {"converged": converged, "model_crs": staging.model_crs},
        )

    return LocalSolverSpec(
        solver="modflow",
        workflow_name=LOCAL_EXEC_WORKFLOW_NAME,
        args_key="mf6_args",
        build_argv=build_argv,
        stdout_name="mf6.stdout",
        stderr_name="mf6.stderr",
        stdout_uri_field="mf6_stdout_uri",
        stderr_uri_field="mf6_stderr_uri",
        exec_kind="exec",
        classify_exit=classify_exit,
    )


def submit_modflow_run(
    staging: DeckStaging,
    *,
    compute_class: str = "standard",
) -> ExecutionHandle:
    """Submit a MODFLOW run via the active solver backend.

    Two dispatch lanes, gated so the Batch path is INERT until NATE flips the
    env (zero regression by default):

      * **GENERIC AWS Batch seam** (``is_batch_mode()`` - ``GRACE2_SOLVER_BACKEND``
        ``aws-batch`` + a resolvable ``GRACE2_AWS_BATCH_JOB_DEF_MODFLOW``). Routes
        through the SHARED ``tools.solver.run_solver(solver='modflow',
        model_setup_uri=staging.manifest_uri, compute_class=...)`` - the same
        per-job autoscaled Batch submit SFINCS/SWMM use. The Batch container runs
        the SAME ``services/workers/modflow/entrypoint.py`` (now scheme-aware)
        and writes the SAME ``completion.json`` to
        ``s3://$GRACE2_RUNS_BUCKET/<run_id>/``; the handle's
        ``workflow_name="aws-batch"`` routes ``wait_for_completion`` to the Batch
        poll branch.

      * **Local-exec fallback** (DEFAULT until the Batch env is flipped). Runs the
        ``mf6`` binary directly via the local-exec supervisor
        (``tools.solver.launch_local_solver`` with the MODFLOW local-exec spec):
        the staged deck is downloaded from S3 into ``$GRACE2_RUNS_DIR/<run_id>/``,
        ``mf6`` runs detached, and the supervisor writes the
        MODFLOW-entrypoint-schema completion.json. The returned handle's
        ``workflow_name="local-exec"`` pins the backend for ``wait_for_completion``.

    Both handles feed the SFINCS-shared ``wait_for_completion`` (the Invariant-8
    cancellation seam) - ``run_modflow_tool.py`` is unchanged.

    Raises:
        MODFLOWWorkflowError("MODFLOW_DISPATCH_FAILED"): the dispatch (Batch
            submit or local-backend staging/launch) failed.
    """
    from ..tools.solver import (
        SolverDispatchError,
        launch_local_solver,
    )

    # The AWS Batch dispatch arm was removed (local-only slim); MODFLOW always
    # runs on the local-exec backend via launch_local_solver.
    try:
        return launch_local_solver(
            _modflow_local_spec(staging),
            staging.manifest_uri,
            run_id=staging.run_id,
            compute_class=compute_class,
        )
    except SolverDispatchError as exc:
        raise MODFLOWWorkflowError(
            "MODFLOW_DISPATCH_FAILED",
            message=f"local MODFLOW dispatch failed: {exc}",
            details={"run_id": staging.run_id},
        ) from exc


# --------------------------------------------------------------------------- #
# Local execution mode (GRACE2_MODFLOW_LOCAL=1)
# --------------------------------------------------------------------------- #


def _check_convergence(scratch: Path) -> tuple[bool, str | None]:
    """Parse ``mfsim.lst`` for the convergence marker - entrypoint-identical."""
    lst = scratch / "mfsim.lst"
    if not lst.exists():
        return False, "mfsim.lst absent (mf6 produced no list file)"
    text = lst.read_text(errors="replace")
    if CONVERGENCE_FAILURE_MARKER in text:
        return False, "solver_diverged"
    if NORMAL_TERMINATION_MARKER in text:
        return True, None
    return False, "mfsim.lst has neither normal-termination nor convergence-failure marker"


def run_modflow_local(staging: DeckStaging) -> str:
    """Run the staged deck against a local mf6 binary; write completion.json.

    The live-evidence + dev/test path. Runs the on-disk subdir-organised deck
    (``staging.local_deck_dir``) with the ``mf6`` binary in that dir (mf6 reads
    ``mfsim.nam`` from CWD), parses ``mfsim.lst`` for convergence (the
    entrypoint's authoritative signal), and writes a ``completion.json`` into
    the deck dir whose shape MATCHES the cloud entrypoint's completion contract
    (status / exit_code / converged / model_crs / output_uris). The postprocess
    step then reads it identically to the cloud path.

    Returns:
        The local ``file://`` URI of the deck dir (the postprocess
        ``run_outputs_uri``; it finds ``gwt_model.ucn`` inside).

    Raises:
        MODFLOWWorkflowError("MODFLOW_LOCAL_RUN_FAILED"): mf6 could not launch.
        MODFLOWWorkflowError("MODFLOW_SOLVER_DIVERGED"): list file reports a
            convergence failure (or no normal-termination marker).
    """
    scratch = Path(staging.local_deck_dir)
    mf6 = _mf6_binary()
    started_at = datetime.now(timezone.utc)

    logger.info("run_modflow_local mf6=%s cwd=%s run_id=%s", mf6, scratch, staging.run_id)
    stdout_path = scratch / "mf6.stdout"
    stderr_path = scratch / "mf6.stderr"
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            proc = subprocess.run(
                [mf6],
                cwd=str(scratch),
                stdout=out,
                stderr=err,
                check=False,
            )
        rc = proc.returncode
    except FileNotFoundError as exc:
        raise MODFLOWWorkflowError(
            "MODFLOW_LOCAL_RUN_FAILED",
            message=(
                f"mf6 binary not found at {mf6!r}; set GRACE2_MF6_BIN or "
                "call set_mf6_binary(path). Download the USGS mf6 6.5.0 static "
                "binary (see reports/inflight/job-0220 evidence)."
            ),
            details={"run_id": staging.run_id, "mf6": mf6},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_LOCAL_RUN_FAILED",
            message=f"mf6 subprocess failed to run: {exc}",
            details={"run_id": staging.run_id, "mf6": mf6},
        ) from exc

    converged, conv_note = _check_convergence(scratch)
    logger.info(
        "run_modflow_local mf6 exit=%d converged=%s note=%s",
        rc,
        converged,
        conv_note,
    )

    # Resolve status the same way the entrypoint does: list file authoritative.
    if rc != 0:
        status, exit_code, error = "error", rc, f"mf6 exited with non-zero code {rc}"
    elif not converged:
        status, exit_code, error = "error", 2, conv_note or "solver_diverged"
    else:
        status, exit_code, error = "ok", 0, None

    # Write a completion.json matching the cloud entrypoint's completion schema.
    output_paths = sorted(
        str(p.relative_to(scratch))
        for p in scratch.rglob("*")
        if p.is_file()
        and (p.suffix in {".ucn", ".hds", ".cbc", ".lst"})
    )
    completion = {
        "run_id": staging.run_id,
        "status": status,
        "exit_code": exit_code,
        "converged": converged,
        "model_crs": staging.model_crs,
        "mf6_stdout_uri": f"file://{stdout_path}",
        "mf6_stderr_uri": f"file://{stderr_path}",
        "output_uris": [f"file://{scratch / rel}" for rel in output_paths],
        "started_at": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": error,
    }
    (scratch / "completion.json").write_text(
        json.dumps(completion, indent=2), encoding="utf-8"
    )

    if status != "ok":
        raise MODFLOWWorkflowError(
            "MODFLOW_SOLVER_DIVERGED" if exit_code == 2 else "MODFLOW_LOCAL_RUN_FAILED",
            message=error or "mf6 local run failed",
            details={"run_id": staging.run_id, "exit_code": exit_code},
        )

    return f"file://{scratch}"


# --------------------------------------------------------------------------- #
# Progress emission helper (mirror tools/solver._emit_progress, ContextVar seam)
# --------------------------------------------------------------------------- #


async def emit_modflow_progress(percent: int) -> None:
    """Best-effort progress emission via the active PipelineEmitter step.

    Reads ``current_emitter()`` (job-0160 ContextVar) and pushes a clamped
    progress update on the emitter's most-recent step (the one the bracketing
    ``emit_tool_call`` created for the ``run_modflow_job`` invocation). Outside
    an ``emit_tool_call`` scope (direct call / smoke / unit test) the emitter is
    ``None`` and we skip - progress is a UX nice-to-have, not a correctness
    gate. Mirrors ``tools/solver.wait_for_completion``'s progress emission,
    which is the SFINCS analog; the difference is the SFINCS path holds an
    explicit ``EmitterBinding`` while we read the most-recent step off the
    emitter's snapshot so this works without a separate binding step.
    """
    try:
        from ..pipeline_emitter import current_emitter

        emitter = current_emitter()
        if emitter is None:
            return
        # The bracketing ``emit_tool_call`` created (and is running) the
        # most-recent step; push progress onto it. ``_step_order`` is the
        # emitter's ordered step-id list (job-0035 internal).
        order = getattr(emitter, "_step_order", None)
        if order:
            await emitter.update_progress(order[-1], max(0, min(100, percent)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("emit_modflow_progress skipped: %s", exc)
