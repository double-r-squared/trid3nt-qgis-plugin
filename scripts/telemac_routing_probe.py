#!/usr/bin/env python3
"""telemac_routing_probe.py -- FAST, faithful FIRST-tool routing probe.

Measures the LOCAL model's (qwen3:8b-24k) FIRST tool choice for a candidate
user prompt, reproducing the LIVE agent's per-turn assembly EXACTLY:

  * tool-retrieval enforce, K=8 (TRID3NT_TOOL_RETRIEVAL / TRID3NT_TOOL_RETRIEVAL_K)
    -- the warm discover index subsets the ~194-tool registry to the visible set
    (HOT_SET floor UNION top-K retrieved). run_telemac / the seepage tools are
    NOT in the hot set, so retrieval is the gatekeeper.
  * SYSTEM_PROMPT (+ lessons appendix when TRID3NT_LESSONS=on)
  * TRID3NT_OPENAI_EXTRA_SYSTEM + baked tool-discipline line (added inside the
    openai adapter, same as live)
  * temperature 0.7, single model round -- NO geocode, NO solve. We capture the
    first FunctionCallEvent the model emits and STOP.

This is the routing DECISION only, so it costs one CPU prefill per trial instead
of a full multi-minute solve. It never touches a user case and never runs a tool.

Usage:
  python3 scripts/telemac_routing_probe.py --n 5
  python3 scripts/telemac_routing_probe.py --n 3 --only A1
  python3 scripts/telemac_routing_probe.py --print-visible   # show retrieved set only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# --- Env: mirror the LIVE agent (already set in its process env) ------------- #
os.environ.setdefault("MODEL_PROVIDER", "openai")
os.environ.setdefault("TRID3NT_OPENAI_BASE_URL", "http://127.0.0.1:11434/v1")
os.environ.setdefault("TRID3NT_OPENAI_API_KEY", "not-needed")
os.environ.setdefault("TRID3NT_OPENAI_MODEL", "qwen3:8b-24k")
os.environ.setdefault("TRID3NT_TOOL_RETRIEVAL", "enforce")
os.environ.setdefault("TRID3NT_TOOL_RETRIEVAL_K", "8")
os.environ.setdefault("TRID3NT_LESSONS", "on")
# The live agent's extra-system (verbatim from /proc/<agent>/environ).
os.environ.setdefault(
    "TRID3NT_OPENAI_EXTRA_SYSTEM",
    "Never end a reply with an offer, suggestion, or recommendation for a next "
    "step (no 'Would you like...', no 'I can also...'). State what was done or "
    "found, then stop. The user decides what happens next. Fetch and composer "
    "tools publish their own layers - only call publish_layer when you have a "
    "handle returned by a previous tool result, passed verbatim. If a fetch "
    "returns no data, say so and stop.",
)

AGENT_SRC = Path(__file__).resolve().parent.parent / "server" / "src"
sys.path.insert(0, str(AGENT_SRC))

import trid3nt_server.main as _main  # noqa: E402

_main._import_tools_registry()

from trid3nt_server.adapter import (  # noqa: E402
    SYSTEM_PROMPT,
    build_contents_from_history,
    build_tool_declarations,
)
from trid3nt_server.lessons import lessons_appendix, lessons_enabled  # noqa: E402
from trid3nt_server.openai_adapter import stream_openai, FunctionCallEvent  # noqa: E402
from trid3nt_server.tools import TOOL_REGISTRY  # noqa: E402
from trid3nt_server.tools import discover_dataset as _dd  # noqa: E402
from trid3nt_server.tools.tool_retrieval import retrieve_visible_tools  # noqa: E402

RETRIEVAL_K = int(os.environ.get("TRID3NT_TOOL_RETRIEVAL_K", "8"))

TELEMAC = {"run_telemac"}
SEEPAGE = {"run_river_seepage_job", "run_model_river_seepage_scenario"}

# --- Candidate prompt library ------------------------------------------------ #
PROMPTS = [
    # Bucket A -- SURFACE-WATER SPILL, must route to run_telemac.
    {"id": "A1", "bucket": "A", "prompt":
        "A tanker truck overturned on the bridge and spilled chemicals into the "
        "Snake River near Twin Falls. Show how the contamination travels "
        "downstream over the next few hours."},
    {"id": "A2", "bucket": "A", "prompt":
        "There was a chemical spill directly into the river at Twin Falls, Idaho. "
        "Model how the plume moves down the river channel and where it ends up."},
    {"id": "A3", "bucket": "A", "prompt":
        "A factory discharged a pollutant into the river near Twin Falls. Animate "
        "it flowing downstream with the current."},
    {"id": "A4", "bucket": "A", "prompt":
        "Someone dumped a contaminant into the Snake River at Twin Falls. How far "
        "downstream does it travel? Simulate it washing down the river."},
    {"id": "A5", "bucket": "A", "prompt":
        "Model a dye tracer released into the surface water of the river near Twin "
        "Falls and track it moving downstream."},
    {"id": "A6", "bucket": "A", "prompt":
        "Simulate an oil spill on the river near Twin Falls and show the slick "
        "drifting downstream."},
    # Extra bucket-A variants (my own additions, natural phrasing).
    {"id": "A7", "bucket": "A", "prompt":
        "A chemical spilled into the Snake River near Twin Falls and is being "
        "carried down the river. Show the plume moving downstream in the water."},
    {"id": "A8", "bucket": "A", "prompt":
        "Simulate a contaminant dye spill in the river near Twin Falls, Idaho, and "
        "show how it travels downstream."},
    {"id": "A9", "bucket": "A", "prompt":
        "A truck spilled fuel into the Snake River at Twin Falls. Track how the "
        "spill washes downstream through the river channel over the next few hours."},
    {"id": "A10", "bucket": "A", "prompt":
        "Pollution was released into the river near Twin Falls, Idaho. Show the "
        "contaminant plume drifting downstream with the river current."},
    # Bucket B -- GROUNDWATER controls, must STILL route to seepage/MODFLOW.
    {"id": "B1", "bucket": "B", "prompt":
        "How does contamination from a leaking underground storage tank spread "
        "through the aquifer near Twin Falls?"},
    {"id": "B2", "bucket": "B", "prompt":
        "Model groundwater contamination seeping down from the river into the "
        "aquifer near Twin Falls."},
]


def visible_for(prompt: str) -> set[str]:
    return retrieve_visible_tools(prompt, None, RETRIEVAL_K)


async def first_tool(prompt: str) -> tuple[str | None, float, int]:
    """Return (first_tool_name_or_None, wall_seconds, n_visible_tools)."""
    visible = visible_for(prompt)
    subset = {n: e for n, e in TOOL_REGISTRY.items() if n in visible}
    decls = build_tool_declarations(subset)
    system = SYSTEM_PROMPT
    if lessons_enabled():
        try:
            appx = lessons_appendix(prompt)
            if appx:
                system = SYSTEM_PROMPT + "\n\n" + appx
        except Exception:
            pass
    contents = build_contents_from_history(prompt, [])
    t0 = time.time()
    chosen: str | None = None
    async for ev in stream_openai(contents, decls, system, model=None):
        if isinstance(ev, FunctionCallEvent):
            chosen = ev.name
            break
    return chosen, time.time() - t0, len(visible)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="trials per prompt")
    ap.add_argument("--only", type=str, default=None, help="comma ids e.g. A1,B2")
    ap.add_argument("--print-visible", action="store_true",
                    help="only print the retrieved visible set per prompt, no model calls")
    ap.add_argument("--out", type=str, default=None, help="write raw JSONL here")
    args = ap.parse_args()

    ids = set(args.only.split(",")) if args.only else None
    prompts = [p for p in PROMPTS if ids is None or p["id"] in ids]

    # Warm the discover index (same index the live agent warms at startup).
    print("[probe] warming discover index ...", flush=True)
    tw = time.time()
    _dd._get_index()
    print(f"[probe] index warm in {time.time()-tw:.1f}s "
          f"(registry={len(TOOL_REGISTRY)} tools)", flush=True)

    if args.print_visible:
        for p in prompts:
            v = visible_for(p["prompt"])
            print(f"\n[{p['id']}] {p['bucket']}  visible={len(v)}", flush=True)
            print(f"  telemac_in={bool(v & TELEMAC)}  seepage_in={bool(v & SEEPAGE)}", flush=True)
            print(f"  set={sorted(v)}", flush=True)
        return 0

    out_fh = open(args.out, "w") if args.out else None
    results: list[dict] = []
    for p in prompts:
        v = visible_for(p["prompt"])
        tel_in, seep_in = bool(v & TELEMAC), bool(v & SEEPAGE)
        print(f"\n{'='*70}\n[{p['id']}] bucket={p['bucket']}  "
              f"telemac_retrieved={tel_in} seepage_retrieved={seep_in} "
              f"n_visible={len(v)}", flush=True)
        print(f"  prompt: {p['prompt'][:100]}", flush=True)
        trials: list[str | None] = []
        for i in range(args.n):
            try:
                tool, dt, nvis = await first_tool(p["prompt"])
            except Exception as e:  # noqa: BLE001
                tool, dt, nvis = f"ERROR:{type(e).__name__}:{e}", 0.0, len(v)
            trials.append(tool)
            print(f"  trial {i+1}/{args.n}: {tool!r}  ({dt:.0f}s)", flush=True)
            rec = {"id": p["id"], "bucket": p["bucket"], "trial": i + 1,
                   "tool": tool, "wall_s": round(dt, 1), "n_visible": nvis,
                   "telemac_retrieved": tel_in, "seepage_retrieved": seep_in}
            if out_fh:
                out_fh.write(json.dumps(rec) + "\n")
                out_fh.flush()
        # Score.
        if p["bucket"] == "A":
            hits = sum(1 for t in trials if t in TELEMAC)
        else:
            hits = sum(1 for t in trials if t in SEEPAGE)
        rate = 100.0 * hits / max(len(trials), 1)
        results.append({"id": p["id"], "bucket": p["bucket"], "trials": trials,
                        "hits": hits, "n": len(trials), "rate": rate,
                        "telemac_retrieved": tel_in, "seepage_retrieved": seep_in,
                        "prompt": p["prompt"]})
        print(f"  ==> {'run_telemac' if p['bucket']=='A' else 'seepage'} "
              f"hit-rate {hits}/{len(trials)} = {rate:.0f}%", flush=True)

    if out_fh:
        out_fh.close()

    # Summary.
    print(f"\n\n{'#'*70}\n# SUMMARY (N={args.n} trials/prompt, temp=0.7, "
          f"retrieval enforce K={RETRIEVAL_K})\n{'#'*70}", flush=True)
    print(f"{'id':4} {'bkt':4} {'target':12} {'hit-rate':10} {'retrieved':10} trials", flush=True)
    for r in sorted(results, key=lambda x: (x["bucket"], -x["rate"], x["id"])):
        target = "run_telemac" if r["bucket"] == "A" else "seepage"
        retr = ("tel" if r["telemac_retrieved"] else "-") + "/" + ("seep" if r["seepage_retrieved"] else "-")
        cnt: dict[str, int] = {}
        for t in r["trials"]:
            k = t or "None"
            cnt[k] = cnt.get(k, 0) + 1
        dist = ",".join(f"{k}x{v}" for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]))
        print(f"{r['id']:4} {r['bucket']:4} {target:12} "
              f"{r['hits']}/{r['n']}={r['rate']:.0f}%".ljust(10) +
              f" {retr:10} {dist}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
