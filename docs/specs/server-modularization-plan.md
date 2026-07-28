# Server modularization plan (FOR NATE REVIEW)

Goal (NATE 2026-07-28): move off the monolith so editing parallelizes -
"easier to do with a team or subagents." The recurring collision problem in
our own waves (every slice serializing on server.py / categories.py edits)
is the proof of need: ownership seams must become DIRECTORIES, and central
switches must become REGISTRATION TABLES so adding features means adding
files, not editing shared ones.

## Principles

1. ONE DIRECTORY = ONE OWNER. A subagent lane owns a directory outright;
   parallel lanes never touch the same file. (The engine-door pattern proved
   this: adding a template = adding a folder, zero shared edits.)
2. TABLES OVER SWITCHES. WS envelope handlers, gates, cards, HTTP routes,
   tool registrations - all discovered via registration tables, never
   central if/elif chains. New envelope type = new module + one table entry
   in ITS OWN file.
3. NARROW SEAMS. Each package exports a small __init__ surface; cross-
   package imports go through it. Platform never reaches into agent
   internals (and vice versa) except via declared seams.
4. ENFORCED BY TESTS, not discipline: an architecture conformance suite
   (same pattern as the Qt6 guard) failing on (a) any module > 1500 lines,
   (b) import cycles, (c) cross-package imports bypassing __init__ seams,
   (d) new if/elif dispatch on envelope types outside the tables.
5. Comments rewritten on contact: any code a wave moves gets its comments
   READ and rewritten/deleted by judgment (present-tense, constraint-only,
   accurate) - never regex passes.

## Current state (post engine-door refactor)

- agent/ (AI surface - DONE this wave): adapters/, gates/ (+cards/),
  tools/, workflows/, data/, categories, arg normalizer.
- Platform root: server.py (~13.2k - THE monolith remainder), main.py,
  persistence.py, telemetry.py, tool_catalog_http.py, case_lifecycle.py,
  scenario_reuse.py + credentials/, sandbox/, emission/.

## Target tree (platform side)

    trid3nt_server/
      agent/          (as-is; owns everything LLM-facing)
      transport/      WS server bootstrap, connection + per-session socket
                      registry (the reap invariants), protocol framing,
                      the ENVELOPE HANDLER TABLE (type -> handler module)
      session/        SessionState, live-turn registry, allowed-tool set,
                      turn-scoped state (the dual-socket agreement rules)
      turns/          the turn loop (_stream_model_reply), tool dispatch +
                      persist, gate orchestration (calls agent/gates),
                      circuit breaker, turn memory
      cases/          case lifecycle (list/open/create/auto-create), chat
                      persistence glue, view snapshots + manifests
                      (absorbs case_lifecycle.py + scenario_reuse.py)
      handlers/       WS envelope handler modules BY FAMILY, one file each
                      (secrets, credentials, confirm replies, spatial input,
                      picker replies, mode2 reply, update/version) - each
                      self-registers into the transport table
      http/           tool_catalog_http split: catalog serving, export/
                      ingest routes, health/version route, future
                      plugins.xml serving
      persistence.py  (already cut down this batch), telemetry.py
      credentials/ sandbox/ emission/  (as-is)
      server.py       BOOTSTRAP ONLY: compose the above, < 500 lines.

## Migration waves (each identity-gated: suite baseline, registry identity,
AST no-logic-drift, canary, live WS turn)

- WAVE A (the in-flight fix batch): lessons/vertex/persistence cuts - lands
  first, no overlap.
- WAVE B: transport/ + session/ + handlers/ extraction (the envelope table
  is the load-bearing new mechanism; handlers move family-by-family).
- WAVE C: turns/ + cases/ extraction (turn loop is the riskiest move -
  its own wave, WS-turn smoke mandatory).
- WAVE D: http/ split + server.py reduced to bootstrap; conformance suite
  lands and turns RED-on-regression permanently.
- Comment judgment pass rides every wave on the code it touches (principle 5).

## Ownership matrix after (who can edit in parallel)

    agent/tools + workflows  | engine/template lanes (already proven)
    agent/gates + cards      | gating/UX lanes
    transport/ + handlers/   | protocol lanes (one handler family each)
    turns/                   | turn-loop lane (single-owner, highest care)
    cases/ + persistence     | durability lanes
    http/                    | plugin-integration lanes (update v2 etc.)
    qgis-plugin/             | plugin lanes (already separate)

## Non-goals

No behavior changes, no protocol changes, no premature microservices (the
monolith-until-multiuser decision stands - this is ONE process, modular
inside). The genai-types IR decoupling stays its own queued item.
