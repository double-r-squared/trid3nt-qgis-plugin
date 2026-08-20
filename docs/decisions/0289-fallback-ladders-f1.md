# ADR 0289 -- fallback ladders, wave F1: the module, the router kwargs, the one gate, the SWAN bathymetry ladder

Status: LANDED (rung schema + walker + loudness gate + router plumbing + the
`fetch_topobathy` ladder + 41 offline tests + the live A/B + a live SWAN solve).
The "Consequences" claim that no existing caller changes behavior is SUPERSEDED
BY ADR 0290 (wave F1b) -- it was false: the coverage gate fires for every
`fetch_topobathy` caller, and the three that declared no rung each mishandled it.
Date: 2026-08-19. Implements `docs/design/fallback-ladders.md` (NATE-approved as
written) plus NATE's 2026-08-19 addition: user-supplied data is the TOP rung of
every ladder. Proving case: the SWAN bathymetry rectangle
(`docs/design/fallback-audit.md`, row 5 + `docs/proof/templates/swan_bathy_forensics.png`).

## Where the machinery lives, and why

Two homes, following the folder-per-feature map rather than inventing a third
pattern:

- `trid3nt_server/fallbacks/` -- `ladder.py` (rung schema + ladder validation +
  registry) and `walker.py` (the ONE walker + the activation record). A sibling
  of `data/`, `mesh/`, `gates/`, `emission/` because a ladder is NOT a fetch
  concept: wave F2 migrates mesh rows (SWMM outfall relocation, roughness demo
  defaults) and worker-postprocess rows (landlab CRS) onto the same walker, and
  none of those can depend on the data router. Putting it under `data/` would
  have made every future non-fetch rung import the fetcher package.
- `trid3nt_server/gates/fallback.py` -- the loudness floor and the pause. `gates/`
  is already the home of the gate family this rides (`input_review.py` reaches
  the pending-confirm spine exactly this way), so the gate belongs there and not
  in the new package.

Rung DEFINITIONS live with the capability owner, per the spec: the bathymetry
ladder is declared in `data/fetchers/_router/hooks/topobathy.py`, next to the
legs it names.

## The shapes

`Rung(name, consequence, describes, source | call | params, supplies_param)`.
Exactly one invocation form per rung: a registered `source` tool, a `call`
dotted path, or neither -- meaning "the primary request again with `params`
merged", the form a composite source uses to switch one of its own legs on.
`describes` is user-facing: it is what the narration and the gate card say the
alternative IS.

`Ladder(capability, rungs, refuse_error_code, terminal=REFUSE)`. `rungs` is
ordered top-down and validated at construction: an optional `user_supplied` rung
FIRST, exactly one `primary`, then alternatives that must each carry a
degradation class. `REFUSE` is a module constant on every ladder rather than an
implied fall-off-the-end.

