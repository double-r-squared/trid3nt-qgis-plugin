"""The SOLVE step: stage the manifest, dispatch the worker, wait, surface the gates.

TELEMAC is LOCAL-DOCKER / worker-image only, so the dispatch always goes through
the generic ``run_solver`` seam. The container is the ENGINE ROOM: it meshes
nothing and fetches nothing, so no refusal about the reach's geometry can arise
in it. The server chain refuses those before a manifest is ever staged - which is
why nothing here re-raises a worker gate.

This is the plan's only CONSEQUENTIAL node, and its result carries the result
SELAFIN's URI - so a ledger replay probes that the solved artifact still exists
before a rerun skips a 30-minute solve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_server.workflows.lib import Step

from .deck import stage_manifest
from ..helpers.errors import TelemacDyeScenarioError
from ..helpers.reach import MESH_NODE_CAP, estimate_telemac_solve_seconds

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.solve")

__all__ = [
    "Solve",
    "compute_class",
    "download_result_selafin",
    "read_run_metrics",
    "solve_reach",
]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: Floor on the completion wait. The worst honest mesh (the node cap) with 1.5x
#: headroom bounds the rest: a cap-sized solve once outran the default wait and
#: the publish leg was lost to the timeout.
_MIN_WAIT_S = 1800.0
_WAIT_HEADROOM = 1.5


def read_run_metrics(run_id: str) -> dict[str, Any]:
    """Best-effort read of ``<run_id>/telemac_metrics.json``; ``{}`` on any miss.

    The worker uploads this even on a FAILED run (outputs are uploaded before
    completion.json is written), so it is the channel through which a worker-side
    typed error_code reaches the server.
    """
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


def download_result_selafin(run_id: str) -> str:
    """Download ``r2d_river.slf`` to a local path the postprocess can read.

    The UTM zone is NOT re-read here: it is the server's own measurement, echoed
    through the worker's metrics and already on the solve result. Reading it a
    second time from the same file was a second answer that could disagree with
    the first.
    """
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    runs_bucket = _get_runs_bucket()
    slf_key = f"{run_id}/r2d_river.slf"
    slf_path = str(Path(tempfile.mkdtemp(prefix=f"telemac-dye-{run_id}-"))
                   / "r2d_river.slf")
    try:
        resp = _get_s3_client().get_object(Bucket=runs_bucket, Key=slf_key)
        with open(slf_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {run_id} completed but s3://{runs_bucket}/{slf_key} "
            f"was not downloadable: {exc}") from exc
    return slf_path


async def solve_reach(*, deck: dict[str, Any],
                      compute_class: str = "medium") -> dict[str, Any]:
    """Run the staged reach through the TELEMAC worker and return the run handle.

    The returned ``uri`` is the result SELAFIN under the run prefix: it is what a
    ledger replay probes, so a resumed rerun can only skip the solve while the
    solved artifact is still there.
    """
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
    from trid3nt_server.workflows.telemac.run_telemac import TELEMAC_SOLVER_NAME

    reach = deck["deck"]
    run_tag = deck["run_tag"]
    manifest_uri = await asyncio.to_thread(stage_manifest, deck["case"], run_tag,
                                           outputs=deck["outputs"],
                                           inputs=deck.get("inputs"))
    logger.info("telemac staged case run_tag=%s module=%s steering=%s results=%s "
                "reach=%s inputs=%s -> %s", run_tag, deck["case"]["module"],
                deck["case"]["steering"], deck["case"]["results"], reach["name"],
                [row["dest"] for row in (deck.get("inputs") or [])], manifest_uri)

    emitter = current_emitter()
    handle = run_solver(solver=TELEMAC_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class=compute_class)
    run_id = handle.run_id

    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=TELEMAC_SOLVER_NAME, handle=handle,
        compute_class=compute_class)
    if emitter is not None and sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=sim_step_id))

    progress = asyncio.ensure_future(drive_live_solve_progress(
        emitter=emitter, run_id=run_id, solver=TELEMAC_SOLVER_NAME,
        grid_resolution_m=None, active_cell_count=None, vcpus=None, eta_seconds=None))

    wait_s = max(_MIN_WAIT_S, estimate_telemac_solve_seconds(
        MESH_NODE_CAP, float(reach["duration_s"]),
        float(reach["time_step_s"])) * _WAIT_HEADROOM)
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
        "uri": f"s3://{_get_runs_bucket()}/{batch_run_id}/r2d_river.slf",
        "utm_epsg": int(metrics["utm_epsg"]),
        "metrics": metrics,
    }


#: The compute ladder the dispatcher knows. Anything outside it is a model
#: invention that used to crash the dispatch AFTER the geocode and river fetch.
_ALLOWED_COMPUTE = frozenset(
    {"small", "medium", "standard", "large", "xlarge", "gpu"})


def compute_class() -> Any:
    """A coercion pinning a SUPPLIED ``compute_class`` to a rung the dispatcher serves.

    An ABSENT rung leaves no row at all. A coercion's output is merged into the
    door-1 supplied sheet, so a value emitted for an argument nobody sent resolves
    through the USER door and the run's provenance reports the template's own
    default as "supplied on this invocation" - the falsification this abstention
    exists to prevent. The declared constant-door default seats itself instead.
    """

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


class Solve:
    """Solver dispatch steps. The plan's consequential node."""

    @staticmethod
    def telemac(*, deck: Any, compute_class: Any) -> Step:
        """Dispatch the staged reach to the TELEMAC worker and wait for the result."""
        return Step(runner=f"{_STEPS}.solve.solve_reach", stage="solve",
                    kwargs={"deck": deck, "compute_class": compute_class},
                    consequential=True)
