"""The SOLVE step: stage the manifest, dispatch the worker, wait, surface the gates.

TELEMAC is LOCAL-DOCKER / worker-image only, so the dispatch always goes through
the generic ``run_solver`` seam. Meshing and bank sampling happen INSIDE the
container, which is why the worker's own typed refusals (no NHDArea coverage, a
degenerate reach) reach the server through ``telemac_metrics.json`` and are
re-raised here as the retryable gates that carry their retry suggestions.

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
from .errors import (
    TelemacBanksUnavailableError,
    TelemacDyeScenarioError,
    TelemacReachDegenerateError,
    TelemacReleasePointRejectedError,
)
from .reach import MESH_NODE_CAP, estimate_telemac_solve_seconds

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.solve")

__all__ = [
    "Solve",
    "download_result_selafin",
    "raise_if_banks_unavailable",
    "raise_if_reach_degenerate",
    "raise_if_release_point_rejected",
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


def raise_if_banks_unavailable(metrics: dict[str, Any]) -> None:
    """Surface the worker's banks gate as the typed, retryable error. No-op otherwise."""
    if str(metrics.get("error_code") or "") == "TELEMAC_BANKS_UNAVAILABLE":
        raise TelemacBanksUnavailableError(metrics.get("assumed_channel_width_m"))


def raise_if_reach_degenerate(metrics: dict[str, Any]) -> None:
    """Surface the worker's degenerate-reach gate as the typed, retryable error."""
    if str(metrics.get("error_code") or "") == "TELEMAC_REACH_DEGENERATE":
        raise TelemacReachDegenerateError(
            metrics.get("reach_length_m"), metrics.get("degenerate_channel_width_m"))


def raise_if_release_point_rejected(metrics: dict[str, Any], *,
                                    requested: bool) -> None:
    """Refuse when the worker could not put the source at the supplied point.

    The worker accept-radius-tests a supplied release point against the built mesh
    and, on a miss, walks ``spill_fraction`` instead - it records the miss rather
    than failing, because only the caller knows whether that point was asked for.
    A point that WAS asked for and was not honored is a relocated release: the run
    refuses instead of solving a source the user never placed.
    """
    if not requested:
        return
    rejected = metrics.get("release_point_rejected_dist_m")
    if metrics.get("release_point_used") or rejected is None:
        return
    raise TelemacReleasePointRejectedError(
        rejected, metrics.get("centerline_length_m"), metrics.get("bank_width_mean_m"))


def download_result_selafin(run_id: str) -> tuple[str, int]:
    """Download ``r2d_river.slf`` + read ``utm_epsg``. Returns ``(local_path, epsg)``.

    A SELAFIN carries no CRS of its own, so the run is ungeoreferenceable without
    the metrics' UTM zone - which makes a missing zone a typed failure, not a
    guess.
    """
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    utm_epsg: int | None = None
    try:
        obj = s3.get_object(Bucket=runs_bucket, Key=f"{run_id}/telemac_metrics.json")
        metrics = json.loads(obj["Body"].read().decode("utf-8"))
        if isinstance(metrics, dict) and metrics.get("utm_epsg") is not None:
            utm_epsg = int(metrics["utm_epsg"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac: metrics read failed for run %s: %s", run_id, exc)

    slf_key = f"{run_id}/r2d_river.slf"
    slf_path = str(Path(tempfile.mkdtemp(prefix=f"telemac-dye-{run_id}-"))
                   / "r2d_river.slf")
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=slf_key)
        with open(slf_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {run_id} completed but s3://{runs_bucket}/{slf_key} "
            f"was not downloadable: {exc}") from exc

    if utm_epsg is None:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_OUTPUT_MISSING",
            f"TELEMAC run {run_id} produced no utm_epsg in telemac_metrics.json; "
            "cannot georeference the SELAFIN mesh.")
    return slf_path, utm_epsg


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
    manifest_uri = await asyncio.to_thread(stage_manifest, reach, run_tag)
    logger.info("telemac staged manifest run_tag=%s seed=(%.5f,%.5f) seed_source=%s "
                "reach=%s -> %s", run_tag, reach["seed_lon"], reach["seed_lat"],
                deck.get("seed_source"), reach["name"], manifest_uri)

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
        # A worker that aborted on a typed gate surfaces THAT gate (with its retry
        # suggestions) rather than a generic run-failed error.
        degraded = await asyncio.to_thread(read_run_metrics, batch_run_id)
        raise_if_banks_unavailable(degraded)
        raise_if_reach_degenerate(degraded)
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_RUN_FAILED",
            "TELEMAC dye solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}")

    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    # The release point is reconciled HERE, against the solved mesh: the accept
    # test lives in the worker (it is the mesh that decides), so the server can
    # only learn the verdict once the run has written its metrics. Before the
    # postprocess, so a relocated release never becomes a published product.
    raise_if_release_point_rejected(
        metrics, requested=reach.get("release_lon") is not None)
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
        "bank_provenance": str(metrics.get("bank_source") or "constant_ribbon"),
    }


class Solve:
    """Solver dispatch steps. The plan's consequential node."""

    @staticmethod
    def telemac(*, deck: Any, compute_class: Any) -> Step:
        """Dispatch the staged reach to the TELEMAC worker and wait for the result."""
        return Step(runner=f"{_STEPS}.solve.solve_reach",
                    kwargs={"deck": deck, "compute_class": compute_class},
                    consequential=True)