Consequence classes: the spec's three degradation classes
(`same_data | cross_dataset | synthetic`) plus three structural ones --
`primary` (the declared first choice), `user_supplied` (NATE's addition), and
`refuse` (the terminal rung alone). The loudness floor keys ONLY on the three
degradation classes; the structural ones are not degradations and must not be
forced to lie about themselves by borrowing one.

### The user_supplied rung (NATE, 2026-08-19)

A ladder may declare ONE `user_supplied` rung at the top, naming the request
param that carries the user's own data (`supplies_param`). When that param is
present the walker serves it and stops -- user data outranks every derived rung.
It is not an upload feature: the rung consumes a value already arriving through
tool params, and it rides the existing basis machinery, stamping
`SyntheticInput(basis="user")` on the result exactly as the input-review gate
does for a user-revised value. Wired end to end on the bathymetry ladder:
`fetch_topobathy(bbox, dem_uri="s3://...")` returns a `TopobathyResult` pointing
at the caller's raster with `fallbacks=[user_supplied]` and no fetch at all.

## The walker's contract

One function, `walk_ladder(ladder, params, attempt, allow, gate_mode, gate)`.
It builds the plan (user rung when supplied, then the primary, then the
alternatives the call site permitted BY NAME in the order it named them), fires
the gate before any degradation, and records a `RungRecord(rung, consequence,
coverage, note)` per attempt.

Coverage is cumulative and comes from the seam, not from the walker's guesswork:
a seam that can measure its own coverage raises `LadderGap(covered_fraction,
gap_note)` instead of filling the hole itself, the walker records that rung's
SHARE, and the next rung's share is the remainder. A mosaic therefore reports
"89% cudem_nearshore / 11% etopo_bathy_base" without any per-seam bookkeeping.
A rung that fails outright records coverage 0.0 and the walk descends.

Terminal REFUSE, deliberately, does NOT re-wrap by default: when the last
failure IS the capability's own typed error (a bad bbox, an unreachable
upstream, or the gap error itself -- which already names what is missing AND
which rung would cover it), it propagates VERBATIM. A raw call therefore behaves
exactly as it did before a ladder existed, and no ladder can launder a
`TOPOBATHY_INPUT_INVALID` into a generic refusal. `LadderRefused` (carrying the
ladder's `refuse_error_code`) is raised only in the case the verbatim error would
be misleading: a gap was recorded and the rung meant to fill it failed for its
own unrelated reason, so the refusal has to name both.

## Router plumbing

`fallback=(rung, ...)` and `fallback_gate="auto"|"user_gated"` ride the router's
existing kwarg absorber, the `purpose=` precedent -- zero schema churn, not
LLM-visible, absorbed by the promoted closure's `**_extra_ignored`. `route()`
pops them, resolves the ladder by source name, and walks it with an `attempt`
that re-enters its own pipeline (extracted verbatim as `_route_once`) with the
rung's params merged. A source with NO registered ladder skips the walker
entirely -- 95 specs are byte-identical. A source WITH a ladder and no
`fallback=` kwarg gets primary-or-typed-error: REFUSE is the universal default.

Activation lands on the result envelope through two additive contract fields:
`FallbackActivation` (capability, rung, consequence, coverage, note) and
`LayerURI.fallbacks`. Narration reuses the EXISTING `fallback_note` channel
(already hoisted to the LLM), so there is no new narration seam and no plugin
change. `render_fallback_line` is silent on an undegraded run -- the line exists
to say what was swapped, never to add noise.

## The one gate

`gates/fallback.py` applies the floor: `same_data` walks silently (still
recorded); `cross_dataset` logs a loud line always and PAUSES only in
`user_gated`; `synthetic` ALWAYS pauses with a labeled default of refuse. The
pause is a `tool-payload-warning` on `_PENDING_CONFIRMATIONS` -- the same
envelope and the same resume path as the input-review and mesh gates.

Declining is not a run-cancel (mesh-gate semantics): the walker marks the rung
unpermitted and descends, so the run continues into its own error handling and
ends at the ladder's typed REFUSE.

The fetch path is synchronous and off-loaded, so the gate drives its coroutine
onto the emitter's bound loop with `run_coroutine_threadsafe` (the
`emit_on_fetch._drive_emit` precedent -- the loop is free while the composer is
parked on the thread). With no emitter, no bound loop, or when the fetch is
running ON the loop thread (where a blocking wait would deadlock), the labeled
default applies and a line is logged. A canary never hangs; an unanswered gate
times out into the labeled default rather than reading as a "no".

## The proving case: the SWAN bathymetry rectangle

The exhibit: bbox `(-85.55, 29.70, -85.40, 29.85)` (Port St Joe FL). CUDEM's
hosted 1/9" collection stops mid-AOI. Where it stopped, the 3DEP land leg's flat
~0 m ocean fill painted the water -- a rectangle SWAN reads as dry ground and
excludes from its computational grid. It was SILENT: `fallback_warning` was
`None` on that path, because `_compose_fallback_warnings` only fires when CUDEM
is entirely absent, never when it is merely short.

The ladder, declared on `fetch_topobathy`:

    user_supplied (dem_uri)  ->  cudem_nearshore (primary)
                             ->  etopo_bathy_base (cross_dataset)  ->  REFUSE

