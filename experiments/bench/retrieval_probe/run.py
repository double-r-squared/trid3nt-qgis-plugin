#!/usr/bin/env python3
"""retrieval_probe -- model-free tool-retrieval probe engine (experiments/bench).

NO daemon, NO LLM, ZERO external calls: imports the retrieval seam directly
(trid3nt_server.tools.discovery.search_tools index +
tool_retrieval.retrieve_ranked_tools) and, per record, runs
query -> top-k tool names + RRF scores + turnaround ms.

Grading uses the SAME expected-set schema as routing_sweep with
membership-in-top-k semantics (default top-5, --k override):
  CORRECT iff >=1 acceptable name appears in the top-k AND no forbidden name
  appears in the top-k; else MISS. always_allowed / no_tool / execution are
  accepted by the loader for schema uniformity but not used by the probe.

Because the probe is free it needs NO permission flag (documented in
experiments/README.md). Repetition default: --runs 3 (retrieval is
deterministic per index build, so reps mostly measure timing variance).

Output per invocation: data/<UTC timestamp>/raw_rankings.jsonl (raw top-k
with scores, per record x rep) + results.json + summary.txt.

ASCII only. Packages: trid3nt_server / trid3nt_contracts (agent venv).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_INPUTS = [
    HERE / "inputs" / "phrasings_specific.json",
    HERE / "inputs" / "phrasings_vague.json",
]
DEFAULT_K = 5
DEFAULT_RUNS = 3
#: Ranks recorded per query (>= --k) so misses show WHAT outranked the target.
RECORD_DEPTH_MIN = 10


# ---------------------------------------------------------------------------
# Input loading + validation -- same rules as routing_sweep/run.py (kept
# self-contained per the scaffold spec; the two loaders enforce the identical
# schema and must stay in lockstep).
# ---------------------------------------------------------------------------


class InputLoadError(Exception):
    """Typed load error: malformed record or unknown tool name. NO run happens."""

    def __init__(self, path: Path, record_id: str | None, problem: str) -> None:
        self.path = path
        self.record_id = record_id
        self.problem = problem
        where = f"{path.name}" + (f" record {record_id!r}" if record_id else "")
        super().__init__(f"[INPUT_LOAD_ERROR] {where}: {problem}")


@dataclass(frozen=True)
class ProbeRecord:
    id: str
    category: str
    register: str                 # "specific" | "vague"
    prompt: str
    acceptable: frozenset[str]
    forbidden: frozenset[str]
    no_tool: bool
    source_file: str


def load_live_catalog() -> set[str]:
    """TOOL_REGISTRY after the same startup import path the daemon uses."""
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    import trid3nt_server.categories  # noqa: F401 -- meta-tool registration
    from trid3nt_server.tools import TOOL_REGISTRY

    return set(TOOL_REGISTRY)


def _require(cond: bool, path: Path, rid: str | None, problem: str) -> None:
    if not cond:
        raise InputLoadError(path, rid, problem)


def _name_list(value: Any, path: Path, rid: str, key: str) -> list[str]:
    _require(isinstance(value, list), path, rid, f"expected.{key} must be a list")
    for n in value:
        _require(isinstance(n, str) and n, path, rid, f"expected.{key} entries must be non-empty strings")
    return list(value)


def load_input_file(path: Path, catalog: set[str]) -> list[ProbeRecord]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise InputLoadError(path, None, f"cannot read/parse JSON: {exc}") from exc

    _require(isinstance(doc, dict), path, None, "top level must be an object")
    extra = {k for k in doc if k not in {"defaults", "records"} and not k.startswith("_")}
    _require(not extra, path, None, f"unknown top-level keys: {sorted(extra)}")

    defaults = doc.get("defaults", {})
    _require(isinstance(defaults, dict), path, None, "defaults must be an object")
    default_aa = defaults.get("always_allowed", [])
    _require(isinstance(default_aa, list), path, None, "defaults.always_allowed must be a list")

    records_raw = doc.get("records")
    _require(isinstance(records_raw, list) and records_raw, path, None, "records must be a non-empty list")

    records: list[ProbeRecord] = []
    seen_ids: set[str] = set()
    for i, rec in enumerate(records_raw):
        rid = rec.get("id") if isinstance(rec, dict) else None
        rid = rid if isinstance(rid, str) and rid else f"<index {i}>"
        _require(isinstance(rec, dict), path, rid, "record must be an object")
        extra = {k for k in rec
                 if k not in {"id", "category", "register", "prompt", "execution", "expected"}
                 and not k.startswith("_")}
        _require(not extra, path, rid, f"unknown record keys: {sorted(extra)}")
        _require(rid not in seen_ids, path, rid, "duplicate record id")
        seen_ids.add(rid)

        _require(isinstance(rec.get("category"), str) and rec["category"], path, rid, "category must be a non-empty string")
        _require(rec.get("register") in ("specific", "vague"), path, rid, 'register must be "specific" or "vague"')
        _require(isinstance(rec.get("prompt"), str) and rec["prompt"].strip(), path, rid, "prompt must be a non-empty string")
        _require(rec.get("execution") in ("run", "block_at_invocation"), path, rid,
                 'execution must be "run" or "block_at_invocation"')

        exp = rec.get("expected")
        _require(isinstance(exp, dict), path, rid, "expected must be an object")
        extra = {k for k in exp
                 if k not in {"acceptable", "always_allowed", "no_tool", "forbidden"}
                 and not k.startswith("_")}
        _require(not extra, path, rid, f"unknown expected keys: {sorted(extra)}")

        no_tool = exp.get("no_tool", False)
        _require(isinstance(no_tool, bool), path, rid, "expected.no_tool must be a bool")
        acceptable = _name_list(exp.get("acceptable", []), path, rid, "acceptable")
        aa_raw = exp.get("always_allowed")
        always_allowed = (_name_list(aa_raw, path, rid, "always_allowed")
                          if aa_raw is not None else list(default_aa))
        forbidden = _name_list(exp.get("forbidden", []), path, rid, "forbidden")

        if no_tool:
            _require(not acceptable, path, rid, "no_tool record must have an empty acceptable set")
        else:
            _require(bool(acceptable), path, rid, "acceptable must be non-empty unless no_tool")
        _require(not no_tool, path, rid,
                 "no_tool records do not apply to the retrieval probe (retrieval always ranks)")

        unknown = (set(acceptable) | set(always_allowed) | set(forbidden)) - catalog
        _require(not unknown, path, rid,
                 f"tool names not in the live catalog ({len(catalog)} tools): {sorted(unknown)}")

        records.append(ProbeRecord(
            id=rid,
            category=rec["category"],
            register=rec["register"],
            prompt=rec["prompt"].strip(),
            acceptable=frozenset(acceptable),
            forbidden=frozenset(forbidden),
            no_tool=no_tool,
            source_file=path.name,
        ))
    return records


# ---------------------------------------------------------------------------
# Probe + grading (deterministic; NO LLM).
# ---------------------------------------------------------------------------


def ndcg_at_k(topk_names: list[str], acceptable: frozenset[str], k: int) -> float:
    """Binary-relevance nDCG over the top-k window.

    rel_i = 1 iff topk_names[i] in acceptable, 0 otherwise. Ideal ranking
    packs min(len(acceptable), k) relevant hits into the top ranks (the
    number of acceptable tools is usually small and known from the input
    record, independent of what actually got retrieved).
    """
    import math

    dcg = sum(
        1.0 / math.log2(i + 2)
        for i, n in enumerate(topk_names[:k])
        if n in acceptable
    )
    ideal_hits = min(len(acceptable), k)
    if ideal_hits == 0:
        return 0.0
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_hits))
    return round(dcg / idcg, 4) if idcg else 0.0


def mrr_at_k(topk_names: list[str], acceptable: frozenset[str], k: int) -> float:
    """Reciprocal rank of the first acceptable hit within the top-k window (0 if none)."""
    for i, n in enumerate(topk_names[:k], start=1):
        if n in acceptable:
            return round(1.0 / i, 4)
    return 0.0


def grade_topk(record: ProbeRecord, topk_names: list[str], k: int) -> dict:
    """Membership-in-top-k grading over the record's sets, plus ranked-quality metrics."""
    acceptable_hits = [n for n in topk_names if n in record.acceptable]
    forbidden_hits = [n for n in topk_names if n in record.forbidden]
    first_rank = None
    for i, n in enumerate(topk_names, start=1):
        if n in record.acceptable:
            first_rank = i
            break
    verdict = "CORRECT" if acceptable_hits and not forbidden_hits else "MISS"
    return {
        "verdict": verdict,
        "acceptable_hits": acceptable_hits,
        "forbidden_hits": forbidden_hits,
        "rank_of_first_acceptable": first_rank,
        "top1_correct": bool(topk_names) and topk_names[0] in record.acceptable,
        "ndcg_at_k": ndcg_at_k(topk_names, record.acceptable, k),
        "mrr_at_k": mrr_at_k(topk_names, record.acceptable, k),
    }


