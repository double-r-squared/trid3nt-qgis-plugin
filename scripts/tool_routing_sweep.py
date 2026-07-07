#!/usr/bin/env python3
"""tool_routing_sweep.py -- pass 3: can the LOCAL LLM route to + call EVERY tool?

One natural-language prompt per registered tool, driven through the live agent
WS exactly like a user (reuses tool_routing_bench machinery). Scored per tool:

  HIT       the expected tool was called (args validated by the agent)
  MISS      a different tool was called first (recorded)
  NO_CALL   the model answered without calling any tool
  ERROR     transport / timeout before any signal

Prompts are auto-generated from each tool's registry description with the tool
name stripped (so the words the model sees resemble what a user would say,
without leaking the identifier). Solver/composer prompts cancel as soon as the
tool STARTS (routing is proven; no sims run). KEY-gated tools stay in -- the
agent calling them and raising a credential-request still proves routing.

Excluded (never user-initiated): run_solver, wait_for_completion,
spatial-input plumbing. Sequential, one prompt at a time, resumable via
docs/reports/tool-routing-results.jsonl.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "docs" / "reports" / "tool-routing-results.jsonl"
REPORT = REPO / "docs" / "reports" / "tool-routing-report.md"

# Reuse the bench's WS machinery (it is a script, so load by path).
_spec = importlib.util.spec_from_file_location(
    "tool_routing_bench", REPO / "scripts" / "tool_routing_bench.py"
)
bench = importlib.util.module_from_spec(_spec)
sys.modules["tool_routing_bench"] = bench
_spec.loader.exec_module(bench)

EXCLUDE = {
    "run_solver", "wait_for_completion",  # plumbing the LLM reaches via composers
}

# Hand-tuned prompts where the generated one reads wrong. Everything else is
# generated from the description.
HAND_PROMPTS = {
    "geocode_location": "Where exactly is Ybor City in Tampa? Find it on the map.",
    "publish_layer": "Publish the most recent raster layer so I can see it on the map.",
    "web_fetch": "Fetch this page and summarize it: https://www.weather.gov/tbw/",
    "code_exec_request": "Run a quick Python calculation for me: what is 365 * 24?",
}

TAMPA = "for the downtown Tampa, Florida area"


def _gen_prompt(name: str, desc: str) -> str:
    if name in HAND_PROMPTS:
        return HAND_PROMPTS[name]
    first = re.split(r"(?<=[.!?])\s", (desc or "").strip())[0].strip().rstrip(".")
    # strip identifier-ish tokens so the prompt doesn't leak the tool name
    first = re.sub(re.escape(name), "", first, flags=re.I)
    first = re.sub(r"``[^`]*``|`[^`]*`", "", first)
    first = re.sub(r"\s+", " ", first).strip(" -:;,")
    if not first:
        first = name.replace("_", " ")
    # imperative-ize: many descriptions already start with a verb
    return f"{first} {TAMPA}."


def build_specs() -> list[dict]:
    import importlib as il, pkgutil
    import grace2_agent.tools as pkg
    from grace2_agent.tools import get_registered_tools
    for m in pkgutil.iter_modules(pkg.__path__):
        il.import_module(f"grace2_agent.tools.{m.name}")
    try:
        import grace2_agent.workflows as wpkg
        for m in pkgutil.iter_modules(wpkg.__path__):
            try:
                il.import_module(f"grace2_agent.workflows.{m.name}")
            except Exception:
                pass
    except ImportError:
        pass
    specs = []
    for i, rt in enumerate(get_registered_tools(), 1):
        name = rt.metadata.name
        if name in EXCLUDE:
            continue
        desc = (rt.fn.__doc__ or "").strip()  # metadata has no description; the fn docstring is the schema description source
        specs.append({
            "id": i,
            "short": name,
            "prompt": _gen_prompt(name, desc),
            "expected": name,
            "expected_set": {name},
            "is_solver": bench._is_solver_tool(name),
        })
    return specs


def load_done() -> dict[str, dict]:
    done = {}
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["tool"]] = r
    return done


def write_report(done: dict[str, dict], total: int) -> None:
    from collections import Counter
    c = Counter(r["outcome"] for r in done.values())
    lines = [
        "# TRID3NT Local tool-routing sweep (pass 3) -- qwen3:8b-16k",
        "",
        f"Updated: {dt.datetime.now().isoformat(timespec='seconds')}  ",
        f"Scored {len(done)}/{total} | " + " | ".join(f"{k} {v}" for k, v in sorted(c.items())),
        "",
        "| tool | outcome | first_call | seconds |",
        "|---|---|---|---|",
    ]
    for name in sorted(done):
        r = done[name]
        lines.append(f"| {name} | {r['outcome']} | {r.get('first_call','')} | {r.get('seconds',0):.0f} |")
    REPORT.write_text("\n".join(lines) + "\n")


async def main() -> None:
    import websockets

    specs = build_specs()
    done = load_done()
    todo = [s for s in specs if s["short"] not in done]
    print(f"{len(specs)} prompts; {len(done)} done; {len(todo)} to run")
    RESULTS.parent.mkdir(parents=True, exist_ok=True)

    RECONNECT_EVERY = 8  # fresh WS + case periodically: no history bleed, bounded server state
    ws = None
    session_id = case_id = None
    ran_on_conn = 0
    for spec in todo:
        if ws is None or ran_on_conn >= RECONNECT_EVERY:
            if ws is not None:
                await ws.close()
            session_id = bench.new_id()
            ws = await websockets.connect(bench.WS_URL, max_size=None)
            await bench.do_handshake(ws, session_id)
            case_id = await bench.create_case(ws, session_id, f"routing-sweep-{session_id[:6]}")
            ran_on_conn = 0
        t0 = dt.datetime.now()
        try:
            result = await bench.run_one_prompt(ws, session_id, case_id, spec)
            score = bench.score_prompt(spec, result)
            verdict = score["verdict"]
            outcome = ("HIT" if verdict in ("SELECTED_CORRECT", "CHAIN_CORRECT")
                       else "NO_CALL" if verdict == "NO_CALL"
                       else "MISS")
            first_call = score.get("first_tool") or ""
        except Exception as exc:  # noqa: BLE001 - keep sweeping
            outcome, first_call, result = "ERROR", f"{type(exc).__name__}: {exc}"[:80], {}
            # transport died: force a reconnect next loop
            try:
                await ws.close()
            except Exception:
                pass
            ws = None
        rec = {
            "tool": spec["short"], "outcome": outcome, "first_call": first_call,
            "prompt": spec["prompt"][:140],
            "seconds": (dt.datetime.now() - t0).total_seconds(),
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
        }
        with RESULTS.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        done[spec["short"]] = rec
        ran_on_conn += 1
        print(f"[{len(done)}/{len(specs)}] {spec['short']}: {outcome} ({first_call}) {rec['seconds']:.0f}s")
        write_report(done, len(specs))
    if ws is not None:
        await ws.close()
    write_report(done, len(specs))
    print("routing sweep complete")


if __name__ == "__main__":
    asyncio.run(main())
