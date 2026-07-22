# experiments/ -- how we test

The empirical testing framework for TRID3NT routing + retrieval quality
(background: docs/decisions/0017 + 0018). Method, in one line:

    hypothesis -> approved JSON inputs -> permission-gated execution
    -> deterministic grading -> repetition/variance -> co-located verdict

- **Hypothesis first.** Every experiment states what it expects to see
  before anything runs.
- **Approved inputs.** Input sets are JSON files reviewed by NATE before ANY
  run. New/edited input files carry a top-level `"_comment"` key marked
  `DRAFT` until reviewed. Both current input sets are DRAFT.
- **Permission-gated execution.** Engines that spend anything (LLM turns,
  external API calls) NEVER auto-run: they require an explicit
  `--i-have-permission` flag and, without it, only print their resource
  profile (record count, tiers, expected API classes). Engines with zero
  external calls (retrieval_probe) need no flag -- stated per engine below.
- **Deterministic grading.** NO LLM anywhere in grading. The fired-tool
  sequence comes from the pipeline envelopes (tool names in `pipeline-state`
  / `tool-io`), recorded raw into the experiment's `data/`.
- **Repetition/variance.** `--runs N`; results report per-run and aggregate
  (mean/min/max) figures.
- **Co-located verdict.** Conclusions live next to the data that produced
  them, never only in chat.

## Folder conventions

Reusable engines live under `bench/<engine>/` (`run.py` + `inputs/` +
`data/<UTC timestamp>/` per invocation). An EXPERIMENT is a folder holding:

    hypothesis.txt   what we expect and why (written BEFORE the run)
    config           the exact inputs/flags used (input JSON copy or a note)
    data/            raw + graded outputs (engine data/<timestamp>/ dirs)
    graphs/          any plots derived from data/
    verdict.txt      the conclusion, written AFTER the run, referencing data/

For quick benches, an engine's `data/<timestamp>/` dir can be promoted into
an experiment folder by adding `hypothesis.txt` + `verdict.txt` beside it.

## Input record schema (both engines)

File level:

    {
      "_comment": "DRAFT - ...",            <- DRAFT marker until NATE approves
      "defaults": {"always_allowed": ["geocode_location", "publish_layer",
                                      "compute_layer_bounds"]},
      "records": [ ... ]
    }

Record:

    {
      "id": str, "category": str, "register": "specific" | "vague",
      "prompt": str, "execution": "run" | "block_at_invocation",
      "expected": {
        "acceptable":     [tool names],      # required unless no_tool
        "always_allowed": [tool names],      # optional; OVERRIDES the file default
        "no_tool":        bool,              # true = zero tools expected
        "forbidden":      [tool names]       # optional
      }
    }

At LOAD every tool name in every set is validated against the live catalog
(`trid3nt_server.tools.TOOL_REGISTRY` populated via the same
`main._import_tools_registry()` path the daemon uses at startup, plus the
`categories.py` meta-tools). A malformed record or unknown name is a typed
`InputLoadError` -- nothing runs.

## Grading rules (deterministic, final)

PASS iff:

1. at least one fired tool is in `acceptable`, AND
2. every fired tool is in `acceptable` UNION `always_allowed`, AND
3. no fired tool is in `forbidden`.

`no_tool` records PASS iff zero tools fired.

Engine bookkeeping exclusions (recorded raw, never graded as "fired"): LLM
generation steps, and the three always-available meta/discovery tools
(`list_categories`, `list_tools_in_category`, `discover_dataset`) -- they
are the routing mechanism, not a routing outcome.

## Verdict taxonomy (error taxonomy)

| Verdict                | Meaning |
|------------------------|---------|
| CORRECT                | PASS on a run-tier (or no_tool) record |
| CORRECT_BLOCKED        | block_at_invocation tier: correct pick + schema-valid args; execution deliberately skipped |
| SELECTED_WRONG_BLOCKED | sets violated; the engine cancelled the turn rather than execute the mis-route |
| NO_CALL                | tool expected, none fired |
| FALSE_POSITIVE         | no_tool record, but a tool fired |
| UPSTREAM_FAILURE       | LLM-provider exhaustion; EXCLUDED from the accuracy denominator, own column |
| TOOL_UPSTREAM_ERROR    | correct pick, vendor died; own column, NOT a routing failure |

Routing accuracy = (CORRECT + CORRECT_BLOCKED + TOOL_UPSTREAM_ERROR) /
(records - UPSTREAM_FAILURE). Upstream detection is by deterministic string
patterns on typed error envelopes (see the pattern constants in each
`run.py`) -- never model judgment.

## Execution tiers

- **run** -- the turn executes to completion (fetch/compute-class records);
  the whole fired chain is graded. A set VIOLATION mid-turn is cancelled
  immediately (no value in executing a mis-route) -> SELECTED_WRONG_BLOCKED.
- **block_at_invocation** -- solver/simulation-class records: routing is
  graded, execution is deliberately skipped. v1 implements the block
  CLIENT-SIDE: `always_allowed` tools (geocode/publish/bounds) may run; at
  the FIRST material tool-call envelope the engine grades and immediately
  cancels the turn. The blocked tool may briefly START before the cancel
  lands. The server-side pre-dispatch block hook (landing with the current
  server batch) replaces this, making the block airtight before any fetch.

## Engines

### bench/routing_sweep -- API-driven routing benchmark

WS client against the live daemon (patterns from `scripts/ws_smoke.py` +
`scripts/tool_routing_bench.py`). Per record: fresh case, send prompt, watch
envelopes, apply the grade. Permission-gated: requires
`--i-have-permission`; without it prints the resource profile only.

    venvs/agent/bin/python experiments/bench/routing_sweep/run.py            # profile only
    venvs/agent/bin/python experiments/bench/routing_sweep/run.py \
        --runs 3 --i-have-permission                                          # real run

Env: the DAEMON must run with `TRID3NT_AMBIGUITY_MARGIN=0` (kills ADR 0018
ambiguity asks so the sweep never stalls on a tool-candidates card; the
engine sets it in its own process and prints a reminder -- it cannot set the
daemon's env). Outputs: `data/<timestamp>/raw_envelopes.jsonl` +
`results.json` + `summary.txt`.

Inputs: `inputs/domain_sweep_specific.json` + `inputs/domain_sweep_vague.json`
(DRAFT) -- one chain per `categories.py` category (12) x both registers +
1 no_tool record per register; solver/simulation-category records are
`block_at_invocation`.

### bench/retrieval_probe -- model-free retrieval probe

No daemon, no model, ZERO external calls -- therefore NO permission flag
needed. Imports the retrieval seam directly (`discover_dataset` index +
`tool_retrieval.retrieve_ranked_tools`); per record: query -> top-k names +
scores + turnaround ms. Grading: same set schema, membership-in-top-k
semantics (`--k`, default 5): CORRECT iff an acceptable name is in the top-k
and no forbidden name is; else MISS. Repetition default `--runs 3` (cheap).

    venvs/agent/bin/python experiments/bench/retrieval_probe/run.py

Outputs: `data/<timestamp>/raw_rankings.jsonl` + `results.json` +
`summary.txt`. Inputs: `inputs/phrasings_specific.json` +
`inputs/phrasings_vague.json` (DRAFT) -- ~20 phrasings per register over the
same 12 categories + the `spatial_query` analytical phrasings + 2 typo'd
variants per register.
