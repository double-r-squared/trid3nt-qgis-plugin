#!/usr/bin/env python3
"""fetcher_fold_routing -- thin per-arm retrieval runner (experiments/fetcher_fold_routing).

Executes ONE arm (baseline or fold) of the pre-registered routing-parity
experiment (see DESIGN.md). Model-free, in-process, ZERO external calls:
imports the SAME production retrieval seam the retrieval_probe bench uses
(search_tools._get_index to warm + tool_retrieval.retrieve_ranked_tools to
rank) and, per input phrasing, records the full ranked list (name, rrf_score).

Arms (contract sec 3.3 toggle; canonical sequence mirrored from
test_router_engine.py::test_fold_arm_on_surfaces_virtual_under_twin_name):

  baseline : TRID3NT_FETCHER_FOLD_ARM UNSET, NO specs registered. The tree
             exactly as today; the twin fetch_X indexed with its own
             docstring + co-located corpus.yaml.
  fold     : TRID3NT_FETCHER_FOLD_ARM=1 + register_specs_from_tree(). Each of
             the 5 pilot twins is surfaced UNDER its own name by the
             spec-driven virtual entry (fetch_X__spec relabeled to fetch_X,
             tier=general); the twin's callable is swapped for router.route.

Determinism: retrieval is deterministic per index build, so ONE run per arm
(DESIGN "one run per arm"). Each arm runs in its OWN process (this script) so
no module-global (registry / _INDEX / env) leaks across arms -- the strongest
guarantee for the control byte-identity check.

Output: results/<arm>_rankings.jsonl (one JSON row per input record, full
ranked list) + results/<arm>_meta.json (env + index provenance).

ASCII only. Run inside venvs/agent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "inputs" / "phrasings.json"
RESULTS = HERE / "results"
FOLD_ENV = "TRID3NT_FETCHER_FOLD_ARM"

#: Ranked depth recorded per phrasing. >= the deepest grading window (hit@8)
#: with headroom so a miss shows WHAT outranked the target, and == production
#: MAX_K so we record exactly the pool the live gate would see.
RECORD_DEPTH = 25

PILOT_TWINS = [
    "fetch_gridmet",
    "fetch_hifld_critical_infrastructure",
    "fetch_noaa_coops_tides",
    "fetch_esri_landcover_10m",
    "fetch_census_acs",
]


def build_records(arm: str) -> list[dict]:
    """Flatten phrasings.json into ordered records with stable ids.

    id scheme is arm-independent (keyed off the input file only) so the two
    arms' JSONL rows line up 1:1 for the identity check + per-record diff.
    """
    doc = json.loads(INPUTS.read_text())
    if not doc.get("status", "").startswith("SIGNED_NATE"):
        raise SystemExit(
            f"[ABORT] inputs not NATE-signed (status={doc.get('status')!r}); "
            "refusing to run per standing methodology."
        )
    acc_key = "acceptable_fold" if arm == "fold" else "acceptable_baseline"
    records: list[dict] = []
    for pilot in doc["pilots"]:
        src = pilot["source"]
        acceptable = list(pilot[acc_key])
        for i, ph in enumerate(pilot["phrasings"]):
            records.append({
                "id": f"{src}#{i}",
                "group": src,
                "kind": ph["kind"],
                "prompt": ph["text"],
                "acceptable": acceptable,
            })
    for i, ph in enumerate(doc["control"]["phrasings"]):
        records.append({
            "id": f"control#{i}",
            "group": "control",
            "kind": "control",
            "prompt": ph["text"],
            "acceptable": list(ph["acceptable"]),
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", required=True, choices=["baseline", "fold"])
    args = parser.parse_args()
    arm = args.arm

    # --- Arm environment: set BEFORE importing the retrieval seam so the
    #     env-gated pool substitution reads the intended state. Explicit unset
    #     for baseline defends against a dirty parent shell. ---
    if arm == "fold":
        os.environ[FOLD_ENV] = "1"
    else:
        os.environ.pop(FOLD_ENV, None)

    # Full startup import path the daemon (and retrieval_probe) uses.
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    from trid3nt_server.agent.tools.fetchers._router import registration
    from trid3nt_server.agent.tools.search.search_tools import search_tools as st
    from trid3nt_server.agent.tools.search.tool_retrieval import retrieve_ranked_tools

    catalog_size = len(TOOL_REGISTRY)

    # --- Fold arm: register the spec-driven virtual tools from the tree. ---
    registered_aliases: list[str] = []
    if arm == "fold":
        assert registration.fold_arm_enabled() is True, "fold env not seen"
        registered_aliases = registration.register_specs_from_tree()
    else:
        assert registration.fold_arm_enabled() is False, "baseline saw fold env"
        assert not registration.registered_spec_names(), "baseline has specs registered"

    spec_names = sorted(registration.registered_spec_names())

    # --- Warm the index EXPLICITLY (matches retrieval_probe: retrieve_ranked_
    #     tools returns [] on a cold index; _get_index sets the module global
    #     _INDEX that retrieve_ranked_tools reads). Reset first so this process
    #     builds fresh against the intended arm state. ---
    st._reset_index_for_tests()
    t0 = time.perf_counter()
    index = st._get_index()
    index_build_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    index_names = list(index.tool_names)

    # Arm invariants (recorded, and hard-asserted for the fold arm).
    twin_in_index = {t: (t in index_names) for t in PILOT_TWINS}
    spec_alias_in_index = {
        registration.virtual_alias(t): (registration.virtual_alias(t) in index_names)
        for t in PILOT_TWINS
    }
    if arm == "fold":
        assert sorted(spec_names) == sorted(PILOT_TWINS), (
            f"fold specs {spec_names} != pilot twins {PILOT_TWINS}"
        )
        assert all(twin_in_index.values()), "a pilot twin missing from fold index"
        assert not any(spec_alias_in_index.values()), "a __spec alias leaked into the pool"

    records = build_records(arm)

    RESULTS.mkdir(parents=True, exist_ok=True)
    out_jsonl = RESULTS / f"{arm}_rankings.jsonl"
    rows: list[dict] = []
    with out_jsonl.open("w", encoding="utf-8") as fh:
        for rec in records:
            t1 = time.perf_counter()
            ranked = retrieve_ranked_tools(rec["prompt"], RECORD_DEPTH)
            turnaround_ms = round((time.perf_counter() - t1) * 1000.0, 3)
            row = {
                "arm": arm,
                "record_id": rec["id"],
                "group": rec["group"],
                "kind": rec["kind"],
                "prompt": rec["prompt"],
                "acceptable": rec["acceptable"],
                "depth": RECORD_DEPTH,
                "turnaround_ms": turnaround_ms,
                "ranked": [{"name": n, "score": s} for n, s in ranked],
            }
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            rows.append(row)

    meta = {
        "arm": arm,
        "fold_env_value": os.environ.get(FOLD_ENV),
        "fold_arm_enabled": registration.fold_arm_enabled(),
        "timestamp_utc": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "record_count": len(records),
        "record_depth": RECORD_DEPTH,
        "catalog_size": catalog_size,
        "registered_spec_names": spec_names,
        "registered_aliases": sorted(registered_aliases),
        "index_tool_count": len(index_names),
        "index_build_ms": index_build_ms,
        "dense_backend": index.backend_name,
        "bm25": index.bm25 is not None,
        "twin_in_index": twin_in_index,
        "spec_alias_in_index": spec_alias_in_index,
        "external_calls": "none (model-free; in-process retrieval seam only)",
    }
    (RESULTS / f"{arm}_meta.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")

    print(f"[{arm}] records={len(records)} catalog={catalog_size} "
          f"index_tools={len(index_names)} backend={index.backend_name} "
          f"bm25={index.bm25 is not None} build={index_build_ms}ms "
          f"specs={spec_names} -> {out_jsonl.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