The gap check runs in `topobathy.validate` -- PRE-CACHE. A partial-coverage gap
is a property of the REQUEST, not of the fetched bytes: run in the delegate it
would be skipped on a cache hit and a stored fake-land surface would be served
without the ladder ever running. Coverage is exact geometry (CUDEM tiles are
non-overlapping 0.25-degree squares whose NW corner is in the filename), and a
gap that cannot be PROVEN is never claimed: an unparseable footprint, an
unreachable tile index, a zero-tile AOI, or a request that already lays the
global ETOPO column down all pass through untouched.

Scope held to the exhibit: the ZERO-CUDEM AOI keeps today's ETOPO auto-fallback
plus its loud warning (audit row 5, graded OK-as-is), so no existing caller
(SFINCS coastal, GeoClaw inundation, SCHISM surge) changes behavior. Declaring
that path as a rung is wave F2.

### Live A/B (real fetchers, real CUDEM manifest, MinIO)

A -- UNDECLARED, `fetch_topobathy(bbox=EXHIBIT)`, raised
`TopobathyCoverageGapError` / `TOPOBATHY_COVERAGE_GAP`, `covered_fraction=0.8889`:

> the NOAA NCEI CUDEM 1/9" nearshore composite covers 89% of AOI (-85.55, 29.7,
> -85.4, 29.85): 3 tile(s) spanning (-85.75,29.50,-85.25,30.00). The remaining
> 11% of the AOI lies outside that footprint and has NO nearshore bathymetry
> source. Filling it from the 3DEP land DEM would paint flat 0 m ocean -- a fake
> landmass a wave/surge solver excludes as dry ground -- so this fetch refuses
> instead. Permit the 'etopo_bathy_base' rung
> (fallback=("etopo_bathy_base",)) to fill the gap from the global ETOPO 2022
> relief model, loudly labeled.

B -- DECLARED, `fallback=("etopo_bathy_base",)`, served
`s3://trid3nt-cache/cache/static-30d/topobathy/7b0bad3d137a27b1944ce336252f12bc.tif`
with `fallbacks = [cudem_nearshore/primary/0.8889,
etopo_bathy_base/cross_dataset/0.1111]` and
`fallback_note = "Fallback ladder (fetch_topobathy): 89% cudem_nearshore
[primary] + 11% etopo_bathy_base [cross_dataset]."`

Raster forensics on the same AOI, same merge code:

| | cells EXACTLY 0.0 | cells < 0 (wet) | fallback_warning |
|---|---|---|---|
| pre-ladder (3DEP land fill over the gap) | 12.99% | 84.04% | `null` (SILENT) |
| declared etopo rung | 0.00% | 96.95% | superseded by the ladder note |

12.99% matches the 12.7% the forensics figure measured on the SWAN grid.

Live SWAN solve through the composer, run `01M0GG8J16Q2SBVXWDHNAJXYBR`
(local-docker `trid3nt-local/swan:latest`, stationary, Hs 3.0 m / Tp 9.0 s /
180 deg / side S): `max_hs_m=3.051`, `mean_tp_s=9.143`,
`wave_area_km2=228.59`; the peak layer carries both `fallbacks` rows and
`fallback_note = "Wave bed: Fallback ladder (fetch_topobathy): 89%
cudem_nearshore [primary] + 11% etopo_bathy_base [cross_dataset]."` The solved
`bottom.bot` (10201 cells) is 0.06% exactly-0.0 and 3.02% below DEPMIN -- the
rectangle is gone; what remains is the real shoreline.

## Consequences

- A source that declares a ladder can no longer substitute silently, and a call
  site's tolerance is one kwarg where the fetch happens.
- `fetch_topobathy` on a partial-CUDEM AOI now REFUSES unless the caller permits
  the ETOPO rung. SWAN permits it (`_SWAN_BATHY_FALLBACK`); a caller that wants
  CUDEM or nothing gets exactly that by saying nothing.
- Every topobathy result now carries at least the `primary` activation row, so
  "what actually served?" is answerable from the envelope alone.
- `LayerURI` gained an additive `fallbacks` list. Default-empty means "no ladder
  governs this fetch", never "nothing was substituted" -- until F2 finishes, an
  empty list is not evidence.
- Wave F2 migrates the audit's remaining data-bearing rows onto this walker,
  deprecates the ad-hoc `force_bathy_base` / `skip_land` policy params in favour
  of declared rungs, and adds the sweep guard against naked substitution.
