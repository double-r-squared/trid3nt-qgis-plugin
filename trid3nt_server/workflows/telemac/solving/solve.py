"""The SOLVE step: stage the manifest, dispatch the worker, wait, surface the gates.

The container is the ENGINE ROOM: it meshes nothing and fetches nothing, so
nothing here re-raises a worker gate. This is the plan's only CONSEQUENTIAL node
and its result carries the result SELAFIN's URI, which a ledger replay probes."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_server.workflows.runtime import Step

from ..helpers.errors import OpenWaterError, TelemacDyeScenarioError
from ..helpers.reach import MESH_NODE_CAP, estimate_telemac_solve_seconds
from .run_telemac import TELEMAC_SOLVER_NAME

logger = logging.getLogger("trid3nt_server.workflows.telemac.solving.solve")

__all__ = [
    "Solve",
    "compute_class",
    "dispatch_and_wait",
    "download_result",
    "read_run_metrics",
    "solve_case",
    "solve_reach",
]

_SOLVING = "trid3nt_server.workflows.telemac.solving"

#: Floor on the completion wait: the worst honest mesh (the node cap) with 1.5x
#: headroom bounds the rest. A cap-sized solve outruns the default wait, and the
#: publish leg is then lost to the timeout.
_MIN_WAIT_S = 1800.0
_WAIT_HEADROOM = 1.5

#: Wall-clock ceiling on one authored case. A real catchment is tens of thousands
#: of elements over hours of simulated time at a 3 s step, which is an HOURS-class
#: solve. The number is a bound on the wait, not an estimate of the run: it exists
#: so a wedged container becomes a typed failure instead of a daemon that never
#: returns.
_CASE_TIMEOUT_S = 86400.0


async def dispatch_and_wait(*, manifest_uri: str, compute_class: str,
                           module: str, label: str, timeout_s: float,
                           grid_resolution_m: float | None = None,
                           active_cell_count: int | None = None) -> tuple[Any, str]:
    """Dispatch a staged manifest, drive the cards, wait, and hand back the result.

    Judges nothing: a non-complete status is the caller's error to raise."""
    from trid3nt_server.emission.pipeline_emitter import (
        current_emitter,
        mint_dispatch_and_sim_cards,
        route_sim_terminal,
    )
    from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress
    from trid3nt_server.workflows.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    emitter = current_emitter()
    handle = run_solver(solver=TELEMAC_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class=compute_class)
    run_id = handle.run_id
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=TELEMAC_SOLVER_NAME, module=module, handle=handle,
        compute_class=compute_class)
    if emitter is not None and sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=sim_step_id))
    progress = asyncio.ensure_future(drive_live_solve_progress(
        emitter=emitter, run_id=run_id, solver=TELEMAC_SOLVER_NAME,
        grid_resolution_m=grid_resolution_m, active_cell_count=active_cell_count,
        vcpus=None, eta_seconds=None))

    run_result = None
    try:
        run_result = await wait_for_completion(handle, timeout_s=timeout_s)
    except asyncio.CancelledError:
        logger.info("telemac %s solve cancelled awaiting solver", label)
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    finally:
        progress.cancel()
        try:
            await progress
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        set_emitter_binding(None)
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)
    return run_result, (getattr(run_result, "run_id", None) or run_id)


def read_run_metrics(run_id: str) -> dict[str, Any]:
    """Best-effort read of ``<run_id>/telemac_metrics.json``; ``{}`` on any miss.

    Uploaded even on a FAILED run, so a worker-side error_code reaches here."""
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    try:
        obj = _get_s3_client().get_object(
            Bucket=_get_runs_bucket(), Key=f"{run_id}/telemac_metrics.json")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001 -- absence => no typed gate to surface
        logger.info("telemac: run metrics read miss for %s: %s", run_id, exc)
        return {}


def download_result(run_id: str, basename: str, *,
                    error_code: str = "TELEMAC_OUTPUT_MISSING") -> str:
    """Download one of a run's result files to a local path a postprocess reads.

    The UTM zone is NOT re-read here; it is already on the solve result."""
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    runs_bucket = _get_runs_bucket()
    local = str(Path(tempfile.mkdtemp(prefix=f"telemac-{run_id}-")) / basename)
    try:
        body = _get_s3_client().get_object(
            Bucket=runs_bucket, Key=f"{run_id}/{basename}")["Body"].read()
        with open(local, "wb") as fh:
            fh.write(body)
    except Exception as exc:  # noqa: BLE001
        raise OpenWaterError(
            f"TELEMAC run {run_id} completed but s3://{runs_bucket}/{run_id}/"
            f"{basename} was not downloadable: {exc}",
            error_code=error_code) from exc
    return local


