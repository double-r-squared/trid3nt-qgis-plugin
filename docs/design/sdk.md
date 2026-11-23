# The SDK front - a case and its models

`trid3nt.case(path)` and a `Model` over one template declaration, used from plain Python with no daemon, no
harness, no server and no object store. The code is `trid3nt/sdk/`; the fill and the run under it are the template
runtime's own (`docs/design/workflows.md`).

## The object

    from trid3nt import case
    c = case("dye-l1")                 # the directory dye-l1 under the current one, opened or created
    m = c.telemac_dye_release(release=(lon, lat), spill_duration_s=1800, DURATION=1800.0)
    m.FRICTION_COEFFICIENT = 35        # checked on set
    form = m.fill()                    # every input's verdict and the written, checked deck; nothing solved
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
- The model version hashes its template pin, stated inputs, keywords, steps and strictness.

## fill and run

- `m.fill()` is the template fill (`Workflow.filled`) over what the model states and, where it is ready, the
  engine's launch up to its deck (`Workflow.prepared`: the world, the mesh, the settle, the deck written and its
  staged directory checked against itself) -> a `Form`: each input's `Verdict` (state, value, origin, source, code,
  reason, remedies), `ready`, the `(code, sentence)` that keeps it from ready (an input's, or the typed refusal the
  launch stopped on), `deck` (a `Staged`: the run id, the sha256 of every file the engine receives by its name in
  the run directory, one sha256 over them all - the lock's deck hash - and the mesh's content key) and `checked`
  (the staged deck passed the engine's own check; a deck is staged only once it has, and where the check refused
  the refusal's code is `<ENGINE>_STAGED_*`). Nothing is solved. It never raises for an input; a fault in the code
  still raises.
- `m.run()` solves the deck the last fill wrote (`Workflow.execute(state, deck)`: the engine's `solve`, the
  publish, the record) in this process with no emitter bound: the products are written to the case directory and
  recorded as a daemon run's are, and reach no map. It returns a `Result` (run id, the typed result, the run's
  journal record) or a `Failure` (code, sentence, and the form where the fill refused).
