// The mesh seam as a checkable model.
//
// SysML v2 TEXTUAL notation, restricted to the subset the project checker
// reads. The conventions are the ones the solve seam established: ``code:``
// binds a block to the module it IS, ``forbid:`` states a dependency rule,
// ``constructor:`` names a function that builds a contract, and
// ``pass-through:`` marks an end that forwards the contract VERBATIM and
// therefore owes no item evidence and supplies none.
//
// Evidence is per interface USAGE: each hop's writer end and consumer end must
// name every non-optional item the hop carries. Every name below is the tree's
// own - item names are the literal keys the code writes and reads, and every
// verify names a test that exists.

package MeshSeam {

    doc /*
    One RECIPE goes in and one accepted mesh comes out. A mesh is defined by
    exactly one object - three mesher-agnostic params plus an ordered list of
    ops - so the recipe is the interface everything here has an end on, and the
    blocks exist to give it ends. What a mesher measures, what a primitive
    paints and what a box refuses all travel as keys, and a key one side stopped
    reading is the failure this model is drawn to catch.
    */

    // ----------------------------------------------------------------- //
    // Blocks
    // ----------------------------------------------------------------- //

    part def MeshRouter {
        doc /*
        The one door a mesh ask enters. Validates the ask into a recipe, and
        answers WHICH mesh a run uses - supplied, discovered in the case, or the
        declared build.
        */
    }

    part def RecipeObject {
        doc /*
        THE mesh-defining object. Frozen, editable only by returning a new one,
        and the whole of what a rebuild replays. It is a value: it knows nothing
        about the session that holds it or the gate that presents it.
        */
    }

    part def OpTool {
        doc /*
        The runtime face of the word a recipe is written in: append one entry,
        alter one by index, remove one. The whole of the refinement loop.
        */
    }

    part def MeshSession {
        doc /*
        The mesh under construction. Holds ONE recipe, regenerates WHOLESALE on
        every change, journals the edit events, and freezes the accepted mesh
        onto its artifact record.
        */
    }

    part def MeshGate {
        doc /*
        The one card path. Presents the built mesh with its probes and its
        numbered ops, and takes back the size word or the revert - nothing
        per-mesher, because everything an op can say is said through the op
        tool.
        */
    }

    part def MesherRegistry {
        doc /*
        What a MESHER is: namespaces, a role adapter, a default recipe. It also
        binds a recipe's ops against the real callables, which is where the
        signature is the schema.
        */
    }

    part def MesherAdapter {
        doc /*
        One mesh library's role adapter: the recipe's extent becomes this
        library's domain object, the one size word threads as its defaults, and
        what comes back is the neutral mesh every writer reads.
        */
    }

    part def MesherBox {
        doc /*
        The mesh library where it lives - a GPL-isolated image with no network
        and nothing of this server in it. One config in, arrays and a stats
        document out, or a typed refusal written as a document.
        */
    }

    part def ShippedDriver {
        doc /*
        A script this server ships INTO an image and executes there. It carries
        the library's own call and nothing of ours, and the purity law is written
        over the DIRECTORY they share - so every driver binds a block here or the
        law is unenforced for the ones that do not.
        */
    }

    part def SharedPrimitives {
        doc /*
        What we impose on a mesh that no library does: the bed, and which
        contiguous runs of the boundary carry which role. Both take the data
        class they are defined over, and neither compensates for the wrong one.
        */
    }

    part def MeshArtifactStore {
        doc /*
        The accepted mesh's record and the seams a later run rediscovers it
        through - the case-scoped stash and the durable sidecar beside the mesh
        objects.
        */
    }

    part def TopologyWriter {
        doc /*
        The accepted topology a geometry file cannot state: which stretch of the
        boundary carries which role, and in which order the solver will number
        them.
        */
    }

    // ----------------------------------------------------------------- //
    // Ports
    // ----------------------------------------------------------------- //

    port def RecipeOut;
    port def RecipeIn;
    port def OpOut;
    port def OpIn;
    port def RegistrationOut;
    port def RegistrationIn;
    port def NamespaceOut;
    port def NamespaceIn;
    port def BedOut;
    port def BedIn;
    port def RolesOut;
    port def RolesIn;
    port def FieldsOut;
    port def FieldsIn;
    port def ArtifactOut;
    port def ArtifactIn;
    port def ProbesOut;
    port def ProbesIn;
    port def ConfigOut;
    port def ConfigIn;
    port def ArraysOut;
    port def ArraysIn;
    port def StatsOut;
    port def StatsIn;
    port def RefusalOut;
    port def RefusalIn;
    port def TopologyOut;
    port def TopologyIn;

    // ----------------------------------------------------------------- //
    // Interfaces - the seam contracts, by their real key names
    // ----------------------------------------------------------------- //

    interface def MeshRecipeAsk {
        doc /*
        THE RECIPE: three mesher-agnostic params - the domain, the one size
        word, the shape - plus the ordered ops list that is the program. Engine
        vocabulary is never a param of the generalization, so a bed and a
        boundary role are entries in ``ops`` and nothing here.

        ``extent`` and ``resolution_m`` are optional because a declared ask
        carries a late-bound read until the interpreter binds it, and the
        mesher's own visible default answers for an ask that never states one.

        constructor: build_recipe
        */
        item mesher : String;
        item kind : String;
        item extent : Domain [0..1];
        item resolution_m : Real [0..1];
        item ops : OpList;
    }

    interface def RecipeOpEntry {
        doc /*
        One entry of the ops list: a function NAME and its kwargs. The name is
        VERBATIM - the wrapped library's own spelling, or a primitive under its
        real def name - because an alias is a word that implies.
        */
        item fn : String;
        item kwargs : Map;
    }

    interface def MesherRegistration {
        doc /*
        What a mesher registers, and the whole of it. A new mesher is these
        three answers and no router grows; ``deterministic`` is a MEASURED claim
        about the library rather than a hope, so it is stated only where a
        measurement stands behind it.
        */
        item build : Callable;
        item kinds : StringList;
        item default_ops : OpList;
        item namespaces : NamespaceList [0..1];
        item deterministic : Boolean [0..1];
    }

    interface def OpNamespaceDeclaration {
        doc /*
        A set of callables a recipe's ops may name, and WHEN they run. The phase
        is DERIVED from the namespace an op was registered in, so it is declared
        here and never by the caller.

        ``module`` is the real module when THIS process can import it - which is
        what lets the signature be the schema - and ``names`` is the declared
        roster for a library that lives where this process cannot import from.
        Exactly one of them is stated, so neither is required here.

        constructor: OpNamespace
        */
        item origin : String;
        item phase : String;
        item module : Module [0..1];
        item names : StringList [0..1];
    }

    interface def BedProvenance {
        doc /*
        What ACTUALLY painted the bed, carried back on the mesh so the journal
        and the artifact can both say it: the ladder rung that served, never the
        row the recipe asked for. A reader downstream tells coarse global relief
        from surveyed topobathy by this and nothing else.

        ``bed_fallback_note`` is the narration a SUBSTITUTION carried, so a bed
        painted from the row that was asked for states none.
        */
        item bed_source : String;
        item bed_fallback_note : String [0..1];
        item synthetic_inputs : RowList;
    }

    interface def BoundaryRoleRuns {
        doc /*
        Which boundary nodes carry which role, and how many contiguous RUNS each
        role landed as. The count is the number the solver's own liquid-boundary
        numbering has to agree with: a role declared across two sections is two
        boundaries, and a role that could name only one face made the second
        mouth a wall.
        */
        item boundary_roles : RoleMap;
        item boundary_role_runs : CountMap;
    }

    interface def MesherContributedFields {
        doc /*
        The artifact fields only the MESHER can state: the per-solver files it
        wrote from its own boundary numbering, and what it segmented that
        boundary into. They ride on the mesh's own meta and the session stages
        them without opinion, so it names none of them.
        */
        item slf_uri : Uri;
        item cli_uri : Uri;
        item topology_uri : Uri;
        item open_boundary_info : Map;
    }

    interface def MeshArtifactRecord {
        doc /*
        The accepted mesh's record - what a run needs to decide "can I solve on
        this?" and, once accepted, to point a solver at it. The RECIPE is frozen
        into ``provenance``, which is what makes the artifact replayable rather
        than merely described.

        constructor: MeshArtifact
        */
        item mesh_id : String;
        item name : String;
        item mode : String;
        item display_uri : Uri;
        item crs_authid : String;
        item has_bathymetry : Boolean;
        item node_count : Integer;
        item element_count : Integer;
        item bbox : RealList;
        item probes : Map;
        item provenance : Map;
        item utm_epsg : Integer [0..1];
        item recipe_uri : Uri [0..1];
        item case_id : String [0..1];
    }

    interface def MeasuredMeshProbes {
        doc /*
        The numeric facts a human or an agent judges the mesh on, measured on the
        topology that EXISTS rather than quoted back off the ask. A mesh whose
        cells the engine realizes says so and reports no edge or angle, which is
        the honest answer rather than a zero.
        */
        item node_count : Integer;
        item element_count : Integer;
        item crs_authid : String;
        item has_bed : Boolean;
        item edge_length_m : Band;
        item min_angle_deg : Real;
        item boundary_edges : Integer;
        item boundary_loops : Integer;
        item cells_realized_by_engine : Boolean [0..1];
        item ops : StringList;
    }

    interface def BoxBuildConfig {
        doc /*
        The whole ask the box is handed: the domain to cut from, the edge band in
        metres, the numbers that make a rebuild reproduce, and the ops as data.
        Exactly one domain is stated - a shoreline to cut the water side from, or
        a polygon to mesh the interior of.

        The op names travel VERBATIM and the driver calls them verbatim: their
        kwargs bind in the box, against the real signature, because the library
        is installed only where this process cannot import it.
        */
        item bbox : RealList;
        item min_edge_length_m : Real;
        item max_edge_length_m : Real;
        item seed : Integer;
        item max_iter : Integer;
        item pre_ops : OpList;
        item post_ops : OpList;
        item shoreline_shp : FileName [0..1];
        item domain_geojson : FileName [0..1];
    }

    interface def BoxMeshArrays {
        doc /*
        What the box built, as arrays: the nodes in lon/lat, the 0-based
        triangles, and the outline vertices the generator was constrained to.
        ``pfix`` comes back so the conformal offset is measured against the
        points the box actually locked rather than the ones the ask named.
        */
        item points : RealArray;
        item cells : IntTable;
        item pfix : RealArray;
    }

    interface def BoxBuildStats {
        doc /*
        What the box MEASURED about its own build, which nothing on this side can
        recompute: how far the rim ran from the ask, which library version ran,
        which sizing functions were actually active, and what its clean passes
        had to say.
        */
        item rim_edge_length_m : Band;
        item engine : String;
        item sizing_functions : StringList;
        item clean_notes : StringList;
    }

    interface def BoxRefusal {
        doc /*
        The box refusing in its own words. The refusals about the DOMAIN are only
        knowable where the library is, so they come back as a document and reach
        the caller as a typed refusal rather than as a return code wrapped in a
        stack trace - carrying, where there is one, the call that DOES what the
        refused ask could not.
        */
        item code : String;
        item message : String;
        item escalation : Map [0..1];
    }

    interface def TopologyBundle {
        doc /*
        The mesher's answers a SELAFIN cannot hold. A bundle naming no liquid
        boundary refuses, because a deck cannot be authored against a boundary
        with no role on it.

        constructor: write_topology
        */
        item roles : RoleMap;
        item liquid_boundary_order : StringList;
    }

    // ----------------------------------------------------------------- //
    // Block usages, bound to the modules they are
    // ----------------------------------------------------------------- //

    part meshRouter : MeshRouter {
        doc /*
        code: trid3nt_server/workflows/mesh/tool.py
        */
        port recipeOut : RecipeOut;
    }

    part recipeObject : RecipeObject {
        doc /*
        code: trid3nt_server/workflows/mesh/recipe.py
        */
        port recipeIn : RecipeIn;
        port opIn : OpIn;
    }

    part opTool : OpTool {
        doc /*
        code: trid3nt_server/workflows/mesh/op_tool.py
        */
        port opOut : OpOut;
    }

    part meshSession : MeshSession {
        doc /*
        code: trid3nt_server/workflows/mesh/session.py
        */
        port opIn : OpIn;
        port opOut : OpOut;
        port bedIn : BedIn;
        port fieldsIn : FieldsIn;
        port fieldsOut : FieldsOut;
        port artifactOut : ArtifactOut;
        port probesOut : ProbesOut;
    }

    part meshGate : MeshGate {
        doc /*
        code: trid3nt_server/workflows/mesh/gate.py
        */
        port probesIn : ProbesIn;
    }

    part mesherRegistry : MesherRegistry {
        doc /*
        code: trid3nt_server/workflows/mesh/meshers/__init__.py
        */
        port registrationIn : RegistrationIn;
        port namespaceIn : NamespaceIn;
    }

    part om2dAdapter : MesherAdapter {
        doc /*
        code: trid3nt_server/workflows/mesh/meshers/om2d.py
        */
        port registrationOut : RegistrationOut;
        port namespaceOut : NamespaceOut;
        port rolesIn : RolesIn;
        port fieldsOut : FieldsOut;
        port configOut : ConfigOut;
        port arraysIn : ArraysIn;
        port statsIn : StatsIn;
        port refusalIn : RefusalIn;
        port topologyOut : TopologyOut;
    }

    part regGridAdapter : MesherAdapter {
        doc /*
        The lattice conforms to the same surface with the smallest possible
        registration: no library namespace of its own, and a default recipe that
        is empty because a lattice at the one size word is the whole of what an
        undeclared ask asked for.

        code: trid3nt_server/workflows/mesh/meshers/reg_grid.py
        */
        port registrationOut : RegistrationOut;
    }

    part om2dBox : MesherBox {
        doc /*
        code: trid3nt_server/workflows/mesh/meshers/drivers/om2d_driver.py
        */
        port configIn : ConfigIn;
        port arraysOut : ArraysOut;
        port statsOut : StatsOut;
        port refusalOut : RefusalOut;
    }

    part selafinCliDriver : ShippedDriver {
        doc /*
        The TELEMAC geometry pair, written by telapy's own Hermes and Conlim
        inside the engine image.

        code: trid3nt_server/workflows/mesh/meshers/drivers/selafin_cli_driver.py
        */
    }

    part telemacCasDriver : ShippedDriver {
        doc /*
        The steering file read and written by the library's own TelemacCas
        against the module dictionary, inside the engine image.

        code: trid3nt_server/workflows/mesh/meshers/drivers/telemac_cas_driver.py
        */
    }

    part telemacResultDriver : ShippedDriver {
        doc /*
        The solved result read through the engine's own TelemacFile, inside the
        engine image, over a result directory mounted read-only.

        code: trid3nt_server/workflows/mesh/meshers/drivers/telemac_result_driver.py
        */
    }

    part sharedPrimitives : SharedPrimitives {
        doc /*
        code: trid3nt_server/workflows/mesh/shared/primitives.py
        */
        port bedOut : BedOut;
        port rolesOut : RolesOut;
    }

    part meshArtifactStore : MeshArtifactStore {
        doc /*
        code: trid3nt_server/workflows/mesh/artifact.py
        */
        port artifactIn : ArtifactIn;
        port fieldsIn : FieldsIn;
    }

    part topologyWriter : TopologyWriter {
        doc /*
        code: trid3nt_server/workflows/mesh/topology.py
        */
        port topologyIn : TopologyIn;
    }

    // ----------------------------------------------------------------- //
    // The flows
    // ----------------------------------------------------------------- //

    interface recipeDeclared : MeshRecipeAsk
        connect meshRouter.recipeOut to recipeObject.recipeIn;

    interface opEdited : RecipeOpEntry
        connect opTool.opOut to meshSession.opIn {
        doc /*
        pass-through: meshSession
        */
    }

    interface opProgrammed : RecipeOpEntry
        connect meshSession.opOut to recipeObject.opIn {
        doc /*
        pass-through: meshSession
        */
    }

    interface om2dRegistered : MesherRegistration
        connect om2dAdapter.registrationOut to mesherRegistry.registrationIn;

    interface regGridRegistered : MesherRegistration
        connect regGridAdapter.registrationOut to mesherRegistry.registrationIn;

    interface om2dNamespaces : OpNamespaceDeclaration
        connect om2dAdapter.namespaceOut to mesherRegistry.namespaceIn;

    interface bedPainted : BedProvenance
        connect sharedPrimitives.bedOut to meshSession.bedIn;

    interface rolesImposed : BoundaryRoleRuns
        connect sharedPrimitives.rolesOut to om2dAdapter.rolesIn;

    interface buildRequested : BoxBuildConfig
        connect om2dAdapter.configOut to om2dBox.configIn;

    interface meshReturned : BoxMeshArrays
        connect om2dBox.arraysOut to om2dAdapter.arraysIn;

    interface statsReturned : BoxBuildStats
        connect om2dBox.statsOut to om2dAdapter.statsIn;

    interface refusalReturned : BoxRefusal
        connect om2dBox.refusalOut to om2dAdapter.refusalIn;

    interface topologyWritten : TopologyBundle
        connect om2dAdapter.topologyOut to topologyWriter.topologyIn;

    interface fieldsCarried : MesherContributedFields
        connect om2dAdapter.fieldsOut to meshSession.fieldsIn {
        doc /*
        pass-through: meshSession
        */
    }

    interface fieldsRecorded : MesherContributedFields
        connect meshSession.fieldsOut to meshArtifactStore.fieldsIn {
        doc /*
        pass-through: meshSession
        */
    }

    interface artifactAccepted : MeshArtifactRecord
        connect meshSession.artifactOut to meshArtifactStore.artifactIn;

    interface probesPresented : MeasuredMeshProbes
        connect meshSession.probesOut to meshGate.probesIn;

    // ----------------------------------------------------------------- //
    // The standing laws this seam is governed by
    // ----------------------------------------------------------------- //

    requirement def RecipeIsTheOneMeshDefiningObject {
        doc /*
        A mesh is not a spec plus a history of edits: it is the current PROGRAM
        that produces it. There is no record object and no edit chain - the
        journal is audit, undo is editing the recipe back, and the one
        structured revert is reset-to-declaration.

        The recipe is a VALUE, so it reaches for nothing that holds it: a recipe
        that could see its session or its gate would be state with a program
        beside it, which is the shape this law replaced.

        forbid: trid3nt_server.workflows.mesh.recipe -> trid3nt_server.workflows.mesh.session
        forbid: trid3nt_server.workflows.mesh.recipe -> trid3nt_server.workflows.mesh.gate
        */
    }

    requirement def RegenerationIsWholesale {
        doc /*
        Every change swaps the recipe and rebuilds the WHOLE mesh. Nothing is
        patched incrementally, because a mesh patched from a program that no
        longer describes it is a mesh nobody can rebuild. The one thing no
        recipe produces - a hand-edit - is adopted and FLAGGED rather than
        pretended into the recipe, and a later recipe edit refuses instead of
        discarding it.
        */
    }

    requirement def CorrectDataClassAtSetBed {
        doc /*
        A bed is TOPOBATHY, and the primitive takes the data class it is defined
        over. Acquiring that class is the DATA row's problem: no ladder rung is
        permitted here on the author's behalf, because a rung this op allowed
        would be a cross-dataset bed nobody wrote down. A domain the primary does
        not cover refuses in the fetcher's own words, and what ACTUALLY painted
        the bed rides back rather than what was asked for.
        */
    }

    requirement def BoundaryRolesAreContiguousRuns {
        doc /*
        A role is a RUN, not a node set: a liquid boundary is numbered by walking
        the boundary, so a scatter of nodes near a face is not a boundary. One
        role may land several runs - a two-mouth estuary has one open boundary in
        two sections - and a node two faces both claim refuses, because it
        carries one boundary condition and picking silently would put a flowrate
        on a stretch meant to hold at a level.
        */
    }

    requirement def SignatureIsTheSchema {
        doc /*
        An op's name must exist in the registering mesher's namespaces and its
        kwargs must bind to the REAL callable's signature. There is no schema of
        ours beside the library's: a function this process cannot import has no
        signature to bind against here, so it passes through with a journaled
        note and the driver that runs it binds against the real one. An unknown
        name is refused with the nearest matches rather than ignored.
        */
    }

    requirement def ModelShrinksWithTheTree {
        doc /*
        A removal deletes the component's model elements - blocks, hops, items,
        verifies - in the SAME commit. A modeled element whose module or test is
        gone fails the checker loudly for exactly one commit's worth of
        attention, and that loud moment IS the guidance; it is never a
        preservation order. The meshers are where this bites: a library that
        leaves takes its block, its hops and its verifies with it, and the roster
        the tree carries is the roster the model states.
        */
    }

    requirement def TheBoxNeverImportsTheServer {
        doc /*
        The mesh library runs in a GPL-isolated image with no network and nothing
        of this server in it. A driver that reached into the server package would
        be running our defaults, our fetches and our opinions inside a box whose
        whole point is that it holds only the library.

        The rule is written over the DIRECTORY every shipped driver lives in, so
        it holds for the engine drivers beside the mesh box for the same reason:
        each one is executed inside an image where nothing of this server exists
        to import.

        forbid: trid3nt_server.workflows.mesh.meshers.drivers -> trid3nt_server
        forbid: trid3nt_server.workflows.mesh.meshers.drivers -> trid3nt_contracts
        */
    }

    requirement def TheMeshFrontIsEngineAgnostic {
        doc /*
        A mesh is a mesh. The three params every mesher means the same thing by
        are the whole of the generalization, and an engine's vocabulary enters
        only as an op. A mesh module that imported an engine's workflows would be
        a second place a mesh gets built, and the mesh a human approved and the
        mesh a solver ran on would be two objects that happen to agree.

        forbid: trid3nt_server.workflows.mesh -> trid3nt_server.workflows.telemac
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement RecipeIsTheOneMeshDefiningObject by recipeObject;
    satisfy requirement RecipeIsTheOneMeshDefiningObject by meshRouter;
    satisfy requirement RecipeIsTheOneMeshDefiningObject by meshSession;
    verify requirement RecipeIsTheOneMeshDefiningObject by
        "tests/test_build_mesh_tool.py::test_a_recipe_builds_nothing";
    verify requirement RecipeIsTheOneMeshDefiningObject by
        "tests/test_build_mesh_tool.py::test_editing_a_recipe_returns_a_new_frozen_one";
    verify requirement RecipeIsTheOneMeshDefiningObject by
        "tests/test_build_mesh_tool.py::test_engine_vocabulary_is_not_a_param_of_the_generalization";
    verify requirement RecipeIsTheOneMeshDefiningObject by
        "tests/test_build_mesh_tool.py::test_accept_freezes_the_recipe_as_the_artifacts_provenance";
    verify requirement RecipeIsTheOneMeshDefiningObject by
        "tests/test_mesh_declaration_travel.py::test_the_mesh_step_carries_the_recipe_whole";

    satisfy requirement RegenerationIsWholesale by meshSession;
    satisfy requirement RegenerationIsWholesale by opTool;
    verify requirement RegenerationIsWholesale by
        "tests/test_build_mesh_tool.py::test_the_same_recipe_builds_the_same_mesh_twice";
    verify requirement RegenerationIsWholesale by
        "tests/test_build_mesh_tool.py::test_reset_puts_the_recipe_back_to_the_declaration";
    verify requirement RegenerationIsWholesale by
        "tests/test_build_mesh_tool.py::test_the_journal_replays_to_an_identical_mesh";
    verify requirement RegenerationIsWholesale by
        "tests/test_build_mesh_tool.py::test_a_recipe_edit_after_a_hand_edit_refuses_rather_than_discarding_it";
    verify requirement RegenerationIsWholesale by
        "tests/test_mesh_gate_loop.py::test_mesh_op_appends_regenerates_and_re_presents";

    satisfy requirement CorrectDataClassAtSetBed by sharedPrimitives;
    verify requirement CorrectDataClassAtSetBed by
        "tests/test_mesh_topology_and_bed.py::test_the_bed_op_permits_no_ladder_rung_on_the_authors_behalf";
    verify requirement CorrectDataClassAtSetBed by
        "tests/test_mesh_topology_and_bed.py::test_a_source_naming_nothing_refuses_rather_than_leaving_a_bedless_mesh";
    verify requirement CorrectDataClassAtSetBed by
        "tests/test_mesh_topology_and_bed.py::test_a_conditioning_this_primitive_does_not_perform_refuses";
    verify requirement CorrectDataClassAtSetBed by
        "tests/test_mesh_topology_and_bed.py::test_the_interpolation_is_a_visible_default_off_a_declared_roster";
    verify requirement CorrectDataClassAtSetBed by
        "tests/test_mesh_topology_and_bed.py::test_the_journal_names_the_rung_that_ACTUALLY_painted_the_bed";

    satisfy requirement BoundaryRolesAreContiguousRuns by sharedPrimitives;
    satisfy requirement BoundaryRolesAreContiguousRuns by om2dAdapter;
    verify requirement BoundaryRolesAreContiguousRuns by
        "tests/test_mesh_topology_and_bed.py::test_a_scattered_candidate_boundary_resolves_into_two_contiguous_runs";
    verify requirement BoundaryRolesAreContiguousRuns by
        "tests/test_mesh_topology_and_bed.py::test_a_run_that_wraps_the_contours_origin_stays_one_run";
    verify requirement BoundaryRolesAreContiguousRuns by
        "tests/test_mesh_topology_and_bed.py::test_one_role_declared_across_two_faces_lands_as_two_sections";
    verify requirement BoundaryRolesAreContiguousRuns by
        "tests/test_mesh_topology_and_bed.py::test_a_node_two_declared_faces_both_claim_refuses";
    verify requirement BoundaryRolesAreContiguousRuns by
        "tests/test_mesh_topology_and_bed.py::test_a_face_the_mesh_never_reaches_refuses_rather_than_going_unprescribed";

    satisfy requirement SignatureIsTheSchema by mesherRegistry;
    satisfy requirement SignatureIsTheSchema by om2dAdapter;
    verify requirement SignatureIsTheSchema by
        "tests/test_mesh_meshers.py::test_an_op_a_mesher_never_registered_is_refused_by_name";
    verify requirement SignatureIsTheSchema by
        "tests/test_mesh_om2d.py::test_an_unknown_op_refuses_with_the_nearest_names";
    verify requirement SignatureIsTheSchema by
        "tests/test_mesh_om2d.py::test_the_wrapper_registers_the_librarys_own_names_tagged_by_phase";
    verify requirement SignatureIsTheSchema by
        "tests/test_mesh_om2d.py::test_a_library_op_records_the_note_that_its_kwargs_bound_elsewhere";
    verify requirement SignatureIsTheSchema by
        "tests/test_mesh_om2d.py::test_a_required_parameter_the_domain_cannot_supply_refuses_by_name";

    satisfy requirement ModelShrinksWithTheTree by om2dAdapter;
    satisfy requirement ModelShrinksWithTheTree by regGridAdapter;
    satisfy requirement ModelShrinksWithTheTree by om2dBox;
    verify requirement ModelShrinksWithTheTree by
        "tests/test_mesh_meshers.py::test_the_roster_is_the_meshers_the_tree_carries";
    verify requirement ModelShrinksWithTheTree by
        "tests/test_mesh_om2d.py::test_the_roster_is_the_two_meshers_and_nothing_else";
    verify requirement ModelShrinksWithTheTree by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement TheBoxNeverImportsTheServer by om2dBox;
    satisfy requirement TheBoxNeverImportsTheServer by selafinCliDriver;
    satisfy requirement TheBoxNeverImportsTheServer by telemacCasDriver;
    satisfy requirement TheBoxNeverImportsTheServer by telemacResultDriver;
    verify requirement TheBoxNeverImportsTheServer by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";
    verify requirement TheBoxNeverImportsTheServer by
        "tests/test_mesh_om2d.py::test_the_drivers_live_in_the_product_tree_beside_their_callers";

    satisfy requirement TheMeshFrontIsEngineAgnostic by meshRouter;
    satisfy requirement TheMeshFrontIsEngineAgnostic by meshGate;
    satisfy requirement TheMeshFrontIsEngineAgnostic by mesherRegistry;
    verify requirement TheMeshFrontIsEngineAgnostic by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";
    verify requirement TheMeshFrontIsEngineAgnostic by
        "tests/test_mesh_gate_loop.py::test_no_mesher_has_card_code_of_its_own";
    verify requirement TheMeshFrontIsEngineAgnostic by
        "tests/test_build_mesh_tool.py::test_reg_grid_conforms_with_a_near_empty_default_recipe";
}
