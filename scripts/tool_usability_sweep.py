#!/usr/bin/env python3
"""tool_usability_sweep.py -- "usable coverage": is every tool reachable in
<= 2 user turns?

The routing sweep (pass 3) measures the HARSHEST case: one cold, context-free
prompt must route to exactly the right tool. Real usage is interactive -- a
user who doesn't get what they wanted says so and points at the tool (the tool
catalog is a user-visible page in the product). This harness measures that:

  turn 1  the routing sweep's generated prompt (cold discovery)
  turn 2  same case, directed follow-up naming the tool:
          "That is not what I wanted. Use the <tool> tool for this."

Scored per tool:

  USABLE_T1   expected tool called on the cold prompt (== routing HIT)
  USABLE_T2   turn 1 missed, the directed turn 2 reached the tool
  UNUSABLE    the model cannot reach the tool even when told its name
  ERROR       transport / timeout before any signal

Tools that already HIT in a completed routing-results file (--baseline) are
imported as USABLE_T1 without re-running. Sequential, one prompt at a time,
resumable via docs/reports/tool-usability-results.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "docs" / "reports" / "tool-usability-results.jsonl"
REPORT = REPO / "docs" / "reports" / "tool-usability-report.md"

_spec = importlib.util.spec_from_file_location(
    "tool_routing_bench", REPO / "scripts" / "tool_routing_bench.py"
)
bench = importlib.util.module_from_spec(_spec)
sys.modules["tool_routing_bench"] = bench
_spec.loader.exec_module(bench)

_sweep_spec = importlib.util.spec_from_file_location(
    "tool_routing_sweep", REPO / "scripts" / "tool_routing_sweep.py"
)
sweep = importlib.util.module_from_spec(_sweep_spec)
sys.modules["tool_routing_sweep"] = sweep
_sweep_spec.loader.exec_module(sweep)


def _turn2_prompt(name: str) -> str:
    return (
        "That is not what I wanted. Use the "
        f"{name} tool for this, with sensible defaults for anything I did "
        "not specify."
    )


def load_done() -> dict[str, dict]:
    done: dict[str, dict] = {}
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["tool"]] = r
    return done


def write_report(done: dict[str, dict], total: int) -> None:
    from collections import Counter

    c = Counter(r["outcome"] for r in done.values())
    usable = c.get("USABLE_T1", 0) + c.get("USABLE_T2", 0)
    lines = [
        "# TRID3NT Local tool-usability sweep (<= 2 turns)",
        "",
        f"Updated: {dt.datetime.now().isoformat(timespec='seconds')}  ",
        f"Scored {len(done)}/{total} | "
        + " | ".join(f"{k} {v}" for k, v in sorted(c.items())),
        "",
        f"**Usable coverage so far: {usable}/{len(done)}"
        + (f" ({100 * usable / len(done):.0f}%)**" if done else "**"),
        "",
        "| tool | outcome | t1_first_call | t2_first_call | seconds |",
        "|---|---|---|---|---|",
    ]
    for name in sorted(done):
        r = done[name]
        lines.append(
            f"| {name} | {r['outcome']} | {r.get('t1_first_call', '')} "
            f"| {r.get('t2_first_call', '')} | {r.get('seconds', 0):.0f} |"
        )
    REPORT.write_text("\n".join(lines) + "\n")


async def main() -> None:
    import websockets

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--baseline",
        default=str(REPO / "docs" / "reports" / "tool-routing-results.jsonl"),
        help="completed routing-results JSONL; HITs import as USABLE_T1 "
        "and only the failures re-run",
    )
    args = ap.parse_args()

    specs = sweep.build_specs()
    by_name = {s["short"]: s for s in specs}
    done = load_done()

    # Import baseline HITs (turn-1 success proven -- no need to re-drive).
    baseline_path = Path(args.baseline)
    imported = 0
    if baseline_path.exists():
        for line in baseline_path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            name = r.get("tool")
            if name in by_name and name not in done and r.get("outcome") == "HIT":
                rec = {
                    "tool": name,
                    "outcome": "USABLE_T1",
                    "t1_first_call": r.get("first_call", ""),
                    "t2_first_call": "",
                    "prompt": r.get("prompt", ""),
                    "seconds": r.get("seconds", 0),
                    "ts": dt.datetime.now().isoformat(timespec="seconds"),
                    "imported_from_baseline": True,
                }
                with RESULTS.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
                done[name] = rec
                imported += 1
    print(f"imported {imported} baseline HITs as USABLE_T1")

    todo = [s for s in specs if s["short"] not in done]
    print(f"{len(specs)} tools; {len(done)} done; {len(todo)} to drive (2-turn)")
    RESULTS.parent.mkdir(parents=True, exist_ok=True)

    RECONNECT_EVERY = 4  # 2 turns/tool -> shorter connections than the sweep
    ws = None
    session_id = None
    ran_on_conn = 0
    for spec in todo:
        if ws is None or ran_on_conn >= RECONNECT_EVERY:
            if ws is not None:
                await ws.close()
            session_id = bench.new_id()
            for attempt in range(6):
                try:
                    ws = await websockets.connect(
                        bench.WS_URL, max_size=None, open_timeout=30
                    )
                    await bench.do_handshake(ws, session_id)
                    break
                except Exception:
                    if attempt == 5:
                        raise
                    await asyncio.sleep(60)
            ran_on_conn = 0
        t0 = dt.datetime.now()
        t1_first = t2_first = ""
        try:
            # fresh case per tool: turn 2 must see ONLY its own turn 1
            case_id = await bench.create_case(
                ws, session_id, f"usability-{spec['short'][:24]}"
            )
            if sweep._needs_layer(spec["short"]):
                try:
                    await bench.run_one_prompt(
                        ws, session_id, case_id, sweep.SEED_SPEC
                    )
                except Exception:
                    pass
            result = await bench.run_one_prompt(ws, session_id, case_id, spec)
            score = bench.score_prompt(spec, result)
            t1_first = score.get("first_tool") or ""
            if score["verdict"] in ("SELECTED_CORRECT", "CHAIN_CORRECT"):
                outcome = "USABLE_T1"
            else:
                spec2 = dict(spec, prompt=_turn2_prompt(spec["short"]))
                result2 = await bench.run_one_prompt(
                    ws, session_id, case_id, spec2
                )
                score2 = bench.score_prompt(spec2, result2)
                t2_first = score2.get("first_tool") or ""
                outcome = (
                    "USABLE_T2"
                    if score2["verdict"] in ("SELECTED_CORRECT", "CHAIN_CORRECT")
                    else "UNUSABLE"
                )
        except Exception as exc:  # noqa: BLE001 - keep sweeping
            outcome = "ERROR"
            t2_first = f"{type(exc).__name__}: {exc}"[:80]
            try:
                await ws.close()
            except Exception:
                pass
            ws = None
        rec = {
            "tool": spec["short"],
            "outcome": outcome,
            "t1_first_call": t1_first,
            "t2_first_call": t2_first,
            "prompt": spec["prompt"][:140],
            "seconds": (dt.datetime.now() - t0).total_seconds(),
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
        }
        with RESULTS.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        done[spec["short"]] = rec
        ran_on_conn += 1
        print(
            f"[{len(done)}/{len(specs)}] {spec['short']}: {outcome} "
            f"(t1={t1_first} t2={t2_first}) {rec['seconds']:.0f}s"
        )
        write_report(done, len(specs))
    if ws is not None:
        await ws.close()
    write_report(done, len(specs))
    print("usability sweep complete")


if __name__ == "__main__":
    asyncio.run(main())
