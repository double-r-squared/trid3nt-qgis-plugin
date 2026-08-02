# 0025 - Template library + knob manifests (runtime doctrine)

Date: 2026-07-26. Status: accepted. Supersedes the layer-2 (playground-as-
runtime) framing of ADR 0024; keeps its layer-1 primitives and layer-3
discovery.

## Context

NATE, settling the full-engine-control discussion: granular tool growth
degrades routing (measured: +9 tools demoted 2 canonical queries) and
playground code-rewriting is fragile and unverifiable per-run. "Templates is
the best approach - allow knobs to be turned within the template so we
aren't having to rewrite the template each time." Practitioner prior art
(ras-commander, LLM Forward) independently converges: existing model =
template, file edits = knobs, AI never operates the model freeform.

## Decision

1. RUNTIME = template selection + knob turning. A template is a verified,
   solve-tested engine deck/workflow with a DECLARED KNOB MANIFEST (name,
   meaning, type, bounds). Hot-path scenario tools are templates and stay
   the primary, retrieval-privileged surface. set_*_parameters are the knob
   mechanism (copy-on-write, bounds, read-back).
2. TEMPLATE SOURCES, in order (NATE 2026-07-26): (a) PUBLISHED examples -
   engine documentation/example suites, agency model archives (USGS
   ScienceBase) - "usually an example lives online, so we stick with what's
   been written already"; (b) no published example -> SOLVE-GATED authoring
   (drafted, then must pass solve + bounds + diagnostics verification);
   (c) highly uncertain -> escalate to NATE.
3. Full native APIs are reachable only inside the gated authoring flow, not
   at runtime. Discovery = search over the template library + knob
   manifests.
4. Templates must FAIL HONESTLY into the next layer: a typed limitation
   naming what the template cannot express, never a silent keyhole.

## Consequence

The coverage inventory (engine-coverage-inventory.md) reads as a knob/
template backlog, not a tool backlog. Selected next moves (HELD pending
discussion): SWMM deepening, HEC-RAS replication spike.