async def solve_reach(*, run: dict[str, Any],
                      compute_class: str = "medium") -> dict[str, Any]:
    """Run the staged reach through the TELEMAC worker and return the run handle.

    The returned ``uri`` is the result SELAFIN a ledger replay probes."""
    from trid3nt_server.workflows.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )
    from trid3nt_server.emission.pipeline_emitter import (
        current_emitter,
        mint_dispatch_and_sim_cards,
        route_sim_terminal,
    )
    from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress
    # What the server already knows and the worker cannot learn from the files
    # it is handed. The wait is bounded off the deck's own horizon and step.
    facts = run["case"]["server_facts"]
    manifest_uri = run["manifest_uri"]
    logger.info("telemac dispatching run_tag=%s reach=%s -> %s",
                run["run_tag"], facts["name"], manifest_uri)

    emitter = current_emitter()
    handle = run_solver(solver=TELEMAC_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class=compute_class)
    run_id = handle.run_id

    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=TELEMAC_SOLVER_NAME,
        module=str(run["case"]["module"]), handle=handle,
        compute_class=compute_class)
    if emitter is not None and sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=sim_step_id))

    progress = asyncio.ensure_future(drive_live_solve_progress(
        emitter=emitter, run_id=run_id, solver=TELEMAC_SOLVER_NAME,
        grid_resolution_m=None, active_cell_count=None, vcpus=None, eta_seconds=None))

    wait_s = max(_MIN_WAIT_S, estimate_telemac_solve_seconds(
        MESH_NODE_CAP, float(facts["duration_s"]),
        float(facts["time_step_s"])) * _WAIT_HEADROOM)
    run_result = None
    try:
        run_result = await wait_for_completion(handle, timeout_s=wait_s)
    except asyncio.CancelledError:
        logger.info("telemac solve cancelled awaiting solver")
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    finally:
        progress.cancel()
        try:
            await progress
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        set_emitter_binding(None)

    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    batch_run_id = getattr(run_result, "run_id", None) or run_id
    if run_result is None or run_result.status != "complete":
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_RUN_FAILED",
            "TELEMAC dye solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}")

    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    if metrics.get("utm_epsg") is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {batch_run_id} produced no utm_epsg in "
            "telemac_metrics.json; cannot georeference the SELAFIN mesh.")
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket

    # No local path travels out of this step: it is the ledger's record of the
    # solve, and a replayed record must not hand back a temp file that a later
    # process cannot see. The products step re-downloads from the run prefix,
    # which the replay probe has just confirmed is still there.
    return {
        "run_id": batch_run_id,
        "uri": (f"s3://{_get_runs_bucket()}/{batch_run_id}/"
                f"{run['result_basename']}"),
        "utm_epsg": int(metrics["utm_epsg"]),
        "metrics": metrics,
        "started_at": _run_start_iso(run_result),
    }


def _run_start_iso(run_result: Any) -> str | None:
    """When the solve began, as the mesh's time origin.

    A SELAFIN records no origin; a run with no ``started_at`` states none."""
    started = getattr(run_result, "started_at", None)
    return started.isoformat() if started is not None else None


#: The compute ladder the dispatcher knows. Anything outside it is a model
#: invention, and it crashes the dispatch after the geocode and river fetch.
_ALLOWED_COMPUTE = frozenset(
    {"small", "medium", "standard", "large", "xlarge", "gpu"})


def compute_class() -> Any:
    """A coercion pinning a SUPPLIED ``compute_class`` to a rung the dispatcher serves.

    An ABSENT rung leaves no row: it would resolve as user-supplied."""

    def _coerce(args: Any) -> dict[str, Any]:
        raw = args.get("compute_class")
        value = str(raw or "").strip().lower()
        if not value:
            return {}
        if value not in _ALLOWED_COMPUTE:
            # REFUSED, not substituted. Silently seating 'medium' gave a caller
            # who asked for 'xlarge' a medium solve, no provenance row saying so,
            # and a warning only the log ever saw.
            raise TelemacDyeScenarioError(
                "TELEMAC_COMPUTE_CLASS_UNKNOWN",
                f"compute_class {raw!r} is not a rung this dispatcher serves; the "
                f"ladder is {sorted(_ALLOWED_COMPUTE)}. Omit it to take the "
                "template's declared default.")
        return {"compute_class": value}

    _coerce.__name__ = "compute_class"
    return _coerce


async def solve_case(*, run: dict[str, Any],
                     compute_class: str = "medium") -> dict[str, Any]:
    """Dispatch the staged case to the worker and wait -> the run handle.

    The UTM zone comes from the ASSEMBLER: the worker never learns it."""
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket

    facts = run["case"]["server_facts"]
    label = str(facts["name"])
    logger.info("telemac case dispatching run_tag=%s name=%s -> %s",
                run["run_tag"], facts["name"], run["manifest_uri"])
    run_result, batch_run_id = await dispatch_and_wait(
        manifest_uri=run["manifest_uri"], compute_class=compute_class,
        module=str(run["case"]["module"]), label=label,
        timeout_s=_CASE_TIMEOUT_S, grid_resolution_m=facts.get("mesh_size_m"),
        active_cell_count=facts.get("nelem"))
    if run_result is None or run_result.status != "complete":
        raise OpenWaterError(
            f"the {label} solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            error_code="TELEMAC_RUN_FAILED")
    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    return {
        "run_id": batch_run_id,
        "uri": (f"s3://{_get_runs_bucket()}/{batch_run_id}/"
                f"{run['result_basename']}"),
        "utm_epsg": int(facts["utm_epsg"]), "metrics": metrics,
        "started_at": _run_start_iso(run_result),
    }



class Solve:
    """Solver dispatch steps. The plan's consequential node."""

    @staticmethod
    def telemac(*, run: Any, compute_class: Any) -> Step:
        """Dispatch the staged reach to the TELEMAC worker and wait for the result."""
        return Step(runner=f"{_SOLVING}.solve.solve_reach", stage="solve",
                    kwargs={"run": run, "compute_class": compute_class},
                    consequential=True)
