# tests/

The tree mirrors the product by subsystem: a test file lives in the directory
named for the package it asserts against, and a file whose subject is a dev
tool lives in `tests/scripts/` rather than beside the product it drives.
`tests/contracts/` and `tests/plugin/` assert the two distributions beside the
rest and make up the sixth slice.

| directory | subject | files | collected |
|---|---|---:|---:|
| `_fakes/` | shared doubles: the MCP client, the websocket, the case summary, the reach chain, the read-through injector | - | - |
| `fixtures/` | data the tests read; no code | - | - |
| `adapters/` | provider adapters, the message IR, the turn loop, the stream persistence | 23 | 274 |
| `contracts/` | the typed wire and registry contracts, the schema mirror and its drift | 16 | 351 |
| `credentials/` | credential resolution, the access-token gate and the connect handshake | 4 | 50 |
| `derive/` | the derive tools, the two session tools | 3 | 51 |
| `render/` | the emitter, the uri registry, publication, the format set, charts | 35 | 431 |
| `fetchers/` | the fetch router, its executors, hooks and fallbacks | 82 | 2294 |
| `gates/` | the gates, a declined card at each of them, the code-exec approval gate, the context budget, the circuit breaker | 20 | 303 |
| `inputs/` | the typed inputs: a Point, an Extent, a Shape, the domain with the companions its producer measured, the bed it reads as elevations on the run's own vertical frame, the boundary runs, the line a placed read follows, the observation a run opens on and the weather, rain and sea state a record measured, each from every form it arrives in, the user-input normalizers under them, and a user's own file adopted as a layer | 25 | 319 |
| `mesh/` | the meshers, the mesh gate, topology, the bed over the one source it takes, the runs that prescribe a role and the shoreline the domain's own edge is | 11 | 280 |
| `model/` | the SysML model conformance check | 1 | 23 |
| `plugin/` | the QGIS dock: the client, the Qt bridge, the cards, the render seams; the Qt-wiring subset runs in a subprocess harness | 39 | 403 |
| `runtime/` | the template runtime, its engine-neutral slots and dictionary marks, the run journal, the optional need | 19 | 241 |
| `scripts/` | the dev instruments, the live-run harness and the proof renderers, skipped when `dev/` is absent | 12 | 178 |
| `search/` | dataset and tool retrieval, the OGC adapter, the keyword dictionary read | 13 | 231 |
| `server/` | the HTTP and WS routes, the fetch short-circuit and the Case AOI, persistence | 29 | 261 |
| `solver/` | the executor, the run reads, the engine-room posture, the MODFLOW engine, its authoring and staged check, the GeoClaw engine, its module wrapper, its .data authoring, its grid recipe, its topofiles, its Fault dtopo and its fgmax, fgout and gauge outputs, the plume template, its body wrappers, the SFR keywords and Streams composite, the LAK keywords and Lakes composite, the MAW keywords and MultiAquiferWells composite, and the UZF keywords and UnsaturatedZone composite, and their period settings, the WaterMover and Diversions composites with the cross-section and lake tables, the Interbeds and TimeSeries composites with the boundary givers and the compaction reads, the Observations composite with the OBS files and charts, and the Storm composite and the storm-surge template, and the GeoClaw template's pieces (the SIFT row, the SiftFault, FiniteFault (static or kinematic, with the event-id fetch and the rupture-following flagregion window), SlabFault (a stated rectangle laid on the Slab2 interface), MomentTensorFault (a tensor sized by a named published scaling relation, Wells and Coppersmith or Thingbaijam), FixedGrids and InitialSurface composites, the staged case, the model exchanges: the LocalGridRefinement child, its exchange and transport, WaterMover across models and the sheet-stated DISU, and particle tracking: the ParticleTracking body, the Release composite and the pathline reads, and the GeoClaw restart from a checkpoint, and the Boussinesq module) | 46 | 1088 |
| `telemac/` | the TELEMAC templates, the module surface, what each module writes and the primitives that read it, the listing reads, authoring | 61 | 1001 |
| `tools/` | the registry, the arg normalizer, the tool cache | 10 | 343 |

