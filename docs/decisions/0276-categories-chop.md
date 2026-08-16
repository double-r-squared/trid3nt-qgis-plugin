# ADR 0276 - chop the category routing layer; hardwire retrieval-enforce

Status: LANDED (2026-08-16). NATE order: the Gemini-era category routing layer
is dead weight superseded by embedding retrieval, and the tool-gating log proves
it ("no-op ... already visible via the retrieval-enforce layer" every turn).
Delete `trid3nt_server/agent/categories.py` entirely, make retrieval-enforce the
unconditional built-in surfacing path, and rebuild the catalog page as the
agent's-eye view. Date: 2026-08-16

## Context

`categories.py` (1408 LOC) implemented the CachedContent Option-A architecture:
a 13-category taxonomy (`CATEGORIES` / `PRIMARY_CATEGORY` / `SECONDARY_CATEGORIES`),
two registered browse tools (`list_categories` + `list_tools_in_category`), a
per-session `AllowedToolSet` that widened as the model "opened" categories, and a
post-hoc `validate_function_call` gate that bounced any call outside the accrued
allowed set. The model was supposed to browse categories to discover tools.

That layer was superseded by embedding retrieval. `retrieve_visible_tools`
already computes a per-turn visible set (core floor UNION the Case's accrued set
UNION the top-k RRF ranking) and, under enforce, subsets the tool declarations to
it BEFORE the model sees them. With enforce on, the model only ever sees relevant
tools, so:

- the browse tools were never needed (retrieval surfaces the tool directly);
- the post-hoc `validate_function_call` gate was pure redundancy - the model
  cannot emit a call for a tool it cannot see, and a hallucinated (non-registered)
  name is already caught at dispatch by `_invoke_tool_via_emitter` -> typed
  `ToolNotFoundError`;
- `AllowedToolSet`'s category machinery (opened-categories, `open_category`,
  `tools_for_category` expansion, the meta-tool floor) was dead; only its
  monotonic accrual (never-hide-mid-task) was load-bearing, and enforce already
  unions the visible set into it each turn - `record_dispatch` was redundant
  (a dispatched tool was visible that turn, hence already accrued).

Retrieval was gated behind `TRID3NT_TOOL_RETRIEVAL` (off / shadow / enforce),
defaulting OFF. NATE's `.env.local` carried `enforce`. The off/shadow modes were
a rollout artifact; enforce is the only reality worth keeping.

## Decision

1. **Delete `categories.py` (1408 LOC).** Gone: `CategorySpec` + `CATEGORIES`
   (~250 LOC), `PRIMARY_CATEGORY` (~460), `SECONDARY_CATEGORIES` (~190),
   `HOT_SET_TOOLS` (~60), `AllowedToolSet` + `OutOfAllowedSetError` +
   `UnknownCategoryError` (~230), `validate_function_call` (~45), the
   `list_categories` + `list_tools_in_category` registered tools + impls +
   `tools_for_category` + `_first_sentence` (~130). Registry drops by 2
   (256 -> 254). The always-visible floor survives as `CORE_FLOOR` (12 tools,
   `HOT_SET_TOOLS` MINUS the two dead browse tools) in `tool_retrieval.py` - the
   surfacing module - imported by `tool_gating.py` and the server.

2. **Retrieval-enforce is the unconditional built-in surfacing path.** Remove the
   `TRID3NT_TOOL_RETRIEVAL` knob and the off/shadow modes + their config plumbing
   (`config._TOOL_RETRIEVAL_MODE` / `_TOOL_RETRIEVAL_VALID_MODES` /
   `_tool_retrieval_mode`). Every turn computes the visible set, unions it into
   the Case's monotonic visible set, and subsets the declarations. `K` stays the
   only lever (`TRID3NT_TOOL_RETRIEVAL_K`). The per-turn selection event still
   fires (mode hardcoded `"enforce"`) - the recall@k dashboard consumes it, so it
   is retained. `AllowedToolSet` is replaced by a plain
   `SessionState.visible_tools: set[str]`; `validate_function_call` is removed
   (the dispatch's own registry check is the hallucination guard). NATE's
   `.env.local` `TRID3NT_TOOL_RETRIEVAL=enforce` is now inert but harmless.

3. **The catalog page is the agent's-eye view.** `build_catalog_payload` becomes
   a thin `TOOL_REGISTRY` reader: every tool listed FLAT with its REAL docstring
   (the exact text the model routes on) and facets derived ONLY from metadata the
   tool already carries (`engine`, `tier`, `source_class`). No taxonomy, no
   `_first_paragraph` parallel-description path. A new self-contained
   `GET /catalog` HTML page (inline CSS + JS + embedded data, zero external
   assets) renders it with client-side name/text search + facet filters. The
   `/api/*` routes are untouched.

4. **Registry checklist loses the categories step** (CLAUDE.md law 5; authoring
   docs `writing-a-tool.md` + `adding-an-engine.md`).

## Consequences

- Net -2720 LOC (627 insertions, 3347 deletions across 62 files). Five
  category/validator test files deleted (938 LOC): `test_categories`,
  `test_allowed_set`, `test_validator`, `test_post_hoc_routing`,
  `test_dynamic_hot_set_integration`. Template/router/compute tests lose their
  category-pin assertions (kept their registration + corpus checks).
- The offline four-slice suite holds the exact baseline (4 fetch_resolution + 2
  river_dye). Contracts 721. Registry import 254. ws_smoke all_passed. Retrieval
  sanity: 6/6 diverse prompts HIT with enforce genuinely trimming (36-37 tools
  visible, not the full 254). Catalog page: HTTP 200, self-contained, 254 tools
  with docstrings + facets.
- The never-hide-mid-task invariant now rides `SessionState.visible_tools` (the
  monotonic plain set) instead of `AllowedToolSet`; enforce unions the retrieved
  set into it each turn and the dispatch loop records the dispatched tool into it
  (covering the rare fail-open turn).
