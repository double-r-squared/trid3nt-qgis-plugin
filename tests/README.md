# tests/

The tree mirrors the product by subsystem: a test file lives in the directory
named for the package it asserts against, and a file whose subject is an
instrument lives in `tests/scripts/` rather than beside the product it drives.
`plugin/tests/` and `contracts/tests/` stay inside their own distributions and
join the run as the sixth slice.

| directory | subject | files | collected |
|---|---|---:|---:|
| `_fakes/` | shared doubles: the MCP client, the websocket, the case summary, the reach chain, the read-through injector | - | - |
| `fixtures/` | data the tests read; no code | - | - |
| `adapters/` | provider adapters, the turn loop, the stream persistence | 23 | 468 |
| `credentials/` | credential resolution, the auth handshake, identity | 7 | 95 |
| `emission/` | the emitter, the uri registry, publication, charts | 39 | 503 |
| `fetchers/` | the fetch router, its executors, hooks and fallbacks | 65 | 1563 |
| `gates/` | the gates, the context budget, the circuit breaker | 21 | 312 |
| `mesh/` | the meshers, the mesh gate, topology and bed | 7 | 226 |
| `model/` | the SysML model conformance check | 1 | 19 |
| `plugin/` | the plugin seams the server suite reads offline, by `ast` | 1 | 3 |
| `processing/` | the processing tools | 33 | 460 |
| `runtime/` | the declarative runtime, scenario reuse, the run journal | 12 | 354 |
| `sandbox/` | the code-exec sandbox | 2 | 34 |
| `scripts/` | the instruments, the drivers and the proof renderers | 4 | 39 |
| `search/` | dataset and tool retrieval, the catalog | 20 | 313 |
| `server/` | the HTTP and WS routes, persistence, telemetry | 27 | 559 |
| `solver/` | the solver seam, the run reads, the engine-room posture | 6 | 59 |
| `telemac/` | the TELEMAC templates, authoring and postprocesses | 24 | 429 |
| `tools/` | the registry, the arg normalizer, the tool cache | 14 | 402 |

A directory carries a `conftest.py` only when a fixture lands in it: the root
holds what crosses subsystems (`fake_s3`, `fake_llm`, `empty_registry`, and the
two autouse resets), `telemac/` holds the container-boundary stubs, and nothing
else has one.

## Running it

Six slices by subsystem, each its own foreground invocation, from the repo root:

    make test-fetchers        # tests/fetchers                                 1563
    make test-spatial         # tests/processing tests/emission tests/mesh     1189
    make test-engines         # tests/telemac tests/runtime tests/solver tests/search   1155
    make test-server          # tests/server tests/gates tests/credentials tests/sandbox tests/model tests/scripts   1058
    make test-model-surface   # tests/adapters tests/tools                      870
    make test-packages        # contracts/tests plugin/tests tests/plugin       816

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
