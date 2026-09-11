# tests/

The tree mirrors the product by subsystem: a test file lives in the directory
named for the package it asserts against, and a file whose subject is a dev
tool lives in `tests/scripts/` rather than beside the product it drives.
`plugin/tests/` and `contracts/tests/` stay inside their own distributions and
join the run as the sixth slice.

| directory | subject | files | collected |
|---|---|---:|---:|
| `_fakes/` | shared doubles: the MCP client, the websocket, the case summary, the reach chain, the read-through injector | - | - |
| `fixtures/` | data the tests read; no code | - | - |
| `adapters/` | provider adapters, the message IR, the turn loop, the stream persistence | 22 | 291 |
| `credentials/` | credential resolution, the auth handshake, identity | 7 | 95 |
| `derive/` | the derive tools | 33 | 460 |
| `emission/` | the emitter, the uri registry, publication, charts | 38 | 493 |
| `fetchers/` | the fetch router, its executors, hooks and fallbacks | 65 | 1561 |
| `gates/` | the gates, the context budget, the circuit breaker | 21 | 311 |
| `inputs/` | the typed inputs: a Point, an Extent, a Shape, each from every form it arrives in, and the AOI acquired from any of them | 4 | 39 |
| `mesh/` | the meshers, the mesh gate, topology and bed | 7 | 226 |
| `model/` | the SysML model conformance check | 1 | 19 |
| `plugin/` | the plugin seams the server suite reads offline, by `ast` | 1 | 3 |
| `publishing/` | the one publisher: a field to a layer, a series or a profile to a chart, a field over time to an animation, a track to a vector layer, a series at a station to the point layer carrying it; the rasterizers and the COG seam | 3 | 38 |
| `runtime/` | the declarative runtime, the run journal | 9 | 309 |
| `sandbox/` | the code-exec sandbox | 2 | 34 |
| `scripts/` | the dev instruments, the live-run harness and the proof renderers, skipped when `dev/` is absent | 6 | 45 |
| `search/` | dataset and tool retrieval, the OGC adapter | 18 | 280 |
| `server/` | the HTTP and WS routes, dispatch reuse, persistence, telemetry | 27 | 559 |
| `solver/` | the solver seam, the run reads, the engine-room posture | 6 | 59 |
| `telemac/` | the TELEMAC templates, the module surface and its primitives, authoring and the open-water postprocesses | 24 | 450 |
| `tools/` | the registry, the arg normalizer, the tool cache | 14 | 402 |

| file | what it is |
|---|---|
| `conftest.py` | The fixtures that cross subsystems, and the two autouse resets. |

A directory carries a `conftest.py` only when a fixture lands in it: the root
holds what crosses subsystems (`fake_s3`, `fake_llm`, `empty_registry`, and the
two autouse resets), `telemac/` holds the container-boundary stubs, and nothing
else has one.

## Running it

Six slices by subsystem, each its own foreground invocation, from the repo root:

    make test-fetchers        # tests/fetchers                                 1561
    make test-spatial         # tests/derive tests/emission tests/mesh tests/publishing   1217
    make test-engines         # tests/telemac tests/runtime tests/inputs tests/solver tests/search   1137
    make test-server          # tests/server tests/gates tests/credentials tests/sandbox tests/model tests/scripts   1078
    make test-model-surface   # tests/adapters tests/tools                      667
    make test-packages        # contracts/tests plugin/tests tests/plugin       821

The prose guards - history markers, dead references, the package maps, the
template pages, banner comments - are LINTS rather than tests: they read the
tree rather than the product's behaviour, so they live in `dev/lint/` and run
with `make lint`, which skips itself on a clone that carries no `dev/`.

`make test` runs all six in order. Each target expands to

    env -u TRID3NT_CACHE_BUCKET venvs/agent/bin/python -m pytest <dirs> \
        -p no:cacheprovider --timeout=300 -q

with the paths unquoted so the shell expands them, `TRID3NT_CACHE_BUCKET` unset
so no test can reach a live cache bucket, and the cache provider off so a slice
leaves nothing behind. The baseline is **all six slices, zero failures**.

## Standing exceptions

A test tests a product behavior, never the harness. Nine tests in
`plugin/tests/` break that rule and are named here rather than quietly
tolerated: Qt cannot be imported in-process alongside the server suite, so the
product assertions live inside a `qt_*_harness.py` subprocess and the
pytest-visible test asserts only that the harness exited 0 and printed its
marker. A harness that stops asserting still exits 0 and still prints its
marker, so the shim stays green forever.

Eight carry the `qt_harness_shim` marker (`pytest -m qt_harness_shim` lists
them):

| test | marker it reads |
|---|---|
| `test_tool_picker.py::TestToolPickerQt` | `TOOL-PICKER-OK` |
| `test_charts.py::TestChartsWindow` | `CHARTS-OK` |
| `test_dock_ui.py::TestDockUiBatch` | `DOCK-UI-OK` |
| `test_case_bbox.py::TestCaseBboxDock` | `CASE-BBOX-OK` |
| `test_mesh_temporal.py::TestQtMeshTemporalAndDeclaredStyle` | `QT-MESH-TEMPORAL-OK` |
| `test_qt_bridge.py::TestQtBridgeStart` | `QT-BRIDGE-OK` |
| `test_remote_endpoints.py::TestRemoteEndpointsDock` | `REMOTE-ENDPOINTS-OK` |
| `test_provider_config.py::TestDockProviderConfigWiring` | `SAVE_PAYLOAD_OK` + `MODEL_REPOPULATE_OK` |

The ninth the ruling names, `test_install_dependencies.py`, carries NO marker:
read end to end it drives no Qt harness - its `TestMain` cases assert the
product's own return codes over a mocked `subprocess.run`. It is a product test
and is not an exception.

The reshape - each harness printing `RESULT <name> PASS|FAIL` per assertion, the
shim reporting one pytest case per line and failing when an expected name is
absent - is its own change.