def run_probe(records: list[ProbeRecord], runs: int, k: int, out_dir: Path,
              meta: dict) -> None:
    from trid3nt_server.tools.discovery import search_tools as dd
    from trid3nt_server.tools.discovery.tool_retrieval import retrieve_ranked_tools

    # Warm the index EXPLICITLY (timed, reported separately -- retrieve_ranked_
    # tools returns [] on a cold index rather than building it).
    t0 = time.perf_counter()
    index = dd._get_index()
    index_build_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    meta["index_build_ms"] = index_build_ms
    meta["index_tool_count"] = len(index.tool_names)
    meta["dense_backend"] = index.backend_name
    meta["bm25"] = index.bm25 is not None
    print(f"[index] warmed: {len(index.tool_names)} tools, backend={index.backend_name}, "
          f"bm25={index.bm25 is not None}, build={index_build_ms}ms")

    record_depth = max(k, RECORD_DEPTH_MIN)
    raw_path = out_dir / "raw_rankings.jsonl"
    per_run: list[list[dict]] = []
    with raw_path.open("a", encoding="utf-8") as raw_fh:
        for run_index in range(1, runs + 1):
            graded: list[dict] = []
            for record in records:
                t1 = time.perf_counter()
                ranked = retrieve_ranked_tools(record.prompt, record_depth)
                turnaround_ms = round((time.perf_counter() - t1) * 1000.0, 3)
                names = [n for n, _ in ranked]
                g = grade_topk(record, names[:k], k)
                raw_fh.write(json.dumps({
                    "run": run_index,
                    "record_id": record.id,
                    "prompt": record.prompt,
                    "k": k,
                    "turnaround_ms": turnaround_ms,
                    "ranked": [{"name": n, "score": s} for n, s in ranked],
                }) + "\n")
                graded.append({
                    "record_id": record.id,
                    "category": record.category,
                    "register": record.register,
                    "turnaround_ms": turnaround_ms,
                    "topk": names[:k],
                    **g,
                })
            per_run.append(graded)

    write_outputs(out_dir, meta, per_run, k)


