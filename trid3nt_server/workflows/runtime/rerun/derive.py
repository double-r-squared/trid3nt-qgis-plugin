"""RERUN-WITH-OVERRIDES: derive a run from a parent run, with named values replaced.

The sheet comes from the PARENT, not the wire; overrides seat through the USER door
and what reads them re-derives, while a pinned user value keeps its precedence.
A HOT START is the same ledger row with nothing moved: the run named by
``continue_from`` hands over the state it ended at, and the child carries the
same scenario on from there.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ..errors import DeclarativeError
from ..ledger import StepLedger, invocation_key
from ..params import ResolvedParams, doors
from ..resolver import rederive_revised, reseat_revised
from ..snapshot import Derivation, RunSnapshot, read_snapshot
from ..validity import check_validity
from .reuse import reuse_plan

__all__ = ["RerunRefused", "rerun"]

logger = logging.getLogger("trid3nt_server.workflows.runtime.rerun")


class RerunRefused(DeclarativeError):
    """A derivation that cannot honestly be made from the run it names."""

    error_code = "RERUN_REFUSED"


async def rerun(parent_run_id: str, overrides: Mapping[str, Any],
                continue_from: str | None = None) -> Any:
    """Derive a child run from ``parent_run_id`` with ``overrides`` seated on it.

    ``continue_from`` names the run whose ENDING STATE this one opens at."""
    if not isinstance(parent_run_id, str) or not parent_run_id.strip():
        raise RerunRefused("run_id must be the id of a run to derive from.",
                           error_code="RERUN_PARENT_UNKNOWN")
    if (not isinstance(overrides, Mapping) or not overrides) and not continue_from:
        raise RerunRefused(
            f"rerunning {parent_run_id} with no overrides would reproduce it "
            "exactly. Name at least one value to move, or name the run this "
            "one continues.",
            error_code="RERUN_OVERRIDES_EMPTY")

    snap = await read_snapshot(parent_run_id.strip())
    if snap is None:
        raise RerunRefused(
            f"run {parent_run_id!r} has no derivable record. A run is derivable "
            "from once it completes and until its snapshot expires; a run that "
            "never solved leaves none at all.",
            error_code="RERUN_PARENT_UNKNOWN")

    workflow = _workflow(snap)
    declared = {prm.name for prm in workflow.params}
    unknown = sorted(set(overrides) - declared)
    if unknown:
        raise RerunRefused(
            f"{snap.workflow} declares no {unknown}; it takes "
            f"{sorted(declared)}. An override is seated by NAME and is never "
            "absorbed silently.",
            error_code="RERUN_OVERRIDE_UNKNOWN")
    if snap.sheet_names != declared:
        raise RerunRefused(
            f"{snap.workflow} declares different params now than when run "
            f"{parent_run_id} ran (added {sorted(declared - snap.sheet_names)}, "
            f"gone {sorted(snap.sheet_names - declared)}), so that run's sheet is "
            "not one this template can be asked to run. Ask the question fresh.",
            error_code="RERUN_TEMPLATE_CHANGED")

    continued = await _continued_state(continue_from, workflow) \
        if continue_from else None
    parent = ResolvedParams({row.name: row for row in snap.sheet})
    note = f"override of run {parent_run_id}"
    # A CONSTANT-door param IS overridable here: the constant door governs what the
    # model's schema offers, and this is not that surface - recalibration is how a
    # constant moves, and it moves by being NAMED.
    child, changed = reseat_revised(workflow.params, parent, dict(overrides or {}),
                                    note=note, door=doors.USER)
    if not changed and continued is None:
        raise RerunRefused(
            f"every override already equals run {parent_run_id}'s own value "
            f"({sorted(overrides)}), so the child would BE the parent.",
            error_code="RERUN_OVERRIDE_INERT")
    child, rederived, conflicts = await rederive_revised(
        workflow.params, child, changed, occasion=note)
    for conflict in conflicts:
        logger.info("%s rerun of %s: %s", snap.workflow, parent_run_id, conflict)
    # Before anything is written: a derivation that breaks a coupled rule has to
    # refuse as a REFUSAL the caller can act on, and it must not leave a seeded
    # ledger behind for a run that was never allowed to start. ``execute`` checks
    # again for the fresh lane; checking twice costs a predicate call.
    check_validity(workflow.validity, child, workflow=snap.workflow)

    cut, reusable = reuse_plan(workflow.plan, workflow.data,
                               tuple(changed) + tuple(rederived),
                               continued=continued is not None)
    if cut is None:
        raise RerunRefused(
            f"nothing in {snap.workflow} reads {sorted(changed)}, so the child "
            "would reproduce run "
            f"{parent_run_id} exactly. Override a value the plan uses.",
            error_code="RERUN_OVERRIDE_INERT")

    inherited = [rec for rec in snap.records if rec.index < cut]
    inherited_data = [rec for rec in snap.data_records
                      if rec.node.removeprefix("data:") in reusable]
    ledger = await StepLedger.load(
        invocation_key(snap.workflow, child.values_dict(),
                       input_mode=snap.input_mode, continued=continued),
        snap.workflow)
    await ledger.seed(inherited, inherited_data)
    logger.info("%s deriving from run %s: overrode %s (re-derived %s); cut at node "
                "%d, inheriting %d node records + data %s",
                snap.workflow, parent_run_id, sorted(changed), sorted(rederived),
                cut, len(inherited), sorted(reusable))

    return await workflow.execute(
        child, input_mode=snap.input_mode, keywords=snap.keywords, resume=True,
        supplied=snap.supplied, continued=continued,
        derived_from=Derivation(parent_run_id=parent_run_id,
                                overrides=tuple(changed),
                                continued_from=continue_from))


async def _continued_state(run_id: str, workflow: Any) -> str:
    """The artifact a run ENDED at, off its own record -> what the child opens on.

    The solve step's own result: the engine reads that file's last record as
    the initial state, so a run that never solved has no state to hand over."""
    snap = await read_snapshot(str(run_id).strip())
    if snap is None:
        raise RerunRefused(
            f"run {run_id!r} has no record to continue from. A run is "
            "continuable once it completes and until its snapshot expires.",
            error_code="RERUN_CONTINUATION_UNKNOWN")
    solved = [rec for rec in snap.records if rec.node == workflow.solve_step
              and isinstance(rec.result, Mapping) and rec.result.get("uri")]
    if not solved:
        raise RerunRefused(
            f"run {run_id} left no solved state: its {workflow.solve_step!r} "
            "step wrote no result file, and a continuation reads the last "
            "record of one.",
            error_code="RERUN_CONTINUATION_UNSOLVED")
    return str(solved[-1].result["uri"])


def _workflow(snap: RunSnapshot) -> Any:
    """The declared workflow the parent ran, off the live registry."""
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get(snap.workflow)
    workflow = getattr(getattr(entry, "fn", None), "workflow", None)
    if workflow is None:
        raise RerunRefused(
            f"run {snap.run_id} was produced by {snap.workflow!r}, which is not a "
            "declared workflow in this build. Only a declared workflow has a plan "
            "to re-walk.",
            error_code="RERUN_TEMPLATE_CHANGED")
    return workflow
