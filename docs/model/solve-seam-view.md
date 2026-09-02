# SolveSeam - derived view

GENERATED from `docs/model/solve-seam.sysml` by `scripts/model_check.py --view`. Never hand-edited: regenerate it, and `tests/test_model_conformance.py` fails while it is stale.

## Blocks and flows

```mermaid
flowchart LR
    deckAuthor["DeckAuthor<br/>trid3nt_server/workflows/telemac/steps/deck.py"]
    diagnosticsReader["DiagnosticsReader<br/>trid3nt_server/workflows/solver/diagnostics/telemac.py"]
    launcherArm["LauncherArm<br/>trid3nt_server/workflows/telemac/run_telemac.py"]
    manifestStager["ManifestStager<br/>trid3nt_server/workflows/telemac/steps/open_water.py"]
    meshAcceptance["MeshAcceptance<br/>trid3nt_server/workflows/mesh/step.py"]
    packetAssembler["PacketAssembler<br/>scripts/assemble_proof_packet.py"]
    runReader["RunReader<br/>trid3nt_server/workflows/telemac/steps/run_reads.py"]
    solveStep["SolveStep<br/>trid3nt_server/workflows/telemac/steps/solve.py"]
    supervisor["Supervisor<br/>trid3nt_server/workflows/solver/solver.py"]
    telapyChild["TelapyChild<br/>workers/telemac/entrypoint.py"]
    topologyWriter["TopologyWriter<br/>trid3nt_server/workflows/mesh/topology.py"]
    workerEntrypoint["WorkerEntrypoint<br/>workers/telemac/entrypoint.py"]
    deckAuthor -- "ManifestCaseSection" --> manifestStager
    manifestStager -- "ManifestCaseSection" --> workerEntrypoint
    solveStep -- "WorkerCompletionRecord" --> packetAssembler
    solveStep -- "WorkerCompletionRecord" --> diagnosticsReader
    launcherArm -- "WorkerCompletionRecord" --> supervisor
    supervisor -- "WorkerCompletionRecord" --> solveStep
    workerEntrypoint -- "CaseEcho" --> launcherArm
    deckAuthor -- "CaseEcho" --> workerEntrypoint
    workerEntrypoint -- "SolverListing" --> diagnosticsReader
    workerEntrypoint -- "SolverListing" --> runReader
    telapyChild -- "SolverListing" --> workerEntrypoint
    meshAcceptance -- "AcceptedMeshRecord" --> deckAuthor
    workerEntrypoint -- "WorkerCompletionRecord" --> launcherArm
    topologyWriter -- "TopologyBundle" --> deckAuthor
```

## Interface items

### `AcceptedMeshRecord`

The recipe-frozen artifact's fields the deck consumes. The granularity the deck records is the one the ACCEPTED mesh was built at, measured on its own cells - never the edge that was asked for.

| item | type | required |
| --- | --- | --- |
| `artifact` | MeshArtifact | required |
| `mesh_id` | String | required |
| `slf_uri` | Uri | required |
| `cli_uri` | Uri | required |
| `topology_uri` | Uri | required |
| `display_uri` | Uri | required |
| `node_count` | Integer | required |
| `element_count` | Integer | required |
| `min_edge_m` | Real | required |
| `provenance` | Map | required |

### `CaseEcho`

What the SERVER already knows and the container cannot learn from the files it is handed. The worker copies it into its report VERBATIM: a fact re-derived in the container is a second answer free to disagree with the first.

| item | type | required |
| --- | --- | --- |
| `utm_epsg` | Integer | required |
| `bbox` | RealList | required |
| `npoin` | Integer | required |
| `nelem` | Integer | required |
| `mesh_size_m` | Real | required |
| `bed_source` | String | required |
| `result_slf` | FileName | required |

### `ManifestCaseSection`

The CASE a worker runs: which engine, which deck it reads, and which files must exist for the run to have happened. The section key is what the entrypoint dispatches on, and the strict gate refuses any key outside this list.

| item | type | required |
| --- | --- | --- |
| `module` | String | required |
| `steering` | FileName | required |
| `results` | FileNameList | required |
| `family` | String | required |
| `echo` | EchoMap | required |
| `user_fortran` | DirName | optional |
| `coupling` | String | optional |
| `continue_from` | FileName | optional |

### `SolverListing`

The solver's own listing, teed off the child's stdout. It is the run's evidence: every closure a run narrates is parsed out of it rather than recomputed from the fields.

| item | type | required |
| --- | --- | --- |
| `full_listing` | FileName | required |

### `TopologyBundle`

