#!/usr/bin/env python3
"""routing_failure_split.py -- split pass-3 failures: RETRIEVAL vs MODEL.

For every MISS / NO_CALL in tool-routing-results.jsonl, replay the SAME prompt
through the agent's own ``retrieve_visible_tools`` (BM25+dense top-k, k from
TRID3NT_TOOL_RETRIEVAL_K) with a fresh allowed-set -- no LLM involved -- and ask:
was the expected tool even on the shortlist the model saw?

  RETRIEVAL-MISS  expected tool NOT in top-k -> the model never had a chance;
                  fix = raise K / improve descriptions, cheap
  MODEL-MISS      expected tool WAS in top-k and the model picked another /
                  declined -> model-capability signal; fix = better model

Caveat: the live run's allowed-set accrues tools across a case (core floor +
previously seen), so this fresh-set replay is a close approximation, biased
slightly TOWARD retrieval-miss. Ranks are recorded so borderline cases are
visible. Output: docs/reports/tool-routing-failure-split.md.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "docs" / "reports" / "tool-routing-results.jsonl"
OUT = REPO / "docs" / "reports" / "tool-routing-failure-split.md"


def main() -> None:
    import importlib, pkgutil
    import trid3nt_server.tools as pkg
    for m in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"trid3nt_server.tools.{m.name}")
    try:
        import trid3nt_server.workflows as wpkg
        for m in pkgutil.iter_modules(wpkg.__path__):
            try:
                importlib.import_module(f"trid3nt_server.workflows.{m.name}")
            except Exception:
                pass
    except ImportError:
        pass
    from trid3nt_server.tools.tool_retrieval import retrieve_visible_tools

    k = int(os.environ.get("TRID3NT_TOOL_RETRIEVAL_K", "8"))

    rows = {}
    for line in RESULTS.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            rows[r["tool"]] = r

    failed = {n: r for n, r in rows.items() if r["outcome"] in ("MISS", "NO_CALL")}
    split = []
    for name, r in sorted(failed.items()):
        visible = retrieve_visible_tools(r["prompt"], None, k)
        in_k = name in visible
        # rank via a wider pull for context
        wide = retrieve_visible_tools(r["prompt"], None, 50)
        cls = "MODEL-MISS" if in_k else "RETRIEVAL-MISS"
        split.append({
            "tool": name, "outcome": r["outcome"], "class": cls,
            "in_top_k": in_k, "in_top_50": name in wide,
            "first_call": r.get("first_call", ""),
        })

    c = Counter(s["class"] for s in split)
    hits = sum(1 for r in rows.values() if r["outcome"] == "HIT")
    lines = [
        "# Pass-3 failure split: retrieval vs model (qwen3:8b-16k, K=%d)" % k,
        "",
        f"Scored {len(rows)} | HIT {hits} | failures split: "
        + " | ".join(f"{a} {b}" for a, b in sorted(c.items())),
        "",
        "RETRIEVAL-MISS = expected tool absent from the top-K shortlist (model never saw it).",
        "MODEL-MISS = tool was on the menu; the model chose otherwise.",
        "",
        "| tool | outcome | class | in top-50? | model called |",
        "|---|---|---|---|---|",
    ]
    for s in split:
        lines.append(
            f"| {s['tool']} | {s['outcome']} | {s['class']} | "
            f"{'y' if s['in_top_50'] else 'N'} | {s['first_call']} |"
        )
    OUT.write_text("\n".join(lines) + "\n")
    print(f"HIT {hits}/{len(rows)} | " + " | ".join(f"{a}: {b}" for a, b in sorted(c.items())))
    print(f"written: {OUT}")


if __name__ == "__main__":
    main()
