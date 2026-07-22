#!/usr/bin/env python3
"""routing_sweep -- API-driven tool-routing benchmark engine (experiments/bench).

Drives the LIVE daemon over WebSocket (ws://127.0.0.1:8765/ws by default),
one fresh case per record: send the prompt, watch the pipeline envelopes
(pipeline-state / tool-io), grade the fired-tool sequence deterministically
against the record's expected sets. NO LLM anywhere in grading.

Reuses the WS patterns of scripts/ws_smoke.py + scripts/tool_routing_bench.py
(handshake, case create, envelope drain, payload-warning auto-confirm), kept
self-contained here so the bench scripts stay untouched.

EXECUTION POLICY (v1, client-side): the server-side pre-dispatch block hook
does NOT exist yet. For execution=block_at_invocation records the engine
grades on the FIRST MATERIAL tool-call envelope (material = not in the
record's always_allowed set, not a meta/discovery tool, not an LLM
bookkeeping step) and immediately cancels the turn. The blocked tool may
briefly START before the cancel lands -- the server-side pre-dispatch hook
replaces this after the current server batch, making the block airtight
before any fetch. Run-tier records are also cancelled the moment the fired
sequence VIOLATES the record's sets (no value in letting a mis-routed tool
execute) -> SELECTED_WRONG_BLOCKED / FALSE_POSITIVE.

PERMISSION GATE: NEVER auto-runs. Without --i-have-permission the script
loads + validates the inputs, prints the resource profile (record count,
tiers, expected API classes) and exits 2.

ENV: the DAEMON must run with TRID3NT_AMBIGUITY_MARGIN=0 (kills ADR 0018
ambiguity asks so the sweep never stalls on a tool-candidates card). The
engine cannot set the daemon's env; it prints the requirement and sets the
var in its own process only for any in-process seams.

Output per run: data/<UTC timestamp>/raw_envelopes.jsonl (every envelope in
+ out, raw) + results.json + summary.txt.

ASCII only. Packages: trid3nt_server / trid3nt_contracts (agent venv).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_INPUTS = [
    HERE / "inputs" / "domain_sweep_specific.json",
    HERE / "inputs" / "domain_sweep_vague.json",
]
DEFAULT_WS_URL = "ws://127.0.0.1:8765/ws"
DEFAULT_RECORD_TIMEOUT_S = 240.0

# LLM bookkeeping step names (tool_routing_bench.py) -- never "fired tools".
LLM_STEP_NAMES = frozenset({
    "llm_generation", "gemini_generate", "thinking", "llm",
    "model_generate", "generate", "bedrock_generate", "ollama_generate",
})

# The three always-available meta/discovery tools (categories.AllowedToolSet
# _META_TOOLS). They are the routing MECHANISM (discovery round-trips), not a
# routing outcome, so they are excluded from the graded fired sequence --
# recorded raw, never graded. Documented in experiments/README.md.
META_TOOLS = frozenset({"list_categories", "list_tools_in_category", "discover_dataset"})

# Deterministic string patterns for the two upstream columns (documented in
# experiments/README.md; grading stays LLM-free).
LLM_UPSTREAM_PATTERNS = (
    "LLM", "PROVIDER", "RATE_LIMIT", "RATE LIMIT", "OVERLOAD", "EXHAUST",
    "QUOTA", "MODEL_UNAVAILABLE", "CONTEXT_LENGTH",
)
TOOL_UPSTREAM_PATTERNS = (
    "UPSTREAM", "HTTP_5", "HTTP 5", "502", "503", "504", "TIMEOUT",
    "TIMED OUT", "SERVICE_UNAVAILABLE", "SERVICE UNAVAILABLE",
    "CONNECTION", "VENDOR", "REMOTE",
)
ARG_INVALID_PATTERNS = ("USER_INPUT_REQUIRED", "MISSING_ARG", "VALIDATION", "INVALID_ARG")

VERDICTS = (
    "CORRECT",
    "CORRECT_BLOCKED",
    "SELECTED_WRONG_BLOCKED",
    "NO_CALL",
    "FALSE_POSITIVE",
    "UPSTREAM_FAILURE",
    "TOOL_UPSTREAM_ERROR",
)
# Correct routing picks (TOOL_UPSTREAM_ERROR = correct pick, vendor died --
# its own column, not a routing failure). UPSTREAM_FAILURE is excluded from
# the accuracy denominator entirely.
CORRECT_PICK_VERDICTS = frozenset({"CORRECT", "CORRECT_BLOCKED", "TOOL_UPSTREAM_ERROR"})


# ---------------------------------------------------------------------------
# Input loading + validation (typed load errors; unknown name = no run).
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
class SweepRecord:
    id: str
    category: str
    register: str                 # "specific" | "vague"
    prompt: str
    execution: str                # "run" | "block_at_invocation"
    acceptable: frozenset[str]
    always_allowed: frozenset[str]
    forbidden: frozenset[str]
    no_tool: bool
    source_file: str


def load_live_catalog() -> set[str]:
    """The live tool catalog: TOOL_REGISTRY after the startup import path.

    ``main._import_tools_registry()`` is the SAME call the daemon makes at
    startup, so the loader validates against exactly the startup-registered
    names (incl. catalog_search / catalog_fetch / list_qgis_algorithms /
    describe_qgis_algorithm and the categories.py meta-tools).
    """
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


def load_input_file(path: Path, catalog: set[str]) -> list[SweepRecord]:
    """Load + validate one input JSON. Every tool name in every set is checked
    against the live catalog; malformed record or unknown name raises
    InputLoadError (typed) and NOTHING runs."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise InputLoadError(path, None, f"cannot read/parse JSON: {exc}") from exc

    _require(isinstance(doc, dict), path, None, "top level must be an object")
    allowed_top = {"defaults", "records"}
    extra = {k for k in doc if k not in allowed_top and not k.startswith("_")}
    _require(not extra, path, None, f"unknown top-level keys: {sorted(extra)}")

    defaults = doc.get("defaults", {})
    _require(isinstance(defaults, dict), path, None, "defaults must be an object")
    default_aa = defaults.get("always_allowed", [])
    _require(isinstance(default_aa, list), path, None, "defaults.always_allowed must be a list")

    records_raw = doc.get("records")
    _require(isinstance(records_raw, list) and records_raw, path, None, "records must be a non-empty list")

    records: list[SweepRecord] = []
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
        # Record-level always_allowed OVERRIDES the file-level default.
        aa_raw = exp.get("always_allowed")
        always_allowed = (_name_list(aa_raw, path, rid, "always_allowed")
                          if aa_raw is not None else list(default_aa))
        forbidden = _name_list(exp.get("forbidden", []), path, rid, "forbidden")

        if no_tool:
            _require(not acceptable, path, rid, "no_tool record must have an empty acceptable set")
        else:
            _require(bool(acceptable), path, rid, "acceptable must be non-empty unless no_tool")

        unknown = (set(acceptable) | set(always_allowed) | set(forbidden)) - catalog
        _require(not unknown, path, rid,
                 f"tool names not in the live catalog ({len(catalog)} tools): {sorted(unknown)}")

        records.append(SweepRecord(
            id=rid,
            category=rec["category"],
            register=rec["register"],
            prompt=rec["prompt"].strip(),
            execution=rec["execution"],
            acceptable=frozenset(acceptable),
            always_allowed=frozenset(always_allowed),
            forbidden=frozenset(forbidden),
            no_tool=no_tool,
            source_file=path.name,
        ))
    return records


