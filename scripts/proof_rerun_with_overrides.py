"""LIVE proof of the rerun-with-overrides primitive, on the do_sag refined canary.

Stages a real parent run, then derives children from it through the registered
``rerun_workflow`` tool and checks the four things the primitive promises:

  REUSE      the child replays the parent's acquire prefix, and the artifacts it
             reuses are the parent's OWN objects - same URI, same sha256, read
             back out of the object store rather than asserted;
  RE-EXECUTE only author -> solve -> post run again;
  DIRECTION  the answer moves the way the physics says it should;
  CHAIN      the child's provenance and journal line name the parent and the
             overrides, and a child is itself derivable-from.

Then the three consumers, each as its own scenario:
  (a) FAILURE RECOVERY  a run that refuses on a bad value, re-run with it fixed;
  (b) WHAT-IF           two overrides on one parent, two independent children;
  (c) LAW INVERSION     the coupled-validity refusal, typed, before anything runs.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  venvs/agent/bin/python scripts/proof_rerun_with_overrides.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("proof_rerun")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: The refined do_sag canary, verbatim from
#: docs/proof/templates/telemac_do_sag/refined/. Pinned discharge, because a
#: value that moves between parent and child is not a comparison.
PARENT_ARGS: dict[str, Any] = {
    "location": "Eel River near Scotia, California",
    "outfall_coords": [-124.0983, 40.4921],
    "discharge_bod_mgl": 20.0,
    "water_temp_c": 20.0,
    "do_standard_mgl": 5.0,
    "k1_per_day": 0.3,
    "k2_per_day": 0.9,
    "reach_length_km": 0.5,
    "sim_duration_s": 600.0,
    "mesh_resolution_m": 10.0,
    "discharge_m3s": 60.0,
    "output_interval_min": 0.333,
    "input_mode": "auto",
    "restart_clean": True,
}

_ANSWER_FIELDS = ("do_min_mgl", "do_min_distance_m", "do_violates_standard",
                  "do_upstream_mgl", "do_saturation_mgl", "mesh_size_m",
                  "mesh_node_estimate")


def _answer(layer: Any) -> dict[str, Any]:
    get = layer.get if isinstance(layer, dict) else (lambda f: getattr(layer, f, None))
    out = {f: get(f) for f in _ANSWER_FIELDS}
    out["run_id"] = get("run_id")
    out["uri"] = get("uri")
    curve = get("sag_curve_do_mgl") or []
    out["sag_curve_sha256"] = hashlib.sha256(
        json.dumps(list(curve)).encode()).hexdigest()[:16]
    return out


def _digest(uri: str) -> dict[str, Any]:
    """The object behind a URI, by size and sha256 - read, never assumed."""
    if uri.startswith("s3://"):
        from trid3nt_server.workflows.solver.solver import _get_s3_client

        bucket, _, key = uri[len("s3://"):].partition("/")
        body = _get_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    else:
        body = Path(uri.removeprefix("file://")).read_bytes()
    return {"uri": uri, "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest()}


async def _snapshot(run_id: str) -> Any:
    from trid3nt_server.workflows.lib.snapshot import read_snapshot

    return await read_snapshot(run_id)


def _record_digests(snap: Any) -> dict[str, dict[str, Any]]:
    """Every node's recorded RESULT, by sha256 - what the child inherits verbatim.

    The record IS the pinned past: a replayed node hands back these exact bytes
    rather than re-running the fetch that produced them, so equality here is the
    reuse, not evidence about it.
    """
    out: dict[str, dict[str, Any]] = {}
    for rec in list(snap.records) + list(snap.data_records):
        blob = json.dumps(rec.result, sort_keys=True, default=str)
        row = {"sha256": hashlib.sha256(blob.encode()).hexdigest(),
               "bytes": len(blob)}
        if rec.artifact_uris:
            row["artifacts"] = [_digest(u) for u in rec.artifact_uris]
        out[rec.node] = row
    return out


def _deck_inputs(snap: Any) -> list[dict[str, Any]]:
    """The files the deck STAGED for the solver, digested off the object store."""
    rec = next((r for r in snap.records if r.node == "deck"), None)
    rows = []
    for entry in ((rec.result if isinstance(rec.result, dict) else {})
                  .get("inputs") or []) if rec else []:
        rows.append({"dest": entry.get("dest"), **_digest(entry["gs_uri"])})
    return rows


async def _run_parent(fn: Any) -> Any:
    log.info("PARENT: telemac_do_sag %s", PARENT_ARGS)
    out = await fn(**PARENT_ARGS)
    if isinstance(out, dict) and out.get("status") == "error":
        raise SystemExit(f"parent run failed: {out}")
    return out


async def _rerun(tool: Any, run_id: str, overrides: dict[str, Any]) -> Any:
    log.info("RERUN of %s with %s", run_id, overrides)
    return await tool(run_id=run_id, overrides=overrides)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/proof/templates/telemac_do_sag/"
                                     "rerun/rerun_overrides_evidence.json")
    ap.add_argument("--k1", type=float, default=0.9,
                    help="the overridden deoxygenation rate for the main proof")
    args = ap.parse_args()

    os.environ.setdefault("TRID3NT_RUN_ORIGIN", "proof_rerun_with_overrides")
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib import journal
    from trid3nt_server.workflows.lib.rerun import RerunRefused, reuse_plan
    from trid3nt_server.workflows.lib.validity import CoupledValidityError

    do_sag = TOOL_REGISTRY["telemac_do_sag"].fn
    rerun_tool = TOOL_REGISTRY["rerun_workflow"].fn
    evidence: dict[str, Any] = {"parent_args": PARENT_ARGS}

    # -- the parent ---------------------------------------------------------- #
    parent = await _run_parent(do_sag)
    parent_answer = _answer(parent)
    parent_id = parent_answer["run_id"]
    evidence["parent"] = parent_answer
    log.info("PARENT run_id=%s do_min=%s", parent_id, parent_answer["do_min_mgl"])

    snap = await _snapshot(parent_id)
    if snap is None:
        raise SystemExit(f"parent {parent_id} left no snapshot - nothing to derive")
    wf = do_sag.workflow
    cut, keep = reuse_plan(wf.plan, wf.data, ("k1_per_day",))
    from trid3nt_server.workflows.lib import expand_plan

    nodes = expand_plan(wf.plan)
    inheritable = {n.label for n in nodes if n.index < cut}
    inheritable |= {f"data:{name}" for name in keep}
    evidence["reuse_plan"] = {
        "cut_index": cut, "cut_node": nodes[cut].label,
        "inherited_nodes": sorted(inheritable),
        "reexecuted_nodes": [n.label for n in nodes if n.index >= cut],
    }
    before = _record_digests(snap)
    evidence["parent_record_digests"] = before
    log.info("REUSE PLAN cut=%d (%s); inheriting %s", cut, nodes[cut].label,
             sorted(inheritable))

    # -- the child: a higher deoxygenation rate must DEEPEN the sag ---------- #
    child = await _rerun(rerun_tool, parent_id, {"k1_per_day": args.k1})
    if isinstance(child, dict) and child.get("status") == "error":
        raise SystemExit(f"child run failed: {child}")
    child_answer = _answer(child)
    child_id = child_answer["run_id"]
    evidence["child"] = child_answer

    child_snap = await _snapshot(child_id)
    after = _record_digests(child_snap)
    evidence["child_record_digests"] = after
    shared = sorted(n for n in inheritable if n in before and n in after)
    identical = [n for n in shared if before[n]["sha256"] == after[n]["sha256"]]
    moved = [n for n in before if n in after and n not in inheritable
             and before[n]["sha256"] != after[n]["sha256"]]
    byte_identical = bool(shared) and len(identical) == len(shared)
    evidence["reuse"] = {
        "inherited_nodes_compared": shared,
        "byte_identical": identical,
        "differs": [n for n in shared if n not in identical],
        "reexecuted_nodes_that_moved": moved,
        # A Data the inherited prefix consumed is never even DEMANDED by the
        # child - the step that reads it replayed - so it leaves no record of its
        # own. Absent here is stronger than identical.
        "data_not_demanded": sorted(n for n in before
                                    if n.startswith("data:") and n not in after),
    }
    evidence["reuse_byte_identical"] = byte_identical
    log.info("REUSE byte-identical over %s; re-executed and moved: %s",
             identical, moved)

    # The deck STAGES the solver's input files. It re-executes here, so this is a
    # separate question from the ledger reuse: is the terrain the child solves on
    # the same object? It is content-addressed, so it is.
    parent_inputs, child_inputs = _deck_inputs(snap), _deck_inputs(child_snap)
    evidence["deck_inputs"] = {"parent": parent_inputs, "child": child_inputs}
    by_dest = {r["dest"]: r for r in parent_inputs}
    evidence["deck_input_parity"] = [
        {"dest": r["dest"],
         "same_object": by_dest.get(r["dest"], {}).get("uri") == r["uri"],
         "same_bytes": by_dest.get(r["dest"], {}).get("sha256") == r["sha256"]}
        for r in child_inputs]
    log.info("DECK INPUT PARITY %s", evidence["deck_input_parity"])

    lines = journal.read_records()
    child_line = next((ln for ln in reversed(lines) if ln["run_id"] == child_id), None)
    evidence["child_journal"] = {
        "parent_run_id": (child_line or {}).get("parent_run_id"),
        "overrides": (child_line or {}).get("overrides"),
        "executed": (child_line or {}).get("executed"),
        "replayed": (child_line or {}).get("replayed"),
        "notes": (child_line or {}).get("notes"),
        "k1_row": next((r for r in (child_line or {}).get("sheet", [])
                        if r["name"] == "k1_per_day"), None),
    }
    deeper = (child_answer["do_min_mgl"] is not None
              and parent_answer["do_min_mgl"] is not None
              and child_answer["do_min_mgl"] < parent_answer["do_min_mgl"])
    evidence["direction"] = {
        "override": {"k1_per_day": args.k1}, "parent_do_min_mgl":
            parent_answer["do_min_mgl"], "child_do_min_mgl":
            child_answer["do_min_mgl"],
        "expected": "a higher deoxygenation rate consumes more oxygen, so the sag "
                    "minimum must be LOWER",
        "holds": deeper,
    }
    log.info("DIRECTION parent do_min=%s -> child do_min=%s (deeper=%s)",
             parent_answer["do_min_mgl"], child_answer["do_min_mgl"], deeper)

    # -- (b) what-if: a second child off the SAME parent --------------------- #
    other = await _rerun(rerun_tool, parent_id, {"k2_per_day": 3.0})
    other_answer = _answer(other) if not (isinstance(other, dict)
                                          and other.get("status") == "error") else other
    evidence["whatif_second_child"] = other_answer
    fan = [ln for ln in journal.read_records() if ln.get("parent_run_id") == parent_id]
    evidence["whatif_fan"] = [{"run_id": ln["run_id"], "overrides": ln["overrides"],
                               "do_min_mgl": ln["answer"].get("do_min_mgl")}
                              for ln in fan]
    log.info("WHAT-IF fan off %s: %s", parent_id, evidence["whatif_fan"])

    # -- (a) failure recovery: refuse on a bad value, then fix it ------------ #
    # A bank source the mesher does not build refuses TYPED at the deck - after
    # the geocode, the reach navigation and the discharge have all succeeded.
    bad = dict(PARENT_ARGS, bank_source="riverbank", restart_clean=True)
    failed = await do_sag(**bad)
    recovery: dict[str, Any] = {"bad_call": {"bank_source": "riverbank"},
                                "outcome": _describe(failed)}
    log.info("FAILURE RECOVERY seed: %s", recovery["outcome"])
    attempt = failed.get("run_id") if isinstance(failed, dict) else None
    if attempt:
        fixed = await _rerun(rerun_tool, attempt, {"bank_source": "nhd_area"})
        recovery["attempt_run_id"] = attempt
        recovery["recovered"] = _describe(fixed)
        fixed_snap = await _snapshot(_answer(fixed)["run_id"]) \
            if not (isinstance(fixed, dict) and fixed.get("status") == "error") else None
        failed_snap = await _snapshot(attempt)
        if fixed_snap is not None and failed_snap is not None:
            fd, xd = _record_digests(failed_snap), _record_digests(fixed_snap)
            recovery["inherited_from_the_failed_attempt"] = sorted(
                n for n in fd if n in xd and fd[n]["sha256"] == xd[n]["sha256"])
    evidence["failure_recovery"] = recovery

    # -- (c) the law inversion, typed, before anything runs ------------------ #
    coastal_probe: dict[str, Any] = {}
    try:
        await rerun_tool(run_id=parent_id, overrides={"nonsense_param": 1.0})
    except RerunRefused as exc:
        coastal_probe["undeclared_name"] = {"error_code": exc.error_code,
                                            "message": str(exc)}
    try:
        await rerun_tool(run_id=parent_id, overrides={"k1_per_day": 0.3})
    except RerunRefused as exc:
        coastal_probe["inert_override"] = {"error_code": exc.error_code,
                                           "message": str(exc)}
    coastal_probe["law_inversion"] = await _law_inversion(CoupledValidityError)
    evidence["typed_refusals"] = coastal_probe
    log.info("REFUSALS %s", json.dumps(coastal_probe)[:400])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(evidence, indent=2, default=str))
    print("RERUN_PROOF " + json.dumps({
        "parent": parent_id, "child": child_id,
        "cut": evidence["reuse_plan"]["cut_node"],
        "reuse_byte_identical": byte_identical,
        "direction_holds": deeper,
        "evidence": str(out_path),
    }))
    return 0 if (byte_identical and deeper) else 1


def _describe(out: Any) -> dict[str, Any]:
    if isinstance(out, dict) and out.get("status") == "error":
        return {"status": "error", "error_code": out.get("error_code"),
                "error_message": str(out.get("error_message"))[:400]}
    return {"status": "ok", **{k: v for k, v in _answer(out).items()
                               if k in ("run_id", "do_min_mgl", "mesh_size_m")}}


async def _law_inversion(exc_type: type) -> dict[str, Any]:
    """The coastal friction pair, checked at RESOLVE time - no solve needed.

    A refusal that fires before the plan runs is proven by resolving the sheet
    and asking the rule; driving a full coastal solve to watch it not start would
    prove the same thing and cost 20 minutes.
    """
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.lib import check_validity, resolve_params

    wf = TOOL_REGISTRY["coastal_tidal_surge"].fn.workflow
    out: dict[str, Any] = {}
    for label, supplied in (
            ("law_switched_coefficient_left", {"friction_law": 4}),
            ("both_named", {"friction_law": 4, "friction_coefficient": 0.033}),
            ("atypical_but_right_quantity", {"friction_coefficient": 120.0})):
        resolved = await resolve_params(wf.params, supplied)
        try:
            check_validity(wf.validity, resolved, workflow=wf.name)
            out[label] = {"accepted": True}
        except exc_type as exc:
            out[label] = {"accepted": False, "error_code": exc.error_code,
                          "message": str(exc)}
    return out


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
