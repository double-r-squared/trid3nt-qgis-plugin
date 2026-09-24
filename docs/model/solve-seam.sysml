// plane: workflow | system: assembler -> solver -> products
//
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
    One authored run goes out as a manifest, one container solves it, and one
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

    part def StagedCheck {
        doc /*
        The last read before the image: the run directory the box will receive,
        checked against itself clause by clause. It reads the STAGED artifacts
        rather than what the authoring believes it wrote, so it can disagree
        with the author.
        */
    }

    part def BedPainter {
        doc /*
        The one op that gives every node of a domain its elevation, off the one
        source the bed slot carries - and refuses, by name and by count, the
        nodes that source never measured.
        */
    }

    part def Serializer {
        doc /*
        The steering files themselves - every keyword the run is solved under,
        written by telapy against the engine's own dictionary from the sheet a
        template filled. There is no second writer of the format.
        */
    }

    part def UniformFlow {
        doc /*
        The depth a measured section conveys a flow at. ONE derivation, read as a
        reach's outflow stage and as a catchment outlet's whole Z(Q) curve.
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
        image that wrote them, and the primitive set a module's outputs are
        named from - a field, a series, an envelope, the extent, the mesh, the
        mass balance - each a read of those fields. It parses nothing: the byte
        layout is the engine's to know, and a second implementation of it on
        this side is a second thing to be wrong about it.
        */
    }

    part def DiagnosticsReader {
        doc /*
        The engine-agnostic diagnostics face: reads the folded completion
        extras first and the worker's own report only as a fallback.
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

    // ----------------------------------------------------------------- //
    // Interfaces - the seam contracts, by their real key names
    // ----------------------------------------------------------------- //

    interface def ManifestCaseSection {
        doc /*
        The CASE a worker runs: which engine, which steering file it reads, and
        which files must exist for the run to have happened. The section key is
        what the entrypoint dispatches on, and the strict gate refuses any key
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
        item cores : Integer [0..1];
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
        boundary refuses, because a steering file cannot be authored against a
        boundary with no role on it, and one stating no prescription per boundary
        refuses too: it was numbered before the numbering was the engine's own.
        */
        item roles : RoleMap;
        item liquid_boundary_order : StringList;
        item liquid_boundary_prescribes : StringList;
    }

    interface def AcceptedMeshRecord {
        doc /*
        The recipe-frozen artifact's fields the authoring consumes. The
        granularity the run records is the one the ACCEPTED mesh was built at,
        measured on its own cells - never the edge that was asked for.
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

    part opening : Assembler {
        doc /*
        THE SETTLE every authored family that stands in open water passes
        through - a reach, a catchment, a basin - handing the same staging one
        case, so there is ONE writer of the server facts.

        code: trid3nt_server/workflows/telemac/authoring/opening.py
        */
        port serverFactsOut : ServerFactsOut;
        port topologyIn : TopologyIn;
    }

    part acceptedMesh : Assembler {
        doc /*
        What ANY accepted mesh hands the author, read once for every stage that
        measures it: the staged files, the nodes, the bed and the channel a
        boundary face cuts.

        code: trid3nt_server/workflows/telemac/authoring/accepted_mesh.py
        */
        port meshIn : MeshIn;
    }

    part ratingCurve : Assembler {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/rating_curve.py
        */
    }

    part referenceSurface : Assembler {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/reference_surface.py
        */
    }

    part walkedBoundary : Assembler {
        doc /*
        A harbour is settled by the wave that enters it rather than by a level
        the water stands at, so it walks its own boundary and states its own
        server facts.

        code: trid3nt_server/workflows/telemac/authoring/walked_boundary.py
        */
    }

    part releasePoint : Assembler {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/release_point.py
        */
    }

    part stagedCheck : StagedCheck {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/staged_check.py
        */
    }

    part bedPainter : BedPainter {
        doc /*
        code: trid3nt_server/workflows/mesh/shared/primitives.py
        */
    }

    part steeringStatements : Serializer {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/serializer.py
        */
    }

    part uniformFlow : UniformFlow {
        doc /*
        It is its own block rather than part of the assembler because the number
        it derives has to be derived beside the roughness the deck goes on to
        write, and because two families read the same derivation: a reach reads
        it at one discharge, a catchment outlet reads it swept over a range.

        code: trid3nt_server/workflows/telemac/helpers/uniform_flow.py
        */
    }

    part manifestStager : ManifestStager {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/staging.py
        */
        port caseOut : CaseOut;
    }

    part solveStep : SolveStep {
        doc /*
        code: trid3nt_server/workflows/telemac/engine.py
        */
        port completionIn : CompletionIn;
    }

    part launcherArm : LauncherArm {
        doc /*
        code: trid3nt_server/workflows/telemac/engine.py
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
        code: trid3nt_server/workflows/telemac/modules/listing.py
        */
    }

    part resultReader : ResultReader {
        doc /*
        code: trid3nt_server/workflows/telemac/modules/outputs.py
        */
    }

    part diagnosticsReader : DiagnosticsReader {
        doc /*
        code: trid3nt_server/workflows/solver/diagnostics/telemac.py
        */
        port completionIn : CompletionIn;
        port listingIn : ListingIn;
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
        connect meshAcceptance.meshOut to acceptedMesh.meshIn;

    interface topologyRead : TopologyBundle
        connect topologyWriter.topologyOut to opening.topologyIn;

    interface caseStaged : ManifestCaseSection
        connect manifestStager.caseOut to workerEntrypoint.caseIn;

    interface serverFactsStaged : ServerFacts
        connect opening.serverFactsOut to workerEntrypoint.serverFactsIn {
        doc /*
        pass-through: workerEntrypoint
        */
    }

    interface serverFactsReturned : ServerFacts
        connect workerEntrypoint.serverFactsOut to launcherArm.metricsIn {
        doc /*
        pass-through: workerEntrypoint
        pass-through: launcherArm
        */
    }

    interface metricsWritten : WorkerRunReport
        connect workerEntrypoint.metricsOut to launcherArm.metricsIn {
        doc /*
        pass-through: launcherArm
        */
    }

    interface completionFolded : FoldedRunPhysics
        connect launcherArm.completionOut to supervisor.completionIn {
        doc /*
        pass-through: launcherArm
        pass-through: supervisor
        */
    }

    interface frameCountFolded : FrameCountCrossCheck
        connect launcherArm.completionOut to supervisor.completionIn {
        doc /*
        pass-through: launcherArm
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
        Server-known facts are stated by the server, copied by the worker
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

    requirement def OnePartitionStatedTwice {
        doc /*
        A solve is sized in CORES and nothing else: one runtime lever holding
        the number itself, whose default is the module's own PARALLEL
        PROCESSORS value and which refuses by name past the cores this box has
        rather than being cut down to fit. The engine is told the number by
        that same keyword and the worker's launcher is handed it too, because a
        count the launcher partitions on that the steering file does not state
        is a run whose deck describes a partition it never had.

        A partitioned case therefore takes the module's own CLI launcher, which
        runs the partitioner and mpirun: the stepped telapy arm drives one
        process with no partitioner behind it, so a core count on that arm
        would be a number the deck states and the run never gets. One core is
        the keyword's own default and states nothing at all, and a module whose
        dictionary spells no processor keyword says on the card that it runs
        serial rather than leaving the lever looking like it did something.
        */
    }

    requirement def AContinuationIsARerunLedgerRow {
        doc /*
        A run that carries another on is a DERIVATION of it, not a value some
        question declares: rerun_workflow names the run whose ending state this
        one opens at, the ledger stages that run's own result as the previous
        computation, and the record says which run was continued. The cut is
        the step that reads the run's continuation, so a hot start re-solves
        while inheriting the world the parent measured.

        Naming the file IS the continuation: the engine reads its last record
        as the initial state and the deck's own initial-condition statements go
        unread. The staged file is the parent's result, whose format is the
        keyword's own default, so nothing states that. Every forcing file the
        child writes is read on the clock the parent ended at, because a table
        that does not span the run's own instants stops the reader.
        */
    }

    requirement def ExecutorKnowsNoEngine {
        doc /*
        One executor over one file per engine: the executor stages, launches,
        supervises and polls, and an engine contributes its spec, its verdict
        on an exit and its wait. An executor that imports an engine has an
        engine's knowledge in it, and the next engine arrives as a branch.

        forbid: trid3nt_server.workflows.solver -> trid3nt_server.workflows.telemac
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
        fields - the SELAFIN library's, called in this process by selafin_io -
        and no second parser of fields anywhere. It is not the engine's reader
        inside the image: that one costs a container per file and cannot open a
        3D result through the renderer at all. The packet's frame count is outside
        that scope and stays hand-rolled header arithmetic by design: it is the
        independent reader FrameCountCrossCheck exists to disagree with the
        worker's number, and converting it would make the cross-check agree
        with itself.

        forbid: trid3nt_server.workflows.telemac.modules.outputs -> struct
        forbid: trid3nt_server.workflows.telemac.products.postprocess_telemac -> struct
        forbid: trid3nt_server.workflows.telemac.products.run_reads -> struct
        forbid: trid3nt_server.workflows.telemac.authoring.opening -> struct
        forbid: trid3nt_server.workflows.telemac.authoring.release_point -> struct
        */
    }

    requirement def NothingLeavesTheDaemonThatDiesInTheFirstSecond {
        doc /*
        One typed check stands between the authored run directory and the image,
        and each of its clauses refuses by name: every file the steering names is
        present with the module's own spelling, the boundary file's node count
        and numbering match the walk over the geometry's own connectivity, every
        liquid face the boundary file opens carries a value in the list the
        engine reads it from, a series covers the run's window, the bed is whole
        over the mesh, the clock the deck states is the window the fill states,
        and the partition fits the cores this box has.

        It reads the STAGED artifacts - the deck as written, the pair as staged,
        the table as written - rather than the values the authoring held, because
        a check that reads the author's own numbers back agrees with itself. A
        run that would stop inside Fortran in its first second is refused here,
        where the refusal can name which file disagrees with which.
        */
    }

    requirement def OutflowStageIsNormalDepth {
        doc /*
        The reach run's outflow stage is the NORMAL DEPTH for the discharge the
        same run prescribes (bathymetry methodology M4). Under the deferral of
        synthetic bathymetry this is the whole of the Producer stage: it
        produces no bathymetry, it computes a level over bathymetry already
        measured, which is why it stands while the synthetic producer does not.

        The four inputs are the run's own. The friction slope is the fall the
        accepted mesh carries between its two role faces, over the length of the
        line that mesh was built on. The channel is the transect the outflow face
        cuts through the painted bed, read in boundary-walk order so the face is
        a section rather than a scatter. The roughness is the coefficient this
        run goes on to write, under the law it goes on to write. The discharge
        is the one it prescribes upstream. Nothing external enters - no gauge, no
        rating curve, no second vertical datum - which is what makes the stage
        internally consistent with whatever bed the ladder delivered, and is the
        published reason it is the community default when the bed came off a
        surface rather than a survey.

        The value it replaced was not a property of the reach: 2 m was the same
        number on a mountain creek and a coastal plain river, on the one boundary
        the run's entire water surface is anchored to. The SAME derivation is
        now also what a fresh run STARTS from: the normal DEPTH, laid
        bed-parallel, which is the uniform-flow surface the outflow stage is the
        downstream end of - so the initial free surface and the prescribed
        outflow agree by construction and the reach opens at its own equilibrium.
        ``init_depth_m`` had no role left once that followed: a blanket 2 m start
        drained into the derived boundary over the first minutes of every
        horizon, which was a transient the run had to spend before it was
        answering the question it was asked.

        Bed-parallel, and NOT a constant elevation at the stage. The stage is
        derived only where the reach falls - the derivation refuses a reach that
        does not - so a horizontal surface at the outlet's level leaves every
        node above it dry, the prescribed-flowrate face among them, and the
        engine refuses a discharge it has no water to impose (DEBIMP: PROBLEM ON
        BOUNDARY NUMBER). Measured on the flagship coarse reach: 14 of 907 nodes
        wet, exit code 2. The two statements carry the same number and only one
        of them is a river. Spin-up as REFINED-run behaviour is a separate, later
        choice; this is the fresh-run start.

        Uniform flow is a numerically convenient fiction rather than a measured
        boundary, so every input it cannot measure REFUSES by name: a reach with
        no measured fall, an outflow face with no painted section, a friction law
        whose coefficient no conveyance reads, a discharge or roughness that is
        not positive. Defaulting past any of them would put an underived level
        back on that boundary with nothing saying so.

        A reach that does not fall is the one of those with a measurement for an
        answer. Where the two role faces sit level - a reach cut from a surface
        DEM, where the water top is the floor - there is no uniform-flow depth
        to derive at all, and the outflow HOLDS at a level that was measured
        instead: an ELEVATION on the datum the bed is painted on, which is what
        a number handed to that slot means. A level at or below the section's
        own deepest node refuses by naming both numbers, because a height above
        a gauge's own zero is a different surface and adopting one silently is
        the second vertical datum this derivation exists without.
        */
    }

    requirement def CatchmentOutletHoldsADerivedRatingCurve {
        doc /*
        SIGNED DECISION - the rain-on-grid catchment outlet holds a DERIVED
        stage-discharge curve, which is the ONE fact a subcritical outlet takes
        from outside its own interior. It replaces a boundary that prescribed a
        level and had no value written at its number: bord.f then fell through to
        the boundary file's own zero, and the outlet was a hard zero-DEPTH
        Dirichlet on the one face the basin drains through. Measured on the
        Coweeta design storm: all three outlet nodes at 0.0000 m in every frame.

        The curve is the SAME uniform-flow derivation the reach's outflow stage
        is, evaluated over a range instead of at one discharge, and every input
        is measured on the accepted mesh: the section the outlet face cuts
        through the painted bed, the bed gradient over the elements that face
        belongs to, the roughness the run's own friction field carries there, and
        the bottom-friction law the deck goes on to write. Nothing external
        enters. A gauged curve is the calibration-era swap through this same
        keyword, which is why the mechanism is the curve rather than a constant.

        The RANGE is a bound, not a guess: every drop the run holds falls on the
        mesh, so the gross rain rate over the meshed area - the storm's peak
        intensity times the triangulation's own summed area - is a ceiling on the
        outlet flux, because infiltration only removes water and storage only
        delays it. The curve is swept from the dry section up to the stage that
        ceiling stands at, its points spaced uniformly in STAGE because the
        engine interpolates the level linearly between them.

        Everything it cannot measure refuses by name: a flat bed at the outlet
        has no uniform-flow depth, an unpainted face has no section, a friction
        field with no positive roughness at the outlet has no conveyance, and a
        storm of zero has no range. Defaulting past any of them would put the
        clamp back with nothing saying so.
        */
    }

    requirement def OneNumberDefinesTheStructure {
        doc /*
        The declared structure's HALF-WIDTH is what the mesher removes water
        inside and what the deck calls solid. A survey maps a breakwater as a
        centreline, the recipe punches the centreline buffered by half the
        declared width, and the water inside that footprint is gone - so a
        boundary node stands on the punched outline exactly when it lies within
        the half-width of the centreline, and there is no other way for a
        boundary node to be there.

        Two numbers let the two disagree. A band sized off the element edge ALONE
        was measured at 7.7 m against a 10 m cut on the om2d arm: it sat entirely
        inside the footprint, took ZERO boundary nodes, and the harbour solved
        with its breakwater as absorbing shore - the sheltering answer read off a
        structure the deck never stated.

        What a node is measured against is the outline plus the mesh's OWN edge,
        because a relaxation places a node on a locked outline to within the edge
        it was built at. Point Judith, measured: 176 boundary nodes at 9-11 m off
        a 20 m structure with nothing at all between 11 and 12 m, so an equality
        at 10.000 m cuts one population in half and alternates along the walk.

        And the two roles settle as RUNS, not as scatters of nodes: front2.f
        refuses "a solid point between two liquid points" and the reverse by
        name, so a lone node whose two walk neighbours agree with each other
        takes their role. This is the mesh seam's own
        BoundaryRolesAreContiguousRuns, applied where the harbour settle was
        deciding node by node.
        */
    }

    requirement def EveryNodeCarriesAMeasuredBed {
        doc /*
        A domain is a polygon a question was asked over; a survey says where its
        floor was sounded, and the two disagree over an arm or a slip. The domain
        is NOT narrowed to the survey: the answer to "the survey does not reach
        this water" is a bed that does.

        So the bed slot takes ONE source and the composition is a spatial MERGE
        of the two available layers, done by a derive before the op: the survey
        wins every cell it measured, the wider surface fills the rest, and the
        sidecar records which input painted each cell. A ladder would pick one
        source for the whole domain and could not say that; this picks one per
        cell and writes it down. The merge lands both on the wider surface's
        zero - through the offset the call states, else the one the survey
        publishes about itself, reading a surface of DEPTHS the other way round
        on the way - and refuses naming both frames where neither exists, as it
        refuses a surface that states no zero at all: a floor whose zero nobody
        carried is a floor nobody can place.

        What survives of the clip is the REFUSAL. The painter names the count of
        nodes the one source did not measure rather than filling them, because a
        node given an elevation nobody sounded is a bed the run then answers
        over.
        */
    }

    requirement def ADredgeCutsOnlyWhatTheStockHolds {
        doc /*
        A dredge states a GRADE as a depth under the surface the run opens on,
        and a STOCK as the thickness of erodible bed under the fairway. The two
        are one statement: the cut the grade asks for at the shallowest node it
        works is material that has to come out of that stock.

        A grade the stock cannot reach is not caught by the engine at authoring.
        NESTOR digs at the stated rate until the bed under the dredger stops
        being one it may cut, and then abandons the solve part-way through the
        first pass, reporting a node number and an active layer thickness about
        a channel nobody meant to ask for. A stated grade and a stated stock are
        both known before the case is written, so the deepest cut over the dug
        area is measured there, against the bed the ACCEPTED mesh carries, and a
        conflict refuses naming the node, its bed, the grade and the stock.
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
        the topology bundle. Flipping the table moves both files together.

        The table's FREE-EXIT row is the case where prescribing nothing is a
        stated answer rather than a gap: an all-KSORT quad leaves bord.f
        overriding neither the depth nor the velocity, so the water leaves at
        whatever the interior brings to the face. The steering file then writes
        no value there and says which number it is declining to write at. A quad
        that prescribes nothing under ANY OTHER role still refuses: the same
        word then means the two files were decided apart, and a value written
        into a list the engine does not read is the silence this contract exists
        to end.

        The row is vocabulary, not a recommendation, and it has no live caller.
        A free exit is well-posed only while the normal velocity LEAVES:
        propin_telemac2d.f refuses a free velocity whose normal component enters
        (ILL-POSED PROBLEM, ENTERING FREE VELOCITY), and a rain-fed catchment
        outlet reverses. Measured on the Coweeta design storm: 14 such warnings,
        +29,425 m3/s injected through the outlet in one printout, a domain taken
        from 1.90e6 to 1.50e7 m3 against a 4.41e6 m3 storm, 68.63 m depths and a
        runoff volume of zero - with CORRECT END OF RUN and continuity at 1e-15,
        because the engine conserved exactly what it wrongly did.

        The table's RATING-CURVE row is the case where the quad alone cannot say
        the whole condition. Its codes are the outflow's, because bord.f reads a
        stage-discharge curve only where the depth is prescribed, so quad-derived
        `prescribes` reads "elevation" for both - and the two are still different
        boundaries: an outflow owes a constant in PRESCRIBED ELEVATIONS, a rating
        curve owes STAGE-DISCHARGE CURVES = 1 at its own number and the curve
        file the level is interpolated out of. The ROLE is what the steering
        author reads to tell them apart, exactly as it reads FREE_EXIT to tell a
        stated silence from a disagreement. One table, two files, still.

        The number a value is written AT is the other half of the same contract,
        and it is measured by the engine's own rule (bief/front2.f): each contour
        is walked from the south-westernmost boundary point, a segment is solid
        when either end is, and the run straddling that start folds back into one
        boundary. Numbering from file-row order agreed only by luck. On a reach
        whose inflow face holds the domain's south-west corner the two disagreed,
        and the run then stated its level at the inflow's number and its
        flowrate at the outflow's: the inflow supplied nothing, the outflow was
        clamped to elevation zero, and the run drained its initial condition
        while both prescribed numbers sat unread. That run reported CORRECT END
        OF RUN, which is why this is a modeled contract and not a comment.
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement OneNumberDefinesTheStructure by walkedBoundary;
    verify requirement OneNumberDefinesTheStructure by
        "tests/telemac/test_open_water_domains.py::test_the_footprint_and_the_solid_faces_are_cut_at_the_same_width";
    verify requirement OneNumberDefinesTheStructure by
        "tests/telemac/test_open_water_domains.py::test_a_boundary_node_is_on_the_structure_when_it_stands_on_the_punched_outline";
    verify requirement OneNumberDefinesTheStructure by
        "tests/telemac/test_open_water_domains.py::test_a_lone_node_between_two_of_another_kind_is_not_a_face";

    satisfy requirement EveryNodeCarriesAMeasuredBed by bedPainter;
    verify requirement EveryNodeCarriesAMeasuredBed by
        "tests/mesh/test_bed_sources_and_runs.py::test_a_domain_no_source_covers_refuses_by_name";
    verify requirement EveryNodeCarriesAMeasuredBed by
        "tests/mesh/test_bed_sources_and_runs.py::test_the_bed_is_one_source_and_two_are_merged_before_the_op";
    verify requirement EveryNodeCarriesAMeasuredBed by
        "tests/inputs/test_bed_merge.py::test_the_primary_wins_where_it_measured_and_the_fallback_fills_the_rest";
    verify requirement EveryNodeCarriesAMeasuredBed by
        "tests/inputs/test_bed_merge.py::test_the_sidecar_says_which_input_painted_each_cell";
    verify requirement EveryNodeCarriesAMeasuredBed by
        "tests/inputs/test_bed_merge.py::test_two_datums_refuse_by_name";

    satisfy requirement ADredgeCutsOnlyWhatTheStockHolds by referenceSurface;
    verify requirement ADredgeCutsOnlyWhatTheStockHolds by
        "tests/telemac/test_telemac_nestor_dredge.py::test_a_grade_the_bed_has_no_stock_to_reach_refuses_at_authoring";

    satisfy requirement OnePartitionStatedTwice by manifestStager;
    satisfy requirement OnePartitionStatedTwice by steeringStatements;
    satisfy requirement OnePartitionStatedTwice by workerEntrypoint;
    verify requirement OnePartitionStatedTwice by
        "tests/telemac/test_telemac_cores.py::test_a_run_that_states_no_count_leaves_the_modules_own_keyword_standing";
    verify requirement OnePartitionStatedTwice by
        "tests/telemac/test_telemac_cores.py::test_a_count_past_this_box_refuses_by_name";
    verify requirement OnePartitionStatedTwice by
        "tests/telemac/test_telemac_cores.py::test_the_stated_count_states_the_keyword_the_engine_reads_it_under";
    verify requirement OnePartitionStatedTwice by
        "tests/telemac/test_telemac_cores.py::test_the_case_carries_the_same_count_the_steering_file_states";
    verify requirement OnePartitionStatedTwice by
        "tests/telemac/test_telemac_cores.py::test_a_module_that_spells_no_processor_keyword_says_so_on_the_card";
    verify requirement OnePartitionStatedTwice by
        "tests/telemac/test_telemac_cores.py::test_the_direct_solver_runs_serial_whatever_was_asked_for";
    verify requirement OnePartitionStatedTwice by
        "workers/telemac/test_entrypoint.py::test_a_partitioned_solve_takes_the_launcher_arm_with_the_same_count";
    verify requirement OnePartitionStatedTwice by
        "workers/telemac/test_entrypoint.py::test_a_serial_solve_still_runs_in_process_and_states_no_count";

    satisfy requirement AContinuationIsARerunLedgerRow by opening;
    satisfy requirement AContinuationIsARerunLedgerRow by workerEntrypoint;
    verify requirement AContinuationIsARerunLedgerRow by
        "tests/runtime/test_rerun_with_overrides.py::test_the_cut_of_a_hot_start_is_the_step_that_opens_on_the_other_run";
    verify requirement AContinuationIsARerunLedgerRow by
        "tests/runtime/test_rerun_with_overrides.py::test_a_continuation_is_a_different_invocation_than_the_run_it_continues";
    verify requirement AContinuationIsARerunLedgerRow by
        "tests/runtime/test_rerun_with_overrides.py::test_a_run_with_no_solved_state_cannot_be_continued";
    verify requirement AContinuationIsARerunLedgerRow by
        "tests/runtime/test_rerun_with_overrides.py::test_the_record_of_a_continued_run_says_which_run_it_continued";
    verify requirement AContinuationIsARerunLedgerRow by
        "tests/telemac/test_telemac_reach_staged_inputs.py::test_a_continuation_is_a_rerun_row_and_no_template_declares_it";
    verify requirement AContinuationIsARerunLedgerRow by
        "tests/telemac/test_telemac_module_surface.py::test_a_continuation_names_the_file_and_nothing_else";

    satisfy requirement BoundaryCodesMatchTheSteering by topologyWriter;
    satisfy requirement BoundaryCodesMatchTheSteering by opening;
    satisfy requirement BoundaryCodesMatchTheSteering by steeringStatements;
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/telemac/test_telemac_boundary_contract.py::test_flipping_the_strategy_moves_the_quad_and_the_keyword_together";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/telemac/test_telemac_boundary_contract.py::test_the_engine_numbers_from_its_own_south_west_corner_not_from_row_order";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/telemac/test_telemac_boundary_contract.py::test_a_liquid_run_that_straddles_the_first_row_is_ONE_boundary";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/telemac/test_telemac_boundary_contract.py::test_the_run_prescribes_at_the_number_whose_quad_reads_it";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/telemac/test_telemac_boundary_contract.py::test_a_boundary_whose_quad_prescribes_nothing_refuses_rather_than_writing";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/telemac/test_telemac_boundary_contract.py::test_the_free_exit_role_prescribes_nothing_as_a_stated_choice";
    verify requirement BoundaryCodesMatchTheSteering by
        "tests/mesh/test_mesh_topology_and_bed.py::test_a_bundle_that_states_no_prescription_per_boundary_refuses";

    satisfy requirement CatchmentOutletHoldsADerivedRatingCurve by ratingCurve;
    satisfy requirement CatchmentOutletHoldsADerivedRatingCurve by uniformFlow;
    verify requirement CatchmentOutletHoldsADerivedRatingCurve by
        "tests/telemac/test_telemac_outflow_stage.py::test_the_rating_curve_starts_dry_and_ends_at_the_stated_ceiling";
    verify requirement CatchmentOutletHoldsADerivedRatingCurve by
        "tests/telemac/test_telemac_outflow_stage.py::test_the_curve_rises_monotonically_so_the_engine_can_interpolate_it";
    verify requirement CatchmentOutletHoldsADerivedRatingCurve by
        "tests/telemac/test_telemac_outflow_stage.py::test_every_point_is_the_normal_depth_the_reachs_own_stage_would_be";
    verify requirement CatchmentOutletHoldsADerivedRatingCurve by
        "tests/telemac/test_telemac_outflow_stage.py::test_the_curve_file_is_written_in_the_engines_own_block_format";
    verify requirement CatchmentOutletHoldsADerivedRatingCurve by
        "tests/telemac/test_telemac_outflow_stage.py::test_the_catchments_outlet_slope_is_the_bed_over_the_elements_it_touches";
    verify requirement CatchmentOutletHoldsADerivedRatingCurve by
        "tests/telemac/test_telemac_outflow_stage.py::test_a_flat_outlet_refuses_rather_than_holding_a_level_nobody_measured";
    verify requirement CatchmentOutletHoldsADerivedRatingCurve by
        "tests/telemac/test_telemac_outflow_stage.py::test_the_flow_range_is_the_gross_rain_rate_on_the_meshed_area";

    satisfy requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by stagedCheck;
    satisfy requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by manifestStager;
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_whole_staged_run_passes_every_clause";
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_file_the_steering_names_and_nobody_staged_refuses_by_name";
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_name_staged_in_another_spelling_is_named_as_that";
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_boundary_file_of_another_length_refuses_against_the_walk";
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_boundary_file_numbered_against_another_walk_refuses";
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_liquid_face_the_deck_prescribes_nothing_at_refuses";
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_series_that_stops_short_of_the_window_refuses";
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_node_the_bed_never_painted_refuses";
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_clock_whose_steps_do_not_make_the_window_refuses";
    verify requirement NothingLeavesTheDaemonThatDiesInTheFirstSecond by
        "tests/telemac/test_telemac_staged_check.py::test_a_partition_past_the_box_refuses_rather_than_being_cut_to_fit";

    satisfy requirement OutflowStageIsNormalDepth by uniformFlow;
    satisfy requirement OutflowStageIsNormalDepth by opening;
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_outflow_stage.py::test_the_stage_is_the_depth_at_which_the_section_conveys_the_discharge";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_outflow_stage.py::test_the_friction_slope_is_the_measured_fall_over_the_measured_length";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_outflow_stage.py::test_the_outflow_face_is_measured_as_a_transect_of_the_painted_bed";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_outflow_stage.py::test_the_channel_length_is_measured_between_the_two_runs_own_centres";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_outflow_stage.py::test_a_bigger_discharge_stands_higher_in_the_same_channel";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_outflow_stage.py::test_strickler_and_its_reciprocal_manning_derive_the_same_stage";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_outflow_stage.py::test_an_input_the_stage_cannot_be_derived_from_refuses_by_name";

    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_module_surface.py::test_the_open_channel_body_is_written_at_the_derivation_it_was_solved_for";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_outflow_stage.py::test_a_flatter_reach_stands_higher_for_the_same_flow";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_open_channel.py::test_a_reach_that_does_not_fall_holds_at_the_level_that_was_measured";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_open_channel.py::test_a_level_at_or_below_the_bed_refuses_rather_than_opening_dry";
    verify requirement OutflowStageIsNormalDepth by
        "tests/telemac/test_telemac_open_channel.py::test_a_reach_that_does_not_fall_and_has_no_level_refuses_by_name";

    satisfy requirement MetricsAlways by workerEntrypoint;
    verify requirement MetricsAlways by
        "workers/telemac/test_entrypoint.py::test_a_child_that_dies_still_leaves_the_metrics_written";

    satisfy requirement ServerFactsDoctrine by opening;
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
        "tests/telemac/test_telemac_engine.py::test_classify_exit_clean_exit_but_no_correct_end_is_error";

    satisfy requirement ExecutorKnowsNoEngine by supervisor;
    satisfy requirement ExecutorKnowsNoEngine by launcherArm;
    verify requirement ExecutorKnowsNoEngine by
        "tests/solver/test_executor_import_graph.py::test_the_executor_imports_no_engine";
    verify requirement ExecutorKnowsNoEngine by
        "tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement WorkersNeverImportServer by workerEntrypoint;
    satisfy requirement WorkersNeverImportServer by telapyChild;
    verify requirement WorkersNeverImportServer by
        "tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement NoSecondParserOfTheFormat by resultReader;
    satisfy requirement NoSecondParserOfTheFormat by runReader;
    satisfy requirement NoSecondParserOfTheFormat by opening;
    verify requirement NoSecondParserOfTheFormat by
        "tests/telemac/test_telemac_result_reader.py::test_no_reader_on_this_side_parses_the_format";
    verify requirement NoSecondParserOfTheFormat by
        "tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement ReadersNeverImportTheWorker by runReader;
    satisfy requirement ReadersNeverImportTheWorker by diagnosticsReader;
    verify requirement ReadersNeverImportTheWorker by
        "tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree";
}
