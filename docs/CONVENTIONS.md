# Code documentation conventions

The rule in one line: names and structure carry the meaning; comments
carry constraints; documents carry knowledge. Prose in code is a
maintenance liability - every sentence must earn its place.

## Docstring budget, by surface type

| Surface | Budget | Rationale |
|---|---|---|
| LLM-facing tool docstrings (registered tools, template composers) | RICH - routing block, args, fallback ladder, constraints | This is product material: the model's routing interface, not documentation. Truncation limits already force discipline. |
| Contract types (wire/registry shapes) | Wire semantics only; one line where the shape is self-evident | Contracts are read by the model and by reviewers of the wire. |
| Public seams (module-level APIs other features import) | 1-3 lines: what it is + non-obvious constraints. Args documented only where the name/type does not say it. | A seam's docstring is its promise, not its manual. |
| Module docstrings | <= 3 lines: what lives here | The folder structure is the primary map. |
| Private helpers (leading underscore, single-feature use) | NONE, unless a non-obvious constraint exists | The name, signature, and 20 readable lines ARE the documentation. If a private helper needs a paragraph, it needs a better name or a split. |
| Tests | One line per test max: the behavior pinned. Module docstring only for suite-level conventions (baselines, harness quirks). | Test names carry the spec. |

## Comments

A comment states a constraint the code cannot express: an invariant, a
gotcha, an external-system behavior, a deliberate non-obvious choice.
Never: narration of the next line, history, provenance, citations,
attributions, milestones, or claims about other code (those rot into
falsehoods - verified 2026-08-15).

## Where knowledge lives instead

- docs/decisions/ - why things are the way they are (ADR-lite).
- docs/design/ - architecture maps and feature guides, written for
  agents and humans to READ BEFORE editing a feature. One page per
  feature folder is the target, created/updated when a feature changes
  shape.
- docs/validation/ - evidence and coverage.
- Commit messages - provenance, spec references, wave history.

## Enforcement

- New code follows the budget from birth; review flags prose overruns
  like any other defect.
- The documentation share of a non-LLM-facing module (docstring +
  comment lines / total) should sit well under 20%; a module pushing
  past that is either under-named or over-narrated.
- LLM-facing surfaces are exempt from the share metric but not from the
  constraint rule (no history, no citations, front-load routing).
