// plane: tool | system: session-code
//
// The tool plane's modeled seams: the two tools that run in the user's QGIS
// session through the plugin wire, the approval card in front of the code one,
// the read over the engine keyword dictionaries a caller reaches a surface too
// large to carry through, and the library listing over the two registries.
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

    part def ConnectGate {
        doc /*
        The handshake in front of the wire: the token the daemon holds, the
        presented one verified against it, and the refusal that closes a
        connection carrying anything else.
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

    part def BorrowedProvider {
        doc /*
        The daemon end of the borrowed data provider: it builds the row's
        datasource string, asks the session for the layer and waits, and turns
        the answer into the record an overlay is or the bytes the read-through
        stores.
        */
    }

    part def ProviderOpener {
        doc /*
        The plugin end: opens the uri through the named QGIS provider, adds the
        overlay to the map, or windows, exports and uploads a materialised row,
        and answers with the provider's own text when it will not open.
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

    part def LibraryListing {
        doc /*
        The listing over the two registries: every registered tool under the
        subsystem it belongs to, every source row under its class and kind. It
        states metadata the tool or the row already carries and computes no
        fact of its own.
        */
    }

    part def LibraryPanel {
        doc /*
        The panel that reads the listing. It presents the order the listing
        arrived in and re-sorts nothing.
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

    interface def LayerRequest {
        doc /*
        The envelope on the wire: one provider layer for the session to open,
        keyed by the row's own cache key, which the answer echoes.
        */
        item key : String;
        item provider : String;
        item uri : String;
        item name : String;
        item bbox : Sequence;
        item mode : String;
    }

    interface def LayerResponse {
        doc /*
        What the session did with it: the staged object a materialised row was
        uploaded to, unset for an overlay, or the provider's own error text.
        */
        item key : String;
        item uri : String [0..1];
        item error : String [0..1];
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

    part connectGate : ConnectGate {
        doc /*
        code: trid3nt_server/server/protocol/auth.py
        */
    }

    part mintedToken : ConnectGate {
        doc /*
        code: trid3nt_server/credentials/auth_handshake.py
        */
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

    part borrowedProvider : BorrowedProvider {
        doc /*
        code: trid3nt_server/tools/fetchers/_router/executors/qgis_provider.py
        */
        port requestOut : RequestOut;
        port responseIn : ResponseIn;
    }

    part providerOpener : ProviderOpener {
        doc /*
        code: plugin/render/layer_request.py
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

    part libraryListing : LibraryListing {
        doc /*
        code: trid3nt_server/server/protocol/doors/library.py
        */
    }

    part libraryPanel : LibraryPanel {
        doc /*
        code: plugin/ui/library_window.py
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

    interface layerRequested : LayerRequest
        connect borrowedProvider.requestOut to providerOpener.requestIn;

    interface layerAnswered : LayerResponse
        connect providerOpener.responseOut to connectionLoop.responseIn;

    interface layerResolved : LayerResponse
        connect connectionLoop.responseOut to borrowedProvider.responseIn;

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

    requirement def TheConnectGateIsAlwaysOn {
        doc /*
        One token is the one user, and presenting it is the whole of becoming
        that user. The daemon mints the token at first start into its own
        config, prints it once, and from then on verifies what a connection
        presents against it; nothing about the socket's origin substitutes for
        the string, because a process on this machine is not the person.

        A connection presenting nothing, or the wrong token, is refused typed
        and closed, and an envelope arriving before the handshake gets the same
        refusal rather than the turn loop. The gate fails CLOSED: a daemon
        holding no token matches nothing. Nothing here mints an identity - the
        session it binds is the one user the token stands for.
        */
    }

    requirement def AnOverlayIsNeverAnInput {
        doc /*
        A borrowed provider layer opened in mode open is context on the user's
        map: it writes one journal line, enters no store and publishes no
        packet row, because nothing downstream can read features QGIS draws
        from a service. A row that wants to be an input is materialised - the
        session exports it and the read-through lands it under the row-shaped
        cache key, so the next call is a hit that never asks the session again.
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

    requirement def TheLibraryStatesOnlyWhatTheRegistriesCarry {
        doc /*
        The library is a READ over what is registered and what is declared.
        Every registered tool is listed once under one subsystem - a tool under
        no known module prefix is listed as other rather than dropped, so the
        listing can never be quietly shorter than the registry - and every
        source row is listed under the class and kind the row itself declares.
        Every fact beside a name is metadata the tool or the row already
        carries; the listing measures nothing and concludes nothing.

        One ask reaches both facets. The tools are ranked by the same corpus the
        model routes on, so the person and the model are answered off one
        shortlist. A source row carries no corpus phrasing and is reached only
        through the data classes the ask names, and it carries no score, because
        a number beside a row would read as a measurement of the row.

        The order is the server's. The panel that reads the listing re-sorts
        nothing, so what a person sees and what the listing states are one
        order.
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

    satisfy requirement TheConnectGateIsAlwaysOn by connectGate;
    satisfy requirement TheConnectGateIsAlwaysOn by mintedToken;
    satisfy requirement TheConnectGateIsAlwaysOn by connectionLoop;
    verify requirement TheConnectGateIsAlwaysOn by
        "tests/credentials/test_auth_handshake.py::test_mints_token_once_into_the_config_file";
    verify requirement TheConnectGateIsAlwaysOn by
        "tests/credentials/test_auth_handshake.py::test_env_override_mints_nothing";
    verify requirement TheConnectGateIsAlwaysOn by
        "tests/credentials/test_remote_daemon_access.py::test_verify_access_token_fails_closed_without_a_token";
    verify requirement TheConnectGateIsAlwaysOn by
        "tests/credentials/test_remote_daemon_access.py::test_correct_token_binds";
    verify requirement TheConnectGateIsAlwaysOn by
        "tests/credentials/test_remote_daemon_access.py::test_wrong_token_typed_close";
    verify requirement TheConnectGateIsAlwaysOn by
        "tests/credentials/test_remote_daemon_access.py::test_token_less_auth_envelope_typed_close";
    verify requirement TheConnectGateIsAlwaysOn by
        "tests/credentials/test_remote_daemon_access.py::test_pre_handshake_envelope_is_refused";

    satisfy requirement AnOverlayIsNeverAnInput by borrowedProvider;
    satisfy requirement AnOverlayIsNeverAnInput by providerOpener;
    verify requirement AnOverlayIsNeverAnInput by
        "tests/fetchers/test_router_qgis_provider.py::test_mode_open_returns_the_record_and_never_the_store";
    verify requirement AnOverlayIsNeverAnInput by
        "tests/fetchers/test_router_qgis_provider.py::test_mode_open_on_a_layer_row_refuses_rather_than_publish_a_packet_row";
    verify requirement AnOverlayIsNeverAnInput by
        "tests/fetchers/test_router_qgis_provider.py::test_mode_materialise_lands_the_export_under_the_row_key";
    verify requirement AnOverlayIsNeverAnInput by
        "tests/server/test_layer_request_seam.py::test_a_foreign_session_cannot_answer_it";

    satisfy requirement ADeriveToolNeverFetches by runPyqgis;
    satisfy requirement ADeriveToolNeverFetches by runQgisAlgorithm;
    verify requirement ADeriveToolNeverFetches by
        "tests/derive/test_probe_point.py::test_outside_bounds_is_honest_null";
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

    satisfy requirement TheLibraryStatesOnlyWhatTheRegistriesCarry by libraryListing;
    satisfy requirement TheLibraryStatesOnlyWhatTheRegistriesCarry by libraryPanel;
    verify requirement TheLibraryStatesOnlyWhatTheRegistriesCarry by
        "tests/server/test_library_http_route.py::test_every_registered_tool_is_listed_once";
    verify requirement TheLibraryStatesOnlyWhatTheRegistriesCarry by
        "tests/server/test_library_http_route.py::test_every_coverage_row_is_listed_under_its_own_class_and_kind";
    verify requirement TheLibraryStatesOnlyWhatTheRegistriesCarry by
        "tests/server/test_library_http_route.py::test_a_data_row_carries_its_cell_window_and_datum";
    verify requirement TheLibraryStatesOnlyWhatTheRegistriesCarry by
        "tests/server/test_library_http_route.py::test_search_route_answers_both_facets_from_one_ask";
    verify requirement TheLibraryStatesOnlyWhatTheRegistriesCarry by
        "tests/server/test_library_http_route.py::test_a_class_the_ask_never_names_brings_no_rows";
    verify requirement TheLibraryStatesOnlyWhatTheRegistriesCarry by
        "plugin/tests/test_library_panel.py::test_tools_keep_their_subsystem_and_wire_order";
    verify requirement TheLibraryStatesOnlyWhatTheRegistriesCarry by
        "plugin/tests/test_library_panel.py::test_rows_sit_under_class_then_kind_with_their_fetcher_first";
}
