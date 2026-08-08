# 0175: Showcase-Case seeding through the product `!run` path

## Context

The engine-template proofs (ADRs 0141-0174) have always terminated in
`docs/proof/` renders and ADR smoke logs. None of them ever landed a Case in a
QGIS profile a human could open and inspect. NATE's directive: seed real,
inspectable Cases through the PRODUCT path so the templates can be opened,
scrubbed, and eyeballed in QGIS -- not re-derived from a report.

## Decision

Add `scripts/seed_showcase_cases.py`: a headless WebSocket client that drives the
live daemon exactly as the QGIS plugin does, one Case per showcase template.

- **Product path, end to end.** The driver speaks the real A.3/A.4 envelope
  sequence: `auth-token` -> `session-resume` -> `case-command create` (a named
  Case, title `showcase: <template>`) -> `dev-tool-invoke {name, args, case_id,
  raw_text}`. `dev-tool-invoke` is the ADR 0114 `!run` direct-invocation seam:
  the SAME registry closure, gates, layer materialization, and Case persistence a
  model-issued call rides. Nothing is published out-of-band; MinIO/persistence
  are exercised through the daemon.
- **Proven args only.** Every arg set is mined from an ADR smoke report or a
  `scripts/run_*_direct.py` driver; the provenance is recorded in each entry's
  `note`. No physics is invented. Boulder (0141), SF/East Bay (0149/0164),
  Apalachee (0147), Chattanooga (0152), Crescent City (0148), Galveston
  (0168), Platte valley (0165/0166), Sacramento/Colusa (0169), Eel (0154),
  Muncie (0170-0172).
- **Gates auto-driven honestly.** The solver-confirm / granularity /
  fetch-resolution gates all surface as `tool-payload-warning`; the client
  auto-confirms `proceed` (the same envelope the plugin sends). A
  `confirmation-request` is auto-approved. The input-review gate runs in AUTO
  mode with labeled defaults. Any gate that genuinely needs interactive input
  (`spatial-input-request` / `disambiguation` / `clarification` /
  `recovery-choice`) is recorded as `blocked` and skipped -- never faked.
- **Honesty floor for pass/fail.** The tool-io `is_error` flag is authoritative.
  Success requires a non-error dispatch that emitted something inspectable: a map
  layer (LayerPanel), a chart-dock chart (validation/report templates), or an
  explicit `status=ok`. A non-error dispatch with no emission is `no_result`, not
  a pass.
- **Durability verified.** After seeding, a SECOND connection reopens every Case
  (`case-command select`) and confirms the persisted `loaded_layers` survive the
  reconnect -- the per-Case layer-durability norm, proven live (e.g. landlab flow
  accumulation reopened with 2 persisted layers). Chart-only validation Cases
  persist 0 loaded_layers by design (charts durabilize via `chat_history`, not the
  LayerPanel), which the report states honestly.
- **Offline check.** `--dry-run` connects to nothing: it prints the plan and
  round-trips every reconstructed `!run` line back through the PRODUCT parser
  (`trid3nt.net.run_invocation.parse_run_invocation`), asserting the line parses
  to the same `(name, args)` -- a hermetic contract check that reuses product
  code (24/24 round-trip clean).

## Consequence

Every gate-free / auto-mode-defaultable engine template now has a durable,
named Case in the profile, each carrying a copy-pasteable `!run` line. The
driver never deletes or mutates an existing Case (NATE's keep-set is sacred); it
only creates `showcase:`-prefixed Cases. It touches no template file
(`test_template_hygiene.py` green). Re-running appends fresh duplicates rather
than reconciling -- acceptable for a seeding aid, and the cheapest-first ordering
means a slow engine tail never starves the fast, high-value Cases.