def aggregate_run(graded: list[dict]) -> dict:
    n = len(graded)
    hits = sum(1 for g in graded if g["verdict"] == "CORRECT")
    top1 = sum(1 for g in graded if g["top1_correct"])
    times = [g["turnaround_ms"] for g in graded]
    ndcgs = [g["ndcg_at_k"] for g in graded]
    mrrs = [g["mrr_at_k"] for g in graded]
    by_register: dict[str, dict] = {}
    for reg in ("specific", "vague"):
        sub = [g for g in graded if g["register"] == reg]
        if sub:
            sub_n = len(sub)
            by_register[reg] = {
                "n": sub_n,
                "hit_at_k": sum(1 for g in sub if g["verdict"] == "CORRECT"),
                "ndcg_at_k_mean": round(sum(g["ndcg_at_k"] for g in sub) / sub_n, 4),
                "mrr_at_k_mean": round(sum(g["mrr_at_k"] for g in sub) / sub_n, 4),
            }
    return {
        "n": n,
        "hit_at_k": hits,
        "hit_at_k_rate": round(hits / n, 4) if n else None,
        "top1_correct": top1,
        "top1_rate": round(top1 / n, 4) if n else None,
        "ndcg_at_k_mean": round(sum(ndcgs) / n, 4) if n else None,
        "mrr_at_k_mean": round(sum(mrrs) / n, 4) if n else None,
        "turnaround_ms_mean": round(sum(times) / n, 3) if n else None,
        "turnaround_ms_min": min(times) if times else None,
        "turnaround_ms_max": max(times) if times else None,
        "by_register": by_register,
    }


