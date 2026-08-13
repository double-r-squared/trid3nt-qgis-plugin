# ADR 0222 - Provenance-transparency audit of the tool surface (post surge-arc)

Status: accepted (audit only; no code changes)
Date: 2026-08-11

Cross-links: ADR 0219 (the R1/R2/R3 rulings this audits against), ADR 0217 (the
synthetic-shelf surge showcase that triggered the rulings), ADR 0215 (the wells-FGB
ambient-AWS silent-degrade fix), ADR 0198 (the dev-tool-invoke error-swallowing fix).

## Context

The SCHISM surge arc (0217 -> 0219) exposed a failure class: a template that FETCHES
real data but SILENTLY falls back to fabricated data (a synthetic sloping shelf) on
fetch failure, misrepresenting real geography while reading `status=ok`. ADR 0219
landed three rulings (R1 synthetic-is-never-a-fallback-tier, R2 resolution-is-a-user-
lever, R3 cross-dataset-substitution-must-be-loud). NATE ordered a TRANSPARENT,
READ-ONLY sweep of the existing surface for the same failure classes, findings first,
no mass-fix (accumulate-batch norm).

## Decision (audit method + verdict)

Systematic grep-driven hunt across the four classes over
`server/src/trid3nt_server/agent/workflows/` (~103 leaf templates) + `tools/` +
`tools/fetchers/_router/hooks/` (~60 hooks), triaging each except-body as
`raise typed error` (honest) vs `swallow -> default/synthetic` (violation-shaped), and
each degrade as envelope-labeled vs log-only. Top-3 verified end-to-end (call site ->
composer -> envelope). Full findings in
`docs/validation/provenance-audit-2026-08-11.md`.

Verdict: the surge-class defect is NOT reproduced elsewhere. ZERO CRITICAL findings
(no docstring lies measured-when-synthetic). The fetcher hook layer is clean (typed
router errors on upstream failure; parse-drops never fabricate). Residual findings are
consistency + completeness gaps, ranked in the report:

- Two seismic templates (`scenario_gmf`, `psha`) auto-fall-back to a synthetic source;
  both LABEL it in the envelope but are not opt-in-gated the way the post-0219 surge is
  - an R1-consistency question for NATE (psha's area source is the strongest exemption
  candidate as standard PSHA methodology).
- One genuine opaque-ish degrade: SFINCS `setup_mask_active` widens the active-mask
  elevation window on a DEM-range read failure, surfaced only in an `.inp` comment +
  log, not the user envelope (top-ranked fix).
- Provenance is present but UNSTRUCTURED (prose caveats vs structured `SyntheticInput`
  review entries) across the MODFLOW archetype family + pelicun.
- A few silent numeric clamps on granularity/domain-extent (defensible guardrails).

## Consequence

- A ranked findings table + a TOP-10 fix list awaits NATE's go; nothing was fixed
  (accumulate-batch). Two of the top-3 are NATE-judgment calls (R1 scope for seismic
  source models), not mechanical fixes.
- The reference-compliant patterns are recorded for reuse: `pahm_surge`
  (opt-in + WARNING banner + `domain_provenance` + `res_basis`), `swmm network_import`
  (`depth_basis` -> `SyntheticInput` -> `gate_input_review`), `mesh_acquisition` (loud
  cross-dataset note), the fetcher hooks (typed router errors), `topobathy`
  (`record_provenance` + `fallback_warning`).
- Coverage caveat recorded: the sweep is systematic by pattern, not an exhaustive
  line-by-line read; absence of a finding for a template is "no pattern hit", not proof
  of compliance.

Supersedes nothing. Board/velocity untouched (orchestrator's).
