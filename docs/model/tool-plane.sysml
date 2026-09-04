// plane: tool | system: code-exec
//
// The tool plane's first seam: the box a user-confirmed snippet runs in.
//
// SysML v2 TEXTUAL notation, restricted to the subset the project checker
// reads. Conventions as the other seams: ``code:`` binds a block to the module
// it IS, ``forbid:`` states a dependency rule, ``constructor:`` names a
// function that builds a contract, and ``pass-through:`` marks an end that
// forwards a contract verbatim.
//
// The plane holds more than this - the registry, retrieval, the processing
// tools - and none of it is modeled here. This seam is modeled because it was
// touched; the rest of the plane is stated as unmodeled in the index rather
// than sketched.

package ToolPlane {

    doc /*
    The escape hatch for ad-hoc computation is the one tool that runs code
    nobody wrote in advance, so what contains it has to be structural rather
    than careful. It is a container with no network: the denial is on the
    launch line, where a snippet cannot reach it, instead of inside the
    interpreter where a determined payload rebinds past it.

    The same posture decides where data comes from. A box with no network
    cannot fetch, so everything a snippet reads is STAGED into the run
    directory first, by the process that holds the credentials and answers to
    the gates. Fetch first, analyze second: a world-read stays visible to the
    substrate that governs world-reads, and the analysis runs on what it was
    handed.

    The run is synchronous and takes seconds, so the tool that drives it is
    off-loaded off the event loop. A box run on the loop is a stalled
    heartbeat, which is a disconnected client.
    */

    // ----------------------------------------------------------------- //
    // Blocks
    // ----------------------------------------------------------------- //

    part def CodeExecTool {
        doc /*
        The confirmed snippet's entry point: the gate's result comes in, the
        box's envelope goes back out as the payload the card and the narration
        both read.
        */
    }

    part def SandboxBox {
        doc /*
        The host end. It stages every ref into a private run directory, builds
        the one constrained launch line, and reads back what the driver wrote -
        or states honestly that nothing was written.
        */
    }

    part def SandboxDriver {
        doc /*
        The engine room inside the box: staged paths opened as handles, the
        snippet executed, its result converted and bounded. It imports nothing
        from the server package - the mount is the only thing connecting them.
        */
    }

    part def ToolDispatch {
        doc /*
        Where a sync tool body is routed onto a worker thread instead of the
        event loop, and the set that is routed there unconditionally.
        */
    }

    // ----------------------------------------------------------------- //
    // Ports
    // ----------------------------------------------------------------- //

    port def RequestOut;
    port def RequestIn;
    port def PayloadOut;
    port def PayloadIn;
    port def EnvelopeOut;
    port def EnvelopeIn;
    port def OffloadOut;
    port def OffloadIn;

    // ----------------------------------------------------------------- //
    // Interfaces - the seam contracts, by their real key names
    // ----------------------------------------------------------------- //

    interface def SnippetRequest {
        doc /*
        What the tool hands the box: the exact code the user approved, and the
        layers it may touch. ``layer_refs`` is the whole data surface - a
        snippet reads what is named here and has no other way to reach
        anything, which is what makes the approval card an honest account of
        what the code can see.

        constructor: submit_sandbox_job
        */
        item python_code : String;
        item layer_refs : Map;
        item timeout_seconds : Integer [0..1];
    }

    interface def StagedPayload {
        doc /*
        The run directory's own payload, written before the container starts.
        Its ``layer_refs`` are BOX-SIDE paths under the staged directory, not
        the URIs the tool passed: by the time the driver reads this, every byte
        it names is already on local disk.
        */
        item python_code : String;
        item layer_refs : Map;
    }

    interface def RunEnvelope {
        doc /*
        What one run produced. ``status`` is the terminal honesty: a snippet
        that hit the network boundary comes back ``blocked`` and one that ran
        past its cap comes back ``timeout``, never dressed up as a result. The
        truncation flags are the other half of that - a bounded stream says it
        was bounded rather than presenting a head as the whole.

        ``layer_errors`` names each ref that could not be staged or opened, so
        a missing layer is a stated gap the snippet and the narration can both
        see rather than a silent empty handle.
        */
        item status : String;
        item stdout : String;
        item stderr : String;
        item result : Map;
        item error : String;
        item stdout_truncated : Boolean;
        item stderr_truncated : Boolean;
        item layer_errors : Map [0..1];
        item duration_s : Real [0..1];
        item wallclock_cap_seconds : Integer [0..1];
    }

    interface def SyncOffloadRouting {
        doc /*
        The tool name the dispatch routes off the loop unconditionally. It is
        declared as data on the dispatch side and satisfied by the tool body
        being emit-free, which is what makes running it in a worker thread
        safe.
        */
        item code_exec_request : String;
    }

    // ----------------------------------------------------------------- //
    // Parts - bound to the modules they ARE
    // ----------------------------------------------------------------- //

    part codeExecTool : CodeExecTool {
        doc /*
        code: trid3nt_server/tools/meta/code_exec_tool/code_exec_tool.py
        */
        port requestOut : RequestOut;
        port envelopeIn : EnvelopeIn;
        port offloadOut : OffloadOut;
    }

    part sandboxBox : SandboxBox {
        doc /*
        code: trid3nt_server/sandbox/box.py
        */
        port requestIn : RequestIn;
        port payloadOut : PayloadOut;
        port envelopeIn : EnvelopeIn;
        port envelopeOut : EnvelopeOut;
    }

    part sandboxDriver : SandboxDriver {
        doc /*
        code: trid3nt_server/sandbox/driver.py
        */
        port payloadIn : PayloadIn;
        port envelopeOut : EnvelopeOut;
    }

    part toolDispatch : ToolDispatch {
        doc /*
        code: trid3nt_server/server/dispatch/emitter.py
        */
        port offloadIn : OffloadIn;
    }

    // ----------------------------------------------------------------- //
    // Hops
    // ----------------------------------------------------------------- //

    interface snippetDispatched : SnippetRequest
        connect codeExecTool.requestOut to sandboxBox.requestIn;

    interface payloadStaged : StagedPayload
        connect sandboxBox.payloadOut to sandboxDriver.payloadIn;

    interface envelopeWritten : RunEnvelope
        connect sandboxDriver.envelopeOut to sandboxBox.envelopeIn;

    interface envelopeReturned : RunEnvelope
        connect sandboxBox.envelopeOut to codeExecTool.envelopeIn;

    interface offloadDeclared : SyncOffloadRouting
        connect codeExecTool.offloadOut to toolDispatch.offloadIn;

    // ----------------------------------------------------------------- //
    // The laws this seam is governed by
    // ----------------------------------------------------------------- //

    requirement def SandboxIsNetworkNone {
        doc /*
        The box runs with the network OFF, declared on the launch line. A
        snippet is code nobody reviewed, so containment cannot be a guard the
        same interpreter runs: a monkeypatched socket is rebindable and a
        credential-scrubbed environment is only as good as its allowlist. A
        container with no interfaces is neither - the kernel refuses the packet
        whether it came from urllib, ctypes or a shelled-out binary.

        The denial being structural is what lets the seam be honest about it: a
        snippet that reaches for the world comes back ``blocked``, carrying the
        failure exactly as the box met it rather than a message this code wrote
        about it.

        The driver enforces the other half by depending on nothing: a module
        inside the box that could import the server package could import its
        fetchers, and the boundary would be one edge away from gone.

        forbid: trid3nt_server.sandbox.driver -> trid3nt_server
        */
    }

    requirement def DataEntersStaged {
        doc /*
        The box takes files and values HANDED to it and fetches nothing. Every
        ref is materialized into the run directory by the host - the process
        that holds the credentials and answers to the gates - and the payload
        the driver reads names local paths only.

        This is the same rule the substrate keeps everywhere: a world-read
        happens on the fetch path where it is visible, cached and gated, and
        never as a side effect of some other operation. A snippet that could
        open a URI would be a second, unwatched fetcher inside the one place
        that is meant to compute.

        A ref that cannot be staged is NAMED rather than silently dropped: the
        original string is handed through and the reason rides in
        ``layer_errors``, so the snippet fails on a stated absence.
        */
    }

    requirement def OffloadKeepsTheLoopUnblocked {
        doc /*
        The tool that drives the box is off-loaded unconditionally. One run is
        a container start, a staging fetch and seconds of compute, all
        synchronous; run on the event loop it starves the heartbeat and the
        client reconnects mid-analysis.

        The off-load is safe because the tool body is emit-free - the confirm
        card is emitted on the loop before dispatch and the result envelope
        after it - so no worker thread ever touches the loop.
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement SandboxIsNetworkNone by sandboxBox;
    satisfy requirement SandboxIsNetworkNone by sandboxDriver;
    verify requirement SandboxIsNetworkNone by
        "tests/test_sandbox_box.py::test_the_box_runs_with_the_network_off";
    verify requirement SandboxIsNetworkNone by
        "tests/test_sandbox_box.py::test_a_snippet_cannot_reach_the_network";
    verify requirement SandboxIsNetworkNone by
        "tests/test_sandbox_box.py::test_a_denied_egress_is_reported_as_blocked_rather_than_as_a_bug";
    verify requirement SandboxIsNetworkNone by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement DataEntersStaged by sandboxBox;
    satisfy requirement DataEntersStaged by sandboxDriver;
    verify requirement DataEntersStaged by
        "tests/test_sandbox_box.py::test_a_local_raster_is_staged_and_opens_as_a_handle";
    verify requirement DataEntersStaged by
        "tests/test_sandbox_box.py::test_a_remote_ref_is_fetched_by_the_host_before_the_box_starts";
    verify requirement DataEntersStaged by
        "tests/test_sandbox_box.py::test_frames_stage_as_an_ordered_list";
    verify requirement DataEntersStaged by
        "tests/test_sandbox_box.py::test_a_ref_that_cannot_be_staged_is_named_rather_than_crashing_the_run";
    verify requirement DataEntersStaged by
        "tests/test_sandbox_box.py::test_the_box_reaches_for_nothing_from_the_inside";

    satisfy requirement OffloadKeepsTheLoopUnblocked by codeExecTool;
    satisfy requirement OffloadKeepsTheLoopUnblocked by toolDispatch;
    verify requirement OffloadKeepsTheLoopUnblocked by
        "tests/test_sandbox_box.py::test_the_tool_that_drives_the_box_is_always_offloaded";
    verify requirement OffloadKeepsTheLoopUnblocked by
        "tests/test_sandbox_box.py::test_the_offload_keeps_the_loop_unblocked";
    verify requirement OffloadKeepsTheLoopUnblocked by
        "tests/test_sync_tool_offload_stage0.py::test_gate_refuses_emitting_sync_tool";
}
