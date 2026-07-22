"""ELMFIRE wildfire-spread engine: inputs, deck build, staging, dispatch (FIRE-3).

The ELMFIRE analogue of ``run_geoclaw.py`` / ``run_swmm.py``. This module owns:

  1. **Input acquisition** (``fetch_elmfire_inputs``): the 8 fuels/topography
     rasters the FIRE-2 deck builder consumes — LANDFIRE fbfm40/cbh/cbd/cc/ch
     via ``fetch_landfire_fuels`` (all five layers shipped by FIRE-2), the DEM
     via ``fetch_dem`` (USGS 3DEP), and slope/aspect derived from that SAME DEM
     via ``compute_slope`` / ``compute_aspect`` (degrees — the ELMFIRE
     convention). Every fetcher's typed error propagates honestly (CONUS-only
     LANDFIRE coverage fails typed, never hallucinated fuels).
  2. **Deck build** (``build_elmfire_deck``): drives the FIRE-2 deck builder
     (``services/workers/elmfire/deck_builder.py`` — imported by path because
     ``services/workers/`` is not on the agent import path, mirroring how the
     Landlab/OpenQuake specs reach ``services.workers.*`` from the repo root)
     to produce the same-grid EPSG:5070 30 m input deck + rendered
     ``elmfire.data`` namelist + deck manifest.
  3. **Staging** (``stage_elmfire_manifest``): the run_solver manifest. Under
     the ``local-docker`` backend the manifest + deck stay on the local disk
     (``file://`` URIs — ``launch_local_solver`` resolves them by scheme,
     exactly like the sfincs_builder local-manifest fallback). Under the
     ``aws-batch`` backend the deck files + manifest are uploaded to the cache
     bucket (mirrors ``stage_geoclaw_manifest``) — that lane stays INERT until
     FIRE-4 provisions the ECR image + Batch job definition
     (``TRID3NT_AWS_BATCH_JOB_DEF_ELMFIRE`` / ``ELMFIRE_BATCH_JOB_DEF_NAME``).
  4. **Solver registration**: ``'elmfire'`` in ``SOLVER_WORKFLOW_REGISTRY``
     (AWS-Batch sentinel, the FIRE-4 seam) + a ``LocalSolverSpec`` docker
     runner for the FIRE-1 proven image ``trid3nt/elmfire:dev`` (rootless
     docker via DOCKER_HOST, deck dir mounted, ``--cpus`` capped).
  5. **Gate arithmetic** (``estimate_elmfire_grid`` /
     ``estimate_elmfire_runtime_s``): PURE approximations backing the server
     solver-confirm card (cell count + estimated runtime) — no rasterio, no
     network, safe to call from the gate.

Cloud/Batch wiring is FIRE-4: this module only leaves the clean seam (the
solver-registry key + the pinned job-definition name constant). Nothing here
touches AWS at import time.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.elmfire_contracts import ElmfireRunArgs

logger = logging.getLogger("trid3nt_server.workflows.run_elmfire")

__all__ = [
    "ELMFIRE_SOLVER_NAME",
    "ELMFIRE_BATCH_JOB_DEF_NAME",
    "DEFAULT_ELMFIRE_IMAGE",
    "DEFAULT_ELMFIRE_BINARY",
    "ELMFIRE_OUTPUT_GLOBS",
    "ElmfireWorkflowError",
    "ElmfireStaging",
    "load_deck_builder",
    "fetch_elmfire_inputs",
    "build_elmfire_deck_spec",
    "build_elmfire_deck",
    "stage_elmfire_manifest",
    "estimate_elmfire_grid",
    "estimate_elmfire_runtime_s",
    "elmfire_local_spec",
    "register_elmfire_solver",
    "register_elmfire_local_spec",
]

#: The solver-registry key (``run_solver(solver='elmfire', ...)``).
ELMFIRE_SOLVER_NAME: str = "elmfire"

#: FIRE-4 seam: the canonical AWS Batch job-definition NAME the infra job will
#: register (per-solver env override ``TRID3NT_AWS_BATCH_JOB_DEF_ELMFIRE`` is
#: the activation switch — ``_resolve_batch_job_def`` reads it; we deliberately
#: do NOT seed ``SOLVER_BATCH_JOBDEF_REGISTRY`` so the Batch lane stays inert
#: until FIRE-4 provisions the ECR image + job def). Nothing in FIRE-3 touches
#: AWS Batch.
ELMFIRE_BATCH_JOB_DEF_NAME: str = "grace2-elmfire"

#: The FIRE-1 proven local image (env ``TRID3NT_ELMFIRE_IMAGE`` overrides).
DEFAULT_ELMFIRE_IMAGE: str = "trid3nt/elmfire:dev"

#: The solver binary inside the image (release-pinned name; env
#: ``TRID3NT_ELMFIRE_BINARY`` overrides when the image is rebuilt on a newer
#: release tag).
DEFAULT_ELMFIRE_BINARY: str = "elmfire_2025.0526"

#: Default --cpus cap for the local docker run (env ``TRID3NT_ELMFIRE_CPUS``).
DEFAULT_ELMFIRE_CPUS: str = "4"

#: Output globs the solver supervisor uploads from the rundir. ELMFIRE with
#: ``CONVERT_TO_GEOTIFF=.FALSE.`` (the FIRE-2 namelist) writes ESRI BIL rasters
#: (.bil + .hdr sidecars) into ``outputs/``; .tif is included so a future
#: CONVERT_TO_GEOTIFF flip keeps working, .csv catches fire-size stats dumps.
ELMFIRE_OUTPUT_GLOBS: list[str] = [
    "outputs/*.bil",
    "outputs/*.hdr",
    "outputs/*.tif",
    "outputs/*.csv",
]

#: Runtime heuristic (s) per cell-simulated-hour for the confirm-gate estimate.
#: Calibrated on the FIRE-1 evidence: tutorial 01 = 160k cells x 6.17 h in
#: 4.3 s (~4.4e-6); verification 01 = 5.76M cells x 7 h in 67 s (~1.7e-6).
#: 5e-6 is deliberately conservative; ``_ELMFIRE_RUNTIME_FLOOR_S`` covers
#: container start + deck I/O overhead. A HINT for the confirm card, never a
#: narrated number (Invariant 1).
_ELMFIRE_SEC_PER_CELL_HOUR: float = 5.0e-6
_ELMFIRE_RUNTIME_FLOOR_S: float = 15.0


class ElmfireWorkflowError(RuntimeError):
    """Raised on a fatal ELMFIRE workflow-stage failure.

    Carries an open-set ``error_code`` (mirrors ``GeoClawWorkflowError``) so
    the tool wrapper returns a typed error dict the LLM narrates honestly.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


