# ADR 0232 - resolve_resolution: the one resolution-resolve seam

Status: Accepted
Date: 2026-08-12

## Context

NATE's code-review ask (2026-08-12), looking at `hecras/flood_2d`: the
`_resolution_with_basis(bbox, requested)` helper enforces the declared range,
autoscale-coarsens within it, and labels the result with a basis + note -- **"is
this pattern used elsewhere and can we generalize it in a util or helper?"**

ADR 0225 already extracted the ENFORCE half (`enforce_resolution` + the
`resolution_review_note` labeler live in
`agent/tools/resolution_declared.py`). What stayed hand-rolled at the call site
was the RESOLVE half: seed the candidate, apply the autoscale, and pick the
`(value, basis, note)` triple. This ADR consolidates that resolve step into the
same module. EXTRACTION ONLY -- no contract-level machinery, no settings layer,
no `target_resolution_m` renames (all deferred by NATE); this consolidates what
exists.

## Site inventory

Greping `_resolution_with_basis`, `enforce_resolution`, and `basis=` across the
workflows tree, the "resolve a resolution to (value, basis, note)" pattern that
flood_2d exemplifies is narrower than the raw `basis=` hit count suggests. The
hits sort into four classes:

| class | sites | verdict |
| --- | --- | --- |
| full resolve+enforce+autoscale+basis | `hecras/flood_2d` (`_resolution_with_basis`) | MIGRATE (the reference) |
| entangled resolve (custom lazy-`measured` enforce + native-naming value + tin-budget note + `fetch_res_m` fan-out) | `schism/pahm_surge` | OUT - the helper would replace only a one-line `"user"/"derived"` ternary while the branch (tin dims, native-string value, node-budget note) stays; force-fitting risks the pinned envelope |
| bare `enforce_resolution` call, no basis triple | `sfincs/flood` (quadtree base), `geoclaw/inundation` (scenario tiling) | OUT - already the single 0225 enforce call; nothing to collapse |
| domain-extent / node-budget clamp or no-enforce passthrough | `telemac/river_dye`, `telemac/do_sag`, `mesh/generate_mesh`, `landlab/*` (`target_resolution_m`) | OUT - the 0225 river_dye precedent (domain clamps are not the declared-range resolve); landlab forwards the value with no enforce at all |
| provenance-label ternary on a NON-resolution param (`vs30`, `magnitude`, `flow_scale`, AMR window, solver-settings delta) | `openquake` x5, `hecras/riverine_flood`, `hecras/levee_breach`, `sfincs/numerical_physics`, `geoclaw/amr_regions`, `schism/*` | OUT - a DIFFERENT (broader) `basis="user" if X else "default_demo"` pattern; not a resolution resolve. A general provenance-label helper is a separate, deferred question |

Net: the genuine resolve-with-basis duplication that the util removes is **one
site** (flood_2d). The util's forward value is the shared seam plus the
self-enforcing sweep (below) so the next resolution-resolve site uses it instead
of re-hand-rolling -- and so flood_2d's copy cannot silently re-grow.

## Decision

`resolve_resolution(requested, *, spec=None, autoscale=None, default=None,
measured=None) -> ResolvedResolution(value, basis, note)` in
`resolution_declared.py`:

1. ENFORCE `requested` against `spec` (0225 quote-back; `measured` folds a cost
   line into the card). `requested=None` is in-range by construction.
2. SEED the candidate = `requested`, else `default` (the tool's native/default;
   `None` stays `None` -> forward native).
3. AUTOSCALE: `autoscale(seed)` when a scaler is given -- coarsen WITHIN the range
   for tractability (the granularity-gate degrade).
4. LABEL: `basis="user"` iff the caller asked AND nothing moved the value; else
   `basis="derived"` with a labeled `resolution_review_note`.

### Canonical basis vocabulary: `user` / `derived`

NATE's sketch floated `'autoscale'`/`'auto'`/`'default'`. The actual resolve
sites -- and the tests that pin them -- use exactly two strings: `"user"` (the
caller's ask, forwarded unchanged) and `"derived"` (the tool resolved it: an
autoscale-coarsening OR a native default). flood_2d pins `"user"`;
`schism/pahm_surge` pins BOTH `"user"` and `"derived"` on its envelope. So the
uniform vocabulary is `{user, derived}`, keeping every envelope byte-identical;
the review NOTE -- not the basis -- carries the finer autoscaled-vs-native
distinction.

### Deviation from the sketched signature

The sketch applied `autoscale()` (zero-arg) only when `requested is None`. The
reference site coarsens the USER's in-range ask too (the soft cell-cap degrade),
so `autoscale` is `Callable[[float], float]` applied to the seed regardless of
provenance -- this matches flood_2d, which is the pattern NATE pointed at.

## Consequence

- flood_2d's 24-line `_resolution_with_basis` deletes; its call site collapses to
  one `resolve_resolution(...)`. The `enforce_resolution` import drops (folded
  into the helper). The autoscale-binds NOTE wording unifies onto the shared
  `resolution_review_note` string (the bespoke "soft cell cap / granularity-gated"
  phrasing was unpinned) -- a deliberate wording unification, not a behaviour
  change to any pinned envelope.
- A self-enforcing sweep test bans any workflow file from defining its own
  `*resolution_with_basis*` function -- the resolve pattern cannot re-duplicate.
- `pahm_surge` and the bare-enforce sites stay as-is (documented above); the
  broader non-resolution `basis=` provenance-label ternary is left untouched.

## Deferred-design relationship

Downstream of 0225 (declared ranges + the enforce/label seam). The deferred items
NATE named -- contract-level resolution machinery, a settings layer, the
`target_resolution_m` rename -- are unchanged by this ADR; it only lifts the
existing resolve step into the existing module.
