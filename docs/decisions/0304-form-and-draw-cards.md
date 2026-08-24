# ADR 0304 - The FORM and DRAW cards (declarative wave 2)

Status: LANDED (wave 2 of the declarative campaign)
Design: `docs/design/declarative-workflows.md` (Gates section)
Supersedes, in part: ADR 0303's `GATE_NOT_YET_SUPPORTED` placeholder.

## Context

Wave 1 landed the plan value, the interpreter and the six doors. Its gates were
half-wired: a `FormGate` in `user_gated` mode rode the existing input-review
card, which renders the resolved values as a PARAGRAPH of provenance text with
no editor per row; a `DrawGate` in `user_gated` mode raised
`GATE_NOT_YET_SUPPORTED` naming this wave. The declaration already knew each
param's label, units, door, bounds and derivation - none of it reached the user.

Kickoff premise corrected on contact (law 5): the kickoff said BOTH gates raise
the not-yet-supported error. Only the draw gate did.

## Decision

### 1. No new envelope kind. Two existing spines carry both cards.

- **Form card** rides `tool-payload-warning` -> `tool-payload-confirmation`, the
  same pause/resume spine the input review already used, with ONE additive
  optional field: `param_sheet` (`ParamSheet` / `ParamSheetRow` in
  `contracts/trid3nt_contracts/payload_warning.py`). Its presence is what makes
  the card a property grid; every other gate is byte-identical.
- **Draw card** rides `spatial-input-request` -> `spatial-input-response`, the
  pair `request_spatial_input` already pauses on, reached from inside a tool
  through `current_emitter()` + the `_PENDING_SPATIAL_INPUTS` registry the WS
  loop resolves (the same trick the in-tool input-review gate uses for its own
  registry). No contract change at all.

The four declarable draw kinds map onto affordances that already exist:

| `DrawGate(geometry=...)` | wire `mode` | wire `purpose` |
|---|---|---|
| `point` | `point` | - |
| `rectangle` | `bbox` | - |
| `polygon` | `vector_draw` | `aoi` |
| `polyline` | `vector_draw` | `line` |

### 2. The sheet is the declaration, rendered

