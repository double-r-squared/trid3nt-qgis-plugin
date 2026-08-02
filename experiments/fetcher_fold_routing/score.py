#!/usr/bin/env python3
"""fetcher_fold_routing -- scorer + verdict (experiments/fetcher_fold_routing).

Reads the two per-arm ranked JSONLs produced by run.py, runs the CONTROL
IDENTITY CHECK FIRST (DESIGN sec 4: the control rankings MUST be byte-identical
across arms; ANY drift = instrumentation bug -> ABORT, do NOT score), then
scores both arms against the pre-registered criteria and writes the comparison
table + VERDICT.md.

Scoring REUSES the retrieval_probe scorer code paths verbatim: ndcg_at_k /
mrr_at_k are imported from experiments/bench/retrieval_probe/run.py, and hit@k
uses the identical membership-in-top-k semantics (>=1 acceptable in the top-k
window; these records carry no forbidden set). Metrics: hit@5, hit@8, top1,
nDCG@5, MRR@5 -- per arm, per pilot source, and aggregate.

Pre-registered pass criteria (DESIGN sec "Pass criteria"):
  - Per pilot source: fold hit@8 >= baseline hit@8 AND fold nDCG@5 within 0.05
    of baseline (>= baseline - 0.05).
  - Aggregate (pilot phrasings): fold MRR@5 >= baseline MRR@5 - 0.03.
  - Controls: byte-identical rankings across arms.
  - ANY pilot source failing -> that source's fold blocks; 2+ failing -> the
    architecture iterates before migration.

VERDICT: SUPPORTED / REFUTED / MIXED.

ASCII only. Run inside venvs/agent (imports the retrieval_probe scorer, which
imports the live catalog for its record loader -- we call the pure scorers only).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
PROBE = HERE.parents[1] / "experiments" / "bench" / "retrieval_probe"
sys.path.insert(0, str(PROBE))

from run import mrr_at_k, ndcg_at_k  # noqa: E402  -- REUSE the probe's scorers

K5 = 5
K8 = 8
NDCG_TOL = 0.05
MRR_AGG_TOL = 0.03
PILOT_ORDER = [
    "fetch_gridmet",
    "fetch_hifld_critical_infrastructure",
    "fetch_noaa_coops_tides",
    "fetch_esri_landcover_10m",
    "fetch_census_acs",
]


def load(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        rows[r["record_id"]] = r
    return rows


def names(row: dict) -> list[str]:
    return [e["name"] for e in row["ranked"]]


def pairs(row: dict) -> list[list]:
    return [[e["name"], e["score"]] for e in row["ranked"]]


def hit_at_k(topk: list[str], acceptable: frozenset[str]) -> int:
    """retrieval_probe membership semantics: 1 iff >=1 acceptable in the top-k
    window (records carry no forbidden set)."""
    return 1 if any(n in acceptable for n in topk) else 0


def grade_record(row: dict) -> dict:
    acc = frozenset(row["acceptable"])
    nm = names(row)
    first = next((i + 1 for i, n in enumerate(nm) if n in acc), None)
    return {
        "record_id": row["record_id"],
        "group": row["group"],
        "kind": row["kind"],
        "acceptable": sorted(acc),
        "rank_of_first_acceptable": first,
        "hit_at_5": hit_at_k(nm[:K5], acc),
        "hit_at_8": hit_at_k(nm[:K8], acc),
        "top1": 1 if nm and nm[0] in acc else 0,
        "ndcg_at_5": ndcg_at_k(nm, acc, K5),
        "mrr_at_5": mrr_at_k(nm, acc, K5),
    }


def agg(graded: list[dict]) -> dict:
    n = len(graded)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "hit_at_5": sum(g["hit_at_5"] for g in graded),
        "hit_at_8": sum(g["hit_at_8"] for g in graded),
        "top1": sum(g["top1"] for g in graded),
        "ndcg_at_5_mean": round(sum(g["ndcg_at_5"] for g in graded) / n, 4),
        "mrr_at_5_mean": round(sum(g["mrr_at_5"] for g in graded) / n, 4),
    }


def main() -> int:
    base = load(RESULTS / "baseline_rankings.jsonl")
    fold = load(RESULTS / "fold_rankings.jsonl")

    if set(base) != set(fold):
        raise SystemExit(
            f"[ABORT] record-id sets differ across arms "
            f"(baseline-only={sorted(set(base)-set(fold))}, "
            f"fold-only={sorted(set(fold)-set(base))})"
        )

    # ---------------------------------------------------------------------
    # CONTROL IDENTITY CHECK FIRST (DESIGN sec 4). Byte-identical rankings
    # (ordered (name, score) list) required; ANY drift aborts the scoring.
    # ---------------------------------------------------------------------
    control_ids = sorted(r for r in base if r.startswith("control#"))
    control_drift = []
    for rid in control_ids:
        b_pairs, f_pairs = pairs(base[rid]), pairs(fold[rid])
        b_names, f_names = names(base[rid]), names(fold[rid])
        if b_pairs != f_pairs:
            control_drift.append({
                "record_id": rid,
                "prompt": base[rid]["prompt"],
                "name_order_diff": b_names != f_names,
                "baseline_top5": b_names[:5],
                "fold_top5": f_names[:5],
            })

    control_check = {
        "n_controls": len(control_ids),
        "byte_identical": not control_drift,
        "drift": control_drift,
    }

    if control_drift:
        verdict = "ABORT_INSTRUMENTATION_BUG"
        (RESULTS / "control_check.json").write_text(
            json.dumps(control_check, indent=2) + "\n")
        write_verdict_aborted(control_check)
        print("[ABORT] control rankings drifted across arms -- run INVALID, not scored.")
        for d in control_drift:
            print(f"  {d['record_id']}: name_order_diff={d['name_order_diff']} "
                  f"baseline={d['baseline_top5']} fold={d['fold_top5']}")
        return 2

    # ---------------------------------------------------------------------
    # Score both arms (controls passed the identity check).
    # ---------------------------------------------------------------------
    base_g = {rid: grade_record(base[rid]) for rid in base}
    fold_g = {rid: grade_record(fold[rid]) for rid in fold}

    # Per-record comparison JSONL.
    with (RESULTS / "per_record_comparison.jsonl").open("w", encoding="utf-8") as fh:
        for rid in sorted(base):
            fh.write(json.dumps({
                "record_id": rid,
                "group": base_g[rid]["group"],
                "kind": base_g[rid]["kind"],
                "prompt": base[rid]["prompt"],
                "acceptable": base_g[rid]["acceptable"],
                "baseline": {k: base_g[rid][k] for k in
                             ("rank_of_first_acceptable", "hit_at_5", "hit_at_8",
                              "top1", "ndcg_at_5", "mrr_at_5")},
                "fold": {k: fold_g[rid][k] for k in
                         ("rank_of_first_acceptable", "hit_at_5", "hit_at_8",
                          "top1", "ndcg_at_5", "mrr_at_5")},
                "byte_identical_ranking": pairs(base[rid]) == pairs(fold[rid]),
            }, sort_keys=True) + "\n")

    # Per-source scoring + criteria.
    per_source = []
    n_fail = 0
    for src in PILOT_ORDER:
        bg = [base_g[r] for r in base_g if base_g[r]["group"] == src]
        fg = [fold_g[r] for r in fold_g if fold_g[r]["group"] == src]
        ba, fa = agg(bg), agg(fg)
        hit8_ok = fa["hit_at_8"] >= ba["hit_at_8"]
        ndcg_ok = fa["ndcg_at_5_mean"] >= ba["ndcg_at_5_mean"] - NDCG_TOL
        src_pass = hit8_ok and ndcg_ok
        if not src_pass:
            n_fail += 1
        # per-source byte identity (diagnostic; not a gate)
        src_ids = [r for r in base if base[r]["group"] == src]
        src_identical = all(pairs(base[r]) == pairs(fold[r]) for r in src_ids)
        per_source.append({
            "source": src,
            "baseline": ba,
            "fold": fa,
            "hit_at_8_ok": hit8_ok,
            "hit_at_8_delta": fa["hit_at_8"] - ba["hit_at_8"],
            "ndcg_at_5_ok": ndcg_ok,
            "ndcg_at_5_delta": round(fa["ndcg_at_5_mean"] - ba["ndcg_at_5_mean"], 4),
            "byte_identical": src_identical,
            "pass": src_pass,
        })

    # Aggregate over pilot phrasings (controls excluded -- they are the identity
    # check, byte-identical by construction, and would only dilute deltas).
    base_pilot = [base_g[r] for r in base_g if base_g[r]["group"] != "control"]
    fold_pilot = [fold_g[r] for r in fold_g if fold_g[r]["group"] != "control"]
    base_pa, fold_pa = agg(base_pilot), agg(fold_pilot)
    mrr_agg_ok = fold_pa["mrr_at_5_mean"] >= base_pa["mrr_at_5_mean"] - MRR_AGG_TOL

    # Aggregate over ALL records (informational).
    all_ba, all_fa = agg(list(base_g.values())), agg(list(fold_g.values()))

    controls_ok = control_check["byte_identical"]
    if n_fail == 0 and mrr_agg_ok and controls_ok:
        verdict = "SUPPORTED"
    elif n_fail >= 2 or not controls_ok:
        verdict = "REFUTED"
    else:
        verdict = "MIXED"

    summary = {
        "control_check": control_check,
        "per_source": per_source,
        "aggregate_pilot": {
            "baseline": base_pa,
            "fold": fold_pa,
            "mrr_at_5_delta": round(fold_pa["mrr_at_5_mean"] - base_pa["mrr_at_5_mean"], 4),
            "mrr_at_5_criterion_ok": mrr_agg_ok,
        },
        "aggregate_all_records": {"baseline": all_ba, "fold": all_fa},
        "n_pilot_sources_failing": n_fail,
        "verdict": verdict,
    }
    (RESULTS / "control_check.json").write_text(json.dumps(control_check, indent=2) + "\n")
    (RESULTS / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
    write_verdict(summary)
    print(json.dumps({
        "verdict": verdict,
        "controls_byte_identical": controls_ok,
        "n_pilot_sources_failing": n_fail,
        "aggregate_pilot_mrr5": {"baseline": base_pa["mrr_at_5_mean"],
                                 "fold": fold_pa["mrr_at_5_mean"]},
    }, indent=2))
    return 0


def write_verdict(s: dict) -> None:
    v = s["verdict"]
    ps = s["per_source"]
    ap = s["aggregate_pilot"]
    cc = s["control_check"]
    L: list[str] = []
    L.append("# VERDICT -- fetcher-fold routing parity")
    L.append("")
    L.append(f"**{v}**")
    L.append("")
    L.append("Model-free, deterministic (no LLM). Production retrieval seam: "
             "`search_tools._get_index` warm + `tool_retrieval.retrieve_ranked_tools` "
             "rank, scored by the retrieval_probe `ndcg_at_k`/`mrr_at_k` + hit@k "
             "membership semantics. One run per arm; retrieval proven deterministic "
             "across processes (baseline rep1 vs rep2 byte-identical, all 71 records).")
    L.append("")
    L.append("## Control identity check (FIRST-CLASS, DESIGN sec 4)")
    L.append("")
    L.append(f"- Controls: {cc['n_controls']} untouched-tool phrasings.")
    L.append(f"- Byte-identical rankings across arms (ordered (name, score)): "
             f"**{cc['byte_identical']}**.")
    if cc["byte_identical"]:
        L.append("- No instrumentation drift -> scoring is VALID.")
    else:
        L.append("- DRIFT DETECTED -> run INVALID; scoring aborted.")
    L.append("")
    L.append("## Per-pilot-source results")
    L.append("")
    L.append("Criteria per source: fold hit@8 >= baseline hit@8 AND fold nDCG@5 "
             ">= baseline nDCG@5 - 0.05.")
    L.append("")
    hdr = ("| source | n | hit@8 base->fold | hit@5 base->fold | top1 base->fold "
           "| nDCG@5 base->fold (delta) | MRR@5 base->fold | byte-ident | PASS |")
    sep = ("|---|---|---|---|---|---|---|---|---|")
    L.append(hdr)
    L.append(sep)
    for r in ps:
        b, f = r["baseline"], r["fold"]
        L.append(
            f"| {r['source']} | {b['n']} "
            f"| {b['hit_at_8']}->{f['hit_at_8']} "
            f"| {b['hit_at_5']}->{f['hit_at_5']} "
            f"| {b['top1']}->{f['top1']} "
            f"| {b['ndcg_at_5_mean']}->{f['ndcg_at_5_mean']} ({r['ndcg_at_5_delta']:+}) "
            f"| {b['mrr_at_5_mean']}->{f['mrr_at_5_mean']} "
            f"| {r['byte_identical']} "
            f"| {'PASS' if r['pass'] else 'FAIL'} |"
        )
    L.append("")
    L.append("## Aggregate (pilot phrasings; controls excluded as the identity set)")
    L.append("")
    b, f = ap["baseline"], ap["fold"]
    L.append(f"- n = {b['n']} pilot phrasings.")
    L.append(f"- hit@8: {b['hit_at_8']}/{b['n']} -> {f['hit_at_8']}/{f['n']}")
    L.append(f"- hit@5: {b['hit_at_5']}/{b['n']} -> {f['hit_at_5']}/{f['n']}")
    L.append(f"- top1: {b['top1']}/{b['n']} -> {f['top1']}/{f['n']}")
    L.append(f"- nDCG@5 mean: {b['ndcg_at_5_mean']} -> {f['ndcg_at_5_mean']}")
    L.append(f"- MRR@5 mean: {b['mrr_at_5_mean']} -> {f['mrr_at_5_mean']} "
             f"(delta {ap['mrr_at_5_delta']:+})")
    L.append(f"- Criterion (fold MRR@5 >= baseline - 0.03): "
             f"{f['mrr_at_5_mean']} >= {round(b['mrr_at_5_mean'] - MRR_AGG_TOL, 4)} "
             f"-> **{ap['mrr_at_5_criterion_ok']}**")
    L.append("")
    allr = s["aggregate_all_records"]
    ab, af = allr["baseline"], allr["fold"]
    L.append(f"All 71 records (informational): MRR@5 {ab['mrr_at_5_mean']} -> "
             f"{af['mrr_at_5_mean']}; nDCG@5 {ab['ndcg_at_5_mean']} -> {af['ndcg_at_5_mean']}; "
             f"hit@8 {ab['hit_at_8']}/{ab['n']} -> {af['hit_at_8']}/{af['n']}.")
    L.append("")
    L.append("## Decision")
    L.append("")
    nf = s["n_pilot_sources_failing"]
    if v == "SUPPORTED":
        L.append("All 5 pilot sources meet the per-source criteria, the aggregate "
                 "MRR@5 criterion holds, and controls are byte-identical. The fold's "
                 "central routing risk is disproven on the pilot set -> phase-2 "
                 "migration is unblocked on routing grounds (paper trail only; no "
                 "fetcher is cut by this experiment).")
    elif v == "REFUTED":
        L.append(f"{nf} pilot source(s) fail the per-source criteria (or controls "
                 "drifted). Per DESIGN, 2+ failing -> the architecture iterates "
                 "(spec-doc quality, corpus carriage, or surfacing mechanism) and the "
                 "experiment re-runs before any migration.")
    else:
        L.append(f"{nf} pilot source failed a per-source criterion (fold blocks for "
                 "that source); the rest hold. Per DESIGN a single failure blocks only "
                 "that source's fold.")
    (RESULTS / "VERDICT.md").write_text("\n".join(L) + "\n")


def write_verdict_aborted(cc: dict) -> None:
    L = [
        "# VERDICT -- fetcher-fold routing parity",
        "",
        "**ABORTED -- INSTRUMENTATION BUG (control drift)**",
        "",
        "Per DESIGN sec 4, the control rankings MUST be byte-identical across "
        "arms. They are not -> the run is INVALID and was NOT scored.",
        "",
        f"- Controls checked: {cc['n_controls']}",
        f"- Drifted controls: {len(cc['drift'])}",
        "",
    ]
    for d in cc["drift"]:
        L.append(f"- `{d['record_id']}` ({d['prompt']!r}): "
                 f"name_order_diff={d['name_order_diff']}")
        L.append(f"  - baseline top5: {d['baseline_top5']}")
        L.append(f"  - fold top5: {d['fold_top5']}")
    (RESULTS / "VERDICT.md").write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    sys.exit(main())
