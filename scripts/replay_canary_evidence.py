"""Replay every committed canary and diff its metrics against the recorded ones.

A canary's evidence file already carries the two things a parity check needs -
the tool and the exact args it was called with, and the metrics it answered. So
"is this template still giving the same answer" is a direct re-invocation and a
field-for-field diff, with no session to drive.

Run ids, layer URIs and wall times are excluded for the obvious reason. Anything
else that moves is reported, per key, with both values.

Run:
  cd /home/nate/Documents/trid3nt-local
  set -a; source .env.local; set +a
  venvs/agent/bin/python scripts/replay_canary_evidence.py
  venvs/agent/bin/python scripts/replay_canary_evidence.py --only telemac_do_sag
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("replay_canary")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_ROOT = Path(__file__).resolve().parents[1] / "docs/proof/templates"

#: Keys that identify a RUN rather than an answer. Comparing them would report a
#: difference on every replay and hide the ones that matter.
_VOLATILE = ("run_id", "layer_uri", "uri", "layer_id", "wall_seconds",
             "reference_time", "event_time", "cycle")


def _comparable(metrics: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in (metrics or {}).items()
            if not any(tag in k for tag in _VOLATILE)}


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return a == b or (math.isnan(a) and math.isnan(b))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _diff(recorded: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(set(recorded) | set(fresh)):
        was, now = recorded.get(key, "<absent>"), fresh.get(key, "<absent>")
        if not _same(was, now):
            out[key] = {"recorded": _short(was), "replayed": _short(now)}
    return out


def _short(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) > 6:
        return {"length": len(value), "head": list(value[:4])}
    return value


def _metrics_of(fn: Any, layer: Any, keys: list[str]) -> dict[str, Any]:
    """The run's ANSWER, through the same function that wrote the recorded one.

    Not ``getattr`` over the layer: a declared provenance row (the resolved
    discharge and the note beside it) lives on the ANSWER and nowhere on the
    returned object, so reading attributes would report every one of them as a
    difference.
    """
    workflow = getattr(fn, "workflow", None)
    if workflow is not None:
        return {k: v for k, v in workflow.answer(layer).items() if k in keys}
    get = layer.get if isinstance(layer, dict) else (lambda f: getattr(layer, f, None))
    return {k: get(k) for k in keys}


def _approved_defaults(fn: Any, call: dict[str, Any]) -> dict[str, str]:
    """Supply, EXPLICITLY, the labeled physics defaults a live session approved.

    A canary recorded in a ``user_gated`` session had its physics-consequential
    labeled defaults approved on a card. Headless there is no card, so law 9
    refuses - correctly, and it must keep doing so. Replaying such a canary means
    supplying those DECLARED DEFAULTS by name, which is the same values through
    the user door instead of through the card. Names and values are reported, so
    the report says which rows were approved this way rather than hiding it.
    """
    workflow = getattr(fn, "workflow", None)
    if workflow is None:
        return {}
    approved: dict[str, str] = {}
    for prm in workflow.params:
        if (prm.consequence == "physics" and prm.default is not None
                and prm.name not in call):
            call[prm.name] = prm.default
            approved[prm.name] = prm.default
    return approved


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=None,
                    help="restrict to evidence files whose path contains this")
    ap.add_argument("--approve-defaults", action="store_true",
                    help="supply the declared physics defaults a user_gated "
                         "canary approved on its card, so it replays headless")
    ap.add_argument("--out", default="docs/proof/templates/canary_replay.json")
    args = ap.parse_args()

    from trid3nt_server.tools import TOOL_REGISTRY

    files = sorted(_ROOT.rglob("*_canary_evidence.json"))
    if args.only:
        files = [f for f in files if any(o in str(f) for o in args.only)]
    log.info("replaying %d canaries", len(files))

    report: list[dict[str, Any]] = []
    for path in files:
        evidence = json.loads(path.read_text())
        tool, call = evidence.get("tool"), dict(evidence.get("args") or {})
        recorded = _comparable(evidence.get("metrics") or {})
        entry: dict[str, Any] = {"canary": path.parent.parent.name + "/"
                                 + path.parent.name, "tool": tool}
        if not tool or not call or not recorded:
            # An evidence file written before the tool/args/metrics fields
            # existed describes a run nobody can re-issue. Saying so is the
            # answer; guessing the call from the folder name would not be.
            entry["verdict"] = "NOT REPLAYABLE - evidence carries no tool/args/metrics"
            report.append(entry)
            continue
        if tool not in TOOL_REGISTRY:
            entry["verdict"] = f"NOT REPLAYABLE - {tool} is not registered"
            report.append(entry)
            continue
        fn = TOOL_REGISTRY[tool].fn
        if args.approve_defaults:
            approved = _approved_defaults(fn, call)
            if approved:
                entry["approved_defaults"] = approved
        started = time.monotonic()
        out = await fn(**call)
        entry["wall_seconds"] = round(time.monotonic() - started, 1)
        if isinstance(out, dict) and out.get("status") == "error":
            entry["verdict"] = "FAILED"
            entry["error"] = {"code": out.get("error_code"),
                              "message": str(out.get("error_message"))[:300]}
            log.error("%s FAILED: %s", entry["canary"], entry["error"])
            report.append(entry)
            continue
        fresh = _comparable(_metrics_of(fn, out, sorted(recorded)))
        diff = _diff(recorded, fresh)
        entry["keys_compared"] = len(recorded)
        entry["verdict"] = "IDENTICAL" if not diff else "MOVED"
        if diff:
            entry["diff"] = diff
        log.info("%s %s (%d keys, %.1fs)", entry["canary"], entry["verdict"],
                 len(recorded), entry["wall_seconds"])
        report.append(entry)

    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    moved = [e for e in report if e["verdict"] not in ("IDENTICAL",)]
    print("CANARY_REPLAY " + json.dumps({
        "total": len(report),
        "identical": sum(1 for e in report if e["verdict"] == "IDENTICAL"),
        "not_identical": [{"canary": e["canary"], "verdict": e["verdict"]}
                          for e in moved],
        "report": args.out,
    }))
    return 0 if not moved else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
