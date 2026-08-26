# ADR 0314 - The static plan, and the style contract

Status: LANDED (TELEMAC workflows refactor, wave A).
Supersedes nothing; amends ADR 0303 (the plan value) and ADR 0312 (the skeleton).

## Context

Two things had grown past what their shape could carry.

The PLAN still took a resolved sheet. `plan(p, d, ops)` meant a plan could only be
built once an invocation existed, which forced three consequences: a template's
slot values had to be assembled inside the function (so the recipe read as
construction rather than as declaration), a construction-time branch was possible
(`When(p.get("flag"), ...)`) and therefore had to be POLICED - a whole
read-recording apparatus existed only so the validator could refuse a plan that
branched on a value a form gate could revise - and a `P.` typo could not be caught
until the tool was invoked.

The STYLE decision had three homes that had to agree: a 59-row preset registry
inside `publish.py`, a quantity -> preset map in `emission/quantity_styles.py`, and
a `style_preset` field on every `OutputQuantitySpec`. A CI test asserted they
agreed, which is what a mirror looks like when someone has noticed it. Separately,
every scale was FIXED at a range somebody guessed, so a refined river_dye run that
peaked at 28.7 mg/L was painted flat against a 0-10 preset.

## Decision

### 1. The plan is STATIC

`plan(ops)`. No sheet. Reads are the module-level namespaces `P.<param>` and
`D.<data>`, which build a `ParamRef` / `DataRef` carrying the `file.py:line` where
they were written.

Because the namespaces carry no sheet, a template's slot values become MODULE-LEVEL
BINDING BLOCKS - `PHYSICS`, `FORCING`, `MESH`, `CORRIDOR` above `plan()` - and the
plan becomes a pure assembly of them. The blocks are DEEP-frozen (mappings to
read-only views, sequences to tuples) because they live for the life of the process
and every run reads the same object.

The plan is built and validated ONCE, at registration. A `P.` typo, an unreachable
`Ref`, a misplaced gate or a physics process the facade does not model is an
authoring error that now fails at import with the construction site named.

### 2. Every conditional is a `When`, decided by the interpreter

A `When` condition must be a late-bound read; a concrete value is refused. The
interpreter binds it against the CURRENT sheet when the branch is reached, so an
approved form-gate revision decides which body runs.

That turns the old validator refusal inside out. `_check_revisable_branches` -
which refused a plan that declared a FormGate and branched on a revisable param -
is DELETED, because the shape it forbade is now the intended one.
`_check_when_conditions` replaces it and refuses a condition that names nothing.

The read-recording machinery (`freeze_reads`, `concrete_reads`, the recording
`ParamValues` view) and `ResolvedParams.get` are DELETED. There are no
construction-time reads left to record. `value_of` is the concrete read, and it
belongs to the interpreter and to code running WITH a sheet.

### 3. Producers are demand-pulled; a `Data` may have no producer

The eager independent-Data batch is DELETED. A producer runs when a step that
`Ref`s it executes, which is what makes a `When`-guarded consumer whose branch does
not fire cost no fetch. The trade is stated in the ledger: independent producers no
longer run concurrently.

A `Data` may declare NO producer - a CONTEXT SLOT, written
`Data("structure").supplied(geometry="polyline")`. Naming a default fetcher for a
breakwater or a clip zone is an opinion the question does not carry, so the slot
declares the SHAPE it accepts and nothing else. `.optional()` makes absence legal
and LABELLED; an unsatisfied required slot refuses typed.

The modifier is `.supplied()` throughout, not `.byo()`: `user_supplied` is already
the ladder rung's name and "supplied on this invocation" is already the provenance
vocabulary, so one word covers all three surfaces. No alias survives.

### 4. `declarations.py` beside every template

PARAMS and DOC move one file over. The template keeps the question docstring, DATA,
the binding blocks, `plan`, ANSWER, the chart, the metadata and the registration -
the recipe on one page, the forty-row contract next door.

### 5. The user-input species

One lib module (`workflows/lib/user_input.py`) holds the typed normalizers - point,
polyline, polygon ring, bbox, bearing - and BOTH routes to a param go through it: a
value that arrives DRAWN (the draw gate's reply) and a value that arrives TYPED (a
wire coercion). The gate vocabulary and the wire vocabulary cannot drift, which is
the no-double-middleware law applied to our own front door. `wind_bearing` folded in
entirely; `release_points` SPLIT - the shape normalization to the lib, the
seed-versus-source policy to `river_dye/coercions.py`, because only that question
has to make it.

### 6. The run journal

One append-only JSONL record per completed run, written by the skeleton's publish
stage (one seam, every engine): the resolved sheet WITH its doors and bases, the
answer, the provenance rows, the mesh facts, the wall time, the compute class and
where the run came from. It lives in the persistence directory nothing sweeps,
because artifacts are delete-on-whim and the record of what was asked must outlive
them. `scripts/backfill_run_journal.py` seeds it from surviving run prefixes and
the canary evidence JSONs; swept runs are honestly gone.

### 7. The style contract

`contracts/trid3nt_contracts/styles.yaml` holds the preset table AND the
quantity -> preset defaults in ONE file, so the mirror is not constructible.
`emission/styles.py` is the only resolver. The data-driven rescale LOGIC stays code
(reading band statistics is not a declaration); the POLICY is declared.

`policy: data` is the default for model-output quantities. Two boundaries make it
honest: the SCOPE of "data" is the RUN, never the frame; and a comparison set shares
one range. Fixed ranges remain for domain-standard bounded quantities. Legends
always state which policy ran and over what range.

Style is DISPLAY STATE, so the policy is available up front (`.style()`, replacing
the never-shipped `.render` verb) and after the fact (`restyle_layer`, which
re-emits a published layer's display face and deliberately cannot make one visible).

## Consequences

- Six TELEMAC templates now read as declarations. The physics answers owe parity
  and were re-verified; the representation changed, the numbers did not.
- A style change is a one-line contract edit rather than a code change in three
  places, and a new engine quantity is one row.
- Every model-output raster is now painted over its own range by default. That is a
  VISIBLE change to what published layers look like, and it is the point: the old
  fixed ranges were guesses that flattened any run that left them.
- The four pre-skeleton templates (three SWMM, one MODFLOW) still build their own
  `Plan` from a sheet. They are unaffected except for `p.get` -> `p.value_of`, and
  they adopt the static rule when they migrate onto the skeleton.

## What this does NOT do

The TELEMAC-specific work rides a second wave: the coastal inundation split, the
results-mesh origin fix, the resolution labels, the sim-duration doors, the
agitation exemplar, the steps audit and the proof-folder reorg.
