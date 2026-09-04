# ToolPlane - derived view

GENERATED from `docs/model/tool-plane.sysml` by `scripts/model_check.py --view`. Never hand-edited: regenerate it, and `tests/test_model_conformance.py` fails while it is stale.

Plane: **tool**. System: **code-exec**. One seam of the system of systems indexed by [`README.md`](README.md) - never the whole picture.

## Blocks and flows

```mermaid
flowchart LR
    codeExecTool["CodeExecTool<br/>trid3nt_server/tools/meta/code_exec_tool/code_exec_tool.py"]
    sandboxBox["SandboxBox<br/>trid3nt_server/sandbox/box.py"]
    sandboxDriver["SandboxDriver<br/>trid3nt_server/sandbox/driver.py"]
    toolDispatch["ToolDispatch<br/>trid3nt_server/server/dispatch/emitter.py"]
    sandboxBox -- "RunEnvelope" --> codeExecTool
    sandboxDriver -- "RunEnvelope" --> sandboxBox
    codeExecTool -- "SyncOffloadRouting" --> toolDispatch
    sandboxBox -- "StagedPayload" --> sandboxDriver
    codeExecTool -- "SnippetRequest" --> sandboxBox
```

## Interface items

### `RunEnvelope`

What one run produced. ``status`` is the terminal honesty: a snippet that hit the network boundary comes back ``blocked`` and one that ran past its cap comes back ``timeout``, never dressed up as a result. The truncation flags are the other half of that - a bounded stream says it was bounded rather than presenting a head as the whole. ``layer_errors`` names each ref that could not be staged or opened, so a missing layer is a stated gap the snippet and the narration can both see rather than a silent empty handle.

| item | type | required |
| --- | --- | --- |
| `status` | String | required |
| `stdout` | String | required |
| `stderr` | String | required |
| `result` | Map | required |
| `error` | String | required |
| `stdout_truncated` | Boolean | required |
| `stderr_truncated` | Boolean | required |
| `layer_errors` | Map | optional |
| `duration_s` | Real | optional |
| `wallclock_cap_seconds` | Integer | optional |

### `SnippetRequest`

What the tool hands the box: the exact code the user approved, and the layers it may touch. ``layer_refs`` is the whole data surface - a snippet reads what is named here and has no other way to reach anything, which is what makes the approval card an honest account of what the code can see.

| item | type | required |
| --- | --- | --- |
| `python_code` | String | required |
| `layer_refs` | Map | required |
| `timeout_seconds` | Integer | optional |

### `StagedPayload`

The run directory's own payload, written before the container starts. Its ``layer_refs`` are BOX-SIDE paths under the staged directory, not the URIs the tool passed: by the time the driver reads this, every byte it names is already on local disk.

| item | type | required |
| --- | --- | --- |
| `python_code` | String | required |
| `layer_refs` | Map | required |

### `SyncOffloadRouting`

The tool name the dispatch routes off the loop unconditionally. It is declared as data on the dispatch side and satisfied by the tool body being emit-free, which is what makes running it in a worker thread safe.

| item | type | required |
| --- | --- | --- |
| `code_exec_request` | String | required |

## Requirements

| requirement | satisfied by | verified by |
| --- | --- | --- |
| **DataEntersStaged** | `sandboxBox`, `sandboxDriver` | `tests/test_sandbox_box.py::test_a_local_raster_is_staged_and_opens_as_a_handle`<br/>`tests/test_sandbox_box.py::test_a_remote_ref_is_fetched_by_the_host_before_the_box_starts`<br/>`tests/test_sandbox_box.py::test_frames_stage_as_an_ordered_list`<br/>`tests/test_sandbox_box.py::test_a_ref_that_cannot_be_staged_is_named_rather_than_crashing_the_run`<br/>`tests/test_sandbox_box.py::test_the_box_reaches_for_nothing_from_the_inside` |
| **OffloadKeepsTheLoopUnblocked** | `codeExecTool`, `toolDispatch` | `tests/test_sandbox_box.py::test_the_tool_that_drives_the_box_is_always_offloaded`<br/>`tests/test_sandbox_box.py::test_the_offload_keeps_the_loop_unblocked`<br/>`tests/test_sync_tool_offload_stage0.py::test_gate_refuses_emitting_sync_tool` |
| **SandboxIsNetworkNone** | `sandboxBox`, `sandboxDriver` | `tests/test_sandbox_box.py::test_the_box_runs_with_the_network_off`<br/>`tests/test_sandbox_box.py::test_a_snippet_cannot_reach_the_network`<br/>`tests/test_sandbox_box.py::test_a_denied_egress_is_reported_as_blocked_rather_than_as_a_bug`<br/>`tests/test_model_conformance.py::test_the_model_conforms_to_the_tree` |

## What each requirement says

- **DataEntersStaged** - The box takes files and values HANDED to it and fetches nothing. Every ref is materialized into the run directory by the host - the process that holds the credentials and answers to the gates - and the payload the driver reads names local paths only. This is the same rule the substrate keeps everywhere: a world-read happens on the fetch path where it is visible, cached and gated, and never as a side effect of some other operation. A snippet that could open a URI would be a second, unwatched fetcher inside the one place that is meant to compute. A ref that cannot be staged is NAMED rather than silently dropped: the original string is handed through and the reason rides in ``layer_errors``, so the snippet fails on a stated absence.
- **OffloadKeepsTheLoopUnblocked** - The tool that drives the box is off-loaded unconditionally. One run is a container start, a staging fetch and seconds of compute, all synchronous; run on the event loop it starves the heartbeat and the client reconnects mid-analysis. The off-load is safe because the tool body is emit-free - the confirm card is emitted on the loop before dispatch and the result envelope after it - so no worker thread ever touches the loop.
- **SandboxIsNetworkNone** - The box runs with the network OFF, declared on the launch line. A snippet is code nobody reviewed, so containment cannot be a guard the same interpreter runs: a monkeypatched socket is rebindable and a credential-scrubbed environment is only as good as its allowlist. A container with no interfaces is neither - the kernel refuses the packet whether it came from urllib, ctypes or a shelled-out binary. The denial being structural is what lets the seam be honest about it: a snippet that reaches for the world comes back ``blocked``, carrying the failure exactly as the box met it rather than a message this code wrote about it. The driver enforces the other half by depending on nothing: a module inside the box that could import the server package could import its fetchers, and the boundary would be one edge away from gone.
