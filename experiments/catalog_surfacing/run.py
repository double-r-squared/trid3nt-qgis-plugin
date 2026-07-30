#!/usr/bin/env python3
"""catalog_surfacing -- per-arm runner (experiments/catalog_surfacing/DESIGN.md).

Runs ONE arm of the catalog-surfacing experiment in its OWN process (the arm env
flag is import-time-frozen, so per-process isolation is the strongest guarantee
that registry / index / pool state cannot leak across arms). Two phases:

  1. MODEL-FREE reachability precondition gate (per DESIGN "Method"): for every
     source phrasing, confirm the source is REACHABLE by the arm's discovery
     surface (arm0: ambient visible set; arm1: search_data_catalog card top-k;
     arm2: retrieve_ranked_tools expandable top-k). Deterministic, no model.

  2. MODEL-IN-THE-LOOP drive: each phrasing is a single-turn task seeded with a
     canvas AOI bbox (NOT in the prompt text). The live adapter picks + forms
     args; we capture the FIRST target call (selection + first-attempt args) and,
     on a first-attempt router-validation failure, ONE retry with the typed error
     fed back. Grading itself is deterministic (done in score.py): selected NAME
     vs the acceptable set + router.validate_params pass/fail.

Arms:
  0 baseline : the 14 sources ambient (tier=general). Model fires the per-source
               tool directly.
  1 Design 1 : TRID3NT_CATALOG_ARM=1. 14 tier=catalog (pool-excluded). Ambient
               data surface = search_data_catalog (cards) + fetch_from_catalog
               (source=...). Selection = the fetch hop's `source` arg.
  2 Design 2 : TRID3NT_CATALOG_ARM=2. 14 tier=catalog (pool-excluded, indexed).
               search_tools hit gate-expands the matched source's per-source tool;
               model then fires it directly.

Live model: the stack's default adapter config (.env.local -> MODEL_PROVIDER).
Temperature pinned 0 via TRID3NT_OPENAI_TEMPERATURE=0 (set by the caller), N=1.

ASCII only. Run inside venvs/agent with .env.local sourced (live drive).
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "inputs" / "phrasings.json"
RESULTS = HERE / "results"
ARM_ENV = "TRID3NT_CATALOG_ARM"

#: The generic data-discovery surface, always declared so each arm's intended
#: discovery path is reachable (the arm decides which one the model uses).
_GENERIC_TOOLS = ["search_tools", "search_data_catalog", "fetch_from_catalog"]

#: Seeded canvas AOI (coastal CONUS; NOT emitted into the prompt text). Reused by
#: the model via the case-state note so bbox params are well-formed + in-CONUS.
_SEED_BBOX = [-83.0, 27.5, -82.0, 28.5]

MAX_HOPS = 5
MODEL_RETRIES = 4  # upstream-provider backoff attempts per model turn


def _build_records() -> tuple[list[dict], list[dict]]:
    doc = json.loads(INPUTS.read_text())
    sources: list[dict] = []
    for s in doc["sources"]:
        for i, ph in enumerate(s["phrasings"]):
            sources.append({
                "id": f"{s['source']}#{i}",
                "group": s["source"],
                "acceptable": list(s["acceptable"]),
                "prompt": ph["text"],
            })
    controls: list[dict] = []
    for i, ph in enumerate(doc["control"]["phrasings"]):
        controls.append({
            "id": f"control#{i}",
            "group": "control",
            "acceptable": list(ph["acceptable"]),
            "prompt": ph["text"],
        })
    return sources, controls


def _load_stack():
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    import trid3nt_server.agent.categories  # noqa: F401


# --------------------------------------------------------------------------- #
# Reachability precondition gate (model-free, deterministic).
# --------------------------------------------------------------------------- #


def _reachability(arm: int, records: list[dict]) -> dict:
    from trid3nt_server.agent.tools.fetchers._router import registration as reg
    from trid3nt_server.agent.tools.search.search_tools import search_tools as st
    from trid3nt_server.agent.tools.search.tool_retrieval import (
        MAX_K,
        retrieve_ranked_tools,
        retrieve_visible_tools,
    )

    st._reset_index_for_tests()
    st._get_index()  # warm

    per: list[dict] = []
    reached = 0
    n_sources = 0
    for rec in records:
        target = rec["acceptable"][0]
        prompt = rec["prompt"]
        is_source = rec["group"] != "control"
        if arm == 0:
            names = retrieve_visible_tools(prompt, None, MAX_K)
            ok = target in names
        elif arm == 1:
            names = [c["name"] for c in reg.search_spec_cards(prompt, MAX_K)]
            ok = target in names
        else:  # arm 2
            names = [n for n, _ in retrieve_ranked_tools(prompt, MAX_K)]
            ok = target in names
        # The precondition gate is defined over SOURCE phrasings only (controls
        # route to ambient non-catalog tools, not the discovery surface).
        if is_source:
            n_sources += 1
            reached += int(ok)
        per.append({"id": rec["id"], "group": rec["group"], "target": target,
                    "reached": bool(ok), "is_source": is_source})
    return {
        "arm": arm,
        "n_sources": n_sources,
        "reached": reached,
        "recall": round(reached / max(1, n_sources), 4),
        "per_record": per,
    }


# --------------------------------------------------------------------------- #
# Model-in-the-loop drive.
# --------------------------------------------------------------------------- #


def _validate_source_args(source: str, args: dict) -> tuple[bool, str, str]:
    """(ok, error_msg, error_code) from router.validate_params against the spec."""
    from trid3nt_server.agent.tools.fetchers._router import registration as reg
    from trid3nt_server.agent.tools.fetchers._router import router
    from trid3nt_server.agent.tools.fetchers._router.errors import RouterInputError

    spec = reg._SPEC_REGISTRY.get(source)
    if spec is None:
        return False, f"{source} is not a spec-served source", "UNKNOWN_SOURCE"
    try:
        router.validate_params(spec, args or {})
        return True, "", ""
    except RouterInputError as exc:
        code = getattr(exc, "error_code", "") or getattr(exc, "code", "") or "INPUT_ERROR"
        return False, str(exc), str(code)
    except Exception as exc:  # noqa: BLE001 -- any pre-network validation fault = invalid
        return False, f"{type(exc).__name__}: {exc}", "VALIDATION_FAULT"


async def _one_model_turn(contents, decls, adapter):
    """One live model round with upstream-provider backoff. Returns the first
    FunctionCallEvent (name, call_id, args) or None (text-only), plus any text."""
    from trid3nt_server.agent.adapters.adapter import (
        FunctionCallEvent,
        UpstreamProviderError,
    )

    last_exc: Exception | None = None
    for attempt in range(1, MODEL_RETRIES + 1):
        try:
            fc = None
            texts: list[str] = []
            async for ev in adapter.stream_events_with_contents(
                None, "live", contents, tool_declarations=decls,
                system_prompt=adapter.SYSTEM_PROMPT,
            ):
                if isinstance(ev, FunctionCallEvent) and fc is None:
                    fc = ev
                else:
                    t = getattr(ev, "text", None)
                    if t:
                        texts.append(t)
            return fc, "".join(texts), None
        except UpstreamProviderError as exc:
            last_exc = exc
            await asyncio.sleep(min(2 ** attempt, 20))
        except Exception as exc:  # noqa: BLE001 -- classify
            cls = adapter.classify_provider_error_class(exc)
            if cls == "upstream_provider":
                last_exc = exc
                await asyncio.sleep(min(2 ** attempt, 20))
            else:
                return None, "", ("internal", f"{type(exc).__name__}: {exc}")
    return None, "", ("upstream_provider", f"{type(last_exc).__name__}: {last_exc}")


async def _drive_record(arm, rec, adapter, reg, TOOL_REGISTRY, visible_names, declarable_floor):
    """Drive one record model-in-the-loop; return a graded-input record dict."""
    from trid3nt_server.agent.adapters.adapter import (
        build_contents_from_history,
        build_function_call_content,
        build_function_response_content,
        build_layers_present_note,
        build_tool_declarations,
    )

    prompt = rec["prompt"]
    is_control = rec["group"] == "control"
    note = build_layers_present_note([], case_bbox=_SEED_BBOX)
    contents = build_contents_from_history(prompt, [{"role": "user", "text": note}])

    # Per-turn declared set = the tool-retrieval visible gate INTERSECTED with the
    # ambient declarable floor (_default_declarable_registry, which drops tier in
    # {template,catalog}) -- exactly the production seam. In arm0 the 14 are in the
    # floor (tier=general) so they stay declared; in arm1/arm2 they are floor-excluded
    # so they leave the ambient surface and reach the turn ONLY via the arm's
    # discovery path (search_data_catalog cards / search_tools expansion). The generic
    # discovery tools are always declared.
    wanted = (set(visible_names) & declarable_floor) | set(_GENERIC_TOOLS)
    wanted &= set(TOOL_REGISTRY)

    def _decls():
        return build_tool_declarations({n: TOOL_REGISTRY[n] for n in sorted(wanted)})

    out: dict = {
        "id": rec["id"], "group": rec["group"], "prompt": prompt,
        "acceptable": rec["acceptable"], "arm": arm, "is_control": is_control,
        "fired_sequence": [], "selected_name": None, "selected_args": None,
        "first_attempt_valid": None, "one_retry_valid": None,
        "outcome": None, "upstream_failure": False, "note": "",
    }

    target_source = rec["acceptable"][0]
    selection_call = None  # (source_name_or_tool, args_for_validation)

    for hop in range(MAX_HOPS):
        fc, text, err = await _one_model_turn(contents, _decls(), adapter)
        if err is not None:
            kind, msg = err
            if kind == "upstream_provider":
                out["upstream_failure"] = True
                out["outcome"] = "UPSTREAM_FAILURE"
            else:
                out["outcome"] = "INTERNAL_ERROR"
            out["note"] = msg[:300]
            return out
        if fc is None:
            out["outcome"] = "NO_CALL"
            out["note"] = ("text: " + text[:160]) if text else "no function_call"
            return out

        name, args, cid = fc.name, dict(fc.args or {}), fc.call_id
        out["fired_sequence"].append({"name": name, "args": args})

        # --- Discovery/intermediate hops: execute in-process, feed back, continue.
        is_discovery = name in ("search_tools", "search_data_catalog")
        # For arm1 fetch_from_catalog IS the target (selection = source arg).
        if arm == 1 and name == "fetch_from_catalog":
            src = args.get("source")
            selection_call = (src if isinstance(src, str) else None,
                              args.get("params") if isinstance(args.get("params"), dict) else {})
            break
        if is_discovery:
            resp = await _exec_discovery(name, args, prompt, reg, TOOL_REGISTRY)
            # arm2 gate-expansion: union the search hit's tool_names into declared set.
            if arm == 2 and name == "search_tools":
                for tn in resp.get("_tool_names", []):
                    if tn in TOOL_REGISTRY:
                        wanted.add(tn)
            contents = list(contents) + [
                build_function_call_content(name, args, cid),
                build_function_response_content(name, resp.get("payload", {}), cid),
            ]
            continue

        # --- Non-discovery call: this is the selection for arm0/arm2.
        selection_call = (name, args)
        break

    if selection_call is None:
        out["outcome"] = out["outcome"] or "NO_SELECTION"
        return out

    sel_name, sel_args = selection_call
    out["selected_name"] = sel_name
    out["selected_args"] = sel_args

    # Controls: selection is just the fired tool name; param validity N/A.
    if is_control:
        out["outcome"] = "SELECTED"
        return out

    selected_ok = sel_name == target_source
    out["outcome"] = "SELECTED" if selected_ok else "WRONG_SOURCE"
    if not selected_ok or sel_name is None:
        return out

    # --- Param fidelity: first-attempt router validation. ---
    ok, emsg, ecode = _validate_source_args(sel_name, sel_args)
    out["first_attempt_valid"] = ok
    if ok:
        out["one_retry_valid"] = True
        return out

    # --- One retry: feed the typed error, capture the SECOND target call. ---
    fail_resp = {"error": emsg, "error_code": ecode, "retryable": False,
                 "hint": "correct the arguments per the schema and call again"}
    retry_name = "fetch_from_catalog" if arm == 1 else sel_name
    contents = list(contents) + [
        build_function_call_content(retry_name, out["fired_sequence"][-1]["args"], None),
        build_function_response_content(retry_name, fail_resp, None),
    ]
    for _ in range(MAX_HOPS):
        fc, text, err = await _one_model_turn(contents, _decls(), adapter)
        if err is not None:
            kind, msg = err
            out["one_retry_valid"] = False
            out["note"] = f"retry {kind}: {msg[:200]}"
            if kind == "upstream_provider":
                out["upstream_failure"] = True
            return out
        if fc is None:
            out["one_retry_valid"] = False
            out["note"] = "retry produced no call"
            return out
        name, args, cid = fc.name, dict(fc.args or {}), fc.call_id
        out["fired_sequence"].append({"name": name, "args": args, "retry": True})
        if name in ("search_tools", "search_data_catalog"):
            resp = await _exec_discovery(name, args, prompt, reg, TOOL_REGISTRY)
            contents = list(contents) + [
                build_function_call_content(name, args, cid),
                build_function_response_content(name, resp.get("payload", {}), cid),
            ]
            continue
        if arm == 1 and name == "fetch_from_catalog":
            src = args.get("source")
            rparams = args.get("params") if isinstance(args.get("params"), dict) else {}
            ok2, _, _ = _validate_source_args(src if isinstance(src, str) else "", rparams)
            out["one_retry_valid"] = bool(ok2 and src == target_source)
            return out
        # arm0/arm2 retry target call.
        ok2, _, _ = _validate_source_args(name, args)
        out["one_retry_valid"] = bool(ok2 and name == target_source)
        return out
    out["one_retry_valid"] = False
    return out


async def _exec_discovery(name, args, prompt, reg, TOOL_REGISTRY):
    """Execute an intermediate discovery tool in-process; return {payload, _tool_names}."""
    entry = TOOL_REGISTRY.get(name)
    if entry is None:
        return {"payload": {"error": f"{name} not declared"}, "_tool_names": []}
    fn = entry.fn
    try:
        call_args = dict(args or {})
        # Ensure a topic/query is present so the discovery tool returns something.
        if name == "search_data_catalog" and not call_args.get("topic"):
            call_args["topic"] = prompt
        if name == "search_tools" and not (call_args.get("query") or call_args.get("topic")):
            call_args["query"] = prompt
        result = fn(**_filter_kwargs(fn, call_args))
        if inspect.iscoroutine(result):
            result = await result
    except Exception as exc:  # noqa: BLE001
        return {"payload": {"error": f"{type(exc).__name__}: {exc}"}, "_tool_names": []}
    return _shape_discovery(name, result)


def _filter_kwargs(fn, args):
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return args
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return args
    return {k: v for k, v in args.items() if k in params}


def _shape_discovery(name, result):
    tool_names: list[str] = []
    if name == "search_tools" and isinstance(result, dict):
        for row in result.get("results", []) or []:
            tn = row.get("tool_name") if isinstance(row, dict) else None
            if isinstance(tn, str):
                tool_names.append(tn)
        return {"payload": result, "_tool_names": tool_names}
    if name == "search_data_catalog":
        rows = result if isinstance(result, list) else []
        return {"payload": {"results": rows}, "_tool_names": []}
    return {"payload": result if isinstance(result, dict) else {"results": result}, "_tool_names": tool_names}


async def _run_drive(arm, records, out_jsonl):
    import trid3nt_server.agent.adapters.adapter as adapter
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.tools.fetchers._router import registration as reg
    from trid3nt_server.agent.tools.search.search_tools import search_tools as st
    from trid3nt_server.agent.tools.search.tool_retrieval import (
        MAX_K,
        retrieve_visible_tools,
    )

    import trid3nt_server.server as srv

    st._reset_index_for_tests()
    st._get_index()  # warm the retrieval index once
    declarable_floor = set(srv._default_declarable_registry())

    # RESUME: each record is checkpointed to the jsonl as it completes (flush), so
    # a reap/kill loses at most one in-flight record. Re-running skips done ids.
    done: dict[str, dict] = {}
    if out_jsonl.exists():
        for line in out_jsonl.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["id"]] = r
    if done:
        print(f"[arm{arm}] resume: {len(done)} record(s) already checkpointed",
              flush=True)

    rows: list[dict] = [done[r["id"]] for r in records if r["id"] in done]
    with out_jsonl.open("a", encoding="utf-8") as fh:
        for i, rec in enumerate(records):
            if rec["id"] in done:
                continue
            visible = retrieve_visible_tools(rec["prompt"], None, MAX_K)
            row = await _drive_record(
                arm, rec, adapter, reg, TOOL_REGISTRY, visible, declarable_floor
            )
            rows.append(row)
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            print(f"[arm{arm}] {i+1}/{len(records)} {rec['id']}: "
                  f"sel={row['selected_name']} out={row['outcome']} "
                  f"fa={row['first_attempt_valid']} r1={row['one_retry_valid']}",
                  flush=True)
    return rows


# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", required=True, type=int, choices=[0, 1, 2])
    ap.add_argument("--reach-only", action="store_true",
                    help="run only the model-free reachability gate")
    ap.add_argument("--limit", type=int, default=0, help="cap records (debug)")
    args = ap.parse_args()
    arm = args.arm

    if arm == 0:
        os.environ.pop(ARM_ENV, None)
    else:
        os.environ[ARM_ENV] = str(arm)

    _load_stack()
    from trid3nt_server.agent.tools.fetchers._router import registration as reg
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    import trid3nt_server.server as srv

    assert reg.catalog_arm() == (None if arm == 0 else str(arm)), "arm env not seen"

    sources, controls = _build_records()
    records = sources + controls
    if args.limit:
        records = sources[: args.limit] + controls[: max(2, args.limit // 5)]

    RESULTS.mkdir(parents=True, exist_ok=True)

    # Phase 1: reachability.
    reach = _reachability(arm, records)
    (RESULTS / f"arm{arm}_reachability.json").write_text(
        json.dumps(reach, indent=2) + "\n"
    )
    print(f"[arm{arm}] reachability recall={reach['recall']} "
          f"({reach['reached']}/{reach['n_sources']})", flush=True)

    meta = {
        "arm": arm,
        "arm_env": os.environ.get(ARM_ENV),
        "catalog_arm": reg.catalog_arm(),
        "timestamp_utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "registry_size": len(TOOL_REGISTRY),
        "ambient_declarable_count": len(srv._default_declarable_registry()),
        "n_source_records": len(sources),
        "n_control_records": len(controls),
        "model_provider": os.environ.get("MODEL_PROVIDER", "bedrock"),
        "openai_model": os.environ.get("TRID3NT_OPENAI_MODEL"),
        "temperature": os.environ.get("TRID3NT_OPENAI_TEMPERATURE", "provider-default"),
        "n_trials": 1,
        "seed_bbox": _SEED_BBOX,
        "reachability_recall": reach["recall"],
    }

    if args.reach_only:
        (RESULTS / f"arm{arm}_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        return 0

    # Phase 2: model-in-the-loop drive (incremental checkpoint + resume).
    out_jsonl = RESULTS / f"arm{arm}_records.jsonl"
    t0 = time.perf_counter()
    rows = asyncio.run(_run_drive(arm, records, out_jsonl))
    meta["drive_seconds"] = round(time.perf_counter() - t0, 1)
    (RESULTS / f"arm{arm}_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"[arm{arm}] drive done in {meta['drive_seconds']}s -> {out_jsonl.name} "
          f"({len(rows)} records)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