# ---------------------------------------------------------------------------
# Grading (deterministic; NO LLM).
# ---------------------------------------------------------------------------


@dataclass
class DriveResult:
    fired: list[str] = field(default_factory=list)      # graded fired sequence, in order
    raw_step_names: list[str] = field(default_factory=list)  # everything seen, incl. meta/LLM
    blocked: bool = False                                # engine cancelled the turn
    block_reason: str | None = None
    args_valid: bool = True
    llm_upstream_failure: bool = False
    tool_errors: dict[str, str] = field(default_factory=dict)  # fired tool -> error text
    wall_time_s: float = 0.0
    error_envelopes: list[dict] = field(default_factory=list)
    turn_completed: bool = False


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    up = text.upper()
    return any(p in up for p in patterns)


def sets_pass(record: SweepRecord, fired: list[str]) -> bool:
    """The PASS rule: >=1 fired tool in acceptable AND every fired tool in
    acceptable UNION always_allowed AND no fired tool in forbidden.
    no_tool records PASS iff zero fired."""
    if record.no_tool:
        return not fired
    if not fired:
        return False
    allowed = record.acceptable | record.always_allowed
    return (
        any(t in record.acceptable for t in fired)
        and all(t in allowed for t in fired)
        and not any(t in record.forbidden for t in fired)
    )


