#!/usr/bin/env python3
"""Model-bench scorer + NO_CALL forensic classifier (2026-08-01).

Reads results/model_bench_2026-08-01/<tag>_arm{0,3}_records.jsonl (forensic
records written by run_forensic.py), grades deterministically with the SAME
rules as score.py (selection = selected_name vs acceptable[0] over non-upstream
source records; param validity over selection-correct records), classifies every
NO_CALL from its captured per-call forensics, and writes COMPARISON.md.

NO_CALL classification (decisive = the last forensic call on the record):
  provider_error       -- an exception was raised (error_class set / http error).
  empty_200_completion -- 200, finish_reason stop/None, 0 content, 0 reasoning.
  reasoning_truncated  -- finish_reason == 'length' and 0 content (budget burned
                          on reasoning / cut before any content or tool call).
  model_declined       -- real CONTENT text present, no tool call (genuine decline).
  other                -- anything left (recorded verbatim).

Hypothesis (NATE): the NO_CALL empties are UPSTREAM/provider artifacts, not model
behavior. CONFIRMED if a MAJORITY of NO_CALLs are provider_error OR
empty_200_completion OR reasoning_truncated; REFUTED if a majority are
model_declined (healthy responses that genuinely declined to call a tool).

ASCII only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTDIR = HERE / "results" / "model_bench_2026-08-01"

ARM0_BASELINE_SEL = 60.58  # signed arm0 nemotron baseline (ADR 0050/0055 era)


def _pct(n, d):
    return round(100.0 * n / d, 2) if d else 0.0


def _load(tag, arm):
    p = OUTDIR / f"{tag}_arm{arm}_records.jsonl"
    if not p.exists():
        return None
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _classify_nocall(row):
    """Classify a NO_CALL from the decisive (last) forensic call.

    provider_error       -- an exception was raised (429/5xx/timeout/other).
    reasoning_truncated  -- finish_reason == 'length' (max_tokens hit before a
                            tool call landed -- an infra/config artifact).
    model_declined       -- finish_reason stop AND real CONTENT text present, no
                            tool call (genuine model decision to answer in prose).
    empty_200_completion -- finish_reason stop, 0 content, no tool call (silent /
                            reasoning-only empty -- no user-facing output).
    """
    fs = row.get("_forensics") or []
    if not fs:
        return "other", {}
    f = fs[-1]  # decisive call
    ec = f.get("error_class")
    if ec in ("upstream_provider", "internal") or f.get("provider_msg"):
        return "provider_error", f
    fr = (f.get("finish_reason") or "").lower()
    clen = f.get("content_len") or 0
    ntc = f.get("n_tool_calls") or 0
    if fr == "length":
        return "reasoning_truncated", f
    if clen > 0 and ntc == 0:
        return "model_declined", f
    if clen == 0 and ntc == 0:
        return "empty_200_completion", f
    return "other", f


def grade(tag, arm):
    recs = _load(tag, arm)
    if recs is None:
        return None
    sources = [r for r in recs if r["group"] != "control"]
    controls = [r for r in recs if r["group"] == "control"]
    graded = [r for r in sources if not r.get("upstream_failure")]
    upstream = [r for r in sources if r.get("upstream_failure")]

    sel_ok = [r for r in graded if r["selected_name"] == r["acceptable"][0]]
    sel_acc = _pct(len(sel_ok), len(graded))
    fa = sum(1 for r in sel_ok if r.get("first_attempt_valid") is True)
    r1 = sum(1 for r in sel_ok if r.get("one_retry_valid") is True)

    nocalls = [r for r in sources if r["outcome"] == "NO_CALL"]
    cls_counts = {}
    cls_detail = []
    silent_empty = 0   # empty_200 with ZERO reasoning too = provider returned nothing
    reasoned_empty = 0  # empty_200 but the model DID reason then emit nothing
    for r in nocalls:
        c, f = _classify_nocall(r)
        cls_counts[c] = cls_counts.get(c, 0) + 1
        if c == "empty_200_completion":
            if (f.get("reasoning_len") or 0) == 0 and (f.get("completion_tokens") or 0) in (0, None):
                silent_empty += 1
            else:
                reasoned_empty += 1
        cls_detail.append({"id": r["id"], "class": c,
                           "finish_reason": f.get("finish_reason"),
                           "content_len": f.get("content_len"),
                           "reasoning_len": f.get("reasoning_len"),
                           "completion_tokens": f.get("completion_tokens"),
                           "http_status": f.get("http_status"),
                           "error_class": f.get("error_class")})
    wrong = [r for r in sources if r["outcome"] == "WRONG_SOURCE"]

    return {
        "tag": tag, "arm": arm,
        "n_source": len(sources), "n_graded": len(graded),
        "n_upstream_excl": len(upstream),
        "selection_accuracy": sel_acc, "selected_correct": len(sel_ok),
        "first_attempt_validity": _pct(fa, len(sel_ok)),
        "one_retry_validity": _pct(r1, len(sel_ok)),
        "param_denominator": len(sel_ok),
        "n_nocall": len(nocalls),
        "nocall_rate": _pct(len(nocalls), len(sources)),
        "n_wrong_source": len(wrong),
        "nocall_classes": cls_counts,
        "silent_empty": silent_empty,
        "reasoned_empty": reasoned_empty,
        "nocall_detail": cls_detail,
        "n_controls": len(controls),
    }


def hypothesis_verdict(g):
    """NATE's hypothesis: the NO_CALL empties are UPSTREAM/PROVIDER transport
    artifacts (queueing / silent 200 / 429 / 5xx), not model behavior.

    provider-transport artifacts = provider_error + silent_empty (http 200 but the
      provider returned ZERO tokens -- no content, no reasoning, no tool call).
    model / config = model_declined + reasoned_empty (model reasoned then emitted
      nothing) + reasoning_truncated (max_tokens hit).

    CONFIRMED only if provider-transport artifacts are the MAJORITY of NO_CALLs.
    """
    c = g["nocall_classes"]
    total = g["n_nocall"]
    provider_transport = c.get("provider_error", 0) + g.get("silent_empty", 0)
    model_config = (c.get("model_declined", 0) + g.get("reasoned_empty", 0)
                    + c.get("reasoning_truncated", 0))
    if total == 0:
        return "N/A (no NO_CALL)", provider_transport, model_config
    verdict = "CONFIRMED" if provider_transport > model_config else "REFUTED"
    return verdict, provider_transport, model_config


def reproducibility(tag="nemotron", arm=3):
    """Compare the SIGNED arm3 run's NO_CALL ids to THIS re-run's outcomes on the
    same ids -- did the signed empties reproduce, or do they now fire / decline?"""
    signed_p = HERE / "results" / f"arm{arm}_records.jsonl"
    rerun = _load(tag, arm)
    if not signed_p.exists() or rerun is None:
        return None
    signed = [json.loads(l) for l in signed_p.read_text().splitlines() if l.strip()]
    signed_nocall = {r["id"] for r in signed if r["outcome"] == "NO_CALL"}
    rr = {r["id"]: r for r in rerun}
    still_nocall = fired = declined = missing = 0
    flips = []
    for cid in sorted(signed_nocall):
        r = rr.get(cid)
        if r is None:
            missing += 1
            continue
        if r["outcome"] == "NO_CALL":
            still_nocall += 1
        elif r["selected_name"] is not None and r["outcome"] in ("SELECTED", "WRONG_SOURCE"):
            fired += 1
            flips.append((cid, r["outcome"], r["selected_name"]))
        else:
            declined += 1
    return {"signed_nocall": len(signed_nocall), "still_nocall": still_nocall,
            "now_fired_a_tool": fired, "other": declined, "not_in_rerun": missing,
            "flips_sample": flips[:12]}


def main():
    tags = sys.argv[1:] or ["nemotron", "qwen3-8b", "qwen35-lowvram-9b"]
    results = {}
    for tag in tags:
        for arm in (0, 3):
            g = grade(tag, arm)
            if g:
                results[(tag, arm)] = g

    lines = ["# Model bench 2026-08-01 -- catalog-surfacing arms on nemotron + local ollama",
             "",
             "Deterministic grading identical to score.py (selection = selected_name vs "
             "acceptable[0] over non-upstream source records; param validity over "
             "selection-correct records). N=1, temp 0, max_tokens 4096. Forensic re-run: "
             "per-call HTTP status / finish_reason / content length / reasoning length "
             "captured. NO_CALLs classified.",
             "",
             f"Signed arm0 nemotron baseline (prior, ADR 0050/0055): selection {ARM0_BASELINE_SEL}.",
             "",
             "## Headline metrics",
             "",
             "| Model | Arm | Sel acc | First-attempt | One-retry | Graded | Sel-correct | "
             "NO_CALL | NO_CALL rate | Wrong-src | Upstream excl |",
             "|-------|-----|---------|---------------|-----------|--------|-------------|"
             "---------|--------------|-----------|---------------|"]
    for (tag, arm), g in results.items():
        lines.append(
            f"| {tag} | {arm} | {g['selection_accuracy']} | {g['first_attempt_validity']} | "
            f"{g['one_retry_validity']} | {g['n_graded']} | {g['selected_correct']} | "
            f"{g['n_nocall']} | {g['nocall_rate']} | {g['n_wrong_source']} | {g['n_upstream_excl']} |")

    lines += ["", "## NO_CALL forensic classification",
              "",
              "provider-transport artifacts = provider_error + silent_empty (200 w/ "
              "ZERO tokens). model/config = model_declined + reasoned_empty (reasoned "
              "then emitted nothing) + reasoning_truncated (max_tokens hit). Hypothesis "
              "CONFIRMED only if provider-transport is the MAJORITY.",
              ""]
    for (tag, arm), g in results.items():
        v, ptr, mc = hypothesis_verdict(g)
        lines.append(f"### {tag} arm{arm} -- {g['n_nocall']} NO_CALL(s)")
        lines.append(f"- classes: {json.dumps(g['nocall_classes'], sort_keys=True)} "
                     f"(silent_empty={g.get('silent_empty',0)}, reasoned_empty={g.get('reasoned_empty',0)})")
        lines.append(f"- provider_transport={ptr} vs model_config={mc} -> hypothesis {v}")
        lines.append("")

    repro = reproducibility("nemotron", 3)
    if repro:
        lines += ["## Reproducibility of the SIGNED run's arm3 NO_CALL empties (nemotron re-run)",
                  "",
                  f"- signed arm3 NO_CALL ids: {repro['signed_nocall']}",
                  f"- STILL NO_CALL on re-run: {repro['still_nocall']}",
                  f"- NOW fired a tool: {repro['now_fired_a_tool']}",
                  f"- other outcome: {repro['other']} | not in re-run: {repro['not_in_rerun']}",
                  f"- sample flips (id, outcome, fired): {repro['flips_sample']}",
                  ""]

    (OUTDIR / "COMPARISON_metrics.json").write_text(
        json.dumps({f"{t}_arm{a}": g for (t, a), g in results.items()}, indent=2) + "\n")
    (OUTDIR / "COMPARISON.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