- STALENESS. A written deck is solved only while it is current: the model's `version` is the one the fill was made
  at, the model's row of the case's lock (what each input read, the template's version, the mesh key and the deck
  hash) is the one the fill left, and every staged file still hashes to the deck's sha256. No clock is read. A set
  that changes a value changes the version; another fill of the same template in the case rewrites the row. A deck
  that is not current is dropped, its staged copy removed, and the run fills again - mesh, settle and deck - before
  it solves. A deck is solved once: the next run fills again under a new run id. A launch that is not split
  (PEST++'s study) writes its deck inside its solve, so its form carries none.
- The run's record names `template`, `case`, `model` (the template a model was made over) and `version`; a call
  through the daemon records its template and the turn's case, and no model or version. Each recorded output is
  the layer whole as a reader loads it, so a run read back off its record loads as the map would load it.

## The case is a directory

    <case>/
      trid3nt.lock          what every run read, pinned; what each model ran
      data/<sha256>.<ext>   fetched objects and values, supplied files, held Python declarations
      templates/<name>.py   case-owned Python template declarations
      runs/<run_id>/        what a run wrote, in today's formats; runs/run_journal.jsonl the records
      versions/<v>.lock     the lock as it stood when model version <v> ran
      cache/                the fetch cache, the staged decks (cache/<engine>/<run_id>/) and the meshes

- `case(path)` opens or creates the directory; a bare name is that directory under the current one. Its name is
  the `case` its records carry. The directory is the whole truth: a run needs no object store, and the case moves
  whole - every path it records is relative to it. Daemon cases (the document store) are not directories.
- THE FILESYSTEM BACKEND. `trid3nt/store/objects.py` `FileStore` is the object store over one directory: the
  calls every run makes (`put_object`, `get_object`, `head_object`, `list_objects_v2` and its paginator,
  `download_file`, `upload_file`, `delete_object(s)`), `s3://<bucket>/<key>` standing at `<case>/<bucket>/<key>`
  for the case's own buckets `data`, `runs`, `cache`, a missing object raising S3's own `ClientError`. A fill or
  run of a model binds it for its task (`bind_store`, a context variable): `client()` answers with it,
  `runs_bucket()` is `runs`, `cache_bucket()` is `cache`, `work_dir()` is `cache/work`, and the journal is
  `runs/run_journal.jsonl`. The binding crosses into the mesh build's child with the other bound variables, and
  the solve's supervisor thread runs in the launch's context. The daemon binds none and keeps its store.
- THE SOLVE. A local-docker solve already reaches the store only from this process: the launch reads the staged
  manifest and copies each staged input into the run directory, the container sees only that directory
  (`--network none`), and the supervisor writes the outputs and `completion.json` back through the store. So
  under a case the box reads `cache/` and writes `runs/<run_id>/`, and no worker changes.
- A read under `data/` checks the file's sha256 against its name on every read (`DIGEST_MISMATCH`).
- Each record names what it read and wrote by its path relative to the case (`runs/<run_id>/<file>`).

## The lock

- A fetch is read through the lock (`trid3nt/sdk/lock.py`): the router's pin hook (`router.bind_pin`), bound
  by the case, answers every fetch of the task. A new request (its source and arguments, the router's controls
  left out) is fetched once; every object its value names is copied to `data/<sha256>.<ext>` and the value,
  each such uri written as its `data/` copy, is pinned as `data/<sha256>.json`. The value the fill receives is
  read back off the pin, so the first run reads exactly what each replay reads.
- A request the lock holds is answered from its pinned files, each checked against its sha256; a file that is
  gone is fetched again by that request and refused `DIGEST_MISMATCH` where its bytes differ. Nothing is pinned
  by the cache key: the lock pins what was read.
- OUTSIDE DATA. A row stated as a local file, an http(s) URL or an s3 URI is copied into `data/` at the set,
  and the slot gets its case uri (`s3://data/<sha256>.<ext>`). A shapefile, several files, refuses.
- `trid3nt.lock` holds `fetched` (each pin by request key: source, request, uri, sha256, files) and, per
  model identity, the model's row: `template` (name, the sha256 of its template module), `model` (the version), `inputs`
  (per input read off a pinned object: source, request, uri, sha256), and after a run `mesh` (the content key),
  `deck` (the sha256 of each staged file by its name in the run directory, and one over them all) and `images`
  (each engine's image and the id docker held it under, off `completion.json`). The fill writes the pins, the row,
  its mesh and its deck; the run adds the images and keeps the lock under `versions/<version>.lock`. A lock read
  back and written again is the same bytes.
- A moment stated as `now` is the clock of each fill, so a deck that states it is not the same deck twice. A
  TELEMAC geometry file is titled by its mesh's content key (a supplied mesh, which no recipe keys, by its own
  id), so a mesh built again stages the same `domain.slf`. MODFLOW decks carry no FloPy header, which stamps the
  clock of the write.

## Packed and served

- `c.link("s3://bucket/prefix")` uploads every file of the case but `cache/` at its path relative to the case,
  reads each back checked, and writes every pinned uri as its served key; the lock names the store (`served`).
- `c.pack()` brings every pinned file the directory lacks from the served store, each checked against its
  sha256, and writes every uri relative to the case again. A replay of a served case reads a missing pin from the
  store the same way.

## Refusals

- `DIGEST_MISMATCH` - a pinned file whose bytes are not the ones pinned, read in the directory or from the store.
- `ENGINE_IMAGE_MISSING` - the launch asks docker for the engine's image (`LocalSolverSpec.image`) before it
  starts; an image docker does not hold refuses naming it and the command that builds it today. The id of the
  image that ran is on `completion.json`.
- `RUN_UNSTATED` - unattended, an input that takes a finished run of this case, left unstated where the case
  holds several, refuses with the run ids as its remedies; the review card offers them to a person as before. A
  value the fill derives deterministically and a gate offers is still taken, labeled.

## Templates

    c.copy_template("telemac_dye_release")
    copied = c.model("templates/dye_release.py", **values)
    called = c.telemac_dye_release(**values)

Copying writes the registered declaration byte for byte. Edit the copy's
constants, inputs, coverage steps or engine keywords in Python, then load it
with `c.model`. The model holds the source it loaded. A declaration that fails
to load or build its workflow raises `TEMPLATE_INVALID`. Load Python from cases
you trust, as you would any Python module.

To extend a registered declaration, write a module under the case's templates
directory and use `c.model("templates/extension.py", upstream="telemac_dye_release", **values)`.
A `from` import names the upstream declaration module and takes its members
from the case's pin. For example, an extension can import `INPUTS`,
`STEERING`, `OUTPUTS`, placement declarations and the upstream callable,
replace `CAPTIONS`, then call `register_workflow` with its own module and
chosen coercions. A new `STEERING` body extends the engine wrapper directly.
Loading a case workflow leaves the registered tools and search corpus intact.

The case holds each selected Python source under `data/<sha256>.py` and checks
its bytes on replay. A called template and its extensions share an upstream
pin. A copy has its own upstream baseline from the last copy operation and its
own source hash. The lock's model row records both hashes and the called or
copied path. The run record carries `template_path`, `template_sha256` and
`template_upstream_sha256`. A moved case can load its held source after upstream
changes. For a legacy lock whose recorded hash differs from the installed
source, the SDK refuses `TEMPLATE_SOURCE_MISSING` until that source is available
or the caller explicitly takes the installed version.

When upstream changes, fill reports `TEMPLATE_DRIFT` with the name, pinned and
current SHA-256 hashes in pinned_sha256 and current_sha256,
and a structured fix with call, args and diff. A called or extended model's fix
is `c.repin_template(name)`. A copied model's fix gives the case-relative path
to diff and the call `c.copy_template(name)`. A copy with local edits raises `TEMPLATE_COPY_EDITED` unless the call supplies
`overwrite=True`. Make a new model after either operation to use the new
source. With strict off, the fill uses its pinned declaration. With strict on,
the same code refuses before the deck is staged.

## No pick

The fill picks nothing (`fill.py::register_rank_pick`). An input the model leaves unstated and the match could
fill refuses `DATA_SOURCE_UNSTATED`, its verdict's remedies the ranked candidates by fetcher name, and nothing
is fetched. A source the model names (`{"source": name}`) is called; one that held nothing refuses the same way,
naming the rest. The harness registers the walk at import - an unstated input takes the top survivor, and the
next where that held nothing - so a chat run picks as it always has.

## Generating a case script

The harness entry point is `trid3nt_harness.codegen.generate(template, values, form, case_dir=...)`,
where `form` comes from a strict SDK fill with the harness loaded.
`Script` carries `text`, `path`, `checked`, and a typed `refusal`.
The generator supports called templates with a checked staged deck. Each
recorded input needs an SDK declaration. A runtime companion that has no
Model input returns the SDK's refusal before a script is published.

A matched source is written as `m.<input> = {"source": name, **options}`.
The SDK checks explicit options with the registered row's validator at
assignment and applies them to the issued request. The slot supplies omitted
request arguments. A bare `{"source": name}` still uses the slot's full request.
`Form.choices` records the successful requests per input in order, including the
source, its rank among the eligible rows the matcher exposed, candidate count,
and the request options. An ordered bed walk becomes a list of source
declarations. `Form.steps` carries the coverage overrides the run stated.
The generator renders those overrides, including accepts, and uses the template's
declared defaults for the remaining steps.

The generator checks assignments through a fresh Model. It then runs the
candidate file's fill in a fresh interpreter with the harness and server
unavailable and compares its staged deck hash to the reviewed deck. A typed
refusal or deck mismatch leaves no new script. An existing different script
is preserved.

The generated `case.py` uses jupytext percent cells, imports `trid3nt`, and
runs with `python case.py` from its case directory. The main guard keeps
the fill and solve out of the forkserver child. Each run reads the case's
pinned data.

## Scripts from chat

When the final review proceeds, the daemon captures its accepted edits and
source requests. Once the workflow stages its deck, and before it solves, the
daemon checks the generated declarations through the SDK, then starts the saved
script in a fresh interpreter and process group. The child loads the pinned
template and receives the reviewed launch state over a private inherited
channel. Its fill checks the model version, lock row, and staged file hashes
against the reviewed deck. The child solves that deck and records
`CODEGEN_CHECKED` with `checked=True`. A changed handoff refuses by name.

`LOCAL_CORES` bounds the script process's affinity and numeric library
threads. A positive `SOLVE_BUDGET_S` caps its wall time. The daemon forwards
the existing substeps and solve-progress messages and publishes the child's
recorded outputs through the existing layer registry. Stop cancels the
child's solver wait, which stops the container and records the same partial
products. A generator refusal is recorded and the run solves in-process.

The first pair is kept at `$TRID3NT_HOME/cases/<case_id>/case.py` and
`trid3nt.lock`, with a lock snapshot under `versions/`. Every generated pair
is also kept under `runs/<run_id>/` in the case and at the same run prefix in
the runs bucket. Later runs preserve the case's editable pair. The lock pins
the bytes served to the original run. Running the script calls `c.pack()` to
download and verify those pins before filling and solving locally. The
daemon's child uses its verified, staged handoff and the deployment store.

For a point-seeded domain the script keeps the original point and declares the
resolved river or waterbody type with its existing `place_type` field. Handed
banks are pinned through the existing `banks` input. A PEST++ calibration has
no staged deck at this seam and records `CODEGEN_DECK_MISSING`.

In QGIS, right-click an output layer and choose **Download run script** to
copy the pair into a new directory named for the run. The action uses the
existing signed object download.

## The stance

A model is held `unattended` (`trid3nt/inputs/review.py`): every gate proceeds with its inputs labeled, nothing
is drawn, no run refuses `NO_SESSION`, and the person's AUTO setting is never read. What no source states still
refuses where it is read (law 9): a river reach nobody states refuses `REACH_LENGTH_UNSTATED` as AUTO does.

## Coverage and strictness

    m.steps = {"bed": ["merge_surfaces", ("interpolate_surface", {"max_distance": 50})]}
    form = m.fill()
    form.coverage["bed"]        # Coverage: text, shares by source, unmeasured share, steps, shortfall
    form.warnings               # each unresolved Shortfall the fill let run (strict off)
    c = case("dye", strict=True); m.strict = False   # the model's own wins over its case's

- A COVERAGE STEP is declared, never implicit: a name and its arguments (`trid3nt/inputs/coverage.py` `Step`),
  the name being the derive that runs it - `merge_surfaces` (the bed walks the next ranked surface while the water
  is left a wet hole; with no rank walk registered the walk ends at the sources the model named),
  `interpolate_surface` (the holes inside the cut filled by `algorithm` up to `max_distance` pixels, each unstated
  argument the setting in force, `FILL_ALGORITHM` / `FILL_MAX_DISTANCE`) - or `accept` naming a shortfall's code.
  A slot states the steps it runs (`Slot.steps`; the bed's are those three); any other refuses at the set.
- THE TEMPLATE STATES ITS DEFAULT STEPS on its row (`Input(BED).need("bathymetry").steps("merge_surfaces",
  "interpolate_surface")`), so a bare template call and every daemon run lay the bed as before (a TELEMAC deck
  filled from a raster bed is byte-identical to the one before the steps were declared). The engine and the
  framework state none. A GeoClaw bed, an engine laying its own ladder, states `interpolate_surface`, which
  interpolates a layer of soundings and never a raster the engine lays whole. `m.steps = {input: [...]}` states a
  model's own over its template's, `()` none at all; the steps are part of the model's version.
- THE OPERATIONS ARE FUNCTIONS. `merge_surfaces(layers, region=None, name=None)` and
  `interpolate_surface(layer, region=None, algorithm=None, max_distance=None, name=None)` are registered derive
  tools (`trid3nt/tools/derive/`), callable from a script inside `c.bound()` as from chat: each returns a two-band
  raster whose band 2 names each cell's source (`FILLED` for an interpolated cell), with the share each source
  reached and the share nothing reached. The bed slot lays its surfaces through the same code.
- COVERAGE AS DATA. Each input whose ingestion measures its coverage reports a `Coverage` on the form: the
  coverage lines the ingestion already says (on the card and the journal), the share each source valued over the
  water (the grid where the domain was cut with no polygon), the share nothing valued, the steps in force, and the
  `Shortfall` its declared steps left: `code` (`BED_WET_HOLE`), `cells`, `share`, `farthest_px` (from the
  farthest unvalued cell to a valued one) and `remedies`, each a `Step` - a `merge_surfaces` per ranked source
  the bed has not laid (by `source`), the `interpolate_surface` that reaches `farthest_px` where none declared
  reaches it, and the `accept` naming the code. No sentence is composed for a remedy.
- STRICT. A shortfall is RESOLVED when the steps declared answer it (nothing is left short) or an `accept` names
  its code. `strict=False` (the SDK's default): an unresolved shortfall is a warning on the form and the run goes
  ahead where the engine can. `strict=True` (the harness registers it at import, so every daemon run): it refuses
  `BED_WET_HOLE` as before, the shortfall and its remedies on the form. A node the engine would carry no value at is
  not a strictness level: the mesh refuses `MESH_BED_UNPAINTED` under both. The record names `strict`.

## Out of scope here

Chat dispatch through the generated script; generation for copies and extensions; a fit of the domain to measured water
(`fit_reach`) and a channel-aligned interpolation, neither in the tree; studies;
remote execution; a case's own document store (a finished run of an SDK case is not offered as a run input) - each
a later leg of the SDK plan.