The mesher's answers a SELAFIN cannot hold. A bundle naming no liquid boundary refuses, because a deck cannot be authored against a boundary with no role on it.

| item | type | required |
| --- | --- | --- |
| `roles` | RoleMap | required |
| `liquid_boundary_order` | StringList | required |

### `WorkerCompletionRecord`

The run's only report, written whatever the child did. The launcher arm folds the declared subset of it into the run's completion object, so a reader carries the physics without a second object read. The failure-path listing excerpt the worker adds to this record is NOT modeled as a promised key: no product consumer reads it, and the fold does not carry it.

| item | type | required |
| --- | --- | --- |
| `status` | String | required |
| `correct_end` | Boolean | required |
| `run_id` | String | required |
| `module` | String | required |
| `family` | String | required |
| `wall_s` | Real | required |
| `error` | String | optional |
| `error_code` | String | optional |
| `ntimestep` | Integer | optional |

## Requirements

| requirement | satisfied by | verified by |
| --- | --- | --- |
| **CorrectEndIsTheSuccessConvention** | `workerEntrypoint`, `launcherArm` | `workers/telemac/test_entrypoint.py::test_a_clean_exit_that_wrote_no_result_is_not_a_solve`<br/>`tests/test_run_telemac_chain.py::test_classify_exit_clean_exit_but_no_correct_end_is_error` |
| **EchoDoctrine** | `deckAuthor`, `workerEntrypoint` | `workers/telemac/test_entrypoint.py::test_a_clean_child_that_wrote_its_results_is_the_run_succeeding`<br/>`workers/telemac/test_entrypoint.py::test_the_frame_count_is_measured_off_the_file_the_echo_names`<br/>`workers/telemac/test_entrypoint.py::test_an_unreadable_result_leaves_ntimestep_ABSENT_not_zero` |
| **EmptyResultsRefuses** | `workerEntrypoint` | `workers/telemac/test_entrypoint.py::test_a_case_declaring_no_results_refuses` |
| **MetricsAlways** | `workerEntrypoint` | `workers/telemac/test_entrypoint.py::test_a_child_that_dies_still_leaves_the_metrics_written` |
| **ReadersNeverImportTheWorker** | `runReader`, `diagnosticsReader` | `tests/test_model_conformance.py::test_the_solve_seam_model_conforms` |
| **SolveTimeoutTypesNotHangs** | `workerEntrypoint` | `workers/telemac/test_entrypoint.py::test_the_solve_bound_defaults_to_a_day_and_the_knob_states_it`<br/>`workers/telemac/test_entrypoint.py::test_a_child_that_outruns_the_bound_is_killed_and_still_reports` |
| **StrictGateRefusesUnknownFields** | `workerEntrypoint` | `workers/telemac/test_entrypoint.py::test_the_gate_refuses_an_unknown_key_and_names_the_parser`<br/>`workers/telemac/test_entrypoint.py::test_a_case_with_an_unknown_field_refuses` |
| **WorkersNeverImportServer** | `workerEntrypoint`, `telapyChild` | `tests/test_model_conformance.py::test_the_solve_seam_model_conforms` |

## What each requirement says

- **CorrectEndIsTheSuccessConvention** - Success is a clean child exit AND every declared result file on disk. A solver that returns zero without writing its result has not solved anything, and the exit code alone never decides it - on either side of the seam.
- **EchoDoctrine** - Server-known facts are stated by the deck, copied by the worker VERBATIM, and never re-derived in the container. Worker-measured facts are the worker's own: the frame count is measured off the file the echo names, and an unmeasurable result is the ABSENCE of the key rather than a zero.
- **EmptyResultsRefuses** - A case declaring no results collapses the success convention back to the exit code alone, which is the convention this seam retired. It refuses instead.
- **MetricsAlways** - The run report is written whatever the child does. A Fortran STOP kills the process it runs in, and the report is the only channel the server has for reading what went wrong, so the write outlives the solve.
- **ReadersNeverImportTheWorker** - What a solved run says is read from the artifacts the supervisor uploaded. A reader importing worker code is a second computation of the same quantity, running outside the image that produced it.
- **SolveTimeoutTypesNotHangs** - A wedged solver is a typed report, not a container that never exits. The bound is stated by an environment knob, and an expiry names that knob in the error it writes.
- **StrictGateRefusesUnknownFields** - A dropped key silently no-ops the knob the caller meant to set. The gate refuses instead, and names the parser stamp so a stale image reads as a drifted version rather than as a knob that did nothing.
- **WorkersNeverImportServer** - The container is the engine room: a worker that reaches into the server package has an opinion, a default or a fetch in it.