def grade(record: SweepRecord, result: DriveResult) -> str:
    fired = result.fired
    if result.llm_upstream_failure and not fired:
        return "UPSTREAM_FAILURE"
    if record.no_tool:
        return "FALSE_POSITIVE" if fired else "CORRECT"
    if not fired:
        return "NO_CALL"
    passed = sets_pass(record, fired)
    if result.blocked:
        if passed and result.args_valid:
            # block tier with a correct pick -> CORRECT_BLOCKED; a run-tier
            # record is only ever blocked on a violation, so it cannot land here.
            return "CORRECT_BLOCKED" if record.execution == "block_at_invocation" else "SELECTED_WRONG_BLOCKED"
        return "SELECTED_WRONG_BLOCKED"
    if passed:
        for t in fired:
            if t in record.acceptable and t in result.tool_errors:
                if _matches(result.tool_errors[t], TOOL_UPSTREAM_PATTERNS):
                    return "TOOL_UPSTREAM_ERROR"
        return "CORRECT"
    # A completed run-tier turn that violated the sets (violation only visible
    # at the end, e.g. a missing acceptable hit): same wrong-selection column.
    return "SELECTED_WRONG_BLOCKED"


# ---------------------------------------------------------------------------
# WS driver (ws_smoke.py / tool_routing_bench.py patterns).
# ---------------------------------------------------------------------------

try:
    from trid3nt_contracts import new_ulid as _new_ulid

    def new_id() -> str:
        return _new_ulid()
except ImportError:  # pragma: no cover -- contracts always present in the agent venv
    import uuid

    def new_id() -> str:
        return str(uuid.uuid4()).replace("-", "").upper()[:26]


