#!/usr/bin/env python3
"""retro_compute_metrics -- retro-compute nDCG@5 / MRR@5 over an EXISTING
retrieval_probe data/<timestamp>/ dir's raw_rankings.jsonl.

Pure math over rankings already recorded by run.py -- NO daemon, NO LLM, NO
re-run of retrieval. Reads raw_rankings.jsonl (ranked top-k/depth per
record x rep) plus the same input JSON files run.py used (for each record's
`acceptable` set, keyed by record_id) and writes metrics_addendum.json
BESIDE raw_rankings.jsonl in the same data dir. Does NOT touch/overwrite the
original results.json or summary.txt -- this is an addendum, not a mutation.

Usage:
    python retro_compute_metrics.py --data-dir data/20260723T034651Z-extended-baseline
    (defaults --inputs to the same phrasings_specific.json/phrasings_vague.json
    run.py uses by default; override with --inputs if the run used others)

ASCII only. Packages: trid3nt_server / trid3nt_contracts (agent venv) --
needed only to validate/load the input record schema, same as run.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run import (  # noqa: E402
    DEFAULT_INPUTS,
    InputLoadError,
    load_input_file,
    load_live_catalog,
    mrr_at_k,
    ndcg_at_k,
)


def load_acceptable_map(input_paths: list[Path]) -> dict[str, dict]:
    """record_id -> {"acceptable": frozenset[str], "register": str, "category": str}."""
    catalog = load_live_catalog()
    out: dict[str, dict] = {}
    for path in input_paths:
        for rec in load_input_file(path, catalog):
            out[rec.id] = {
                "acceptable": rec.acceptable,
                "register": rec.register,
                "category": rec.category,
            }
    return out


def retro_compute(data_dir: Path, input_paths: list[Path]) -> dict:
    raw_path = data_dir / "raw_rankings.jsonl"
    if not raw_path.exists():
        raise SystemExit(f"[ERROR] no raw_rankings.jsonl in {data_dir}")

    acceptable_map = load_acceptable_map(input_paths)

    per_run: dict[int, list[dict]] = {}
    missing_ids: set[str] = set()
    with raw_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rid = row["record_id"]
            info = acceptable_map.get(rid)
            if info is None:
                missing_ids.add(rid)
                continue
            k = row["k"]
            names = [entry["name"] for entry in row["ranked"]][:k]
            g = {
                "record_id": rid,
                "register": info["register"],
                "category": info["category"],
                "ndcg_at_k": ndcg_at_k(names, info["acceptable"], k),
                "mrr_at_k": mrr_at_k(names, info["acceptable"], k),
            }
            per_run.setdefault(row["run"], []).append(g)

    if missing_ids:
        raise SystemExit(
            "[ERROR] raw_rankings.jsonl references record_id(s) not found in "
            f"--inputs: {sorted(missing_ids)} -- pass the correct --inputs for "
            "this data dir"
        )

    def agg(graded: list[dict]) -> dict:
        n = len(graded)
        ndcgs = [g["ndcg_at_k"] for g in graded]
        mrrs = [g["mrr_at_k"] for g in graded]
        by_register: dict[str, dict] = {}
        for reg in ("specific", "vague"):
            sub = [g for g in graded if g["register"] == reg]
            if sub:
                sn = len(sub)
                by_register[reg] = {
                    "n": sn,
                    "ndcg_at_k_mean": round(sum(g["ndcg_at_k"] for g in sub) / sn, 4),
                    "mrr_at_k_mean": round(sum(g["mrr_at_k"] for g in sub) / sn, 4),
                }
        return {
            "n": n,
            "ndcg_at_k_mean": round(sum(ndcgs) / n, 4) if n else None,
            "mrr_at_k_mean": round(sum(mrrs) / n, 4) if n else None,
            "by_register": by_register,
        }

    run_indices = sorted(per_run)
    aggregates = [agg(per_run[r]) for r in run_indices]
    ndcg_means = [a["ndcg_at_k_mean"] for a in aggregates if a["ndcg_at_k_mean"] is not None]
    mrr_means = [a["mrr_at_k_mean"] for a in aggregates if a["mrr_at_k_mean"] is not None]

    return {
        "source": "raw_rankings.jsonl",
        "note": "retro-computed nDCG@k / MRR@k over an existing data dir's raw "
                "rankings; pure math, no re-run; results.json/summary.txt "
                "untouched.",
        "data_dir": str(data_dir),
        "inputs": [str(p) for p in input_paths],
        "runs": run_indices,
        "per_run": dict(zip(run_indices, aggregates)),
        "aggregate": {
            "ndcg_at_k_mean": round(sum(ndcg_means) / len(ndcg_means), 4) if ndcg_means else None,
            "ndcg_at_k_min": min(ndcg_means) if ndcg_means else None,
            "ndcg_at_k_max": max(ndcg_means) if ndcg_means else None,
            "mrr_at_k_mean": round(sum(mrr_means) / len(mrr_means), 4) if mrr_means else None,
            "mrr_at_k_min": min(mrr_means) if mrr_means else None,
            "mrr_at_k_max": max(mrr_means) if mrr_means else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True,
                         help="existing retrieval_probe data/<timestamp>/ dir")
    parser.add_argument("--inputs", nargs="*", type=Path, default=DEFAULT_INPUTS,
                         help="input JSON files used for that run (default: "
                              "the current phrasings_specific.json/phrasings_vague.json)")
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    try:
        addendum = retro_compute(data_dir, args.inputs)
    except InputLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    out_path = data_dir / "metrics_addendum.json"
    out_path.write_text(json.dumps(addendum, indent=2, default=str) + "\n")
    print(f"[done] wrote {out_path}")
    print(json.dumps(addendum["aggregate"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
