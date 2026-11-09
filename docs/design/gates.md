# gates -- the one card a person answers before a run

`trid3nt_server/inputs/gate/` is where a turn waits on a person. Its files are
listed in that folder's README; this page says how the one card works.

## One gate machine

`gate_input_review` (`input_review.py`) presents, asks and accepts every
user-gated thing. A caller with a thing of its own under review hands it three
callbacks and nothing else:

- `present` - the round's card: its lines and its sheet of rows.
- `apply_revision` - a reply's edits taken as a change to that thing, after
  which the card is drawn again.
- `blocked` - what keeps a proceed from launching, in words; a proceed while it
  answers anything launches nothing and the card is drawn again.

The mode is the person's alone: the boolean `AUTO` setting (off by default: the run pauses for a person; on: the system proceeds without one),
changed at user level in Settings. No call carries it and no run states it: a call naming
`input_mode` refuses as an unknown argument, and a `settings: AUTO` statement on a run
or a `set_config(scope="run")` refuses by name. Auto launches a ready run at once;
user-gated parks the turn on the card, and with no live session to present on it
refuses by name rather than runs. A gate that must always ask (`set_config`, the key ask,
the inputs card) forces `user_gated` over the setting. A workflow says that refusal at its fill
(`NO_SESSION` on `AUTO`), before any unstated row is fetched or a mesh built; the
gate's own is what a tool with no workflow gets.

## The inputs card

A direct run (`!run <tool>`) of a tool NO workflow stands behind that leaves an
input out, or states one its accept rule refuses, opens the inputs card
(`cards/run_inputs.py`). A workflow opens none: its fill judges every input
inside the run, for a direct run and a model call alike, and a fill that is not
ready refuses by name.

- One row per open input. A row's dropdown lists EXACTLY what that input's
  accept rule takes (`inputs/accept.py`): the case layers the input's own
  ingestion accepts, shown by name and keyed by id - the ingestion of the slot
  the tool annotates the parameter with (`build_mesh`'s `extent: Annotated[Any, EXTENT]`); an enumerated input's own
  values - `build_mesh`'s mesher lists the registered mesher roster; a typed
  input is a text field.
- A pick is a fill of that one input: it goes through the same accept rule and
  the redrawn card shows it taken, or refused with the reason on its row.
- READY is nothing refused and nothing required (no default) missing. Run on a
  ready card is the launch. Run short of ready launches nothing: the card is
  drawn again, its title ending `Run waits on <input> (refused|required)`.
- Cancel launches nothing (`USER_INPUT_CANCELLED`); a card left unanswered past
  its deadline expires as a timeout, never as a decline; six rounds without a
  ready Run refuse the run unlaunched.

## The key card

A keyed source the provider asks a key for - none stored, or the stored one refused (`KEY_REQUIRED`,
`KEY_REJECTED`, read in one place off the provider's own 401/403/498/499 answer) - is asked of the person
on a card (`cards/key_ask.py`) and the call runs once more.

- One row, named by the credential, drawn masked (`ParamSheetRow.secret`: `required` or `rejected`).
  A link shows only when the provider's own answer carried one.
- The plugin keeps the typed key: QgsAuthManager, then `secret-add`, before the reply. The reply is a
  decision with no value, so the key never rides the confirmation, the chat or the model.
- READY is a key held that differs from the refused one; a proceed short of it redraws the card.
- Cancel is a decline (`USER_INPUT_CANCELLED`), not an error. The retry is once: a key refused again
  surfaces as `KEY_REJECTED` to the model, which relays it and never asks for the key in chat.
- A fetch nested in a workflow re-runs under the whole tool call; the cache makes the repeat cheap.

## The mesh on the gate

`tools/mesh/gate.py` builds the round's card for a mesh under construction and
hands it to the same gate: under user-gated the built mesh is presented as an
editable layer with its numeric probes as the card's lines; its rows are the
size word, the recipe ops (read-only - `mesh_op` writes one), the revert and a
hand-edited layer to adopt. Proceed accepts the mesh, a revision rebuilds and
re-presents, cancel refuses the run.

## Invariants

- ONE gate machine. A card contract, a gate loop or a tool surface of a
  caller's own is a defect.
- A DECLINE IS NOT AN ERROR: a cancel raises a `UserDeclinedError` carrying the
  card's own code; the step is marked cancelled, the model is told the card was
  declined, and the tool's retry budget is untouched.
- A TIMEOUT IS NOT A DECLINE: `CONFIRMATION_TIMEOUT`, the step failed, the
  narration says the card expired unanswered.
- The rule that validates an input is the rule that fills its dropdown; nothing
  is listed per tool.
- A key is entered on the key card or in Settings and nowhere else; no tool argument carries one.
- The model never invents physics for an input nothing measures: such an input
  declares no default, and the fill refuses it unstated before any card opens.
