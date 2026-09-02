// The TELEMAC solve seam as a checkable model.
//
// SysML v2 TEXTUAL notation, restricted to the subset the project checker
// reads: part def, part, port def, port, interface def, item, interface
// (connect), requirement def, satisfy, verify. Nothing else parses, and the
// checker validates ONLY this project's conformance rules against the live
// tree - never general SysML semantics. Item types are recorded and never
// resolved; there is no inheritance and no expression language.
//
// Four doc-line conventions carry the code binding, because the model is only
// worth having if it can be checked against the code it describes:
//
//   code:   <repo-relative path>          on a part usage - the module the
//                                         block IS
//   forbid: <importer.prefix> -> <imported.prefix>
//                                         on a requirement def - a dependency
//                                         rule read against the modeled
//                                         modules' own imports
//   constructor: <function name>          on an interface def - a function that
//                                         builds this contract, so a caller
//                                         nobody modeled is a finding
//   pass-through: <part usage>            on an interface usage - that end
//                                         forwards the contract VERBATIM, so
//                                         it owes no item evidence and
//                                         supplies none
//
// Evidence is per interface USAGE: each hop's writer end and consumer end must
// name every non-optional item the hop carries. Pooling evidence across the
// hops that share a definition leaves a single-module severance invisible.
//
// Every name below is the tree's own: item names are the literal keys the
// code writes and reads, and every verify names a test that exists.

