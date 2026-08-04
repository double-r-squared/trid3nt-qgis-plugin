# ADR 0114 -- !run CHAT INVOCATION: direct tool dispatch from the composer

Status: accepted (2026-08-04)
Feature: NATE, verbatim -- "we can evoke tool usage by just typing in the tool
signature with args like the LLM or workflow would but within the chat... a pre
symbol like '!run' that is parsed very first thing and if it's found we route
straight to the server and if not we continue normally."
Follows: ADR 0018 (tool-choice picker), the pre-existing `/invoke` operator-debug
directive (server-side, text-parsed) this supersedes as the user-facing path.

## Context

The dock could only reach a tool through the LLM (a chat turn) or the hidden,
text-parsed `/invoke <tool> <json>` server directive. There was no first-class
way to invoke a registered tool with exact args from the composer -- the
fastest way to smoke a fetcher/composer during live testing. NATE wants a
`!run` prefix, parsed CLIENT-side first, that routes a structured call straight
to the server OUTSIDE the LLM loop, rendering on the SAME tool-card path a model
call uses.

## Decision

1. CLIENT PARSE-FIRST (`qgis-plugin/trid3nt/net/run_invocation.py`, pure/no-Qt).
   The dock's send path checks the composer text BEFORE the chat path. A
   `!run`-anchored message parses to `(name, args)`; anything else (including a
   message that merely MENTIONS `!run` mid-sentence) returns `None` and flows to
   chat byte-identically. Two arg styles: pythonic kwargs `tool(k=v, ...)`
   parsed via `ast` in eval-mode + `ast.literal_eval` per keyword value (a SAFE
   literal parser -- never `eval`; positional + non-literal + `**` rejected,
   because the registry closures are keyword-only post-fold), and a JSON-object
   form `tool {"k": v}`. `!run` / `!run help` render a local usage line; a parse
   failure renders a local honest error bubble -- nothing is sent either way.

2. WIRE: a new WS message type `dev-tool-invoke {name, args, case_id, raw_text?}`
   (read defensively off the raw payload dict -- no new contract model, mirroring
   `turn-complete` / `aoi_bbox`). The server handler `_handle_dev_tool_invoke`
   validates the wire shape (name a non-empty str, args a dict) and dispatches
   through the SAME `_dispatch_tool_and_persist` -> `_invoke_tool_via_emitter`
   seam the `/invoke` directive uses. So the payload-warning / code-exec /
   solver gates, the `_ALWAYS_OFFLOAD_SYNC_TOOLS` thread offload, layer
   materialization + Case persistence, the `tool-io` card sidecar, and the
   end-of-turn `turn-complete` ALL ride the identical path a model-issued call
   produces. An unknown tool routes through the same `ToolNotFoundError` ->
   `TOOL_NOT_FOUND` envelope. The prepared-turn scaffolding (case rebind / sync
   / auto-create / user-row persist / turn pin) reuses `_prepare_user_turn`.

3. RESULT RENDERING rides the model-call path verbatim (tool card inline in the
   chat scroll, layer materialization into the current Case).

4. GATE COMPOSITION: every gate is INSIDE `_invoke_tool_via_emitter`, so reusing
   that seam composes them for free -- verified by test. The code-exec HARD
   confirm gate still fires (code_exec_request strips model-supplied
   `confirmed`/`code_exec_id` before the server-owned gate), so `!run
   code_exec_request(...)` is NOT a sandbox-bypass. No gate needs to be
   fail-opened.

5. ATTRIBUTION: the composer's literal `!run <signature>` line is echoed as the
   user bubble AND persisted (via `raw_text` -> `_prepare_user_turn`) as the
   turn's user row, so a Case reopen replays the `!run` signature ABOVE the tool
   card -- durable attribution distinguishing a manual call from a model call
   without a new UI surface. (The tool-card row label renders `step.tool_name`,
   which takes precedence over the human `name`, so a name-prefix marker would
   not surface; the persisted signature bubble is the honest, replay-durable
   attribution instead.)

6. BLUE `!run` SIGNAL (NATE add): the composer (`_ChatInput`, a
   `QPlainTextEdit`) colours the leading anchored `!run` token blue via a
   minimal `QSyntaxHighlighter`, firing on EXACTLY the same predicate
   (`run_invocation.is_run_prefix`) the parse-first routing reads -- ONE shared
   predicate, so the visual signal can never disagree with where the message
   routes (test-pinned: highlight-on iff routes-direct). Only the first block is
   coloured; a mid-sentence `!run` stays uncoloured, matching the routing
   immunity. A native text-color highlighter was possible because the composer
   is already a QPlainTextEdit (no risky widget swap).

## Safety

Always-on in LOCAL mode -- the tailnet is the trust boundary and this is NATE's
product. No dev flag: the only consequential action (`code_exec_request`) keeps
its mandatory HARD confirm gate via the shared invoke seam. The parse is
strictly prefix-anchored, so no chat message can be accidentally routed.

## Consequences

- No new registered tool: registry stays 173 byte-identical; `!run` is a
  protocol feature, not a tool.
- The hidden `/invoke` server directive remains (unused by the dock) as the
  operator-debug seam; `!run` is its first-class, client-parsed, gate-composing
  successor.
- Tests: parser unit tests (both arg styles / malformed / prefix-anchoring /
  mid-sentence immunity / shared-predicate); server handler tests (wire-shape,
  unknown-tool, emission-pipeline invoked, offload rule, gate pass-through); an
  offline E2E round-trip against `stub_server.py`.
