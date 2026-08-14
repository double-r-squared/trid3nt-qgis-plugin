# server.py refactor recon (read-only map, 2026-08-14)

NATE named the pivot: refactor `server/src/trid3nt_server/server.py` -
he inspected it and saw Gemini residuals plus cloud-era leftovers that do
not fit the local product. This is the pre-refactor map. No code was
changed in this pass.

## Headline numbers

- 12,979 lines, 167 top-level defs, 9 classes, in ONE module.
- 84 gemini/google/vertex/genai references; ZERO live google imports -
  every hit is a comment/docstring or the misnamed core dispatcher
  `_dispatch_gemini_and_persist` (line 11260), which today drives
  Bedrock/pluggable models. The Gemini residue is vocabulary, not code
  paths.
- 44 aws/cloud references: DynamoDB (persistence comments + reap TTL
  notes around 6030/10057/10100), aws-batch backend switch (7856,
  "byte-identical when backend is aws-batch/unset"), TiTiler styling
  fallbacks (1190-1222), Cognito zero. Mix of dead-comment and
  live-but-cloud-shaped seams.
- 9 "grace" naming hits (identifier-tier, rebrand rule applies).
- Imported by: main.py (entry), telemetry.py, tool_catalog_http.py,
  cases/ingest_user_layer.py - a small fan-in, which makes extraction
  tractable.

## Region inventory (extraction candidates, in file order)

| Lines (approx) | Region | Note |
|---|---|---|
| 1-350 | module docstring + env-knob helpers (_tool_retrieval_k, _code_exec_approval_timeout_s...) | config module candidate |
| 351-630 | routing-mode helpers + stage labels + pending tool-choice registry | gate/interaction module |
| 632-965 | catalog-offer registry + endpoint probe + addition flow | catalog module |
| 965-1200 | typed error classes (ToolNotFound, PayloadWarningCancelled, CodeExec*, SolverConfirmation*) | errors module |
| 1200-1456 | raster/style/publish helpers (_is_flood_depth_cog, TiTiler preset fallback) | render-style module; TiTiler naming here |
| 1456-1730 | credential registry + bbox/AOI helpers | split: credentials / spatial |
| 1730-2189 | spatial-input errors + AOI zoom + drawn-geometry handling | spatial module |
| 2189-2327 | _LiveTurn | turn lifecycle |
| 2327-~7050 | SessionState (THE giant: ~4,700 lines of per-session state + methods) | the core extraction problem |
| 7051-~7856 | _ReuseEntry + reuse cache | reuse module |
| 7856-~11260 | tool dispatch machinery (solver confirm, payload gates, batch-era switch, code-exec approval) | dispatch module |
| 11260-~12500 | _dispatch_gemini_and_persist (the model-turn driver) + persistence joins | rename + turn-driver module |
| ~12500-12979 | WS handler + serve wiring | protocol module |

## Residue classification

- SAFE RENAMES (no behavior): `_dispatch_gemini_and_persist` ->
  `_dispatch_model_turn_and_persist` (or similar); every "Gemini
  should narrate" docstring -> "the model"; grace* identifiers per the
  rebrand rule.
- DEAD-COMMENT SWEEPS: Vertex-only notes (2489, 3286, 3517), DynamoDB
  prod-persistence notes (1825-1833), "On AWS the prod persistence is
  DynamoDB" - the local product persists via file/Mongo; comments must
  state constraints, not history.
- DECISION-NEEDED (live cloud-shaped seams):
  - aws-batch backend switch (7856): local-docker is the only solver
    backend since cf7129d2 (GRACE-2 side); this seam is dead weight
    here - confirm and delete.
  - TiTiler style fallbacks (1190-1222): QGIS-native rendering replaced
    TiTiler in the QGIS-only product; verify nothing local dials a
    TiTiler URL, then strip the vocabulary.
  - DynamoDB TTL stamping (6030): if the local persistence honors TTL
    differently, simplify.
- adapter.py (dormant Vertex/Gemini path) - separate decision: the
  local product's pluggable-LLM story (bedrock/anthropic/local) may not
  need the dormant seam at all; deleting it is a DELETION_LEDGER row.

## Suggested refactor shape (for the pivot discussion, not started)

Extraction over rewrite, one region per wave, offline suite + WS smoke
green at every step; SessionState last (biggest, most coupled). Naming
sweep + dead-comment sweep can ride ANY wave touching a region (hygiene
norm), but the rename of the core dispatcher should land in the FIRST
wave so the vocabulary stops propagating.