def write_outputs(out_dir: Path, meta: dict, per_run: list[list[dict]], k: int) -> None:
    aggregates = [aggregate_run(g) for g in per_run]
    rates = [a["hit_at_k_rate"] for a in aggregates if a["hit_at_k_rate"] is not None]
    ndcg_means = [a["ndcg_at_k_mean"] for a in aggregates if a["ndcg_at_k_mean"] is not None]
    mrr_means = [a["mrr_at_k_mean"] for a in aggregates if a["mrr_at_k_mean"] is not None]
    overall = {
        "per_run": aggregates,
        "hit_at_k_rate_mean": round(sum(rates) / len(rates), 4) if rates else None,
        "hit_at_k_rate_min": min(rates) if rates else None,
        "hit_at_k_rate_max": max(rates) if rates else None,
        "ndcg_at_k_mean": round(sum(ndcg_means) / len(ndcg_means), 4) if ndcg_means else None,
        "ndcg_at_k_min": min(ndcg_means) if ndcg_means else None,
        "ndcg_at_k_max": max(ndcg_means) if ndcg_means else None,
        "mrr_at_k_mean": round(sum(mrr_means) / len(mrr_means), 4) if mrr_means else None,
        "mrr_at_k_min": min(mrr_means) if mrr_means else None,
        "mrr_at_k_max": max(mrr_means) if mrr_means else None,
    }
    (out_dir / "results.json").write_text(json.dumps({
        "meta": meta, "runs": per_run, "aggregate": overall,
    }, indent=2, default=str) + "\n")

    lines: list[str] = []
    lines.append(f"retrieval_probe summary  ({meta['timestamp']})  k={k}")
    lines.append(f"records={meta['record_count']}  runs={meta['runs']}  "
                 f"backend={meta.get('dense_backend')}  bm25={meta.get('bm25')}  "
                 f"index_build_ms={meta.get('index_build_ms')}")
    lines.append("")
    header = (f"{'record':34} {'reg':9} " +
              " ".join(f"run{i}" for i in range(1, len(per_run) + 1)) +
              "   rank1  top-k (run 1)")
    lines.append(header)
    lines.append("-" * len(header))
    first = per_run[0]
    for i, g0 in enumerate(first):
        verdicts = " ".join(per_run[r][i]["verdict"][:4].ljust(4) for r in range(len(per_run)))
        rank = g0["rank_of_first_acceptable"]
        lines.append(f"{g0['record_id']:34} {g0['register']:9} {verdicts}   "
                     f"{str(rank) if rank else '-':5}  {', '.join(g0['topk'][:3])}")
    lines.append("")
    for i, agg in enumerate(aggregates, 1):
        reg = "  ".join(
            f"{r}={v['hit_at_k']}/{v['n']} nDCG={v['ndcg_at_k_mean']} MRR={v['mrr_at_k_mean']}"
            for r, v in agg["by_register"].items())
        lines.append(f"run {i}: hit@{k}={agg['hit_at_k']}/{agg['n']} "
                     f"({agg['hit_at_k_rate']})  top1={agg['top1_correct']}/{agg['n']}  "
                     f"nDCG@{k}={agg['ndcg_at_k_mean']}  MRR@{k}={agg['mrr_at_k_mean']}  "
                     f"[{reg}]  turnaround ms mean/min/max="
                     f"{agg['turnaround_ms_mean']}/{agg['turnaround_ms_min']}/{agg['turnaround_ms_max']}")
    lines.append(f"aggregate hit@{k}: mean={overall['hit_at_k_rate_mean']} "
                 f"min={overall['hit_at_k_rate_min']} max={overall['hit_at_k_rate_max']}")
    lines.append(f"aggregate nDCG@{k}: mean={overall['ndcg_at_k_mean']} "
                 f"min={overall['ndcg_at_k_min']} max={overall['ndcg_at_k_max']}")
    lines.append(f"aggregate MRR@{k}: mean={overall['mrr_at_k_mean']} "
                 f"min={overall['mrr_at_k_min']} max={overall['mrr_at_k_max']}")
    text = "\n".join(lines) + "\n"
    (out_dir / "summary.txt").write_text(text)
    print(text)


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TRID3NT retrieval probe bench engine (model-free, zero external "
                    "calls -- no permission flag needed; see experiments/README.md)")
    parser.add_argument("--inputs", nargs="*", type=Path, default=DEFAULT_INPUTS,
                        help="input JSON files (default: inputs/phrasings_*.json)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help=f"repetitions (default {DEFAULT_RUNS})")
    parser.add_argument("--k", type=int, default=DEFAULT_K,
                        help=f"top-k membership window for grading (default {DEFAULT_K})")
    args = parser.parse_args()

    try:
        catalog = load_live_catalog()
        records: list[ProbeRecord] = []
        for path in args.inputs:
            records.extend(load_input_file(path, catalog))
    except InputLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"[load] OK: {len(records)} records validated against the live catalog "
          f"({len(catalog)} tools)")

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = HERE / "data" / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "engine": "retrieval_probe",
        "timestamp": timestamp,
        "runs": args.runs,
        "k": args.k,
        "record_count": len(records),
        "inputs": [str(p) for p in args.inputs],
        "catalog_size": len(catalog),
        "external_calls": "none (model-free; in-process retrieval seam only)",
    }
    run_probe(records, args.runs, args.k, out_dir, meta)
    print(f"[done] outputs: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
