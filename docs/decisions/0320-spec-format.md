# 0320 - The spec format

Ruled 2026-08-27, during the workflow-blueprint trim.

## Decision

Every architecture/design spec is one self-contained HTML page, committed
under `docs/specs/` and published as an artifact for review. The format:

- **Vocabulary table first** when terms are load-bearing - the fixed words,
  what each means, what it never means.
- **One section per concern**, eyebrow-labeled. Prose states constraints,
  not narrative; every sentence should survive as a requirement.
- **Code snippets** show the surface as it will actually be written - real
  names, declaration blocks, call sites. A signature block beats a paragraph
  describing one.
- **UML** for structure: class diagrams (mermaid) for the static shape,
  simple flow/state diagrams (inline SVG or mermaid) for loops and
  pipelines. Diagrams depict the mechanism, with labeled arrows.
- **Plain language.** No design-pattern names, no jargon - describe what the
  code does ("a fixed spine of steps; engine-touching steps delegate to the
  official library"), not which pattern it resembles. Pattern vocabulary is
  private analysis language, never spec language.
- **Everything on the page is buildable.** Nothing aspirational; anything
  deliberately excluded is marked out of spec, in place, with one line of
  why. Frozen subsystems are named as frozen.
- **A revision line in the header**: date, rev, what changed. Revisions
  overwrite in place (the artifact URL is stable); the git history is the
  archive.

## Why

The blueprint converged on this shape through NATE's edits: jargon out,
signatures in, UML as the abstraction that actually communicates, and a
hard line between what is being built and what is being mused about. A spec
that mixes analysis with commitment reads as neither.

## First instance

`docs/specs/workflow-blueprint.html` - the mesh-tool spec (rev 4), the
template for the next one.
