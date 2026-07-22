#!/usr/bin/env python3
"""gen_tool_support_page.py -- generate docs/site/tool-support.md from the
tool-sweep results JSONL (docs/reports/tool-sweep-results.jsonl).

The sweep JSONL is append-only across passes; this script takes the LATEST
entry per tool as authoritative for status/time. Notes: the JSONL only
carries notes for SKIP-ARGS rows, so FAIL/KEY/TIMEOUT notes are merged in
from the human-curated sweep checklist
(docs/reports/tool-sweep-checklist.md) -- used only when the checklist row's
status matches the JSONL's latest status (a note from a different status
would be misleading).

Usage:
    python3 scripts/gen_tool_support_page.py            # writes docs/site/tool-support.md
    python3 scripts/gen_tool_support_page.py --check    # exit 1 if the page is stale

Stdlib only; deterministic output (sorted by tool name).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
JSONL_PATH = REPO_ROOT / "docs" / "reports" / "tool-sweep-results.jsonl"
CHECKLIST_PATH = REPO_ROOT / "docs" / "reports" / "tool-sweep-checklist.md"
OUT_PATH = REPO_ROOT / "docs" / "site" / "tool-support.md"

STATUS_ORDER = ["PASS", "KEY", "FAIL", "TIMEOUT", "SKIP-ARGS"]

STATUS_LEGEND = {
    "PASS": "executed successfully against the local stack (Tampa AOI, curated args)",
    "KEY": "needs an API key / registration -- earmarked, see the table below",
    "FAIL": "raised an error in the sweep (see note; several are harness-args or "
    "upstream-data artifacts, not code bugs)",
    "TIMEOUT": "exceeded the sweep's wall-clock cap (thread abandoned)",
    "SKIP-ARGS": "not directly executable by the harness -- required params "
    "(layer URIs, species ids, confirmation handles...) cannot be fabricated; "
    "these tools are exercised through chained LLM turns instead",
}

# KEY earmarks: which credential each KEY-status tool needs (env var names
# verified against the server tool sources).
KEY_EARMARKS = {
    "fetch_airnow_air_quality": (
        "`GRACE2_AIRNOW_API_KEY`",
        "free key from docs.airnowapi.org; also accepted per-call as `api_key`/`secret_ref`",
    ),
    "fetch_cama_flood_discharge": (
        "`GRACE2_CAMA_FLOOD_BASE_URL`",
        "upstream became registration-gated (U-Tokyo Google Form issues a password); "
        "point this at the credentialed mirror URL",
    ),
    "fetch_ebird_observations": (
        "`GRACE2_EBIRD_API_KEY`",
        "free key from ebird.org/api/keygen",
    ),
    "fetch_era5_reanalysis": (
        "`GRACE2_COPERNICUS_CDS_API_KEY`",
        "Copernicus CDS credentials (a `~/.cdsapirc` file also works via cdsapi)",
    ),
    "fetch_iucn_red_list_range": (
        "`GRACE2_IUCN_RED_LIST_API_KEY`",
        "IUCN Red List API token (apiv3.iucnredlist.org)",
    ),
    "fetch_openaq_measurements": (
        "`GRACE2_OPENAQ_API_KEY`",
        "free key from explore.openaq.org",
    ),
}

# Key-gated tools whose latest sweep status is FAIL/TIMEOUT rather than KEY
# (the error surfaces downstream of the missing credential).
KEY_ADJACENT = {
    "fetch_firms_active_fire": (
        "`GRACE2_FIRMS_MAP_KEY`",
        "sweep FAIL: the literal 'demo' MAP_KEY fallback was rejected by FIRMS; "
        "get a free MAP_KEY from firms.modaps.eosdis.nasa.gov",
    ),
    "fetch_gtsm_tide_surge": (
        "`GRACE2_COPERNICUS_CDS_API_KEY`",
        "sweep FAIL: CDS retrieve failed on missing Copernicus credentials "
        "(same CDS setup as fetch_era5_reanalysis)",
    ),
}


def _ascii(text: str) -> str:
    """ASCII-fy note text (repo norm: no typographic dashes/arrows) and make it
    markdown-table safe."""
    replacements = {
        "→": "->",
        "–": "-",
        "—": "--",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "§": "S",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    text = text.encode("ascii", "replace").decode("ascii")
    return text.replace("|", "\\|").replace("\n", " ").strip()


def load_checklist_notes() -> dict[str, tuple[str, str]]:
    """Parse the sweep-checklist markdown table -> {tool: (status, note)}."""
    out: dict[str, tuple[str, str]] = {}
    if not CHECKLIST_PATH.exists():
        return out
    for line in CHECKLIST_PATH.read_text().splitlines():
        if not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("tool", "---"):
            continue
        name, status, _time, note = cells[0], cells[1], cells[2], cells[3]
        if name.startswith("-") or status.startswith("-"):
            continue  # separator row
        out[name] = (status, note)
    return out


def load_latest() -> dict[str, dict]:
    latest: dict[str, dict] = {}
    notes: dict[tuple[str, str], str] = {}  # (name, status) -> last non-empty note
    with JSONL_PATH.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            name = row["name"]
            latest[name] = row
            note = (row.get("note") or "").strip()
            if note:
                notes[(name, row["status"])] = note
    checklist = load_checklist_notes()
    for name, row in latest.items():
        if not (row.get("note") or "").strip():
            note = notes.get((name, row["status"]), "")
            if not note and name in checklist:
                ck_status, ck_note = checklist[name]
                if ck_status == row["status"]:
                    note = ck_note
            row["note"] = note
    return latest


def render(latest: dict[str, dict]) -> str:
    counts: dict[str, int] = {}
    for row in latest.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    total = len(latest)
    ts = max(row.get("ts", "") for row in latest.values())

    lines: list[str] = []
    lines.append("# TRID3NT Local -- Tool Support Matrix")
    lines.append("")
    lines.append(
        "!!! note \"Generated page\""
    )
    lines.append(
        "    Generated by `scripts/gen_tool_support_page.py` in the `trid3nt-local` repo from"
    )
    lines.append(
        "    `docs/reports/tool-sweep-results.jsonl` (the 3-pass direct-execution tool sweep,"
    )
    lines.append(
        f"    latest entry {ts}). Do not edit by hand -- re-run the generator."
    )
    lines.append("")
    lines.append(
        f"Every registered tool was executed directly against the local stack "
        f"(MinIO + local solvers, ~3 km downtown-Tampa AOI, curated arguments; "
        f"layer-consuming tools chained onto real fetched layers). "
        f"**{total} tools**: "
        + " | ".join(
            f"{status} {counts.get(status, 0)}" for status in STATUS_ORDER
        )
        + "."
    )
    lines.append("")
    lines.append("## Status legend")
    lines.append("")
    lines.append("| Status | Meaning |")
    lines.append("|--------|---------|")
    for status in STATUS_ORDER:
        lines.append(f"| `{status}` | {STATUS_LEGEND[status]} |")
    lines.append("")

    lines.append("## KEY-earmarked tools")
    lines.append("")
    lines.append(
        "`KEY` tools work locally once the credential is provided -- each fetcher resolves "
        "its key from a per-call `api_key`/`secret_ref` argument or the env var below "
        "(set it in `.env.local`). They fail with a typed missing-key error, never a "
        "silent empty result."
    )
    lines.append("")
    lines.append("| Tool | Env var | Where to get it |")
    lines.append("|------|---------|-----------------|")
    for name in sorted(KEY_EARMARKS):
        env, how = KEY_EARMARKS[name]
        lines.append(f"| `{name}` | {env} | {how} |")
    lines.append("")
    lines.append(
        "Two more tools are key-gated but surface it as a sweep `FAIL` further downstream:"
    )
    lines.append("")
    lines.append("| Tool | Env var | Detail |")
    lines.append("|------|---------|--------|")
    for name in sorted(KEY_ADJACENT):
        env, how = KEY_ADJACENT[name]
        lines.append(f"| `{name}` | {env} | {how} |")
    lines.append("")

    lines.append("## Full matrix")
    lines.append("")
    lines.append("| Tool | Status | Time (s) | Note |")
    lines.append("|------|--------|----------|------|")
    for name in sorted(latest):
        row = latest[name]
        note = _ascii((row.get("note") or "").strip())
        if len(note) > 220:
            note = note[:217] + "..."
        secs = row.get("seconds", 0)
        secs_str = f"{secs:.0f}"
        lines.append(f"| `{name}` | {row['status']} | {secs_str} | {note} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/site/tool-support.md is stale instead of writing",
    )
    args = parser.parse_args()

    latest = load_latest()
    content = render(latest)

    if args.check:
        current = OUT_PATH.read_text() if OUT_PATH.exists() else ""
        if current != content:
            print(f"[gen_tool_support_page] STALE: {OUT_PATH} (re-run the generator)")
            return 1
        print(f"[gen_tool_support_page] up to date: {OUT_PATH}")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(content)
    print(
        f"[gen_tool_support_page] wrote {OUT_PATH} "
        f"({len(latest)} tools from {JSONL_PATH.name})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
