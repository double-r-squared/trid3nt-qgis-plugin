# Writing a tool

New tools are rare. A tool is a `fetch` or a `derive`, and most needs are
already answered by one of those or by the user's QGIS session.

## Before writing one

1. **The QGIS test.** If a QGIS algorithm answers the question, there is no
   tool of ours: the model runs it in the user's session through
   `run_qgis_algorithm`, and a composed analysis through `run_pyqgis`.
2. **A fetcher is a spec row, not code.** Data from a source is DECLARED as a
   `source.yaml` beside a `corpus.yaml` under `trid3nt_server/tools/fetchers/`;
   the router promotes the row into a registered tool. See
   `trid3nt_server/tools/README.md`.
3. **The derive law: a derive never fetches.** A derive takes a layer or a
   value and gives one back, useful on its own outside any template. The fetch
   that declares the data runs first; the derive refuses, naming that fetch,
   when the layer was not given. `dev/lint/derive_fetches.py` refuses a
   registry lookup, a `read_through` or a fetcher import under `tools/derive`.

## The one real example

Read `trid3nt_server/tools/derive/merge_rasters/` whole. It is a directory of
three files - `merge_rasters.py`, `corpus.yaml`, `__init__.py` - and shows
every seam a derive touches:

- the function under `@register_tool` with its `AtomicToolMetadata`, its
  LLM-facing docstring front-loaded with routing (the adapter keeps the first
  1000 characters);
- the eager import in `trid3nt_server/tools/__init__.py` that makes the
  decorator fire at startup;
- the practitioner phrasings in `corpus.yaml` that retrieval ranks it by;
- a refusal that names the missing input instead of guessing it, and a
  layer result that carries its provenance.

Its test lives under `tests/derive/`, mirroring the product tree.