class RawRecorder:
    """Append-only raw envelope log: data/<ts>/raw_envelopes.jsonl."""

    def __init__(self, path: Path) -> None:
        self._fh = path.open("a", encoding="utf-8")

    def log(self, run_index: int, record_id: str, direction: str, envelope: Any) -> None:
        self._fh.write(json.dumps({
            "ts": time.time(),
            "run": run_index,
            "record_id": record_id,
            "direction": direction,   # "send" | "recv"
            "envelope": envelope,
        }, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def mk(type_: str, session_id: str, payload: dict, case_id: str | None = None) -> dict:
    return {
        "type": type_,
        "id": new_id(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "session_id": session_id,
        "case_id": case_id,
        "payload": payload,
    }


async def _send(ws, rec: RawRecorder, run_index: int, record_id: str, env: dict) -> None:
    rec.log(run_index, record_id, "send", env)
    await ws.send(json.dumps(env))


async def _handshake_and_case(ws, rec: RawRecorder, run_index: int, record_id: str,
                              session_id: str, title: str) -> str:
    await _send(ws, rec, run_index, record_id,
                mk("auth-token", session_id, {"token": "", "anonymous_user_id": None}))
    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    rec.log(run_index, record_id, "recv", ack)
    if ack.get("type") != "auth-ack":
        raise RuntimeError(f"expected auth-ack, got {ack.get('type')}")

    await _send(ws, rec, run_index, record_id,
                mk("session-resume", session_id, {"case_id": None}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        rec.log(run_index, record_id, "recv", msg)
        if msg.get("type") == "session-state":
            break

    await _send(ws, rec, run_index, record_id,
                mk("case-command", session_id, {"command": "create", "args": {"title": title}}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        rec.log(run_index, record_id, "recv", msg)
        if msg.get("type") == "case-open":
            ss = (msg.get("payload") or {}).get("session_state")
            if ss:
                return ss["case"]["case_id"]


async def drive_record(ws_url: str, record: SweepRecord, rec: RawRecorder,
                       run_index: int, timeout_s: float) -> DriveResult:
    """One record: fresh session + case, send prompt, watch envelopes, apply
    the execution policy (see module docstring)."""
    import websockets.asyncio.client as ws_client

    result = DriveResult()
    session_id = new_id()
    t0 = time.monotonic()

    async with ws_client.connect(ws_url, open_timeout=15, close_timeout=10) as ws:
        case_id = await _handshake_and_case(
            ws, rec, run_index, record.id, session_id, f"routing-sweep-{record.id}-r{run_index}")

        await _send(ws, rec, run_index, record.id,
                    mk("user-message", session_id,
                       {"text": record.prompt, "case_id": case_id}, case_id=case_id))

        llm_started = False
        cancel_sent = False
        deadline = t0 + timeout_s

        async def cancel_turn(reason: str) -> None:
            nonlocal cancel_sent
            if cancel_sent:
                return
            cancel_sent = True
            result.blocked = True
            result.block_reason = reason
            await _send(ws, rec, run_index, record.id,
                        mk("cancel", session_id, {"reason": reason}, case_id=case_id))

        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(deadline - time.monotonic(), 20))
            except asyncio.TimeoutError:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            rec.log(run_index, record.id, "recv", msg)
            mtype = msg.get("type", "")
            payload = msg.get("payload") or {}

            if mtype == "pipeline-state":
                llm_started = True
                for step in payload.get("steps") or []:
                    name = step.get("tool_name") or step.get("name") or ""
                    if not name:
                        continue
                    if name not in result.raw_step_names:
                        result.raw_step_names.append(name)
                    if name.lower() in LLM_STEP_NAMES or name in META_TOOLS:
                        continue  # bookkeeping / discovery round-trips: recorded, not graded
                    if name not in result.fired:
                        result.fired.append(name)
                        # --- client-side execution policy on each NEW fired tool ---
                        material = name not in record.always_allowed
                        if record.no_tool:
                            await cancel_turn(f"no_tool record fired {name}")
                        elif material and record.execution == "block_at_invocation":
                            # grade on the FIRST material tool-call, then cancel.
                            await cancel_turn(f"block_at_invocation: first material tool {name}")
                        elif material and (
                            name in record.forbidden
                            or name not in (record.acceptable | record.always_allowed)
                        ):
                            # run tier mis-route: cancel rather than execute it.
                            await cancel_turn(f"set violation: {name}")
                if cancel_sent:
                    break

            elif mtype == "tool-io":
                name = payload.get("tool_name") or ""
                if name and name not in result.raw_step_names:
                    result.raw_step_names.append(name)
                if payload.get("is_error") and name:
                    result.tool_errors[name] = (payload.get("function_response") or "")[:2000]
                    if _matches(result.tool_errors[name], ARG_INVALID_PATTERNS) and len(result.fired) <= 1:
                        result.args_valid = False

            elif mtype == "tool-payload-warning":
                decision = "cancel" if record.execution == "block_at_invocation" else "proceed"
                await _send(ws, rec, run_index, record.id,
                            mk("tool-payload-confirmation", session_id,
                               {"warning_id": payload.get("warning_id"),
                                "decision": decision, "revised_args": None}))

            elif mtype == "error":
                result.error_envelopes.append(payload)
                text = f"{payload.get('error_code', '')} {payload.get('message', '')}"
                if _matches(text, LLM_UPSTREAM_PATTERNS):
                    result.llm_upstream_failure = True
                if _matches(text, ARG_INVALID_PATTERNS) and len(result.fired) <= 1:
                    result.args_valid = False

            elif mtype == "turn-complete":
                if llm_started:
                    result.turn_completed = True
                    break

        # Drain briefly after a cancel so the next record starts clean.
        if cancel_sent:
            drain_deadline = time.monotonic() + 5
            while time.monotonic() < drain_deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    msg = json.loads(raw)
                    rec.log(run_index, record.id, "recv", msg)
                    if msg.get("type") == "turn-complete":
                        break
                except (asyncio.TimeoutError, json.JSONDecodeError):
                    break

    result.wall_time_s = round(time.monotonic() - t0, 1)
    return result


# ---------------------------------------------------------------------------
# Resource profile (printed WITHOUT --i-have-permission; no run happens).
# ---------------------------------------------------------------------------


def print_resource_profile(records: list[SweepRecord], runs: int, ws_url: str) -> None:
    run_tier = [r for r in records if r.execution == "run" and not r.no_tool]
    block_tier = [r for r in records if r.execution == "block_at_invocation"]
    no_tool = [r for r in records if r.no_tool]
    api_classes = sorted({t for r in run_tier for t in r.acceptable})
    blocked_classes = sorted({t for r in block_tier for t in r.acceptable})

    print("=" * 72)
    print("ROUTING SWEEP -- RESOURCE PROFILE (no run: --i-have-permission absent)")
    print("=" * 72)
    files = sorted({r.source_file for r in records})
    print(f"input files      : {', '.join(files)}")
    print(f"records          : {len(records)} total "
          f"(specific={sum(1 for r in records if r.register == 'specific')}, "
          f"vague={sum(1 for r in records if r.register == 'vague')})")
    print(f"execution tiers  : run={len(run_tier)}  "
          f"block_at_invocation={len(block_tier)}  no_tool={len(no_tool)}")
    print(f"repetitions      : --runs {runs} -> {len(records) * runs} agent turns at {ws_url}")
    print(f"LLM provider     : the daemon's configured model backend, "
          f"{len(records) * runs} turns (+ any in-turn retries)")
    print(f"expected API classes (run tier acceptable sets, MAY execute):")
    for t in api_classes:
        print(f"  - {t}")
    print(f"blocked at invocation (graded, deliberately NOT executed):")
    for t in blocked_classes:
        print(f"  - {t}")
    print("env requirement  : daemon MUST run with TRID3NT_AMBIGUITY_MARGIN=0")
    print("inputs status    : DRAFT -- NATE reviews before ANY run")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Aggregation + reporting.
# ---------------------------------------------------------------------------


def aggregate_run(graded: list[dict]) -> dict:
    counts = {v: 0 for v in VERDICTS}
    for g in graded:
        counts[g["verdict"]] += 1
    denominator = len(graded) - counts["UPSTREAM_FAILURE"]
    correct = sum(counts[v] for v in CORRECT_PICK_VERDICTS)
    return {
        "counts": counts,
        "denominator_excl_upstream_failure": denominator,
        "routing_accuracy": round(correct / denominator, 4) if denominator else None,
    }


def write_outputs(out_dir: Path, meta: dict, per_run: list[list[dict]]) -> None:
    aggregates = [aggregate_run(g) for g in per_run]
    accs = [a["routing_accuracy"] for a in aggregates if a["routing_accuracy"] is not None]
    overall = {
        "per_run": aggregates,
        "routing_accuracy_mean": round(sum(accs) / len(accs), 4) if accs else None,
        "routing_accuracy_min": min(accs) if accs else None,
        "routing_accuracy_max": max(accs) if accs else None,
    }
    (out_dir / "results.json").write_text(json.dumps({
        "meta": meta, "runs": per_run, "aggregate": overall,
    }, indent=2, default=str) + "\n")

    lines: list[str] = []
    lines.append("routing_sweep summary  (" + meta["timestamp"] + ")")
    lines.append(f"records={meta['record_count']}  runs={meta['runs']}  ws={meta['ws_url']}")
    lines.append("")
    header = (f"{'record':34} {'tier':6} " +
              " ".join(f"run{i}" for i in range(1, len(per_run) + 1)))
    lines.append(header)
    lines.append("-" * len(header))
    by_id: dict[str, list[str]] = {}
    tiers: dict[str, str] = {}
    for run_graded in per_run:
        for g in run_graded:
            by_id.setdefault(g["record_id"], []).append(g["verdict"])
            tiers[g["record_id"]] = "block" if g["execution"] == "block_at_invocation" else "run"
    for rid, verdicts in by_id.items():
        lines.append(f"{rid:34} {tiers[rid]:6} " + " ".join(verdicts))
    lines.append("")
    lines.append("per-run verdict counts (UPSTREAM_FAILURE excluded from the accuracy")
    lines.append("denominator; TOOL_UPSTREAM_ERROR = correct pick, own column):")
    for i, agg in enumerate(aggregates, 1):
        cs = agg["counts"]
        lines.append(f"  run {i}: " + "  ".join(f"{v}={cs[v]}" for v in VERDICTS if cs[v]) +
                     f"  accuracy={agg['routing_accuracy']}")
    lines.append(f"aggregate routing accuracy: mean={overall['routing_accuracy_mean']} "
                 f"min={overall['routing_accuracy_min']} max={overall['routing_accuracy_max']}")
    text = "\n".join(lines) + "\n"
    (out_dir / "summary.txt").write_text(text)
    print(text)


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TRID3NT routing sweep bench engine (permission-gated; see experiments/README.md)")
    parser.add_argument("--inputs", nargs="*", type=Path, default=DEFAULT_INPUTS,
                        help="input JSON files (default: inputs/domain_sweep_*.json)")
    parser.add_argument("--runs", type=int, default=1,
                        help="repetitions of the full sweep (default 1)")
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL,
                        help=f"daemon WS endpoint (default {DEFAULT_WS_URL})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_RECORD_TIMEOUT_S,
                        help="per-record wall clock cap in seconds (default 240)")
    parser.add_argument("--i-have-permission", action="store_true",
                        help="EXPLICIT permission to actually drive the daemon; "
                             "without it only the resource profile is printed")
    args = parser.parse_args()

    # LOAD: validate everything against the live catalog. Typed error = no run.
    try:
        catalog = load_live_catalog()
        records: list[SweepRecord] = []
        for path in args.inputs:
            records.extend(load_input_file(path, catalog))
    except InputLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"[load] OK: {len(records)} records validated against the live catalog "
          f"({len(catalog)} tools)")

    if not args.i_have_permission:
        print_resource_profile(records, args.runs, args.ws_url)
        return 2

    # Documented requirement: the DAEMON must run with TRID3NT_AMBIGUITY_MARGIN=0.
    os.environ["TRID3NT_AMBIGUITY_MARGIN"] = "0"  # in-process seams only
    print("[env] TRID3NT_AMBIGUITY_MARGIN=0 set for this process; the DAEMON "
          "must also run with it (see experiments/README.md)")

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = HERE / "data" / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    recorder = RawRecorder(out_dir / "raw_envelopes.jsonl")

    meta = {
        "engine": "routing_sweep",
        "timestamp": timestamp,
        "ws_url": args.ws_url,
        "runs": args.runs,
        "record_count": len(records),
        "inputs": [str(p) for p in args.inputs],
        "catalog_size": len(catalog),
        "ambiguity_margin_env": os.environ.get("TRID3NT_AMBIGUITY_MARGIN"),
        "execution_policy": "v1 client-side block (cancel on first material tool-call); "
                            "server-side pre-dispatch hook pending",
    }

    per_run: list[list[dict]] = []
    try:
        for run_index in range(1, args.runs + 1):
            graded: list[dict] = []
            for record in records:
                print(f"[run {run_index}/{args.runs}] {record.id} ({record.execution}) ...",
                      flush=True)
                try:
                    result = asyncio.run(drive_record(
                        args.ws_url, record, recorder, run_index, args.timeout))
                except Exception as exc:  # noqa: BLE001 -- record the failure, keep sweeping
                    result = DriveResult()
                    result.error_envelopes.append({"engine_exception": str(exc)})
                    result.llm_upstream_failure = _matches(str(exc), LLM_UPSTREAM_PATTERNS)
                verdict = grade(record, result)
                graded.append({
                    "record_id": record.id,
                    "category": record.category,
                    "register": record.register,
                    "execution": record.execution,
                    "verdict": verdict,
                    "fired": result.fired,
                    "raw_step_names": result.raw_step_names,
                    "blocked": result.blocked,
                    "block_reason": result.block_reason,
                    "args_valid": result.args_valid,
                    "tool_errors": result.tool_errors,
                    "error_envelopes": result.error_envelopes,
                    "wall_time_s": result.wall_time_s,
                })
                print(f"    -> {verdict}  fired={result.fired}  t={result.wall_time_s}s")
                time.sleep(2)  # let the daemon settle between records
            per_run.append(graded)
    finally:
        recorder.close()

    write_outputs(out_dir, meta, per_run)
    print(f"[done] outputs: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
