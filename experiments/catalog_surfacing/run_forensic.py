#!/usr/bin/env python3
"""catalog_surfacing -- FORENSIC per-arm runner (model bench 2026-08-01).

A results-side COPY of run.py (server code untouched). It reuses run.py's SIGNED
grading logic VERBATIM (imports run._build_records / _drive_record /
_drive_record_arm3 / _reachability) and only swaps ONE thing: the model turn.

run._one_model_turn is monkeypatched with a FORENSIC turn that calls the
OpenAI-compatible client DIRECTLY (reusing the adapter's own public wire helpers
so the request is byte-identical: same messages incl. SYSTEM_PROMPT + the baked
tool-discipline line, same tools, same temperature, same max_tokens, same model)
and captures per-call: HTTP status, finish_reason, raw completion (content) length,
reasoning length, tool-call count, retry count, and any provider error body/class.
The (fc, text, err) return contract is IDENTICAL to run._one_model_turn (text =
CONTENT deltas only, reasoning is NOT counted as text -- exactly as the original),
so the NO_CALL / SELECTED / param grading is unchanged. Forensics ride in a side
channel written only under results/model_bench_2026-08-01/.

Every NO_CALL is later classified (score-side) as one of:
  provider_error        -- an exception was raised (429/5xx/timeout/other).
  empty_200_completion  -- 200, finish_reason stop/None, 0 content, 0 reasoning,
                           0 tool calls (silent empty -- the provider-artifact shape).
  reasoning_truncated   -- finish_reason == 'length' with reasoning/content burned
                           and no tool call (max_tokens starved a reasoning model --
                           an infra/config artifact, NATE's 'truncated completion').
  model_declined        -- real CONTENT text present, no tool call (genuine decline).

ASCII only. Run inside venvs/agent with the model env exported by the caller.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTDIR = HERE / "results" / "model_bench_2026-08-01"

# --- import the signed runner module (run.py) by path so we reuse its logic. ---
_spec = importlib.util.spec_from_file_location("cs_run", str(HERE / "run.py"))
run = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["cs_run"] = run
_spec.loader.exec_module(run)  # type: ignore[union-attr]

# Per-record forensic buffer (reset by the driver before each record).
_CUR_FORENSICS: list[dict] = []


async def _forensic_one_model_turn(contents, decls, adapter):
    """Drop-in forensic replacement for run._one_model_turn.

    Direct OpenAI-compatible streaming call reusing the adapter's public wire
    builders. Returns (fc, text, err) EXACTLY like the original; appends a
    forensic record per call to _CUR_FORENSICS.
    """
    from openai import AsyncOpenAI
    import openai as _openai

    from trid3nt_server.adapters.adapter import FunctionCallEvent  # noqa: F401
    from trid3nt_server.adapters import openai_adapter as oa
    from trid3nt_server.gates.context_budget import openai_max_output_tokens

    messages = oa.contents_to_openai_messages(
        list(contents), system_prompt=adapter.SYSTEM_PROMPT, show_thinking=False
    )
    tools = oa.tool_declarations_to_openai_tools(decls)

    kwargs = {
        "model": oa.openai_model(None),
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": oa.openai_temperature(),
        "max_tokens": openai_max_output_tokens(),
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    client = AsyncOpenAI(
        base_url=oa.openai_base_url(),
        api_key=oa.openai_api_key(),
        default_headers=oa.openai_default_headers(),
    )

    forensic = {
        "http_status": None, "finish_reason": None, "content_len": 0,
        "reasoning_len": 0, "n_tool_calls": 0, "retry_count": 0,
        "prompt_tokens": None, "completion_tokens": None,
        "reasoning_tokens": None, "error_class": None, "error_body": None,
        "provider_msg": None, "empty_choices": False,
    }

    last_exc = None
    for attempt in range(1, run.MODEL_RETRIES + 1):
        forensic["retry_count"] = attempt - 1
        content_buf: list[str] = []
        reasoning_len = 0
        tool_accum: dict[int, dict] = {}
        finish_reason = None
        saw_any_choice = False
        try:
            stream = await client.chat.completions.create(**kwargs)
            forensic["http_status"] = 200
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                for choice in choices:
                    saw_any_choice = True
                    fr = getattr(choice, "finish_reason", None)
                    if fr:
                        finish_reason = fr
                    delta = getattr(choice, "delta", None)
                    if delta is None:
                        continue
                    txt = getattr(delta, "content", None)
                    if txt:
                        content_buf.append(txt)
                    reasoning = getattr(delta, "reasoning", None) or getattr(
                        delta, "reasoning_content", None
                    )
                    if not reasoning:
                        extra = getattr(delta, "model_extra", None)
                        if isinstance(extra, dict):
                            reasoning = extra.get("reasoning") or extra.get("reasoning_content")
                    if reasoning and isinstance(reasoning, str):
                        reasoning_len += len(reasoning)
                    for tcd in getattr(delta, "tool_calls", None) or []:
                        idx = tcd.index
                        acc = tool_accum.setdefault(idx, {"name": "", "id": "", "args": ""})
                        if tcd.id:
                            acc["id"] += tcd.id
                        fn = getattr(tcd, "function", None)
                        if fn:
                            if getattr(fn, "name", None):
                                acc["name"] += fn.name
                            if getattr(fn, "arguments", None):
                                acc["args"] += fn.arguments
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    forensic["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
                    forensic["completion_tokens"] = getattr(usage, "completion_tokens", None)
                    details = getattr(usage, "completion_tokens_details", None)
                    if details is None:
                        ex = getattr(usage, "model_extra", None)
                        if isinstance(ex, dict):
                            details = ex.get("completion_tokens_details")
                    if isinstance(details, dict):
                        forensic["reasoning_tokens"] = details.get("reasoning_tokens")
                    elif details is not None:
                        forensic["reasoning_tokens"] = getattr(details, "reasoning_tokens", None)
        except Exception as exc:  # noqa: BLE001 -- classify like the adapter does
            last_exc = exc
            cls = adapter.classify_provider_error_class(exc)
            forensic["provider_msg"] = f"{type(exc).__name__}: {exc}"[:300]
            forensic["error_class"] = cls
            status = getattr(exc, "status_code", None)
            if status is None:
                resp = getattr(exc, "response", None)
                status = getattr(resp, "status_code", None)
            forensic["http_status"] = status
            body = getattr(exc, "body", None)
            if body is not None:
                try:
                    forensic["error_body"] = json.dumps(body)[:400]
                except (TypeError, ValueError):
                    forensic["error_body"] = str(body)[:400]
            transient = False
            try:
                transient = oa._is_transient_upstream(exc)
            except Exception:  # noqa: BLE001
                transient = cls == "upstream_provider"
            if transient and attempt < run.MODEL_RETRIES:
                await asyncio.sleep(min(2 ** attempt, 20))
                continue
            _CUR_FORENSICS.append(dict(forensic))
            if cls == "upstream_provider":
                return None, "", ("upstream_provider", forensic["provider_msg"])
            return None, "", ("internal", forensic["provider_msg"])

        # Stream completed without exception.
        text = "".join(content_buf)
        fc = None
        for _idx, acc in sorted(tool_accum.items()):
            if not acc["name"]:
                continue
            try:
                args = json.loads(acc["args"]) if acc["args"].strip() else {}
            except json.JSONDecodeError:
                args = {}
            fc = FunctionCallEvent(
                name=acc["name"], call_id=acc["id"] or None,
                args=args if isinstance(args, dict) else {},
            )
            break
        forensic["finish_reason"] = finish_reason
        forensic["content_len"] = len(text)
        forensic["reasoning_len"] = reasoning_len
        forensic["n_tool_calls"] = len(tool_accum)
        forensic["empty_choices"] = not saw_any_choice
        _CUR_FORENSICS.append(dict(forensic))
        return fc, text, None

    # Retry exhaustion on a transient error.
    _CUR_FORENSICS.append(dict(forensic))
    return None, "", ("upstream_provider", f"{type(last_exc).__name__}: {last_exc}" if last_exc else "exhausted")


run._one_model_turn = _forensic_one_model_turn  # monkeypatch the signed driver


async def _drive_forensic(arm, records, out_jsonl):
    import trid3nt_server.adapters.adapter as adapter
    from trid3nt_server.data import TOOL_REGISTRY
    from trid3nt_server.data.fetchers._router import registration as reg
    from trid3nt_server.data.search.search_tools import search_tools as st
    from trid3nt_server.data.search.tool_retrieval import MAX_K, retrieve_visible_tools
    import trid3nt_server.server as srv

    st._reset_index_for_tests()
    st._get_index()
    if arm == 3:
        from trid3nt_server.data.fetchers._router import stratified as strat
        strat.reset_source_stratum_index_for_tests()
        strat.source_stratum_index()
    declarable_floor = set(srv._default_declarable_registry())

    done: dict[str, dict] = {}
    if out_jsonl.exists():
        for line in out_jsonl.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                done[r["id"]] = r
    if done:
        print(f"[arm{arm}] resume: {len(done)} already done", flush=True)

    rows = [done[r["id"]] for r in records if r["id"] in done]
    global _CUR_FORENSICS
    with out_jsonl.open("a", encoding="utf-8") as fh:
        for i, rec in enumerate(records):
            if rec["id"] in done:
                continue
            _CUR_FORENSICS = []
            visible = retrieve_visible_tools(rec["prompt"], None, MAX_K)
            if arm == 3:
                row = await run._drive_record_arm3(rec, adapter, reg, TOOL_REGISTRY, visible, declarable_floor)
            else:
                row = await run._drive_record(arm, rec, adapter, reg, TOOL_REGISTRY, visible, declarable_floor)
            row["_forensics"] = list(_CUR_FORENSICS)
            rows.append(row)
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            f0 = row["_forensics"][0] if row["_forensics"] else {}
            print(f"[arm{arm}] {i+1}/{len(records)} {rec['id']}: sel={row['selected_name']} "
                  f"out={row['outcome']} fr={f0.get('finish_reason')} "
                  f"clen={f0.get('content_len')} rlen={f0.get('reasoning_len')} "
                  f"ntc={f0.get('n_tool_calls')}", flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arm", required=True, type=int, choices=[0, 3])
    ap.add_argument("--tag", required=True, help="model tag for output filenames")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--stratified", type=int, default=0,
                    help="PARTIAL sample: first N phrasings per SOURCE group (spans "
                         "all 14 sources) + first N*2 controls. Beats a --limit prefix "
                         "which is dominated by the earliest source groups.")
    args = ap.parse_args()
    arm = args.arm

    if arm == 0:
        os.environ.pop(run.ARM_ENV, None)
    else:
        os.environ[run.ARM_ENV] = str(arm)

    run._load_stack()
    from trid3nt_server.data.fetchers._router import registration as reg
    from trid3nt_server.data import TOOL_REGISTRY
    import trid3nt_server.server as srv

    assert reg.catalog_arm() == (None if arm == 0 else str(arm)), "arm env not seen"

    sources, controls = run._build_records()
    records = sources + controls
    if args.stratified:
        from collections import defaultdict
        by_grp: dict[str, list] = defaultdict(list)
        for r in sources:
            by_grp[r["group"]].append(r)
        sampled = []
        for grp in sorted(by_grp):
            sampled.extend(by_grp[grp][: args.stratified])
        records = sampled + controls[: args.stratified * 2]
    elif args.limit:
        records = sources[: args.limit] + controls[: max(2, args.limit // 5)]

    OUTDIR.mkdir(parents=True, exist_ok=True)

    reach = run._reachability(arm, records)
    (OUTDIR / f"{args.tag}_arm{arm}_reachability.json").write_text(json.dumps(reach, indent=2) + "\n")
    print(f"[arm{arm}] reachability recall={reach['recall']} ({reach['reached']}/{reach['n_sources']})", flush=True)

    meta = {
        "arm": arm, "tag": args.tag,
        "timestamp_utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "registry_size": len(TOOL_REGISTRY),
        "ambient_declarable_count": len(srv._default_declarable_registry()),
        "n_source_records": len(sources), "n_control_records": len(controls),
        "model_provider": os.environ.get("MODEL_PROVIDER"),
        "openai_model": os.environ.get("TRID3NT_OPENAI_MODEL"),
        "openai_base_url": os.environ.get("TRID3NT_OPENAI_BASE_URL"),
        "temperature": os.environ.get("TRID3NT_OPENAI_TEMPERATURE", "provider-default"),
        "max_tokens": os.environ.get("TRID3NT_OPENAI_MAX_TOKENS", "4096(default)"),
        "n_trials": 1, "seed_bbox": run._SEED_BBOX,
        "reachability_recall": reach["recall"],
    }

    out_jsonl = OUTDIR / f"{args.tag}_arm{arm}_records.jsonl"
    t0 = time.perf_counter()
    rows = asyncio.run(_drive_forensic(arm, records, out_jsonl))
    meta["drive_seconds"] = round(time.perf_counter() - t0, 1)
    (OUTDIR / f"{args.tag}_arm{arm}_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"[arm{arm}] done in {meta['drive_seconds']}s -> {out_jsonl.name} ({len(rows)} records)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
