# ToolPlane - derived view

GENERATED from `docs/model/tool-plane.sysml` by `scripts/model_check.py --view`. Never hand-edited: regenerate it, and `tests/model/test_model_conformance.py` fails while it is stale.

Plane: **tool**. System: **session-code**. One seam of the system of systems indexed by [`README.md`](README.md) - never the whole picture.

## Blocks and flows

```mermaid
flowchart LR
    approvalCard["ApprovalCard<br/>plugin/ui/gate.py"]
    codeExecGate["CodeExecGate<br/>trid3nt_server/gates/confirm.py"]
    connectionLoop["ConnectionLoop<br/>trid3nt_server/server/protocol/loop.py"]
    keywordLookup["KeywordLookup<br/>trid3nt_server/workflows/telemac/modules/describe.py"]
    runPyqgis["SessionCodeTool<br/>trid3nt_server/tools/derive/run_pyqgis/run_pyqgis.py"]
    runQgisAlgorithm["SessionAlgorithmTool<br/>trid3nt_server/tools/derive/run_qgis_algorithm/run_qgis_algorithm.py"]
    sessionExecutor["SessionExecutor<br/>plugin/render/processing.py"]
    sessionRequest["SessionRequest<br/>trid3nt_server/server/processing.py"]
    toolDispatch["ToolDispatch<br/>trid3nt_server/server/dispatch/emitter.py"]
    runQgisAlgorithm -- "SessionAsk" --> sessionRequest
    codeExecGate -- "GrantedApproval" --> runPyqgis
    toolDispatch -- "StrippedApproval" --> codeExecGate
    codeExecGate -- "CodeApprovalCard" --> approvalCard
    runPyqgis -- "SessionAsk" --> sessionRequest
    sessionRequest -- "ProcessingRequest" --> sessionExecutor
    sessionExecutor -- "ProcessingResponse" --> connectionLoop
    connectionLoop -- "ProcessingResponse" --> sessionRequest
```

## Interface items

### `CodeApprovalCard`

The card the user reads: the id the decision is keyed by, the exact code, and its caption. No layer list: the session's own project is the surface, and the user is looking at it.

| item | type | required |
| --- | --- | --- |
| `code_exec_id` | String | required |
| `python_code` | String | required |
| `rationale` | String | optional |

### `GrantedApproval`

What the gate injects on the user's ``proceed``: the approval and the id of the card it was given on, which the tool carries to the session so the outcome joins that card.

| item | type | required |
| --- | --- | --- |
| `confirmed` | Boolean | required |
| `code_exec_id` | String | required |

### `ProcessingRequest`

The envelope on the wire: one request the session runs, keyed by an unguessable id the answer echoes.

| item | type | required |
| --- | --- | --- |
| `request_id` | String | required |
| `kind` | String | required |
| `algorithm` | String | optional |
| `params` | Map | required |
| `code` | String | optional |
| `code_exec_id` | String | optional |

### `ProcessingResponse`

What the session produced. ``status`` is the honest terminal outcome; an ``error`` is the session's own traceback, never a message this side wrote about it.

| item | type | required |
| --- | --- | --- |
| `request_id` | String | required |
| `status` | String | required |
| `result` | Map | optional |
| `error` | String | optional |
| `stdout` | String | optional |

### `SessionAsk`

What a tool hands the request seam: the kind of thing to run and its body - an algorithm id with its params, or the approved code.

| item | type | required |
| --- | --- | --- |
| `kind` | String | required |
| `algorithm` | String | optional |
| `params` | Map | optional |
| `code` | String | optional |
| `code_exec_id` | String | optional |

### `StrippedApproval`

What the dispatch removes from a model-issued call before the gate sees it: the approval flag and the card id, so only the gate can put them back.

| item | type | required |
| --- | --- | --- |
| `confirmed` | Boolean | required |
| `code_exec_id` | String | required |

## Requirements

