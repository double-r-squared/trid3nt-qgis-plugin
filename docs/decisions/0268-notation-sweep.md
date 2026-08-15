# ADR 0268 - server-refactor wave 8: notation sweep (docstrings/comments)

Status: LANDED (partial scope; continuation queued). Date: 2026-08-15.

## Context

NATE's hard rule: docstrings/comments should be focused on the constraint
itself, not carry ADR/FR/NFR/Appendix/job-id/Decision-letter citations or
wave/date archaeology. Those notations belong in commit messages, not in
source. A repo-wide measurement found ~1,404 lines across ~296 Python files
under `server/src/trid3nt_server/` matching the notation regex:

```
ADR[- ]?0[0-9]|FR-[A-Z]+-[0-9]|NFR-|Appendix [A-Z]|job[- ]?0[0-9]{3}|Decision [A-Z]\b
```

(Actual measured baseline at sweep start: 1421 matching lines across 296 `.py`
files under `server/src/trid3nt_server/`, plus 125 lines in `.yaml`/`.md`
sidecars that are out of scope per the kickoff -- Python source
docstrings/comments only.)

## What landed this wave

Swept every subsystem under `server/src/trid3nt_server/` EXCEPT the two
largest trees, `agent/tools/` (141 files, ~700 matching lines) and
`agent/workflows/` (106 files, ~400 matching lines), which are scoped to a
continuation wave (see below).

Before/after match counts, by subsystem (Python files only):

| Subsystem | Before | After |
|---|---:|---:|
| `server/` (incl. `_core.py`) | 121 | 0 |
| `agent/mesh/` | 27 | 0 |
| `agent/gates/` (incl. `cards/`) | 15 | 0 |
| `emission/` | 24 | 1 (see exception below) |
| `credentials/` | 5 | 0 |
| `cases/` | 5 | 0 |
| `sandbox/` | 3 | 0 |
| `agent/adapters/adapter.py` | 6 | 0 |
| `agent/categories.py` | 17 | 0 |
| `agent/tool_arg_normalizer.py` | 6 | 0 |
| `main.py` | ~35 | 0 |
| `persistence.py` | 11 | 0 |
| `case_lifecycle.py` | 1 | 0 |
| `telemetry.py` | 1 | 0 |
| `tool_catalog_http.py` | 3 | 0 |
| `agent/tools/cache.py` (touched; rest of `agent/tools/` deferred) | 19 | 0 |
| **Deferred: `agent/tools/` (rest)** | ~141 files / ~700 lines | unchanged |
| **Deferred: `agent/workflows/`** | 106 files / ~400 lines | unchanged |

One line is a deliberate exception: `emission/layer_uri_emit.py:92` embeds
`(job-0254 guardrail; see Decision 11.)` inside a `logger.warning(...)`
message string -- a runtime log payload, excluded per the kickoff's "string
literals that are runtime behavior" carve-out. Left as-is.

**48 files touched, ~120 individual reword/delete edits.** Split:

- **Reword** (majority, ~85 edits): citation stripped, constraint kept --
  e.g. `"ADR 0014: persist the session registry's short-handle map"` ->
  `"Persist the session registry's short-handle map"`; `"FR-DC-6
  short-circuit"` -> `"Uncacheable-tools short-circuit"`; `"Appendix A.7
  replace-not-reconcile"` -> `"replace-not-reconcile"` (the mechanism, stated
  plainly, without the citation).
- **Delete** (~35 edits): pure provenance/history with no surviving
  constraint -- `"server-refactor wave 4, ADR 0264"` module-docstring
  parentheticals, `"Door dissolution (ADR 0094)"` clauses once the sentence
  already states what's true now, `"V&V wave (ADR 0021, lane C)"` labels,
  `job-0277`/`job-0203` inline tags on log/bind calls.
- One larger reword: `agent/mesh/hecras_geometry.py`'s ~30-line module
  docstring narrating the HEC-RAS 2D-geometry-write unblock kept every
  engineering fact (dWSE deltas, correlation coefficients, the
  Windows-DLL-vs-Linux-headless finding) but dropped every `ADR 01xx` label
  and the `2026-08-03 M3 finding` date-archaeology framing.

No pinned tests reference the exact citation strings changed (checked via
`grep -rn "ADR 0014"` etc. against `server/tests/` for direct string
assertions and `inspect.getsource` source-pinning patterns -- none found).
Zero code changes; docstrings/comments only.

## Deferred: `agent/tools/` and `agent/workflows/`

These two trees hold the bulk of the remaining ~1,100 matching lines and are
the LLM-facing tool-docstring surface (routing/constraint content the
Bedrock 1000-char truncation makes precious). They deserve their own
focused wave with per-tool docstring review rather than a rushed pass
appended to this one. Queued as the wave-9 kickoff. Exact remaining counts
(measured at this wave's close):

- `agent/tools/`: 141 files, ~700 matching lines (of which `cache.py`, the
  one file touched this wave, is now clean).
- `agent/workflows/`: 106 files, ~400 matching lines.

## Gates run

- `pytest server/tests -k fetch_resolution`: 4 failed / 19 passed (matches
  the documented offline baseline exactly).
- `pytest server/tests -k river_dye`: 2 failed / 52 passed (the `[p-r]`
  slice named in the kickoff; matches baseline).
- `pytest contracts/tests`: 8 failed / 702 passed -- pre-existing, unrelated
  to this sweep (contracts/ package untouched; confirmed via `git status`).
- Tool-registry import (`main._import_tools_registry()`): 256 tools
  registered, zero import errors.
- `retrieve_visible_tools(prompt, None, 8)` spot-check on 5 diverse prompts:
  no crash, 255/256 tools visible each time (expected fail-open behavior
  with `allowed_set=None`; unaffected by this wave since no tool docstrings
  were touched).
- Daemon restart (`make stop && make up`): clean boot, no import/registration
  errors in `logs/agent.log`.
- `scripts/ws_smoke.py`: `all_passed=True`.

Flood canary not run (comments-only change, explicitly optional per kickoff;
skipped this pass in favor of closing out the gate list above).