package SolveSeam {

    doc /*
    One authored deck goes out as a manifest, one container solves it, and one
    run report comes back. Every regression this seam has produced was a
    severed interface - a key one side wrote that the other side stopped
    reading - so the interfaces are the model and the blocks exist to give
    them ends.
    */

    // ----------------------------------------------------------------- //
    // Blocks
    // ----------------------------------------------------------------- //

    part def DeckAuthor {
        doc /*
        Serializes the approved sheet into the engine's own steering files and
        states what the server measured that the container cannot learn from
        the files it is handed.
        */
    }

    part def ManifestStager {
        doc /*
        THE manifest writer for the family. Builds the case section and puts
        the document where the worker's entrypoint dispatches on it.
        */
    }

    part def LauncherArm {
        doc /*
        The local-docker spec: the launch line, the declared network, and the
        exit classifier that folds the worker's report into the run's
        completion record.
        */
    }

    part def Supervisor {
        doc /*
        Waits on the container, uploads the mounted outputs, and ALWAYS writes
        the terminal completion object the poller is waiting on.
        */
    }

    part def SolveStep {
        doc /*
        The plan's consequential node: stage, dispatch, wait, and read the run
        report back off the run prefix.
        */
    }

    part def WorkerEntrypoint {
        doc /*
        The engine room. One manifest in, one solved run directory out: no
        network, no object store, no defaults of its own.
        */
    }

    part def TelapyChild {
        doc /*
        The solve itself, in a child process, so a Fortran STOP cannot take
        the run report down with it.
        */
    }

    part def RunReader {
        doc /*
        What a solved run's own files say, read on the server rather than
        recomputed in the container. It is handed listing TEXT by whichever step
        downloaded the file, so it is no end of the listing seam; it stands here
        for the law that forbids it importing the worker.
        */
    }

    part def DiagnosticsReader {
        doc /*
        The engine-agnostic diagnostics face: reads the folded completion
        extras first and the worker's own report only as a fallback.
        */
    }

    part def PacketAssembler {
        doc /*
        Cross-checks the recorded frame count against the file itself, so one
        reader is never the only reader.
        */
    }

    part def MeshAcceptance {
        doc /*
        The accepted mesh's record - the frozen recipe's artifact plus the
        facts a solve is staged from.
        */
    }

    part def TopologyWriter {
        doc /*
        The accepted topology a geometry file cannot state: which stretch of
        the boundary carries which role, and in which order the solver will
        number them.
        */
    }

    // ----------------------------------------------------------------- //
    // Ports
    // ----------------------------------------------------------------- //

    port def CaseOut;
    port def CaseIn;
    port def EchoOut;
    port def EchoIn;
    port def MetricsOut;
    port def MetricsIn;
    port def CompletionOut;
    port def CompletionIn;
    port def ListingOut;
    port def ListingIn;
    port def TopologyOut;
    port def TopologyIn;
    port def MeshOut;
    port def MeshIn;

    // ----------------------------------------------------------------- //
    // Interfaces - the seam contracts, by their real key names
    // ----------------------------------------------------------------- //

    interface def ManifestCaseSection {
        doc /*
        The CASE a worker runs: which engine, which deck it reads, and which
        files must exist for the run to have happened. The section key is what
        the entrypoint dispatches on, and the strict gate refuses any key
        outside this list.

        constructor: case_section
        constructor: stage_telemac_manifest
        */
        item module : String;
        item steering : FileName;
        item results : FileNameList;
        item family : String;
        item echo : EchoMap;
        item user_fortran : DirName [0..1];
        item coupling : String [0..1];
        item continue_from : FileName [0..1];
    }

    interface def CaseEcho {
        doc /*
        What the SERVER already knows and the container cannot learn from the
        files it is handed. The worker copies it into its report VERBATIM: a
        fact re-derived in the container is a second answer free to disagree
        with the first.
        */
        item utm_epsg : Integer;
        item bbox : RealList;
        item npoin : Integer;
        item nelem : Integer;
        item mesh_size_m : Real;
        item bed_source : String;
        item result_slf : FileName;
    }

    interface def WorkerRunReport {
        doc /*
        The run's only report, written whatever the child did. Success is not
        the worker's word for it: the launcher's classifier reads the
        CORRECT-END flag and the exit code together, so the report states what
        happened and the server states what it means.
        */
        item correct_end : Boolean;
        item run_id : String;
        item module : String;
        item family : String;
        item wall_s : Real;
        item error : String [0..1];
        item error_code : String [0..1];
    }

    interface def RunTerminalSignal {
        doc /*
        The terminal object the poller is waiting on. The supervisor writes it
        whatever the container did, and it is the run's identity and verdict -
        never its physics.
        */
        item run_id : String;
        item status : String;
    }

    interface def FoldedRunPhysics {
        doc /*
        The declared metrics subset the launcher arm folds into the run's
        completion, so the diagnostics face carries the physics without a
        second object read.

        The failure path adds the listing excerpt, which is the only listing a
        reader has when the run died before its listing file was uploaded. It
        is optional because a run that reached a correct end carries no tail.
        */
        item correct_end : Boolean;
        item wall_s : Real;
        item listing_tail : String [0..1];
    }

    interface def FrameCountCrossCheck {
        doc /*
        The frame count the worker recorded, beside the file it measured it on,
        so the packet can open that file and disagree. One reader is never the
        only reader of a number a delivery rests on.
        */
        item result_slf : FileName;
        item ntimestep : Integer;
    }

    interface def SolverListing {
        doc /*
        The solver's own listing, teed off the child's stdout. It is the run's
        evidence: every closure a run narrates is parsed out of it rather than
        recomputed from the fields.
        */
        item full_listing : FileName;
    }

    interface def TopologyBundle {
        doc /*
        The mesher's answers a SELAFIN cannot hold. A bundle naming no liquid
        boundary refuses, because a deck cannot be authored against a boundary
        with no role on it.
        */
        item roles : RoleMap;
        item liquid_boundary_order : StringList;
    }

    interface def AcceptedMeshRecord {
        doc /*
        The recipe-frozen artifact's fields the deck consumes. The granularity
        the deck records is the one the ACCEPTED mesh was built at, measured
        on its own cells - never the edge that was asked for.
        */
        item artifact : MeshArtifact;
        item mesh_id : String;
        item slf_uri : Uri;
        item cli_uri : Uri;
        item topology_uri : Uri;
        item display_uri : Uri;
        item node_count : Integer;
        item element_count : Integer;
        item min_edge_m : Real;
        item provenance : Map;
    }

    // ----------------------------------------------------------------- //
    // Block usages, bound to the modules they are
    // ----------------------------------------------------------------- //

    part deckAuthor : DeckAuthor {
        doc /*
        code: trid3nt_server/workflows/telemac/steps/deck.py
        */
        port caseOut : CaseOut;
        port echoOut : EchoOut;
        port topologyIn : TopologyIn;
        port meshIn : MeshIn;
    }

    part rainDeckAuthor : DeckAuthor {
        doc /*
        The rain-on-grid leg authors its own deck against the same contracts:
        a second author of the case and the echo, modeled so a severance in it
        fires like a severance in the open-water author's.

        code: trid3nt_server/workflows/telemac/steps/rain_on_grid.py
        */
        port caseOut : CaseOut;
        port echoOut : EchoOut;
        port topologyIn : TopologyIn;
    }

    part manifestStager : ManifestStager {
        doc /*
        code: trid3nt_server/workflows/telemac/steps/open_water.py
        */
        port caseIn : CaseIn;
        port caseOut : CaseOut;
    }

    part solveStep : SolveStep {
        doc /*
        code: trid3nt_server/workflows/telemac/steps/solve.py
        */
        port completionIn : CompletionIn;
    }

    part launcherArm : LauncherArm {
        doc /*
        code: trid3nt_server/workflows/telemac/run_telemac.py
        */
        port metricsIn : MetricsIn;
        port completionOut : CompletionOut;
    }

    part supervisor : Supervisor {
        doc /*
        code: trid3nt_server/workflows/solver/solver.py
        */
        port completionIn : CompletionIn;
        port completionOut : CompletionOut;
    }

    part workerEntrypoint : WorkerEntrypoint {
        doc /*
        code: workers/telemac/entrypoint.py
        */
        port caseIn : CaseIn;
        port echoIn : EchoIn;
        port echoOut : EchoOut;
        port metricsOut : MetricsOut;
        port listingOut : ListingOut;
    }

    part telapyChild : TelapyChild {
        doc /*
        code: workers/telemac/entrypoint.py
        */
        port listingOut : ListingOut;
    }

    part runReader : RunReader {
        doc /*
        code: trid3nt_server/workflows/telemac/steps/run_reads.py
        */
    }

    part diagnosticsReader : DiagnosticsReader {
        doc /*
        code: trid3nt_server/workflows/solver/diagnostics/telemac.py
        */
        port completionIn : CompletionIn;
        port listingIn : ListingIn;
    }

    part packetAssembler : PacketAssembler {
        doc /*
        code: scripts/assemble_proof_packet.py
        */
        port completionIn : CompletionIn;
    }

    part meshAcceptance : MeshAcceptance {
        doc /*
        code: trid3nt_server/workflows/mesh/step.py
        */
        port meshOut : MeshOut;
    }

    part topologyWriter : TopologyWriter {
        doc /*
        code: trid3nt_server/workflows/mesh/topology.py
        */
        port topologyOut : TopologyOut;
    }

    // ----------------------------------------------------------------- //
    // The flows
    // ----------------------------------------------------------------- //

    interface meshAccepted : AcceptedMeshRecord
        connect meshAcceptance.meshOut to deckAuthor.meshIn;

    interface topologyRead : TopologyBundle
        connect topologyWriter.topologyOut to deckAuthor.topologyIn;

    interface rainTopologyRead : TopologyBundle
        connect topologyWriter.topologyOut to rainDeckAuthor.topologyIn;

    interface caseAuthored : ManifestCaseSection
        connect deckAuthor.caseOut to manifestStager.caseIn;

    interface rainCaseAuthored : ManifestCaseSection
        connect rainDeckAuthor.caseOut to manifestStager.caseIn;

    interface caseStaged : ManifestCaseSection
        connect manifestStager.caseOut to workerEntrypoint.caseIn;

    interface echoStaged : CaseEcho
        connect deckAuthor.echoOut to workerEntrypoint.echoIn {
        doc /*
        pass-through: workerEntrypoint
        */
    }

    interface rainEchoStaged : CaseEcho
        connect rainDeckAuthor.echoOut to workerEntrypoint.echoIn {
        doc /*
        pass-through: workerEntrypoint
        */
    }

    interface echoReturned : CaseEcho
        connect workerEntrypoint.echoOut to launcherArm.metricsIn {
        doc /*
        pass-through: workerEntrypoint
        */
    }

    interface metricsWritten : WorkerRunReport
        connect workerEntrypoint.metricsOut to launcherArm.metricsIn;

    interface completionFolded : FoldedRunPhysics
        connect launcherArm.completionOut to supervisor.completionIn {
        doc /*
        pass-through: supervisor
        */
    }

    interface frameCountFolded : FrameCountCrossCheck
        connect launcherArm.completionOut to supervisor.completionIn {
        doc /*
        pass-through: supervisor
        */
    }

    interface completionPolled : RunTerminalSignal
        connect supervisor.completionOut to solveStep.completionIn;

    interface completionDiagnosed : FoldedRunPhysics
        connect supervisor.completionOut to diagnosticsReader.completionIn {
        doc /*
        pass-through: supervisor
        */
    }

    interface completionCrossChecked : FrameCountCrossCheck
        connect supervisor.completionOut to packetAssembler.completionIn {
        doc /*
        pass-through: supervisor
        */
    }

    interface listingTeed : SolverListing
        connect telapyChild.listingOut to workerEntrypoint.listingOut;

    interface listingDiagnosed : SolverListing
        connect workerEntrypoint.listingOut to diagnosticsReader.listingIn;

    // ----------------------------------------------------------------- //
    // The standing laws this seam is governed by
    // ----------------------------------------------------------------- //

    requirement def MetricsAlways {
        doc /*
        The run report is written whatever the child does. A Fortran STOP
        kills the process it runs in, and the report is the only channel the
        server has for reading what went wrong, so the write outlives the
        solve.
        */
    }

    requirement def EchoDoctrine {
        doc /*
        Server-known facts are stated by the deck, copied by the worker
        VERBATIM, and never re-derived in the container. Worker-measured facts
        are the worker's own: the frame count is measured off the file the
        echo names, and an unmeasurable result is the ABSENCE of the key
        rather than a zero.
        */
    }

    requirement def SolveTimeoutTypesNotHangs {
        doc /*
        A wedged solver is a typed report, not a container that never exits.
        The bound is stated by an environment knob, and an expiry names that
        knob in the error it writes.
        */
    }

    requirement def EmptyResultsRefuses {
        doc /*
        A case declaring no results collapses the success convention back to
        the exit code alone, which is the convention this seam retired. It
        refuses instead.
        */
    }

    requirement def StrictGateRefusesUnknownFields {
        doc /*
        A dropped key silently no-ops the knob the caller meant to set. The
        gate refuses instead, and names the parser stamp so a stale image
        reads as a drifted version rather than as a knob that did nothing.
        */
    }

    requirement def CorrectEndIsTheSuccessConvention {
        doc /*
        Success is a clean child exit AND every declared result file on disk.
        A solver that returns zero without writing its result has not solved
        anything, and the exit code alone never decides it - on either side of
        the seam.
        */
    }

    requirement def WorkersNeverImportServer {
        doc /*
        The container is the engine room: a worker that reaches into the
        server package has an opinion, a default or a fetch in it.

        forbid: workers -> trid3nt_server
        */
    }

    requirement def ReadersNeverImportTheWorker {
        doc /*
        What a solved run says is read from the artifacts the supervisor
        uploaded. A reader importing worker code is a second computation of
        the same quantity, running outside the image that produced it.

        forbid: trid3nt_server.workflows.telemac.steps.run_reads -> workers
        forbid: trid3nt_server.workflows.solver.diagnostics -> workers
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement MetricsAlways by workerEntrypoint;
    verify requirement MetricsAlways by
        "workers/telemac/test_entrypoint.py::test_a_child_that_dies_still_leaves_the_metrics_written";

    satisfy requirement EchoDoctrine by deckAuthor;
    satisfy requirement EchoDoctrine by rainDeckAuthor;
    satisfy requirement EchoDoctrine by workerEntrypoint;
    verify requirement EchoDoctrine by
        "workers/telemac/test_entrypoint.py::test_a_clean_child_that_wrote_its_results_is_the_run_succeeding";
    verify requirement EchoDoctrine by
        "workers/telemac/test_entrypoint.py::test_the_frame_count_is_measured_off_the_file_the_echo_names";
    verify requirement EchoDoctrine by
        "workers/telemac/test_entrypoint.py::test_an_unreadable_result_leaves_ntimestep_ABSENT_not_zero";

    satisfy requirement SolveTimeoutTypesNotHangs by workerEntrypoint;
    verify requirement SolveTimeoutTypesNotHangs by
        "workers/telemac/test_entrypoint.py::test_the_solve_bound_defaults_to_a_day_and_the_knob_states_it";
    verify requirement SolveTimeoutTypesNotHangs by
        "workers/telemac/test_entrypoint.py::test_a_child_that_outruns_the_bound_is_killed_and_still_reports";

    satisfy requirement EmptyResultsRefuses by workerEntrypoint;
    verify requirement EmptyResultsRefuses by
        "workers/telemac/test_entrypoint.py::test_a_case_declaring_no_results_refuses";

    satisfy requirement StrictGateRefusesUnknownFields by workerEntrypoint;
    verify requirement StrictGateRefusesUnknownFields by
        "workers/telemac/test_entrypoint.py::test_the_gate_refuses_an_unknown_key_and_names_the_parser";
    verify requirement StrictGateRefusesUnknownFields by
        "workers/telemac/test_entrypoint.py::test_a_case_with_an_unknown_field_refuses";

    satisfy requirement CorrectEndIsTheSuccessConvention by workerEntrypoint;
    satisfy requirement CorrectEndIsTheSuccessConvention by launcherArm;
    verify requirement CorrectEndIsTheSuccessConvention by
        "workers/telemac/test_entrypoint.py::test_a_clean_exit_that_wrote_no_result_is_not_a_solve";
    verify requirement CorrectEndIsTheSuccessConvention by
        "tests/test_run_telemac_chain.py::test_classify_exit_clean_exit_but_no_correct_end_is_error";

    satisfy requirement WorkersNeverImportServer by workerEntrypoint;
    satisfy requirement WorkersNeverImportServer by telapyChild;
    verify requirement WorkersNeverImportServer by
        "tests/test_model_conformance.py::test_the_solve_seam_model_conforms";

    satisfy requirement ReadersNeverImportTheWorker by runReader;
    satisfy requirement ReadersNeverImportTheWorker by diagnosticsReader;
    verify requirement ReadersNeverImportTheWorker by
        "tests/test_model_conformance.py::test_the_solve_seam_model_conforms";
}
