// plane: tool | system: session-code
//
// The tool plane's modeled seams: the two tools that run in the user's QGIS
// session through the plugin wire, the approval card in front of the code one,
// and the read over the engine keyword dictionaries a caller reaches a surface
// too large to carry through.
//
// SysML v2 TEXTUAL notation, restricted to the subset the project checker
// reads. Conventions as the other seams: ``code:`` binds a block to the module
// it IS, ``forbid:`` states a dependency rule, ``constructor:`` names a
// function that builds a contract, and ``pass-through:`` marks an end that
// forwards a contract verbatim.
//
// The plane holds more than this - the registry, retrieval, the derive
// tools - and none of it is modeled here. What is modeled is what was touched;
// the rest of the plane is stated as unmodeled in the index rather than
// sketched.

package ToolPlane {

    doc /*
    The session is the one code path. A tool that runs an algorithm or a
    snippet does not run it on the daemon: it emits a request on the plugin
    wire, the user's own QGIS session runs it over the layers on the canvas,
    and the answer comes back on the same wire. Nothing the daemon holds - no
    container, no interpreter of its own - executes what the model wrote.

    Running code the user did not write is a consequential act, so the code
    request is fronted by the approval card: the exact snippet, verbatim, and
    a Run the user presses. The approval is the server's to grant; a flag the
    model supplies is stripped before the gate is reached.

    A derive tool takes a layer and never fetches. The world-read stays on the
    fetch path, where it is declared, cached and gated; the session tools take
    canvas layers by NAME and the rest of the derive set takes a layer uri.
    */

    // Blocks

    part def SessionCodeTool {
        doc /*
        ``run_pyqgis``: the registered entry for a snippet. It refuses to run
        without the gate's approval, asks the session, and returns what the
        session produced.
        */
    }

    part def SessionAlgorithmTool {
        doc /*
        ``run_qgis_algorithm``: the registered entry for one Processing
        algorithm over canvas layers named in its params.
        */
    }

    part def CodeExecGate {
        doc /*
        The approval card in front of a code request: emits the verbatim code,
        waits a bounded time on the user's decision, and injects the approval
        only on a ``proceed``.
        */
    }

    part def ToolDispatch {
        doc /*
        The tool-call site. Before the gate it strips any approval the model
        supplied, so consent is asked for and answered by different hands.
        */
    }

    part def SessionRequest {
        doc /*
        The one place a request leaves the daemon for the session and its
        answer is awaited: the pending registry, the bounded wait, the typed
        refusals for no session, no answer and an error answer.
        */
    }

    part def ConnectionLoop {
        doc /*
        The websocket loop, where the session's answer arrives and resolves
        the pending request on a session match.
        */
    }

    part def SessionExecutor {
        doc /*
        The plugin end: runs ``processing.run`` over the project's layers and
        adds the output, or executes the approved snippet in the session's
        Python, and answers with the outcome as it happened.
        */
    }

    part def ApprovalCard {
        doc /*
        The plugin's parse of the code approval card: the id the decision is
        keyed by and the exact code the user reads before pressing Run.
        */
    }

    part def KeywordLookup {
        doc /*
        The read over the engine's own keyword dictionaries. It answers a question in
        words with the dictionary's entries and runs nothing.
        */
    }

    // Ports

    port def ApprovalOut;
    port def ApprovalIn;
    port def CardOut;
    port def CardIn;
    port def AskOut;
    port def AskIn;
    port def RequestOut;
    port def RequestIn;
    port def ResponseOut;
    port def ResponseIn;

    // Interfaces - the seam contracts, by their real key names

    interface def StrippedApproval {
        doc /*
        What the dispatch removes from a model-issued call before the gate sees
        it: the approval flag and the card id, so only the gate can put them
        back.
        */
        item confirmed : Boolean;
        item code_exec_id : String;
    }

    interface def GrantedApproval {
        doc /*
        What the gate injects on the user's ``proceed``: the approval and the
        id of the card it was given on, which the tool carries to the session
        so the outcome joins that card.
        */
        item confirmed : Boolean;
        item code_exec_id : String;
    }

    interface def CodeApprovalCard {
        doc /*
        The card the user reads: the id the decision is keyed by, the exact
        code, and its caption. No layer list: the session's own project is the
        surface, and the user is looking at it.
        */
        item code_exec_id : String;
        item python_code : String;
        item rationale : String [0..1];
    }

    interface def SessionAsk {
        doc /*
        What a tool hands the request seam: the kind of thing to run and its
        body - an algorithm id with its params, or the approved code.

        constructor: run_in_session
        */
        item kind : String;
        item algorithm : String [0..1];
        item params : Map [0..1];
        item code : String [0..1];
        item code_exec_id : String [0..1];
    }

    interface def ProcessingRequest {
        doc /*
        The envelope on the wire: one request the session runs, keyed by an
        unguessable id the answer echoes.
        */
        item request_id : String;
        item kind : String;
        item algorithm : String [0..1];
        item params : Map;
        item code : String [0..1];
        item code_exec_id : String [0..1];
    }

    interface def ProcessingResponse {
        doc /*
        What the session produced. ``status`` is the honest terminal outcome;
        an ``error`` is the session's own traceback, never a message this side
        wrote about it.
        */
        item request_id : String;
        item status : String;
        item result : Map [0..1];
        item error : String [0..1];
        item stdout : String [0..1];
    }

    // Parts - bound to the modules they ARE

    part runPyqgis : SessionCodeTool {
        doc /*
        code: trid3nt_server/tools/derive/run_pyqgis/run_pyqgis.py
        */
        port approvalIn : ApprovalIn;
        port askOut : AskOut;
    }

    part runQgisAlgorithm : SessionAlgorithmTool {
        doc /*
        code: trid3nt_server/tools/derive/run_qgis_algorithm/run_qgis_algorithm.py
        */
        port askOut : AskOut;
    }

    part codeExecGate : CodeExecGate {
        doc /*
        code: trid3nt_server/gates/confirm.py
        */
        port approvalIn : ApprovalIn;
        port approvalOut : ApprovalOut;
        port cardOut : CardOut;
    }

    part toolDispatch : ToolDispatch {
        doc /*
        code: trid3nt_server/server/dispatch/emitter.py
        */
        port approvalOut : ApprovalOut;
    }

    part sessionRequest : SessionRequest {
        doc /*
        code: trid3nt_server/server/processing.py
        */
        port askIn : AskIn;
        port requestOut : RequestOut;
        port responseIn : ResponseIn;
    }

    part connectionLoop : ConnectionLoop {
        doc /*
        code: trid3nt_server/server/protocol/loop.py
        */
        port responseIn : ResponseIn;
        port responseOut : ResponseOut;
    }

    part sessionExecutor : SessionExecutor {
        doc /*
        code: plugin/render/processing.py
        */
        port requestIn : RequestIn;
        port responseOut : ResponseOut;
    }

    part approvalCard : ApprovalCard {
        doc /*
        code: plugin/ui/gate.py
        */
        port cardIn : CardIn;
    }

    part keywordLookup : KeywordLookup {
        doc /*
        code: trid3nt_server/workflows/telemac/modules/describe.py
        */
    }

    // Hops

    interface approvalStripped : StrippedApproval
        connect toolDispatch.approvalOut to codeExecGate.approvalIn;

    interface cardPresented : CodeApprovalCard
        connect codeExecGate.cardOut to approvalCard.cardIn;

    interface approvalGranted : GrantedApproval
        connect codeExecGate.approvalOut to runPyqgis.approvalIn;

    interface codeAsked : SessionAsk
        connect runPyqgis.askOut to sessionRequest.askIn;

    interface algorithmAsked : SessionAsk
        connect runQgisAlgorithm.askOut to sessionRequest.askIn;

    interface requestEmitted : ProcessingRequest
        connect sessionRequest.requestOut to sessionExecutor.requestIn;

    interface responseAnswered : ProcessingResponse
        connect sessionExecutor.responseOut to connectionLoop.responseIn;

    interface responseResolved : ProcessingResponse
        connect connectionLoop.responseOut to sessionRequest.responseIn;

    // The laws this seam is governed by

    requirement def ConsequentialCodeIsUserGated {
        doc /*
        The session runs code nobody reviewed in advance, so the person whose
        project it touches is the one who lets it run. The tool body REFUSES an
        unconfirmed call outright, and the confirmation is the SERVER's to
        grant: a model-supplied approval flag is stripped before the gate is
        reached, so asking for consent and answering for it cannot be the same
        act.

        The wait is BOUNDED. A gate nobody answers expires into a typed refusal
        and drops its pending entry, because a turn held open forever is a turn
        the user cannot see failing - and the same expiry runs when the waiting
        task is cancelled, so a dropped connection leaves no entry behind.

        The approval card states the code verbatim; the surface it can reach is
        the project the user is looking at.
        */
    }

    requirement def TheSessionIsTheOneCodePath {
        doc /*
        Nothing on the daemon executes what the model wrote. The daemon holds no
        container and no interpreter for a snippet: the request leaves for the
        session, and only the session's own Python and Processing framework run
        it. The modules that carry a request may not spawn a process of their
        own.

        forbid: trid3nt_server.tools.derive.run_pyqgis -> subprocess
        forbid: trid3nt_server.tools.derive.run_qgis_algorithm -> subprocess
        forbid: trid3nt_server.server.processing -> subprocess
        */
    }

    requirement def ASessionAnswersOrRefuses {
        doc /*
        A session request resolves in one of four ways and fabricates in none:
        the session's answer; no live session, a typed refusal before anything
        is sent; no answer within the window, a typed refusal with the pending
        entry dropped; an error answer, the session's own message raised as the
        tool's error. A reply from a session that is not the owner is refused.
        */
    }

    requirement def ADeriveToolNeverFetches {
        doc /*
        A derive tool takes a layer and returns a layer or a value. The layer
        it needs is fetched first by the fetch tool that declares it, and the
        tool refuses, naming that fetch, when the layer was not given - it never
        reaches the registry for a fetcher, the cache shim's read-through, or a
        fetch tool module. The session tools take canvas layers by name for the
        same reason: the model fetches, and when the layer exists, runs.

        forbid: trid3nt_server.tools.derive.run_pyqgis -> trid3nt_server.tools.fetchers
        forbid: trid3nt_server.tools.derive.run_qgis_algorithm -> trid3nt_server.tools.fetchers
        forbid: trid3nt_server.tools.derive.run_pyqgis -> trid3nt_server.tools.cache
        forbid: trid3nt_server.tools.derive.run_qgis_algorithm -> trid3nt_server.tools.cache
        */
    }

    requirement def ASurfaceTooLargeToCarryIsReached {
        doc /*
        A tool docstring is truncated at a thousand characters and the engine's
        keyword surface is more than a thousand KEYWORDS, so the surface is
        reached rather than carried: a read-only tool answers a question in words
        out of the module's own dictionary - the keyword, its help, its labeled
        choices, the default it has when nobody states it, its level and whether
        it names a file - and what a caller does with what it learns is state it
        on a fill through the raw keyword floor.

        The read decides nothing and runs nothing. Its ranking is model-free and
        deterministic, so the same question is answered the same way twice and
        an index nobody can rebuild is never between the caller and the
        dictionary. A module with no dictionary refuses naming the ones there are.
        */
    }

    // Allocations

    satisfy requirement ConsequentialCodeIsUserGated by runPyqgis;
    satisfy requirement ConsequentialCodeIsUserGated by codeExecGate;
    satisfy requirement ConsequentialCodeIsUserGated by toolDispatch;
    verify requirement ConsequentialCodeIsUserGated by
        "tests/derive/test_session_tools.py::test_run_pyqgis_refuses_without_the_card";
    verify requirement ConsequentialCodeIsUserGated by
        "tests/gates/test_code_exec_gate.py::test_dispatch_strips_a_model_supplied_approval";
    verify requirement ConsequentialCodeIsUserGated by
        "tests/gates/test_code_exec_gate.py::test_approve_injects_confirmed_and_the_card_carries_the_code";
    verify requirement ConsequentialCodeIsUserGated by
        "tests/gates/test_code_exec_gate.py::test_cancel_blocks_dispatch_and_leaks_nothing";
    verify requirement ConsequentialCodeIsUserGated by
        "tests/gates/test_code_exec_gate.py::test_an_unanswered_card_expires_typed";
    verify requirement ConsequentialCodeIsUserGated by
        "tests/gates/test_code_exec_gate.py::test_a_cancelled_wait_drops_its_entry";
    verify requirement ConsequentialCodeIsUserGated by
        "tests/gates/test_gate_timeout_local.py::test_local_timeout_is_finite";

    satisfy requirement TheSessionIsTheOneCodePath by runPyqgis;
    satisfy requirement TheSessionIsTheOneCodePath by sessionRequest;
    satisfy requirement TheSessionIsTheOneCodePath by sessionExecutor;
    verify requirement TheSessionIsTheOneCodePath by
        "tests/derive/test_session_tools.py::test_confirmed_code_request_rides_the_wire";
    verify requirement TheSessionIsTheOneCodePath by
        "tests/derive/test_session_tools.py::test_algorithm_request_rides_the_wire_and_returns_the_summary";
    verify requirement TheSessionIsTheOneCodePath by
        "tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement ASessionAnswersOrRefuses by sessionRequest;
    satisfy requirement ASessionAnswersOrRefuses by connectionLoop;
    verify requirement ASessionAnswersOrRefuses by
        "tests/derive/test_session_tools.py::test_no_session_is_a_typed_refusal";
    verify requirement ASessionAnswersOrRefuses by
        "tests/derive/test_session_tools.py::test_a_wait_that_runs_out_is_typed_and_cleans_up";
    verify requirement ASessionAnswersOrRefuses by
        "tests/derive/test_session_tools.py::test_an_error_reply_is_the_sessions_own_message";
    verify requirement ASessionAnswersOrRefuses by
        "tests/derive/test_session_tools.py::test_a_cross_session_reply_is_refused";

    satisfy requirement ADeriveToolNeverFetches by runPyqgis;
    satisfy requirement ADeriveToolNeverFetches by runQgisAlgorithm;
    verify requirement ADeriveToolNeverFetches by
        "tests/derive/test_hydrology_primitives.py::test_bad_inputs_raise";
    verify requirement ADeriveToolNeverFetches by
        "tests/derive/test_compute_flood_depth_damage.py::test_missing_assets_is_a_refusal_never_a_fetch";
    verify requirement ADeriveToolNeverFetches by
        "tests/derive/test_compute_sediment_yield.py::test_missing_layers_refuse_never_fetch";
    verify requirement ADeriveToolNeverFetches by
        "tests/derive/test_model_debris_flow.py::test_missing_layers_refuse_never_fetch";
    verify requirement ADeriveToolNeverFetches by
        "tests/derive/test_compute_model_residuals.py::test_missing_observations_refuse_never_fetch";
    verify requirement ADeriveToolNeverFetches by
        "tests/derive/test_compute_exposure_summary.py::test_absent_layers_are_named_never_fetched";
    verify requirement ADeriveToolNeverFetches by
        "tests/derive/test_query_point_hazard.py::test_place_name_is_not_geocoded_here";
    verify requirement ADeriveToolNeverFetches by
        "tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement ASurfaceTooLargeToCarryIsReached by keywordLookup;
    verify requirement ASurfaceTooLargeToCarryIsReached by
        "tests/search/test_describe_keywords.py::test_a_question_in_words_reaches_the_keyword_that_answers_it";
    verify requirement ASurfaceTooLargeToCarryIsReached by
        "tests/search/test_describe_keywords.py::test_a_match_carries_the_dictionary_s_own_help_choices_and_default";
    verify requirement ASurfaceTooLargeToCarryIsReached by
        "tests/search/test_describe_keywords.py::test_the_same_question_is_answered_the_same_way_twice";
    verify requirement ASurfaceTooLargeToCarryIsReached by
        "tests/search/test_describe_keywords.py::test_a_module_with_no_catalog_refuses_naming_the_ones_there_are";
    verify requirement ASurfaceTooLargeToCarryIsReached by
        "tests/search/test_describe_keywords.py::test_every_corpus_phrasing_surfaces_the_tool_model_free";
}
