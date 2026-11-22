# The SDK front - a case and its models

`trid3nt.case(path)` and a `Model` over one registered template, used from plain Python with no daemon, no
harness, no server and no object store. The code is `trid3nt/sdk/`; the fill and the run under it are the template
runtime's own (`docs/design/workflows.md`).

## The object

    from trid3nt import case
    c = case("dye-l1")                 # the directory dye-l1 under the current one, opened or created
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
  products are written to the case directory and recorded as a daemon run's are, and reach no map. It returns a
  `Result` (run id, the typed result, the run's journal record) or a `Failure` (code, sentence, and the form
  where the fill refused).
- The run's record names `template`, `case`, `model` (the template a model was made over) and `version`; a call
  through the daemon records its template and the turn's case, and no model or version. Each recorded output is
  the layer whole as a reader loads it, so a run read back off its record loads as the map would load it.

## The case is a directory

    <case>/
      trid3nt.lock          what every run read, pinned; what each model ran
      data/<sha256>.<ext>   every byte a run read: each fetched object, each supplied file, each fetched value
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
  template, the model's row: `template` (name, the sha256 of its template module), `model` (the version), `inputs`
  (per input read off a pinned object: source, request, uri, sha256), and after a run `mesh` (the content key),
  `deck` (the sha256 of each staged file by its name in the run directory, and one over them all) and `images`
  (each engine's image and the id docker held it under, off `completion.json`). The fill writes the pins and the
  row; the run adds what it ran and keeps the lock under `versions/<version>.lock`. A lock read back and written
  again is the same bytes.
- A moment stated as `now` is the clock of each fill, so a deck that states it is not the same deck twice. A
  TELEMAC mesh built again is the same mesh under a new id its SELAFIN title carries, so a replay that rebuilds
  it stages another `domain.slf`; the case's `cache/` keeps the mesh a replay reuses. MODFLOW decks carry no
  FloPy header, which stamps the clock of the write.

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

The form ending at the written deck; coverage and strictness; copied templates; the generator; studies; remote
execution; a case's own document store (a finished run of an SDK case is not offered as a run input) - each a
later leg of the SDK plan.
