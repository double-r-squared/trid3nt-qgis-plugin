"""RERUN-WITH-OVERRIDES: derive a run from a parent run, with named values replaced.

The universal recalibration interface. One question, asked again with something
moved: a failed run retried with the value that failed corrected, a what-if fan
off one parent, a calibration loop's next step. All three are this, and the loop
is only this driven by a proposer.

What derivation means, precisely:

* The sheet comes from the PARENT, not from the wire. Its own overrides seat
  through the USER door, labelled as an override of the run they came from, and
  the derivations that read them re-derive - while a value the parent's user
  pinned keeps its precedence, because a derivation is not a licence to overwrite
  somebody's explicit answer.
* The reach of the overrides is read off the PLAN (``reuse.py``). Work the
  overrides do not reach is inherited: the parent's own ledger records are
  planted under the child's key, so the interpreter's ordinary resume path
  replays them and the artifacts the child reuses are the parent's own objects,
  byte for byte, because the record carries their URIs.
* From the sheet on, the child is an ordinary run. It gates, it ledgers, it
  journals, it publishes and it leaves a snapshot of its own - so a child can be
  a parent, which is what a calibration loop is made of.

CONSTANT-door params ARE overridable here. The constant door governs what the
MODEL's plan schema offers, and this is not that surface: recalibration is the
sanctioned way a constant moves, and it moves by being NAMED.
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


async def rerun(parent_run_id: str, overrides: Mapping[str, Any]) -> Any:
    """Derive a child run from ``parent_run_id`` with ``overrides`` seated on it."""
    if not isinstance(parent_run_id, str) or not parent_run_id.strip():
        raise RerunRefused("run_id must be the id of a run to derive from.",
                           error_code="RERUN_PARENT_UNKNOWN")
    if not isinstance(overrides, Mapping) or not overrides:
        raise RerunRefused(
            f"rerunning {parent_run_id} with no overrides would reproduce it "
            "exactly. Name at least one value to move.",
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

    parent = ResolvedParams({row.name: row for row in snap.sheet})
    note = f"override of run {parent_run_id}"
    child, changed = reseat_revised(workflow.params, parent, overrides,
                                    note=note, door=doors.USER)
    if not changed:
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
                               tuple(changed) + tuple(rederived))
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
                       input_mode=snap.input_mode),
        snap.workflow)
    await ledger.seed(inherited, inherited_data)
    logger.info("%s deriving from run %s: overrode %s (re-derived %s); cut at node "
                "%d, inheriting %d node records + data %s",
                snap.workflow, parent_run_id, sorted(changed), sorted(rederived),
                cut, len(inherited), sorted(reusable))

    return await workflow.execute(
        child, input_mode=snap.input_mode, resume=True, supplied=snap.supplied,
        derived_from=Derivation(parent_run_id=parent_run_id,
                                overrides=tuple(changed)))


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
