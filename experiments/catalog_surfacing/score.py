#!/usr/bin/env python3
"""catalog_surfacing -- deterministic grader + VERDICT (experiments/catalog_surfacing).

Reads the three arms' per-record drive outputs (arm{0,1,2}_records.jsonl) + meta +
reachability, grades deterministically (selected NAME vs the acceptable set +
router.validate_params pass/fail already captured by the runner), applies the
PRE-REGISTERED advancement criteria INCLUDING the NATE 2026-07-30 favored-arm rule
(Design 2 favored; Design 1 advances only if it BEATS Design 2 outside the noise
band on selection accuracy AND first-attempt param validity), and writes
results/comparison.json + results/VERDICT.md.

UPSTREAM_PROVIDER failures are graded UPSTREAM_FAILURE and excluded from both
denominators (standing upstream-error rule).

ASCII only.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

NOISE_BAND_PTS = 2.0        # NATE sign-off: tie-break band 2 points
FIRST_ATTEMPT_SLACK = 5.0   # DESIGN (d): first-attempt >= baseline - 5 points
PER_SOURCE_DROP_PTS = 10.0  # per-source guard: >10 pts below baseline flags


def _load_records(arm: int) -> list[dict]:
    p = RESULTS / f"arm{arm}_records.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 2) if den else 0.0


def _grade_arm(arm: int) -> dict:
    recs = _load_records(arm)
    sources = [r for r in recs if r["group"] != "control"]
    controls = [r for r in recs if r["group"] == "control"]

    graded = [r for r in sources if not r.get("upstream_failure")]
    upstream = [r for r in sources if r.get("upstream_failure")]

    # (a) selection accuracy over graded source records.
    sel_hits = sum(1 for r in graded if r["selected_name"] == r["acceptable"][0])
    sel_acc = _pct(sel_hits, len(graded))

    # (b) param fidelity over records where selection was correct.
    selected_ok = [r for r in graded if r["selected_name"] == r["acceptable"][0]]
    fa_hits = sum(1 for r in selected_ok if r.get("first_attempt_valid") is True)
    r1_hits = sum(1 for r in selected_ok if r.get("one_retry_valid") is True)
    first_attempt = _pct(fa_hits, len(selected_ok))
    one_retry = _pct(r1_hits, len(selected_ok))

    # per-source selection accuracy.
    per_source: dict[str, dict] = {}
    by_group: dict[str, list] = {}
    for r in graded:
        by_group.setdefault(r["group"], []).append(r)
    for grp, rs in sorted(by_group.items()):
        hits = sum(1 for r in rs if r["selected_name"] == r["acceptable"][0])
        per_source[grp] = {"n": len(rs), "sel_acc": _pct(hits, len(rs))}

    return {
        "arm": arm,
        "n_source_records": len(sources),
        "n_graded": len(graded),
        "n_upstream_excluded": len(upstream),
        "selection_accuracy": sel_acc,
        "selection_denominator": len(graded),
        "selected_correct": len(selected_ok),
        "first_attempt_validity": first_attempt,
        "one_retry_validity": one_retry,
        "param_denominator": len(selected_ok),
        "per_source": per_source,
        "n_controls": len(controls),
    }


def _control_identity() -> dict:
    """Every control must fire the SAME acceptable tool in ALL three arms."""
    by_arm = {a: {r["id"]: r for r in _load_records(a) if r["group"] == "control"}
              for a in (0, 1, 2)}
    ids = sorted(by_arm[0].keys())
    divergences = []
    identical = 0
    for cid in ids:
        r0, r1, r2 = by_arm[0].get(cid), by_arm[1].get(cid), by_arm[2].get(cid)
        names = [x["selected_name"] if x else None for x in (r0, r1, r2)]
        acc = (r0 or {}).get("acceptable", [None])[0]
        # identity = the SAME fired name across arms (DESIGN (c)); we also note
        # whether it matched the acceptable tool.
        same = len(set(names)) == 1
        if same:
            identical += 1
        else:
            divergences.append({"id": cid, "acceptable": acc, "fired": names})
    return {
        "n_controls": len(ids),
        "identical_across_arms": identical,
        "divergences": divergences,
        "passes": len(divergences) == 0,
    }


def _advances(arm_g: dict, base: dict, controls_pass: bool) -> tuple[bool, list[str]]:
    reasons = []
    ok = True
    if arm_g["selection_accuracy"] < base["selection_accuracy"]:
        ok = False
        reasons.append(
            f"selection {arm_g['selection_accuracy']} < baseline {base['selection_accuracy']}"
        )
    if arm_g["first_attempt_validity"] < base["first_attempt_validity"] - FIRST_ATTEMPT_SLACK:
        ok = False
        reasons.append(
            f"first-attempt {arm_g['first_attempt_validity']} < baseline "
            f"{base['first_attempt_validity']} - {FIRST_ATTEMPT_SLACK}"
        )
    if arm_g["one_retry_validity"] < base["one_retry_validity"]:
        ok = False
        reasons.append(
            f"one-retry {arm_g['one_retry_validity']} < baseline {base['one_retry_validity']}"
        )
    if not controls_pass:
        ok = False
        reasons.append("controls identity check FAILED")
    return ok, reasons


def _verdict(g0, g1, g2, ctrl) -> dict:
    cp = ctrl["passes"]
    d1_adv, d1_reasons = _advances(g1, g0, cp)
    d2_adv, d2_reasons = _advances(g2, g0, cp)

    # NATE sign-off (2026-07-30): Design 2 favored. Design 1 advances ONLY IF it
    # BEATS Design 2 outside the noise band on selection accuracy AND first-attempt
    # param validity; a tie or within-band result selects Design 2.
    winner = "NEITHER"
    rationale = ""
    if not cp:
        winner = "INVALID"
        rationale = ("Control identity check FAILED -- a surfacing change leaked "
                     "into unrelated routing; the run is INVALID (fix + re-run).")
    elif d1_adv and d2_adv:
        d1_beats = (
            g1["selection_accuracy"] > g2["selection_accuracy"] + NOISE_BAND_PTS
            and g1["first_attempt_validity"] > g2["first_attempt_validity"] + NOISE_BAND_PTS
        )
        if d1_beats:
            winner = "DESIGN_1"
            rationale = ("Both advance; Design 1 BEATS Design 2 outside the "
                         f"{NOISE_BAND_PTS}-pt band on selection AND first-attempt "
                         "validity -> Design 1 (per sign-off exception).")
        else:
            winner = "DESIGN_2"
            rationale = ("Both advance; Design 1 does NOT beat Design 2 outside the "
                         f"{NOISE_BAND_PTS}-pt band -> favored Design 2 (NATE "
                         "2026-07-30: surface stability governs).")
    elif d2_adv:
        winner = "DESIGN_2"
        rationale = "Only Design 2 meets the advancement thresholds vs baseline."
    elif d1_adv:
        winner = "DESIGN_1"
        rationale = "Only Design 1 meets the advancement thresholds vs baseline."
    else:
        winner = "NEITHER"
        rationale = ("Neither design meets the pre-registered thresholds vs "
                     "baseline; the registry-shrink is NOT justified by this run.")

    # per-source guard on the winning arm.
    flagged = []
    if winner in ("DESIGN_1", "DESIGN_2"):
        gw = g1 if winner == "DESIGN_1" else g2
        for grp, ps in gw["per_source"].items():
            base_ps = g0["per_source"].get(grp, {}).get("sel_acc", 0.0)
            if ps["sel_acc"] < base_ps - PER_SOURCE_DROP_PTS:
                flagged.append({"source": grp, "arm_sel": ps["sel_acc"],
                                "baseline_sel": base_ps})

    return {
        "winner": winner,
        "rationale": rationale,
        "design_1_advances": d1_adv,
        "design_1_block_reasons": d1_reasons,
        "design_2_advances": d2_adv,
        "design_2_block_reasons": d2_reasons,
        "per_source_flagged": flagged,
        "per_source_flag_blocks_rollout": len(flagged) >= 2,
    }


def main() -> int:
    g = {a: _grade_arm(a) for a in (0, 1, 2)}
    ctrl = _control_identity()
    metas = {}
    for a in (0, 1, 2):
        p = RESULTS / f"arm{a}_meta.json"
        metas[a] = json.loads(p.read_text()) if p.exists() else {}
    reach = {}
    for a in (0, 1, 2):
        p = RESULTS / f"arm{a}_reachability.json"
        if p.exists():
            rd = json.loads(p.read_text())
            reach[a] = {"recall": rd["recall"], "reached": rd["reached"],
                        "n_sources": rd["n_sources"]}

    verdict = _verdict(g[0], g[1], g[2], ctrl)

    comparison = {
        "arms": g,
        "controls": ctrl,
        "reachability": reach,
        "meta": {a: {k: metas[a].get(k) for k in
                     ("catalog_arm", "registry_size", "ambient_declarable_count",
                      "model_provider", "openai_model", "temperature", "n_trials")}
                 for a in (0, 1, 2)},
        "verdict": verdict,
    }
    (RESULTS / "comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")

    _write_verdict_md(g, ctrl, reach, metas, verdict)
    print("VERDICT:", verdict["winner"], "--", verdict["rationale"])
    return 0


def _write_verdict_md(g, ctrl, reach, metas, verdict) -> None:
    def row(a):
        x = g[a]
        return (f"| {a} | {x['selection_accuracy']} | {x['first_attempt_validity']} | "
                f"{x['one_retry_validity']} | {x['n_graded']} | {x['selected_correct']} | "
                f"{x['n_upstream_excluded']} | "
                f"{metas[a].get('ambient_declarable_count','?')} |")

    lines = [
        "# Catalog-surfacing experiment -- VERDICT",
        "",
        f"**Winner: {verdict['winner']}**",
        "",
        verdict["rationale"],
        "",
        "Deterministic grading (selected NAME vs acceptable set + "
        "router.validate_params pass/fail); model-in-the-loop selection/param "
        "formation; N=1, temperature 0. Pre-registered criteria + NATE 2026-07-30 "
        "favored-arm rule (Design 2 favored).",
        "",
        "## Headline metrics",
        "",
        "| Arm | Selection acc | First-attempt validity | One-retry validity | "
        "Graded (den) | Sel-correct | Upstream excl | Ambient declarable |",
        "|-----|---------------|------------------------|--------------------|"
        "--------------|-------------|---------------|--------------------|",
        row(0) + "  <- baseline",
        row(1),
        row(2),
        "",
        "Arm 0 = baseline (14 ambient tier=general). Arm 1 = Design 1 (card-carried). "
        "Arm 2 = Design 2 (discovery-expands-declaration).",
        "",
        "## Reachability precondition (model-free)",
        "",
        "| Arm | Recall (sources) |",
        "|-----|------------------|",
    ]
    for a in (0, 1, 2):
        rc = reach.get(a, {})
        lines.append(f"| {a} | {rc.get('recall','?')} "
                     f"({rc.get('reached','?')}/{rc.get('n_sources','?')}) |")
    lines += [
        "",
        "## Controls (route-identity across arms)",
        "",
        f"- Controls: {ctrl['n_controls']} | identical fired name across all 3 arms: "
        f"{ctrl['identical_across_arms']} | passes: {ctrl['passes']}",
    ]
    if ctrl["divergences"]:
        lines.append("- Divergences (arm0/arm1/arm2 fired):")
        for d in ctrl["divergences"]:
            lines.append(f"  - {d['id']} (acceptable={d['acceptable']}): {d['fired']}")
    lines += [
        "",
        "## Advancement",
        "",
        f"- Design 1 advances vs baseline: {verdict['design_1_advances']}"
        + (f" (blocked: {verdict['design_1_block_reasons']})"
           if verdict["design_1_block_reasons"] else ""),
        f"- Design 2 advances vs baseline: {verdict['design_2_advances']}"
        + (f" (blocked: {verdict['design_2_block_reasons']})"
           if verdict["design_2_block_reasons"] else ""),
        "",
        "## Per-source selection accuracy (arm0 baseline -> arm1 / arm2)",
        "",
        "| Source | n | arm0 | arm1 | arm2 |",
        "|--------|---|------|------|------|",
    ]
    all_sources = sorted(g[0]["per_source"].keys())
    for s in all_sources:
        n = g[0]["per_source"][s]["n"]
        a0 = g[0]["per_source"][s]["sel_acc"]
        a1 = g[1]["per_source"].get(s, {}).get("sel_acc", "-")
        a2 = g[2]["per_source"].get(s, {}).get("sel_acc", "-")
        lines.append(f"| {s} | {n} | {a0} | {a1} | {a2} |")
    if verdict["per_source_flagged"]:
        lines += ["", "### Per-source guard flags (winning arm > 10 pts below baseline)"]
        for f in verdict["per_source_flagged"]:
            lines.append(f"- {f['source']}: arm={f['arm_sel']} vs baseline={f['baseline_sel']}")
        if verdict["per_source_flag_blocks_rollout"]:
            lines.append("- **2+ sources flagged -> rollout BLOCKED until iterated + re-run.**")
    lines.append("")
    (RESULTS / "VERDICT.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
