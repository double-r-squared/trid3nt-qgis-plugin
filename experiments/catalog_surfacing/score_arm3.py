#!/usr/bin/env python3
"""catalog_surfacing -- Design 3 (stratified data pool) deterministic grader.

Grades the arm-3 drive (results/arm3_records.jsonl) against the arm-0 baseline
(results/arm0_records.jsonl), applies the SIGNED Design 3 criteria + the amended
controls gate, and writes results/arm3_comparison.json + results/VERDICT.md.

Design 3 PASS bar (DESIGN.md, recomputed from arm0 here, not hardcoded):
  selection >= arm0 selection AND first-attempt >= arm0 first-attempt - 5 pts
  AND one-retry >= arm0 one-retry AND controls valid (amended gate).
Beat-baseline is a HYPOTHESIS (recorded), not a criterion.

Amended controls gate (SIGNED): a control divergence from arm0 counts against
validity ONLY if it touches the catalog/data surface (arm3 fires fetch_from_catalog
or a spec-served source where arm0 did not) = LEAKAGE -> INVALID. A NO_CALL or
unrelated-tool jitter divergence is re-run N=3 (results/arm3_controls_rerun.jsonl)
with majority grading; residual non-leakage divergence is reported, not invalidating.

UPSTREAM_PROVIDER failures graded UPSTREAM_FAILURE, excluded from both denominators.
ASCII only.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

FIRST_ATTEMPT_SLACK = 5.0  # DESIGN (d): first-attempt >= baseline - 5 points


def _load(arm: str) -> list[dict]:
    p = RESULTS / f"arm{arm}_records.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 2) if den else 0.0


def _grade(records: list[dict]) -> dict:
    sources = [r for r in records if r["group"] != "control"]
    graded = [r for r in sources if not r.get("upstream_failure")]
    upstream = [r for r in sources if r.get("upstream_failure")]

    sel_ok = [r for r in graded if r["selected_name"] == r["acceptable"][0]]
    sel_acc = _pct(len(sel_ok), len(graded))
    fa = sum(1 for r in sel_ok if r.get("first_attempt_valid") is True)
    r1 = sum(1 for r in sel_ok if r.get("one_retry_valid") is True)

    per_source: dict[str, dict] = {}
    by_group: dict[str, list] = {}
    for r in graded:
        by_group.setdefault(r["group"], []).append(r)
    for grp, rs in sorted(by_group.items()):
        hits = sum(1 for r in rs if r["selected_name"] == r["acceptable"][0])
        per_source[grp] = {"n": len(rs), "sel_acc": _pct(hits, len(rs))}

    return {
        "n_source_records": len(sources),
        "n_graded": len(graded),
        "n_upstream_excluded": len(upstream),
        "selection_accuracy": sel_acc,
        "selection_denominator": len(graded),
        "selected_correct": len(sel_ok),
        "first_attempt_validity": _pct(fa, len(sel_ok)),
        "one_retry_validity": _pct(r1, len(sel_ok)),
        "param_denominator": len(sel_ok),
        "per_source": per_source,
    }


def _controls_gate(arm3: list[dict], arm0: list[dict]) -> dict:
    """Amended controls gate: compare arm3 vs arm0 fired names; classify each
    divergence as LEAKAGE (touches the data surface -> invalidates) or JITTER
    (NO_CALL / unrelated tool -> N=3 majority re-run, non-invalidating)."""
    # Data surface = the 14 spec-served source names (every source record's target)
    # plus the composed/generic catalog fetcher itself.
    data_surface = {r["acceptable"][0] for r in arm3 if r["group"] != "control"}
    data_surface |= {"fetch_from_catalog", "search_data_catalog"}

    c3 = {r["id"]: r for r in arm3 if r["group"] == "control"}
    c0 = {r["id"]: r for r in arm0 if r["group"] == "control"}

    rerun_path = RESULTS / "arm3_controls_rerun.jsonl"
    rerun = {}
    if rerun_path.exists():
        for l in rerun_path.read_text().splitlines():
            if l.strip():
                d = json.loads(l)
                rerun[d["id"]] = d

    leakage, jitter, resolved, residual = [], [], [], []
    identical = 0
    for cid in sorted(c3):
        r3, r0 = c3[cid], c0.get(cid)
        n3 = r3["selected_name"]
        n0 = r0["selected_name"] if r0 else None
        acc = r3["acceptable"][0]
        if n3 == n0:
            identical += 1
            continue
        # Divergence. Leakage iff arm3 fired a data-surface tool that arm0 didn't.
        touches_surface = (n3 in data_surface) and (n0 not in data_surface)
        entry = {"id": cid, "acceptable": acc, "arm0": n0, "arm3": n3}
        if touches_surface:
            leakage.append(entry)
            continue
        # Jitter: consult the N=3 majority re-run if present.
        maj = rerun.get(cid, {}).get("majority", "__none__")
        if cid in rerun:
            entry["majority"] = maj
            entry["trials"] = rerun[cid].get("fired_trials")
            # A majority that matches arm0 (or the acceptable tool, or is NOT a
            # data-surface tool) resolves the divergence as model jitter.
            if maj == n0 or maj == acc or (maj not in data_surface):
                resolved.append(entry)
            elif maj in data_surface:
                leakage.append(entry)  # majority leaks -> real
            else:
                residual.append(entry)
        else:
            jitter.append(entry)  # needs N=3 re-run

    return {
        "n_controls": len(c3),
        "identical_across_arms": identical,
        "leakage": leakage,
        "jitter_needs_rerun": jitter,
        "jitter_resolved": resolved,
        "jitter_residual": residual,
        "passes": len(leakage) == 0,
        "rerun_pending": len(jitter) > 0,
    }


def _verdict(g3: dict, g0: dict, ctrl: dict) -> dict:
    sel_bar = g0["selection_accuracy"]
    fa_bar = g0["first_attempt_validity"] - FIRST_ATTEMPT_SLACK
    r1_bar = g0["one_retry_validity"]

    reasons = []
    ok = True
    if g3["selection_accuracy"] < sel_bar:
        ok = False
        reasons.append(f"selection {g3['selection_accuracy']} < arm0 {sel_bar}")
    if g3["first_attempt_validity"] < fa_bar:
        ok = False
        reasons.append(
            f"first-attempt {g3['first_attempt_validity']} < arm0-5 {fa_bar}"
        )
    if g3["one_retry_validity"] < r1_bar:
        ok = False
        reasons.append(f"one-retry {g3['one_retry_validity']} < arm0 {r1_bar}")
    if not ctrl["passes"]:
        ok = False
        reasons.append("controls leakage -> INVALID")

    if ctrl["rerun_pending"]:
        winner = "PENDING_CONTROLS_RERUN"
    elif not ctrl["passes"]:
        winner = "INVALID"
    elif ok:
        winner = "ADVANCE"
    else:
        winner = "NO_ADVANCE"

    beat = g3["selection_accuracy"] > g0["selection_accuracy"]
    return {
        "winner": winner,
        "block_reasons": reasons,
        "bars": {"selection": sel_bar, "first_attempt": round(fa_bar, 2),
                 "one_retry": r1_bar},
        "beat_baseline_hypothesis": {
            "arm0_selection": g0["selection_accuracy"],
            "arm3_selection": g3["selection_accuracy"],
            "beat": beat,
        },
    }


def main() -> int:
    arm3 = _load("3")
    arm0 = _load("0")
    g3 = _grade(arm3)
    g0 = _grade(arm0)
    ctrl = _controls_gate(arm3, arm0)
    verdict = _verdict(g3, g0, ctrl)

    meta = {}
    for a in ("0", "3"):
        p = RESULTS / f"arm{a}_meta.json"
        meta[a] = json.loads(p.read_text()) if p.exists() else {}
    reach3 = {}
    p = RESULTS / "arm3_reachability.json"
    if p.exists():
        rd = json.loads(p.read_text())
        reach3 = {"recall": rd["recall"], "reached": rd["reached"],
                  "n_sources": rd["n_sources"]}

    # Diagnostics: activation + escalation rates over source records.
    src3 = [r for r in arm3 if r["group"] != "control"]
    activated = sum(1 for r in src3 if r.get("stratum_activated"))
    escalated = sum(1 for r in src3 if r.get("stratum_escalated"))
    no_call = sum(1 for r in src3 if r.get("outcome") == "NO_CALL")

    comparison = {
        "arm3": g3, "arm0_baseline": g0, "controls": ctrl,
        "reachability_arm3": reach3, "verdict": verdict,
        "diagnostics": {
            "n_source_records": len(src3), "activated": activated,
            "escalated": escalated, "no_call": no_call,
        },
        "meta": {a: {k: meta[a].get(k) for k in
                     ("catalog_arm", "registry_size", "ambient_declarable_count",
                      "model_provider", "openai_model", "temperature", "n_trials")}
                 for a in ("0", "3")},
    }
    (RESULTS / "arm3_comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
    _write_verdict_md(g3, g0, ctrl, reach3, meta, verdict, comparison["diagnostics"])
    print("ARM3 VERDICT:", verdict["winner"], "reasons:", verdict["block_reasons"])
    if ctrl["rerun_pending"]:
        ids = ",".join(d["id"] for d in ctrl["jitter_needs_rerun"])
        print("CONTROLS RE-RUN NEEDED (amended gate, N=3):", ids)
    return 0


def _write_verdict_md(g3, g0, ctrl, reach3, meta, verdict, diag) -> None:
    b = verdict["bars"]
    lines = [
        "# Catalog-surfacing -- Design 3 (stratified data pool) VERDICT",
        "",
        f"**Winner: {verdict['winner']}**",
        "",
        "Arm 3 = auto-trigger composed declaration (docs/specs/stratified-pools.md): "
        "harness-side source-stratum retrieval declares ONE generic fetcher whose "
        "`source` enum is the matched candidates in rank order, full source cards in "
        "context; the model NEVER initiates discovery. Deterministic grading "
        "(selected `source` vs acceptable set + router.validate_params), N=1, temp 0.",
        "",
        "## Headline metrics (arm3 vs arm0 baseline)",
        "",
        "| Arm | Selection | First-attempt | One-retry | Graded | Sel-correct | "
        "Upstream excl | Ambient declarable |",
        "|-----|-----------|---------------|-----------|--------|-------------|"
        "---------------|--------------------|",
        f"| 0 baseline | {g0['selection_accuracy']} | {g0['first_attempt_validity']} | "
        f"{g0['one_retry_validity']} | {g0['n_graded']} | {g0['selected_correct']} | "
        f"{g0['n_upstream_excluded']} | {meta['0'].get('ambient_declarable_count','?')} |",
        f"| 3 Design 3 | {g3['selection_accuracy']} | {g3['first_attempt_validity']} | "
        f"{g3['one_retry_validity']} | {g3['n_graded']} | {g3['selected_correct']} | "
        f"{g3['n_upstream_excluded']} | {meta['3'].get('ambient_declarable_count','?')} |",
        "",
        f"PASS bars (recomputed from arm0): selection >= {b['selection']}, "
        f"first-attempt >= {b['first_attempt']}, one-retry >= {b['one_retry']}, "
        "controls valid.",
        "",
        f"Beat-baseline hypothesis: arm3 selection {g3['selection_accuracy']} "
        f"{'>' if verdict['beat_baseline_hypothesis']['beat'] else '<='} arm0 "
        f"{g0['selection_accuracy']} -> "
        f"{'BEATS' if verdict['beat_baseline_hypothesis']['beat'] else 'does NOT beat'} "
        "baseline (hypothesis, not a criterion).",
        "",
        f"Diagnostics: {diag['activated']}/{diag['n_source_records']} source asks "
        f"activated the stratum ({diag['escalated']} via escalation); "
        f"{diag['no_call']} NO_CALL (weak-model, no tool emitted).",
        "",
        "## Reachability precondition (model-free, source stratum)",
        "",
        f"- recall {reach3.get('recall','?')} "
        f"({reach3.get('reached','?')}/{reach3.get('n_sources','?')})",
        "",
        "## Controls gate (amended: leakage invalidates; jitter -> N=3 majority)",
        "",
        f"- controls: {ctrl['n_controls']} | identical to arm0: "
        f"{ctrl['identical_across_arms']} | leakage: {len(ctrl['leakage'])} | "
        f"passes: {ctrl['passes']}",
    ]
    if ctrl["leakage"]:
        lines.append("- LEAKAGE divergences (arm0 -> arm3 fired a data-surface tool):")
        for d in ctrl["leakage"]:
            lines.append(f"  - {d['id']} (acc={d['acceptable']}): {d['arm0']} -> {d['arm3']}")
    if ctrl["jitter_needs_rerun"]:
        lines.append("- JITTER divergences PENDING N=3 re-run:")
        for d in ctrl["jitter_needs_rerun"]:
            lines.append(f"  - {d['id']} (acc={d['acceptable']}): {d['arm0']} -> {d['arm3']}")
    if ctrl["jitter_resolved"]:
        lines.append("- JITTER resolved by N=3 majority (non-leakage, non-invalidating):")
        for d in ctrl["jitter_resolved"]:
            lines.append(f"  - {d['id']} (acc={d['acceptable']}): arm3={d['arm3']} "
                         f"majority={d.get('majority')} trials={d.get('trials')}")
    if ctrl["jitter_residual"]:
        lines.append("- JITTER residual (reported, non-invalidating per amended gate):")
        for d in ctrl["jitter_residual"]:
            lines.append(f"  - {d['id']} (acc={d['acceptable']}): arm3={d['arm3']} "
                         f"majority={d.get('majority')}")
    lines += [
        "",
        "## Per-source selection accuracy (arm0 -> arm3)",
        "",
        "| Source | n | arm0 | arm3 |",
        "|--------|---|------|------|",
    ]
    for s in sorted(g0["per_source"]):
        n = g0["per_source"][s]["n"]
        a0 = g0["per_source"][s]["sel_acc"]
        a3 = g3["per_source"].get(s, {}).get("sel_acc", "-")
        lines.append(f"| {s} | {n} | {a0} | {a3} |")
    lines.append("")
    (RESULTS / "VERDICT.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