| file | what it is |
|---|---|
| `conftest.py` | The fixtures that cross subsystems, and the two autouse resets. |
| `door_client.py` | One request driven through the HTTP doors over a real socket, on an ephemeral port. |
| `seated.py` | A whole declared sheet seated off a supplied mapping, row by row through the fill's own `_answer`. |

A directory carries a `conftest.py` only when a fixture lands in it: the root
holds what crosses subsystems (`fake_s3`, `fake_llm`, `empty_registry`, and the
two autouse resets), `telemac/` holds the container-boundary stubs, `contracts/` holds the contract
instance fixtures, and nothing else has one.

## Running it

Six slices by subsystem, each its own foreground invocation, from the repo root:

    make test-fetchers        # tests/fetchers                                                                              2294
    make test-spatial         # tests/derive tests/render tests/mesh                                         762
    make test-engines         # tests/telemac tests/runtime tests/solver tests/search                                      2563
    make test-server          # tests/server tests/inputs tests/gates tests/credentials tests/model tests/scripts       1139
    make test-model-surface   # tests/adapters tests/tools                                                                   616
    make test-packages        # tests/contracts tests/plugin                                                                  754

The source-text guards - history markers, dead references, the package maps,
the template pages, banner comments, import walls, wiring pins, retired names,
the plugin's Qt spellings - are LINTS rather than tests: they read the tree
rather than the product's behaviour, so they live in `dev/lint/` and run with
`make lint`, which skips itself on a clone that carries no `dev/`.

`make test` runs all six in order. Each target expands to

    env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest <dirs> \
        -p no:cacheprovider --timeout=300 -q

with the paths unquoted so the shell expands them, `TRID3NT_CACHE_BUCKET` unset
so no test can reach a live cache bucket, and the cache provider off so a slice
leaves nothing behind. The baseline is **all six slices, zero failures**.

## Standing exceptions

A test tests a product behavior, never the harness. Ten harness classes in
`tests/plugin/` break that rule and are named here rather than quietly
tolerated: Qt cannot be imported in-process alongside the server suite, so the
product assertions live inside a `qt_*_harness.py` subprocess and the
pytest-visible test asserts only that the harness exited 0 and printed its
marker. A harness that stops asserting still exits 0 and still prints its
marker, so the shim stays green forever.

Their eighteen tests carry the `qt_harness_shim` marker (`pytest -m
qt_harness_shim` lists them):

| test | marker it reads |
|---|---|
| `test_tool_picker.py::TestToolPickerQt` | `TOOL-PICKER-OK` |
| `test_charts.py::TestChartsWindow` | `CHARTS-OK` |
| `test_dock_ui.py::TestDockUiBatch` | `DOCK-UI-OK` |
| `test_case_bbox.py::TestCaseBboxDock` | `CASE-BBOX-OK` |
| `test_mesh_temporal.py::TestQtMeshTemporalAndDeclaredStyle` | `QT-MESH-TEMPORAL-OK` |
| `test_layer_rename.py::TestQtLayerRename` | `QT-LAYER-RENAME-OK` |
| `test_qt_bridge.py::TestQtBridgeStart` | `QT-BRIDGE-OK` |
| `test_remote_endpoints.py::TestRemoteEndpointsDock` | `REMOTE-ENDPOINTS-OK` |
| `test_provider_config.py::TestDockProviderConfigWiring` | `SAVE_PAYLOAD_OK` + `MODEL_REPOPULATE_OK` |
| `test_processing.py::TestProcessingInQgis` | the three `[processing]` lines: an algorithm over a canvas layer by name, a snippet, a raising snippet |

The eleventh the ruling names, `test_install_dependencies.py`, carries NO marker:
read end to end it drives no Qt harness - its `TestMain` cases assert the
product's own return codes over a mocked `subprocess.run`. It is a product test
and is not an exception.

The reshape - each harness printing `RESULT <name> PASS|FAIL` per assertion, the
shim reporting one pytest case per line and failing when an expected name is
absent - is its own change.
