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

    part def Assembler {
        doc /*
        ONE flow for every simulation the server authors: the approved sheet
        into the engine's own steering files, every field they name written
        beside them, the whole run directory staged, and the manifest written
        last. It also states what the server measured that the container
        cannot learn from the files it is handed.
        */
    }

    part def SteeringAuthor {
        doc /*
        The steering files themselves - every keyword the run is solved under.
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

    part def ResultReader {
        doc /*
        A solved result's fields, read by the engine's own library inside the
        image that wrote them. It parses nothing: the byte layout is the
        engine's to know, and a second implementation of it on this side is a
        second thing to be wrong about it.
        */
    }

    part def ResultPostprocess {
        doc /*
        The map products a solved result carries: the peak field, the rasters,
        the scalars each deliverable narrates from. It works from the fields the
        reader states and never opens the file itself.
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
    port def ServerFactsOut;
    port def ServerFactsIn;
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
    port def FieldsOut;
    port def FieldsIn;

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
        item server_facts : ServerFactsMap;
        item user_fortran : DirName [0..1];
        item coupling : String [0..1];
        item continue_from : FileName [0..1];
    }

    interface def ServerFacts {
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

        The disagreement is only worth having between INDEPENDENT readers, so
        the packet's own count is header arithmetic over a range read rather
        than the engine's reader over a full download. That independence is the
        exception NoSecondParserOfTheFormat's scope carves out: a cross-check
        sharing the reader it checks would only agree with itself.
        */
        item result_slf : FileName;
        item ntimestep : Integer;
    }

    interface def SolvedResultFields {
        doc /*
        What the engine's reader says a solved result holds: the mesh it was
        computed on, the instants it was written at, and one field per variable
        shaped (frames, nodes).

        The variable names are the engine's OWN names, with no unit glued to
        them - the record stores the two together and splitting them is the
        format knowledge that stays on the engine's side.

        The origins are REPORTED and not applied, and this hop does not require
        them: the coordinates stay as the file stores them, and every
        postprocess adds the origin it recovered from the domain bbox, so
        applying the header's would double the offset on all of them.
        */
        item varnames : StringList;
        item npoin : Integer;
        item nelem : Integer;
        item x : RealArray;
        item y : RealArray;
        item ikle : IntTable;
        item x_origin : Integer [0..1];
        item y_origin : Integer [0..1];
        item times : RealArray;
        item data : FieldMap;
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
        with no role on it, and one stating no prescription per boundary refuses
        too: it was numbered before the numbering was the engine's own.
        */
        item roles : RoleMap;
        item liquid_boundary_order : StringList;
        item liquid_boundary_prescribes : StringList;
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

    part assembler : Assembler {
        doc /*
        Both authored families - a reach and a catchment - are prepared by
        their own entry point and staged by the same flow, so there is ONE
        writer of the case and ONE writer of the server facts.

        code: trid3nt_server/workflows/telemac/authoring/assembler.py
        */
        port caseOut : CaseOut;
        port serverFactsOut : ServerFactsOut;
        port topologyIn : TopologyIn;
        port meshIn : MeshIn;
    }

    part steeringStatements : SteeringAuthor {
        doc /*
        It is a second block rather than part of the assembler because a number
        the steering file DERIVES has to be derived beside the literals it must
        agree with: the outflow stage is a normal depth at the roughness the
        very next statement writes.

        code: trid3nt_server/workflows/telemac/authoring/author.py
        */
    }

    part manifestStager : ManifestStager {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/open_water.py
        */
        port caseIn : CaseIn;
        port caseOut : CaseOut;
    }

    part solveStep : SolveStep {
        doc /*
        code: trid3nt_server/workflows/telemac/solving/solve.py
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
        port serverFactsIn : ServerFactsIn;
        port serverFactsOut : ServerFactsOut;
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
        code: trid3nt_server/workflows/telemac/products/run_reads.py
        */
    }

    part resultReader : ResultReader {
        doc /*
        code: trid3nt_server/workflows/telemac/result_reader.py
        */
        port fieldsOut : FieldsOut;
    }

    part resultPostprocess : ResultPostprocess {
        doc /*
        code: trid3nt_server/workflows/telemac/products/postprocess_telemac.py
        */
        port fieldsIn : FieldsIn;
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
        connect meshAcceptance.meshOut to assembler.meshIn;

    interface topologyRead : TopologyBundle
        connect topologyWriter.topologyOut to assembler.topologyIn;

    interface caseAuthored : ManifestCaseSection
        connect assembler.caseOut to manifestStager.caseIn;

    interface caseStaged : ManifestCaseSection
        connect manifestStager.caseOut to workerEntrypoint.caseIn;

    interface serverFactsStaged : ServerFacts
        connect assembler.serverFactsOut to workerEntrypoint.serverFactsIn {
        doc /*
        pass-through: workerEntrypoint
        */
    }

    interface serverFactsReturned : ServerFacts
        connect workerEntrypoint.serverFactsOut to launcherArm.metricsIn {
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

    interface fieldsRead : SolvedResultFields
        connect resultReader.fieldsOut to resultPostprocess.fieldsIn;

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

    requirement def ServerFactsDoctrine {
        doc /*
        Server-known facts are stated by the deck, copied by the worker
        VERBATIM, and never re-derived in the container. Worker-measured facts
        are the worker's own: the frame count is measured off the file the
        server facts name, and an unmeasurable result is the ABSENCE of the key
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

        forbid: trid3nt_server.workflows.telemac.products.run_reads -> workers
        forbid: trid3nt_server.workflows.solver.diagnostics -> workers
        */
    }

    requirement def NoSecondParserOfTheFormat {
        doc /*
        A result file's byte layout is the engine's to know. The parser this
        side used to carry was wrong about it twice: it refused a truncated
        result the engine reads without complaint, and it handed every consumer
        a variable name with the record's unit still glued on.

        SCOPE: the FIELD DATA a delivery renders. One reader of the format's
        fields - the engine's own, inside the image that wrote them - and no
        second parser of fields anywhere. The packet's frame count is outside
        that scope and stays hand-rolled header arithmetic by design: it is the
        independent reader FrameCountCrossCheck exists to disagree with the
        worker's number, and converting it would make the cross-check agree
        with itself.

        forbid: trid3nt_server.workflows.telemac.result_reader -> struct
        forbid: trid3nt_server.workflows.telemac.products.postprocess_telemac -> struct
        forbid: trid3nt_server.workflows.telemac.products.run_reads -> struct
        forbid: trid3nt_server.workflows.telemac.authoring.assembler -> struct
        */
    }

    requirement def OutflowStageIsNormalDepth {
        doc /*
        SIGNED DECISION - the reach deck's outflow stage is the NORMAL DEPTH for
        the discharge the same deck prescribes (bathymetry methodology M4,
        signed 2026-09-02), replacing the outflow bed plus a declared 2 m. Under
        the HAPPY PATH FIRST, SYNTHETIC DEFERRED amendment this is the whole of
        the Producer stage: it produces no bathymetry, it computes a level over
        bathymetry already measured, which is why it stands while the synthetic
        producer does not.

        The four inputs are the run's own. The friction slope is the fall the
        accepted mesh carries between its two role faces, over the length of the
        line that mesh was built on. The channel is the transect the outflow face
        cuts through the painted bed, read in boundary-walk order so the face is
        a section rather than a scatter. The roughness is the coefficient this
        deck goes on to write, under the law it goes on to write. The discharge
        is the one it prescribes upstream. Nothing external enters - no gauge, no
        rating curve, no second vertical datum - which is what makes the stage
        internally consistent with whatever bed the ladder delivered, and is the
        published reason it is the community default when the bed came off a
        surface rather than a survey.

        The value it replaced was not a property of the reach: 2 m was the same
        number on a mountain creek and a coastal plain river, on the one boundary
        the run's entire water surface is anchored to. ``init_depth_m`` keeps its
        other role, the constant depth a fresh run starts from, and moving it no
        longer moves the boundary.

        Uniform flow is a numerically convenient fiction rather than a measured
        boundary, so every input it cannot measure REFUSES by name: a reach with
        no measured fall, an outflow face with no painted section, a friction law
        whose coefficient no conveyance reads, a discharge or roughness that is
        not positive. Defaulting past any of them would put an underived level
        back on that boundary with nothing saying so.
        */
    }

    requirement def BoundaryCodesMatchTheSteering {
        doc /*
        A TELEMAC boundary states itself TWICE - as a (LIHBOR, LIUBOR, LIVBOR,
        LITBOR) quad on every node of its face in the boundary file, and as a
        value at its own number in PRESCRIBED FLOWRATES or PRESCRIBED ELEVATIONS
        in the steering file - and the engine reads the second only where the
        first says to. bord.f consumes an elevation under LIHBOR = KENT and a
        flowrate under LIUBOR = KENT, and nowhere else, so two files decided
        apart disagree in SILENCE: the number is written, never read, and the
        face runs on what its code alone means.

        ONE decision therefore owns both. The role-to-quad table in the pair
        writer is that decision; the quad lands in the boundary file and the
        steering keyword is derived from the SAME quad, carried to the author on
        the topology bundle. Flipping the table moves both files together, and
        a boundary whose quad prescribes nothing refuses rather than being
        written into a list it does not read.

        The number a value is written AT is the other half of the same contract,
        and it is measured by the engine's own rule (bief/front2.f): each contour
        is walked from the south-westernmost boundary point, a segment is solid
        when either end is, and the run straddling that start folds back into one
        boundary. Numbering from file-row order agreed only by luck. On a reach
        whose inflow face holds the domain's south-west corner the two disagreed,
        and the deck then stated its level at the inflow's number and its
        flowrate at the outflow's: the inflow supplied nothing, the outflow was
        clamped to elevation zero, and the run drained its initial condition
        while both prescribed numbers sat unread. That run reported CORRECT END
        OF RUN, which is why this is a modeled contract and not a comment.
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement BoundaryCodesMatchTheSteering by topologyWriter;
    satisfy requirement BoundaryCodesMatchTheSteering by assembler;
    satisfy requirement BoundaryCodesMatchTheSteering by steeringStatements;
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/test_telemac_boundary_contract.py::test_flipping_the_strategy_moves_the_quad_and_the_keyword_together";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/test_telemac_boundary_contract.py::test_the_engine_numbers_from_its_own_south_west_corner_not_from_row_order";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/test_telemac_boundary_contract.py::test_a_liquid_run_that_straddles_the_first_row_is_ONE_boundary";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/test_telemac_boundary_contract.py::test_the_run_prescribes_at_the_number_whose_quad_reads_it";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/test_telemac_boundary_contract.py::test_a_boundary_whose_quad_prescribes_nothing_refuses_rather_than_writing";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/test_mesh_topology_and_bed.py::test_a_bundle_that_states_no_prescription_per_boundary_refuses";

    satisfy requirement OutflowStageIsNormalDepth by steeringStatements;
    satisfy requirement OutflowStageIsNormalDepth by assembler;
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_the_stage_is_the_depth_at_which_the_section_conveys_the_discharge";
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_the_friction_slope_is_the_measured_fall_over_the_measured_length";
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_the_outflow_face_is_measured_as_a_transect_of_the_painted_bed";
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_the_reach_length_is_walked_along_the_line_the_mesh_was_built_over";
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_a_bigger_discharge_stands_higher_in_the_same_channel";
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_strickler_and_its_reciprocal_manning_derive_the_same_stage";
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_an_input_the_stage_cannot_be_derived_from_refuses_by_name";
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_the_run_states_the_stage_and_every_input_it_was_derived_from";
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_the_initial_depth_is_no_longer_the_level_the_run_is_anchored_at";
    verify requirement OutflowStageIsNormalDepth by
        "tests/test_telemac_outflow_stage.py::test_the_stage_is_derived_at_the_roughness_the_run_goes_on_to_write";

    satisfy requirement MetricsAlways by workerEntrypoint;
    verify requirement MetricsAlways by
        "workers/telemac/test_entrypoint.py::test_a_child_that_dies_still_leaves_the_metrics_written";

    satisfy requirement ServerFactsDoctrine by assembler;
    satisfy requirement ServerFactsDoctrine by workerEntrypoint;
    verify requirement ServerFactsDoctrine by
        "workers/telemac/test_entrypoint.py::test_a_clean_child_that_wrote_its_results_is_the_run_succeeding";
    verify requirement ServerFactsDoctrine by
        "workers/telemac/test_entrypoint.py::test_the_frame_count_is_measured_off_the_file_the_facts_name";
    verify requirement ServerFactsDoctrine by
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
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement NoSecondParserOfTheFormat by resultReader;
    satisfy requirement NoSecondParserOfTheFormat by resultPostprocess;
    satisfy requirement NoSecondParserOfTheFormat by runReader;
    satisfy requirement NoSecondParserOfTheFormat by assembler;
    verify requirement NoSecondParserOfTheFormat by
        "tests/test_telemac_result_reader.py::test_no_reader_on_this_side_parses_the_format";
    verify requirement NoSecondParserOfTheFormat by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement ReadersNeverImportTheWorker by runReader;
    satisfy requirement ReadersNeverImportTheWorker by diagnosticsReader;
    verify requirement ReadersNeverImportTheWorker by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";
}