| requirement | satisfied by | verified by |
| --- | --- | --- |
| **ADeriveToolNeverFetches** | `runPyqgis`, `runQgisAlgorithm` | `tests/derive/test_hydrology_primitives.py::test_bad_inputs_raise`<br/>`tests/derive/test_compute_flood_depth_damage.py::test_missing_assets_is_a_refusal_never_a_fetch`<br/>`tests/derive/test_compute_sediment_yield.py::test_missing_layers_refuse_never_fetch`<br/>`tests/derive/test_model_debris_flow.py::test_missing_layers_refuse_never_fetch`<br/>`tests/derive/test_compute_model_residuals.py::test_missing_observations_refuse_never_fetch`<br/>`tests/derive/test_compute_exposure_summary.py::test_absent_layers_are_named_never_fetched`<br/>`tests/derive/test_query_point_hazard.py::test_place_name_is_not_geocoded_here`<br/>`tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree` |
| **ASessionAnswersOrRefuses** | `sessionRequest`, `connectionLoop` | `tests/derive/test_session_tools.py::test_no_session_is_a_typed_refusal`<br/>`tests/derive/test_session_tools.py::test_a_wait_that_runs_out_is_typed_and_cleans_up`<br/>`tests/derive/test_session_tools.py::test_an_error_reply_is_the_sessions_own_message`<br/>`tests/derive/test_session_tools.py::test_a_cross_session_reply_is_refused` |
| **ASurfaceTooLargeToCarryIsReached** | `keywordLookup` | `tests/search/test_describe_keywords.py::test_a_question_in_words_reaches_the_keyword_that_answers_it`<br/>`tests/search/test_describe_keywords.py::test_a_match_carries_the_dictionary_s_own_help_choices_and_default`<br/>`tests/search/test_describe_keywords.py::test_the_same_question_is_answered_the_same_way_twice`<br/>`tests/search/test_describe_keywords.py::test_a_module_with_no_catalog_refuses_naming_the_ones_there_are`<br/>`tests/search/test_describe_keywords.py::test_every_corpus_phrasing_surfaces_the_tool_model_free` |
| **ConsequentialCodeIsUserGated** | `runPyqgis`, `codeExecGate`, `toolDispatch` | `tests/derive/test_session_tools.py::test_run_pyqgis_refuses_without_the_card`<br/>`tests/gates/test_code_exec_gate.py::test_dispatch_strips_a_model_supplied_approval`<br/>`tests/gates/test_code_exec_gate.py::test_approve_injects_confirmed_and_the_card_carries_the_code`<br/>`tests/gates/test_code_exec_gate.py::test_cancel_blocks_dispatch_and_leaks_nothing`<br/>`tests/gates/test_code_exec_gate.py::test_an_unanswered_card_expires_typed`<br/>`tests/gates/test_code_exec_gate.py::test_a_cancelled_wait_drops_its_entry`<br/>`tests/gates/test_gate_timeout_local.py::test_local_timeout_is_finite` |
| **TheSessionIsTheOneCodePath** | `runPyqgis`, `sessionRequest`, `sessionExecutor` | `tests/derive/test_session_tools.py::test_confirmed_code_request_rides_the_wire`<br/>`tests/derive/test_session_tools.py::test_algorithm_request_rides_the_wire_and_returns_the_summary`<br/>`tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree` |

## What each requirement says

- **ADeriveToolNeverFetches** - A derive tool takes a layer and returns a layer or a value. The layer it needs is fetched first by the fetch tool that declares it, and the tool refuses, naming that fetch, when the layer was not given - it never reaches the registry for a fetcher, the cache shim's read-through, or a fetch tool module. The session tools take canvas layers by name for the same reason: the model fetches, and when the layer exists, runs.
- **ASessionAnswersOrRefuses** - A session request resolves in one of four ways and fabricates in none: the session's answer; no live session, a typed refusal before anything is sent; no answer within the window, a typed refusal with the pending entry dropped; an error answer, the session's own message raised as the tool's error. A reply from a session that is not the owner is refused.
- **ASurfaceTooLargeToCarryIsReached** - A tool docstring is truncated at a thousand characters and the engine's keyword surface is more than a thousand KEYWORDS, so the surface is reached rather than carried: a read-only tool answers a question in words out of the module's own dictionary - the keyword, its help, its labeled choices, the default it has when nobody states it, its level and whether it names a file - and what a caller does with what it learns is state it on a fill through the raw keyword floor. The read decides nothing and runs nothing. Its ranking is model-free and deterministic, so the same question is answered the same way twice and an index nobody can rebuild is never between the caller and the dictionary. A module with no dictionary refuses naming the ones there are.
- **ConsequentialCodeIsUserGated** - The session runs code nobody reviewed in advance, so the person whose project it touches is the one who lets it run. The tool body REFUSES an unconfirmed call outright, and the confirmation is the SERVER's to grant: a model-supplied approval flag is stripped before the gate is reached, so asking for consent and answering for it cannot be the same act. The wait is BOUNDED. A gate nobody answers expires into a typed refusal and drops its pending entry, because a turn held open forever is a turn the user cannot see failing - and the same expiry runs when the waiting task is cancelled, so a dropped connection leaves no entry behind. The approval card states the code verbatim; the surface it can reach is the project the user is looking at.
- **TheSessionIsTheOneCodePath** - Nothing on the daemon executes what the model wrote. The daemon holds no container and no interpreter for a snippet: the request leaves for the session, and only the session's own Python and Processing framework run it. The modules that carry a request may not spawn a process of their own.