@dataclass
class ElmfireStaging:
    """The staged, dispatch-ready run: manifest URI + deck provenance."""

    run_id: str
    manifest_uri: str
    deck_dir: str
    deck_manifest: dict[str, Any]
    run_args: ElmfireRunArgs
    n_cells: int = 0
    staged_inputs: list[dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# FIRE-2 deck-builder import seam.
#
# ``services/workers/`` is NOT on the agent's import path (a pattern the deleted cloud qgis proxy
# note), so the deck builder is loaded by file path from the repo root —
# the same repo-root discovery the Landlab local spec uses
# (``Path(__file__).resolve().parents[4]``). Cached after first load.
# --------------------------------------------------------------------------- #

_TRID3NT_REPO_ROOT = Path(__file__).resolve().parents[4]
_DECK_BUILDER_PATH = (
    _TRID3NT_REPO_ROOT / "services" / "workers" / "elmfire" / "deck_builder.py"
)
_deck_builder_module: Any = None


def load_deck_builder() -> Any:
    """Load ``services/workers/elmfire/deck_builder.py`` (FIRE-2) by path.

    Prefers a regular ``services.workers.elmfire.deck_builder`` import when the
    repo root happens to be on ``sys.path`` (worker containers), else falls
    back to ``importlib`` by file path (the agent venv). Raises a typed
    :class:`ElmfireWorkflowError` when the module is absent (never a silent
    fallback deck).
    """
    global _deck_builder_module
    if _deck_builder_module is not None:
        return _deck_builder_module
    try:  # the worker-image path (repo root on sys.path)
        from services.workers.elmfire import deck_builder as db  # type: ignore

        _deck_builder_module = db
        return db
    except Exception:  # noqa: BLE001 — fall through to the by-path load
        pass
    if not _DECK_BUILDER_PATH.is_file():
        raise ElmfireWorkflowError(
            "ELMFIRE_DECK_BUILDER_UNAVAILABLE",
            f"FIRE-2 deck builder not found at {_DECK_BUILDER_PATH}",
        )
    spec = importlib.util.spec_from_file_location(
        "trid3nt_elmfire_deck_builder", _DECK_BUILDER_PATH
    )
    if spec is None or spec.loader is None:
        raise ElmfireWorkflowError(
            "ELMFIRE_DECK_BUILDER_UNAVAILABLE",
            f"could not build an import spec for {_DECK_BUILDER_PATH}",
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _deck_builder_module = module
    return module


# --------------------------------------------------------------------------- #
# Input acquisition — the 8 fuels/topography rasters via existing tools.
# --------------------------------------------------------------------------- #

#: fetch_landfire_fuels layers consumed by the deck (design doc section 1.1).
_LANDFIRE_DECK_LAYERS: tuple[str, ...] = ("fbfm40", "cbh", "cbd", "cc", "ch")


def _uri_to_deck_input(name: str, uri: str) -> str:
    """Normalize a fetcher LayerURI ``uri`` into a deck-builder input ref.

    The FIRE-2 deck builder accepts local paths and ``s3://`` URIs. ``file://``
    is stripped to a local path; anything else (gs:// etc.) is a typed error —
    never a silently unreadable input.
    """
    if uri.startswith("file://"):
        return uri[len("file://"):]
    if uri.startswith("s3://") or "://" not in uri:
        return uri
    raise ElmfireWorkflowError(
        "ELMFIRE_INPUT_URI_UNSUPPORTED",
        f"inputs.{name}: unsupported URI scheme for the deck builder: {uri!r}",
    )


def fetch_elmfire_inputs(
    bbox: tuple[float, float, float, float],
    *,
    dem_resolution_m: int = 30,
) -> dict[str, str]:
    """Fetch the 8 deck rasters for ``bbox``; return ``{name: path_or_s3_uri}``.

    SYNCHRONOUS (network + raster I/O) — the composer offloads it via
    ``asyncio.to_thread`` (no-sync-blocking-on-the-loop norm).

    - fbfm40/cbh/cbd/cc/ch: ``fetch_landfire_fuels`` (LF2022 ImageServer,
      30-day cache; CONUS-only — its typed coverage error propagates).
    - dem: ``fetch_dem`` (USGS 3DEP) at ``dem_resolution_m``.
    - slp/asp: ``compute_slope`` / ``compute_aspect`` (degrees) derived from
      the SAME fetched DEM so the three topography rasters are consistent.

    Raises :class:`ElmfireWorkflowError` (``ELMFIRE_INPUT_FETCH_FAILED``)
    wrapping the failing fetcher's error with the raster name — the honest
    data-source norm (primary -> typed error; no silent constant substitute).
    """
    from ..tools.compute_aspect import compute_aspect
    from ..tools.compute_slope import compute_slope
    from ..tools.data_fetch import fetch_dem
    from ..tools.fetch_landfire_fuels import fetch_landfire_fuels

    inputs: dict[str, str] = {}

    def _uri_of(layer_obj: Any) -> str:
        uri = getattr(layer_obj, "uri", None) or (
            layer_obj.get("uri") if isinstance(layer_obj, dict) else None
        )
        if not uri:
            raise ValueError("fetcher returned no uri")
        return str(uri)

    for layer in _LANDFIRE_DECK_LAYERS:
        try:
            inputs[layer] = _uri_to_deck_input(
                layer, _uri_of(fetch_landfire_fuels(bbox, layer=layer))
            )
        except ElmfireWorkflowError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap with the raster name
            raise ElmfireWorkflowError(
                "ELMFIRE_INPUT_FETCH_FAILED",
                f"LANDFIRE {layer} fetch failed for bbox {bbox}: {exc}",
                details={"layer": layer, "bbox": list(bbox)},
            ) from exc

    try:
        dem_layer = fetch_dem(bbox, resolution_m=dem_resolution_m)
        dem_uri = _uri_of(dem_layer)
        inputs["dem"] = _uri_to_deck_input("dem", dem_uri)
    except ElmfireWorkflowError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ElmfireWorkflowError(
            "ELMFIRE_INPUT_FETCH_FAILED",
            f"DEM fetch failed for bbox {bbox}: {exc}",
            details={"layer": "dem", "bbox": list(bbox)},
        ) from exc

    # Slope/aspect derived from the SAME DEM (degrees — the ELMFIRE units).
    for name, fn in (("slp", compute_slope), ("asp", compute_aspect)):
        try:
            inputs[name] = _uri_to_deck_input(name, _uri_of(fn(dem_uri)))
        except ElmfireWorkflowError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ElmfireWorkflowError(
                "ELMFIRE_INPUT_FETCH_FAILED",
                f"terrain derivative {name} failed from DEM {dem_uri}: {exc}",
                details={"layer": name, "dem_uri": dem_uri},
            ) from exc

    logger.info(
        "fetch_elmfire_inputs bbox=%s -> %s",
        bbox,
        {k: v[:80] for k, v in inputs.items()},
    )
    return inputs


# --------------------------------------------------------------------------- #
# Deck spec + build.
# --------------------------------------------------------------------------- #
def build_elmfire_deck_spec(
    run_args: ElmfireRunArgs, inputs: dict[str, str]
) -> dict[str, Any]:
    """Map ``ElmfireRunArgs`` + fetched inputs onto the FIRE-2 deck-spec shape.

    Pure translation (unit-testable, no I/O): the fuel-moisture PRESET expands
    into the concrete m1/m10/m100/lh/lw percentages
    (``FUEL_MOISTURE_PRESETS`` — the documented mapping), the wind rides
    through in ELMFIRE's native 20 ft mph convention, ``duration_hours``
    becomes ``duration_s``, and ``DTDUMP`` stays hourly (3600 s) so the
    time-of-arrival thresholding in postprocess aligns with dump cadence.
    """
    moisture = run_args.fuel_moisture_values()
    return {
        "aoi": {"bbox": [float(v) for v in run_args.bbox]},
        "ignitions": [
            {
                "lon": float(run_args.ignition_lonlat[0]),
                "lat": float(run_args.ignition_lonlat[1]),
                "t_ign_s": 0.0,
            }
        ],
        "weather": {
            "ws_mph_20ft": float(run_args.wind_speed_mph),
            "wd_deg": float(run_args.wind_dir_deg),
            **moisture,
        },
        "duration_s": float(run_args.duration_hours) * 3600.0,
        "inputs": dict(inputs),
        "grid": {"target_epsg": 5070, "cellsize_m": float(run_args.cellsize_m)},
        "time": {"dt_s": 30.0, "dtdump_s": 3600.0},
    }


def build_elmfire_deck(
    run_args: ElmfireRunArgs,
    inputs: dict[str, str],
    deck_dir: str | Path,
) -> dict[str, Any]:
    """Build the run-ready deck via the FIRE-2 deck builder; return its manifest.

    SYNCHRONOUS (warping + raster writes) — offload via ``asyncio.to_thread``.
    Deck-builder typed errors (grid mismatch, missing input, no coverage,
    ignition outside domain, ...) are re-raised as
    :class:`ElmfireWorkflowError` PRESERVING the deck builder's ``error_code``
    so the honest-failure taxonomy survives the module boundary.
    """
    db = load_deck_builder()
    spec = build_elmfire_deck_spec(run_args, inputs)
    try:
        return db.build_deck(spec, deck_dir)
    except db.ElmfireDeckError as exc:
        raise ElmfireWorkflowError(
            getattr(exc, "error_code", "ELMFIRE_DECK_ERROR"), str(exc)
        ) from exc


# --------------------------------------------------------------------------- #
# Confirm-gate arithmetic (PURE — no rasterio, callable from server.py).
# --------------------------------------------------------------------------- #
def estimate_elmfire_grid(
    bbox: tuple[float, float, float, float] | list[float],
    cellsize_m: float = 30.0,
) -> tuple[int, int, int]:
    """Approximate the computational grid ``(nx, ny, n_cells)`` for the card.

    Cosine-latitude arithmetic (~111.32 km/deg) — an APPROXIMATION of the
    deck builder's true EPSG:5070 grid (within a few percent at CONUS
    latitudes), kept rasterio-free so the server confirm gate can call it
    inline. The REAL grid is computed (and hard-asserted) by the deck builder.
    """
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    mid_lat = (min_lat + max_lat) / 2.0
    width_m = abs(max_lon - min_lon) * 111_320.0 * math.cos(math.radians(mid_lat))
    height_m = abs(max_lat - min_lat) * 111_320.0
    cs = max(float(cellsize_m), 1.0)
    nx = max(int(math.ceil(width_m / cs)), 1)
    ny = max(int(math.ceil(height_m / cs)), 1)
    return nx, ny, nx * ny


def estimate_elmfire_runtime_s(n_cells: int, duration_s: float) -> float:
    """Heuristic solver runtime (s) for the confirm card (FIRE-1 calibrated).

    ``5e-6 s`` per cell-simulated-hour + a 15 s container/deck-I/O floor. A
    coarse HINT shown on the confirm card — never narrated as a measurement.
    """
    sim_hours = max(float(duration_s), 0.0) / 3600.0
    return max(
        _ELMFIRE_RUNTIME_FLOOR_S,
        _ELMFIRE_SEC_PER_CELL_HOUR * max(int(n_cells), 0) * sim_hours,
    )


# --------------------------------------------------------------------------- #
# Staging — the run_solver manifest (backend-aware).
# --------------------------------------------------------------------------- #
def _deck_files(deck_dir: Path) -> list[Path]:
    """Every file under ``<deck_dir>/inputs`` (rasters + elmfire.data)."""
    inputs_dir = deck_dir / "inputs"
    return sorted(p for p in inputs_dir.iterdir() if p.is_file())


def stage_elmfire_manifest(
    deck_dir: str | Path,
    deck_manifest: dict[str, Any],
    run_args: ElmfireRunArgs,
    *,
    run_id: str | None = None,
) -> ElmfireStaging:
    """Stage the run_solver manifest for the built deck; return the staging.

    The manifest is written to ``<deck_dir>/manifest.json`` with ``file://``
    input refs. ``launch_local_solver`` copies each input into the rundir by
    scheme and the docker spec runs the FIRE-1 image against the mounted rundir.
    No object store is touched at staging time. (The AWS Batch staging lane was
    removed with the batch arm; local-docker is the only backend.)

    Raises ``ElmfireWorkflowError("ELMFIRE_STAGING_FAILED")`` on any staging
    failure (a run cannot dispatch without a reachable manifest — fail loudly).
    """
    deck_dir = Path(deck_dir)
    rid = run_id or new_ulid()
    grid = deck_manifest.get("grid") or {}
    n_cells = int(grid.get("nx", 0)) * int(grid.get("ny", 0))

    files = _deck_files(deck_dir)
    if not files:
        raise ElmfireWorkflowError(
            "ELMFIRE_STAGING_FAILED",
            f"deck at {deck_dir} has no inputs/ files to stage",
        )

    manifest_dict: dict[str, Any] = {
        "engine": ELMFIRE_SOLVER_NAME,
        "run_id": rid,
        "elmfire_args": ["./inputs/elmfire.data"],
        "outputs": list(ELMFIRE_OUTPUT_GLOBS),
        "build_spec": {
            "grid": dict(grid),
            "aoi_bbox_4326": deck_manifest.get("aoi_bbox_4326"),
            "duration_s": deck_manifest.get("duration_s"),
            "weather": deck_manifest.get("weather"),
            "ignitions_lonlat": deck_manifest.get("ignitions_lonlat"),
        },
    }

    inputs = [
        {"gs_uri": f"file://{p}", "dest": f"inputs/{p.name}"} for p in files
    ]
    manifest_dict["inputs"] = inputs
    manifest_path = deck_dir / "manifest.json"
    try:
        manifest_path.write_text(json.dumps(manifest_dict, indent=2))
    except OSError as exc:
        raise ElmfireWorkflowError(
            "ELMFIRE_STAGING_FAILED",
            f"could not write local manifest {manifest_path}: {exc}",
        ) from exc
    manifest_uri = f"file://{manifest_path}"
    logger.info(
        "stage_elmfire_manifest (local) run_id=%s deck=%s files=%d -> %s",
        rid, deck_dir, len(files), manifest_uri,
    )
    return ElmfireStaging(
        run_id=rid,
        manifest_uri=manifest_uri,
        deck_dir=str(deck_dir),
        deck_manifest=deck_manifest,
        run_args=run_args,
        n_cells=n_cells,
        staged_inputs=inputs,
    )


# --------------------------------------------------------------------------- #
# LocalSolverSpec — the FIRE-1 proven container via docker run.
# --------------------------------------------------------------------------- #
def _resolve_docker_host() -> str | None:
    """Resolve the DOCKER_HOST override for the ELMFIRE local runs.

    Order: ``TRID3NT_ELMFIRE_DOCKER_HOST`` env -> the ambient ``DOCKER_HOST``
    (no override needed) -> the per-user ROOTLESS docker socket when it exists
    (``unix:///run/user/<uid>/docker.sock`` — the FIRE-1 proof environment,
    where the rootful daemon socket is not readable by this user) -> ``None``
    (inherit the environment unchanged).
    """
    explicit = (os.environ.get("TRID3NT_ELMFIRE_DOCKER_HOST") or "").strip()
    if explicit:
        return explicit
    if (os.environ.get("DOCKER_HOST") or "").strip():
        return None  # ambient env already points at a daemon
    try:
        uid = os.getuid()
    except AttributeError:  # non-POSIX — nothing to probe
        return None
    rootless = Path(f"/run/user/{uid}/docker.sock")
    if rootless.exists():
        return f"unix://{rootless}"
    return None


def elmfire_local_spec() -> Any:
    """Build the ELMFIRE ``LocalSolverSpec`` (docker runner, FIRE-1 image).

    ``launch_local_solver`` stages the deck's ``inputs/`` into the rundir,
    then this spec launches::

        docker run --rm --name <run_id> --cpus <N> -v <rundir>:/deck -w /deck \
            trid3nt/elmfire:dev bash -c \
            'mkdir -p outputs scratch && elmfire_2025.0526 ./inputs/elmfire.data'

    ``mkdir -p outputs scratch`` recreates the deck's (empty, unstaged) solver
    dirs inside the rundir. The container name == run_id is the Invariant-8
    cancel seam (``docker kill <run_id>``). DOCKER_HOST is threaded through
    ``env_overrides`` (rootless-docker aware — see ``_resolve_docker_host``).
    """
    from ..tools.solver import LOCAL_DOCKER_WORKFLOW_NAME, LocalSolverSpec

    image = os.environ.get("TRID3NT_ELMFIRE_IMAGE") or DEFAULT_ELMFIRE_IMAGE
    binary = os.environ.get("TRID3NT_ELMFIRE_BINARY") or DEFAULT_ELMFIRE_BINARY
    cpus = os.environ.get("TRID3NT_ELMFIRE_CPUS") or DEFAULT_ELMFIRE_CPUS
    docker_host = _resolve_docker_host()

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        solver_args = [str(a) for a in (args or ["./inputs/elmfire.data"])]
        inner = (
            "mkdir -p outputs scratch && "
            + shlex.quote(binary)
            + " "
            + " ".join(shlex.quote(a) for a in solver_args)
        )
        return [
            "docker", "run", "--rm",
            "--name", run_id,
            "--cpus", str(cpus),
            "-v", f"{rundir}:/deck",
            "-w", "/deck",
            image,
            "bash", "-c", inner,
        ]

    return LocalSolverSpec(
        solver=ELMFIRE_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="elmfire_args",
        build_argv=build_argv,
        stdout_name="elmfire.stdout",
        stderr_name="elmfire.stderr",
        stdout_uri_field="elmfire_stdout_uri",
        stderr_uri_field="elmfire_stderr_uri",
        exec_kind="docker",
        classify_exit=None,
        env_overrides={"DOCKER_HOST": docker_host} if docker_host else None,
    )


# --------------------------------------------------------------------------- #
# Registration (mirrors register_geoclaw_solver / register_geoclaw_local_spec).
# --------------------------------------------------------------------------- #
def register_elmfire_solver() -> None:
    """Register ``'elmfire'`` in ``SOLVER_WORKFLOW_REGISTRY`` (idempotent).

    ``run_solver`` only needs the KEY present to dispatch; the local-docker
    backend seam routes runs to :func:`elmfire_local_spec`. (The registry value
    is a presence-gate only; the local sentinel is used since the AWS Batch arm
    was removed.)
    """
    from ..tools.solver import LOCAL_DOCKER_WORKFLOW_NAME, SOLVER_WORKFLOW_REGISTRY

    SOLVER_WORKFLOW_REGISTRY.setdefault(
        ELMFIRE_SOLVER_NAME, LOCAL_DOCKER_WORKFLOW_NAME
    )


def register_elmfire_local_spec() -> None:
    """Register the ELMFIRE LocalSolverSpec factory (local-docker backend)."""
    from ..tools.solver import register_local_solver_spec

    register_local_solver_spec(ELMFIRE_SOLVER_NAME, elmfire_local_spec)


# Register at import so run_solver(solver='elmfire') is wired wherever this
# module is imported (the composer + the tool wrapper both import it).
register_elmfire_solver()
register_elmfire_local_spec()
