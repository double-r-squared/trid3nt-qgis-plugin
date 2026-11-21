# The SDK front - a case and its models

`trid3nt.case(name)` and a `Model` over one registered template, used from plain Python with no daemon, no
harness and no server imported. The code is `trid3nt/sdk/`; the fill and the run under it are the template
runtime's own (`docs/design/workflows.md`).

## The object

    from trid3nt import case
    c = case("dye-l1")
    m = c.telemac_dye_release(release=(lon, lat), spill_duration_s=1800, DURATION=1800.0)
    m.FRICTION_COEFFICIENT = 35        # checked on set
    form = m.fill()                    # every input's verdict; nothing solved
    result = m.run()                   # a Result, or a Failure

- `case.<template>(**values)` makes a Model over any registered template; each value is set as an attribute is.
- An attribute is a name through the workflow's ONE lookup (`Workflow.input`): a template input by its own
  name, an engine keyword by its identifier or its lowercase alias (`m.friction_coefficient`), the dictionary's
  spaced name by item (`m["FRICTION COEFFICIENT"]`), and a coupled body by its module word
  (`m.waqtel.WATER_DENSITY` is `m["waqtel: WATER_DENSITY"]`; the module words are `Workflow.bodies()`).
- A set runs the input's own accept - `Keyword.accept` (type, choices, arity, bounds, the box, a module's
  lock), `Input.accept` (a value's range), the shape check of a row - and raises that input's typed refusal at
  the line; the value held before stays. `None` unstates a value.
- Reading a name gives the value in force: the model's, else the template's, else the module dictionary's
  own. `repr(m)` lists each with its origin.
- `m.version` is the sha256 of what the model states: its template, inputs and keywords.

## fill and run

- `m.fill()` is the template fill (`Workflow.filled`) over what the model states -> a `Form`: each input's
  `Verdict` (state, value, origin, source, code, reason, remedies), `ready`, and the `(code, sentence)` that
  keeps it from ready. It never raises for an input; a fault in the code still raises.
- `m.run()` fills again and, when ready, runs `Workflow.execute` in this process with no emitter bound: the
  products are written to the object store and recorded as a daemon run's are, and reach no map. It returns a
  `Result` (run id, the typed result, the run's journal record) or a `Failure` (code, sentence, and the form
  where the fill refused).
- The run's record names `template`, `case`, `model` (the template a model was made over) and `version`; a call
  through the daemon records its template and the turn's case, and no model or version. Each recorded output is
  the layer whole as a reader loads it, so a run read back off its record loads as the map would load it.

## No pick

The fill picks nothing (`fill.py::register_rank_pick`). An input the model leaves unstated and the match could
fill refuses `DATA_SOURCE_UNSTATED`, its verdict's remedies the ranked candidates by fetcher name, and nothing
is fetched. A source the model names (`{"source": name}`) is called; one that held nothing refuses the same way,
naming the rest. The harness registers the walk at import - an unstated input takes the top survivor, and the
next where that held nothing - so a chat run picks as it always has.

## The stance

A model is held `unattended` (`trid3nt/inputs/review.py`): every gate proceeds with its inputs labeled, nothing
is drawn, no run refuses `NO_SESSION`, and the person's AUTO setting is never read. What no source states still
refuses where it is read (law 9): a river reach nobody states refuses `REACH_LENGTH_UNSTATED` as AUTO does.

## Out of scope here

The case directory, the lock and pinned data; the form ending at the written deck; coverage and strictness;
copied templates; the generator; studies; remote execution - each a later leg of the SDK plan.