`trid3nt_server/declarative/form.py` builds the sheet from the declared `Param`s
and the resolved rows: name, value, units, `desc` as the label, door, basis, the
declared bounds, `user_lever`, and a SOURCE BADGE rendered server-side ("you
supplied this" / "read from your prompt" / "fetched from `<source>`" / "derived
by `<resolver>`" / "labeled default"). The badge is rendered where the doors
live - a client re-deriving "derived from what" out of `basis` would be guessing
at a declaration it cannot see. Rows sort question-bearing first;
`door=constant` rows carry `advanced=True` and the card folds them.

Every row is editable (NATE's ruling). Editing a derived value is WARNED through
its badge and its editor tooltip, never locked.

### 3. Submitting the form IS the approval

The text card's `narrow_scope` reply re-presents the review for another round -
correct when the user adjusted a value they could not see in full. When a
`param_sheet` rides the envelope the whole sheet was on screen, so a submit with
edits proceeds: re-presenting would ask the user to confirm the table they just
filled in, and the edits go on to re-seat and re-derive regardless. One reader
(`gate_input_review`), branch keyed on the field's presence.

### 4. Both gates seat their answer through ONE path

`reseat_revised` gained a `note` kwarg ("revised at input review" / "drawn on the
canvas"); `_seat` in the interpreter then re-derives, evicts the data that read
the changed rows, and re-keys the ledger. The draw gate is a NEW FRONT END to the
wave-1c revision machinery, not new semantics: a drawn point lands on the sheet
exactly as a form edit does, `basis=user`, bounds still applied.

### 5. Refusals name the unmet gate

`GateNotSupportedError` / `GATE_NOT_YET_SUPPORTED` are DELETED (grep-to-zero in
code). A draw gate now refuses `GATE_INPUT_REQUIRED` naming the param and the
reason there is no value - no live map session, a decline, a timeout, or a reply
carrying no geometry. Auto mode refuses as before. A geometry is never invented
in either mode.

### 6. Plugin (0.3.16 -> 0.3.17)

- `FormCard` (`plugin/ui/cards.py`) - the property grid, its own accent, the
  advanced fold, lock-once + fold-to-a-chip like every other gate card. The dock
  picks it over `GateCard` when `parse_param_sheet` finds a sheet; a malformed
  sheet degrades to the existing text card rather than a broken grid.
- `VertexCaptureTool` (`plugin/ui/draw_tools.py`) - the multi-vertex capture the
  plugin never had (click per vertex, right-click to finish, Backspace to undo
  one, Escape to abandon), wired into `SpatialInputCard` for the two SHAPE
  purposes. The TAGGED barrier surface (per-segment wall / flap-gate) still
  degrades honestly to Cancel - the plugin has no tagging affordance, and
  pretending otherwise would send untagged barriers the engine cannot read.
- All parse/resolve logic stays in the pure `ui/gate.py` layer, which is what a
  headless test can hold to the contract.

### 7. Riders

| rider | landed as |
|---|---|
| a. release point as a context layer | `workflows/telemac/release_layer.py` - one seam for every telemac leg, `role="context"`, named `Outfall (user)` / `Outfall (derived)`. It is a resolved PARAM, not a router fetch, so no emit-on-fetch seam can cover it; allow-listed in the ADR-0244 sweep with that reason. |
| b. chart spec + metrics to the run prefix | `RunResult.charts` carries the built specs out of the interpreter; `TelemacDoLayerURI.run_id` carries the prefix out of the solve; `workflows/telemac/run_products.py` writes `chart_spec.json` + `metrics.json` there. Verification cites the product's own chart. |
| c. `interpret.py` -> `interpreter.py` | renamed with every reference site (package import, logger name, 9 test sites); the module-shadowing monkeypatch trap is gone. Grep-to-zero. |
| d. two docstring views | `render_docstring(view="routing"\|"full")`. Full = the model's view (it fills the params); routing = a CHOOSE-the-tool view inside the truncation budget, which the catalog page now takes via `fn.routing_doc`. |
| e. showcase-case shape | the `telemac_do_sag` entry carries NATE's hand-built title suffix and an accurate note. |

## Consequences

- One wire type now has two renderers chosen by payload shape. That is the
  cheapest possible form card, and it means a client that does not know about
  `param_sheet` still shows the review as text and can still answer it.
- The submit-is-approval rule makes the form a ONE-round gate. A user who wants
  to see the re-derived sheet before running has to re-run; the derived rows'
  new values are in the result's provenance either way.
- `spatial_input_vertices_ready` is the only shape gate on a drawn param: three
  vertices for a ring, two for a line. Draw-time CONSTRAINTS (within the reach,
  on the mesh) remain out - the geometry to constrain against is produced after
  the gates, exactly as the design doc says.

## Stated honestly

- **The form card has no live proof yet.** It is proven offline through the wire
  (envelope serialized, parsed back, answered into the same pending registry the
  WS loop resolves) and by contract tests. The only migrated workflow,
  `telemac_do_sag`, declares NO `FormGate`: its `ReachSolve` step is
  `self_gating` (it resolves the NWM carrier discharge INSIDE, after a plan-level
  form would have fired), and the validator refuses a `FormGate` in front of a
  self-gating step. A live form card needs a declared workflow that reviews its
  own declared sheet - the river_dye migration (wave 3). Parked for NATE.
- **The catalog page now shows the routing view for declared tools.** That is
  what the rider asked for, and it is a judgement call: the page is also a HUMAN
  discovery surface, where the fuller doc may read better. One line in
  `catalog_http.py` reverses it.
- **`_first_ring` reads the first drawn polygon only.** A reply carrying several
  polygons contributes one; a draw gate asks for one shape, and the card submits
  one, so the case does not arise today.

## Gates

| gate | result |
|---|---|
| `tests/test_[a-e]*.py` | 1636 passed, 5 skipped, 0 failed (baseline 0) |
| `tests/test_[f-o]*.py` | 6645 passed, 3 skipped, 1 xfailed, **4 failed** - all `test_fetch_resolution_gate.py` (baseline). One extra failure during the wave, `test_input_layer_surfacing.py`, was the ADR-0244 emission sweep catching the new release-point publisher: allow-listed with its reason, green after. |
| `tests/test_[p-r]*.py` | 2102 passed, 2 skipped, **2 failed** - both `test_run_river_dye_scenario.py` (baseline) |
| `tests/test_[s-z]*.py` | 1418 passed, 6 skipped, 0 failed (baseline 0) |
| `contracts/tests` | **729 passed** (721 baseline + 8: the `ParamSheet` / `ParamSheetRow` validators, the JSON round trip, and the back-compatible absent-sheet default) |
| `plugin/tests` | 411 passed, 1 skipped, 2 failed - `test_case_bbox` + `test_tool_picker`, both reproduced on clean HEAD (pre-existing, untouched by this wave) |
| `scripts/ws_smoke.py` | `all_passed=True`, case self-cleaned |
| `scripts/run_sfincs_direct.py` (flood canary) | PASSED, `status=ok`, depth COG published |

Exactly the 4 + 2 baseline failures. No `workers/` path touched, so no image
rebuild is in play.

## Live evidence

`scripts/drive_do_sag_cards.py` - a scripted WS client on the live daemon,
answering the cards as the plugin would. Eel River near Scotia, California,
`input_mode="user_gated"`, BOD 20, 20 C, standard 5, k1 0.3, k2 0.9, 12 km,
mesh auto.

- **The draw card fired and was answered.** `draw gate emitted ...
  param=outfall_coords geometry=point request_id=01M0S51M58Z9KB2BBQHKGWS1QD` ->
  `draw gate answered` -> `the draw gate set ['outfall_coords']; re-seated
  through the GATE door`. The client replied with the USGS Eel River at Scotia
  gage (11477000) location, `[-124.0983, 40.4921]` - a real point on the reach.
- **The drawn value reached the physics.** The sag minimum moved to
  **8.6537 mg/L at 546.1 m** (standard 5.0, `violates=false`), against the
  ADR-0303 pinned reference's 8.5772 mg/L at 10631.7 m for the same reach and
  scenario with the release at the DERIVED mid-reach seed. Different release
  point, different sag - which is the whole point of asking.
- **The outfall layer is on the canvas**: `Outfall (user) -
  scotia_humboldt_county_california_95562_united_s`, `layer_type=vector`,
  `role=context`,
  `s3://trid3nt-runs/inputs/01M0S51MX9MZBGWNB279BXMMVE/release_point.geojson`.
- **The run's own products are in its prefix**: run `01M0S51MY5R9F7ZC2MZB41NK8B`
  ->  `chart_spec.json` (the `do_sag_curve` vega-lite spec, its caption and its
  `source_layer_uri`) and `metrics.json` (the 60-point sag curve plus the
  headline scalars). Neither is rederived.
- Terminal: `telemac_do_sag complete ... do_min=8.65 mg/L at 546.1m
  violates=False executed=['do_field', 'do_field.chart:do_sag_curve']
  replayed=[] notes=[]`.

Two things this run did NOT prove, stated plainly:

- **The form card never appeared** (`form_card_rows=0`), for the structural
  reason above: `telemac_do_sag` declares no `FormGate`. The card the run DID
  show was the composite's own self-gating input review, carrying no
  `param_sheet` - which the driver answered as a plain proceed, exercising the
  back-compatible path. The form card's round trip (envelope out, edit back, the
  run using the edited value, bounds still clamping, cancel refusing typed) is
  proven in `tests/test_declarative_cards.py` through the same serialization and
  the same pending registry the WS loop resolves.
- **The QGIS visual pass is NATE's.** `scripts/install_plugin.sh` syncs the
  profile copy and the daemon's plugin-repo serves 0.3.17; nobody has looked at
  the rendered cards in QGIS yet, and this report does not claim otherwise.

## Kickoff premises checked against the code (law 5)

- "FormGate/DrawGate in user_gated mode ... currently raise the typed
  not-yet-supported error" - only the DRAW gate did. The form gate already rode
  the review spine; what it lacked was a payload the card could render.
- Rider e's "STRIP the legacy `expires_at` field from rows it writes" -
  `expires_at` does not appear in `seed_showcase_cases.py` at all, and Cases have
  been durable since ADR 0267 deleted the TTL stamping (`upsert_case` writes no
  TTL; `_doc_to_case_summary` drops a legacy one on read). Grep-to-zero already.
  Nothing to strip; the driver's docstring now SAYS the property so the next
  reader does not go looking. The still-live `expires_at` on `SessionDocument` is
  a session heartbeat, a different field, deliberately kept.
- Rider e's implied dead `delete_case` in that driver - NOT dead:
  `scripts/drive_telemac_leg_4b.py` imports and calls it for its throwaway proof
  Case. Left alone.
- Rider a's "the do_sag composite knows [the derived seed]" - true, but one level
  down: the seed and its `seed_source` are resolved inside
  `model_telemac_river_dye`, which is where the publisher is called from, not in
  the do_sag step wrapper.
