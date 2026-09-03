// plane: workflow | system: fetcher
//
// The data seam as a checkable model: the bed under a hydraulic run.
//
// SysML v2 TEXTUAL notation, restricted to the subset the project checker
// reads. Conventions as the other seams: ``code:`` binds a block to the module
// it IS, ``forbid:`` states a dependency rule, ``constructor:`` names a
// function that builds a contract, and ``pass-through:`` marks an end that
// forwards a contract verbatim.
//
// This seam exists because a bed is the one input a model cannot be honest
// without and cannot check for itself. Every requirement below is a SIGNED
// decision from the bathymetry methodology (docs/research/bathymetry-sources.md,
// signed 2026-09-02), named in its doc, allocated to the code that embodies it
// and verified by a test that exists.

package DataSeam {

    doc /*
    An elevation surface is not a bed. A DEM measures the water TOP where the
    channel is wet, and reading one as a channel bottom systematically
    under-represents conveyance - a BIAS, not noise, worst on exactly the small
    streams where the substitution is most tempting. So the topobathy row asks
    which water it is looking at, takes the ladder that class earned, and
    refuses when no rung on it ships. What painted the bed and on what vertical
    datum travels with the layer, because a bed whose datum nobody carried is a
    bed nobody can merge.
    */

    // ----------------------------------------------------------------- //
    // Blocks
    // ----------------------------------------------------------------- //

    part def BedLadderRegistry {
        doc /*
        Ladders as DATA plus the per-request chooser. A capability whose
        alternatives depend on a property of the SUBJECT rather than of the
        capability has more than one ladder, and only the request knows which.
        */
    }

    part def LadderWalker {
        doc /*
        The one walker every declared ladder runs through: rungs in order, the
        loudness gate before a degradation, measured coverage reconciled against
        what actually painted, and REFUSE at the end.
        */
    }

    part def FetcherRouter {
        doc /*
        The door a data ask enters. It resolves which ladder governs THIS
        request before the walk, so a row that states its water gets that
        water's ladder and a row that states none is served as before.
        */
    }

    part def WaterBodyClassifier {
        doc /*
        Which water this is, decided from the rows the reach chain already
        holds, and the three ladders one per class - including the two rungs
        that stopped and the class that therefore has none.
        */
    }

    part def BlueTopoSource {
        doc /*
        NOAA's National Bathymetric Source as a fetchable bed: the published
        tile scheme intersected with the AOI, each tile gated on its stated
        datum, merged onto one grid.
        */
    }

    part def BedSourceDeclaration {
        doc /*
        A bed source's declaration - its params, its hooks, its caveats and the
        native cells it actually publishes. Data, not code.
        */
    }

    part def CoastalComposite {
        doc /*
        The CUDEM-first coastal topo-bathy composite: the row's original
        producer, and the rung the coastal ladder falls to when the primary
        cannot cover the AOI.
        */
    }

    part def BedResultModel {
        doc /*
        The layer a bed fetch returns, with the provenance fields that are NOT
        recoverable from the raster bytes - which tiles painted it, at which
        tiers, over what share of the AOI, and on which vertical datum.
        */
    }

    // ----------------------------------------------------------------- //
    // Ports
    // ----------------------------------------------------------------- //

    port def ProvenanceOut;
    port def ProvenanceIn;
    port def DeclarationOut;
    port def DeclarationIn;
    port def RungOut;
    port def RungIn;
    port def LadderOut;
    port def LadderIn;
    port def ChoiceOut;
    port def ChoiceIn;
    port def ClassOut;
    port def ClassIn;
    port def StoppedOut;
    port def StoppedIn;
    port def GapOut;
    port def GapIn;

    // ----------------------------------------------------------------- //
    // Interfaces - the seam contracts, by their real key names
    // ----------------------------------------------------------------- //

    interface def BedProvenance {
        doc /*
        What a bed fetch knows and the bytes do not. The DATUM is the load-
        bearing one: BlueTopo is on NAVD88, an orthometric datum and explicitly
        not a navigational or tidal one, which is the whole reason it merges
        with a NAVD88 land DEM with no transformation. ``coverage_fraction`` is
        below 1.0 for any AOI holding land, by construction, and saying so is
        the honest report rather than a failure.

        constructor: BlueTopoResult
        */
        item vertical_datum : String;
        item tile_count : Integer;
        item resolution_tiers : StringList;
        item coverage_fraction : Real;
        item rung_coverage : Map [0..1];
    }

    interface def BedSourceParams {
        doc /*
        The request a bed source takes. ``min_pixel_m`` only COARSENS: it never
        invents a cell finer than the tile that was read, which is why it is the
        param the resolution declaration hangs off.
        */
        item bbox : RealList;
        item target_crs : String;
        item timeout_s : Real;
        item min_pixel_m : Real [0..1];
    }

    interface def BedLadderRung {
        doc /*
        One alternative on a bed ladder. ``consequence`` is what descending to
        it COSTS, and it is the only thing the loudness gate keys on, so a rung
        that crosses datasets must say so or the gate never asks.

        constructor: Rung
        */
        item name : String;
        item consequence : String;
        item describes : String;
        item call : String [0..1];
        item source : String [0..1];
        item params : Map [0..1];
        item supplies_param : String [0..1];
    }

    interface def BedLadderDeclaration {
        doc /*
        A whole ladder: which capability it governs, its ordered rungs, and the
        typed code its terminal refusal wears so a refusal keeps the
        capability's own error vocabulary.

        constructor: Ladder
        */
        item capability : String;
        item rungs : RungList;
        item refuse_error_code : String;
        item coverage_exempt_params : StringList [0..1];
    }

    interface def PerRequestLadderChoice {
        doc /*
        Which ladder governs ONE request. The chooser reads the request's own
        params; returning nothing means no per-request ladder applies and the
        capability's static ladder stands.
        */
        item capability : String;
        item params : Map;
        item selector : Callable [0..1];
    }

    interface def WaterBodyClassDeclaration {
        doc /*
        The class vocabulary, declared on the row and read by the classifier.
        Three names and no fourth: a class the row can state is a class a ladder
        answers for, and the two must not drift apart.
        */
        item water_body_class : String;
        item coastal_estuary : String;
        item navigable_river : String;
        item small_inland_stream : String;
    }

    interface def StoppedClassRefusal {
        doc /*
        A class whose every rung stopped, and what is missing before it can have
        one. It is carried as DATA so the refusal names the gap rather than
        saying only that there is one.
        */
        item STOPPED_CLASSES : Map;
        item water_body_class : String;
    }

    interface def PartialCoverGap {
        doc /*
        A rung that served PART of the request, and how much. The walker reads
        both off it: the share already painted, and the note the gate shows the
        person being asked to accept the substitution that fills the rest.
        */
        item covered_fraction : Real;
        item gap_note : String;
    }

    // ----------------------------------------------------------------- //
    // Parts - bound to the modules they ARE
    // ----------------------------------------------------------------- //

    part bedLadderRegistry : BedLadderRegistry {
        doc /*
        code: trid3nt_server/fallbacks/ladder.py
        */
        port rungIn : RungIn;
        port ladderIn : LadderIn;
        port choiceOut : ChoiceOut;
    }

    part ladderWalker : LadderWalker {
        doc /*
        code: trid3nt_server/fallbacks/walker.py
        */
        port gapIn : GapIn;
    }

    part fetcherRouter : FetcherRouter {
        doc /*
        code: trid3nt_server/tools/fetchers/_router/router.py
        */
        port choiceIn : ChoiceIn;
    }

    part waterBodyClassifier : WaterBodyClassifier {
        doc /*
        code: trid3nt_server/tools/fetchers/_router/hooks/topobathy_class.py
        */
        port classIn : ClassIn;
        port rungOut : RungOut;
        port ladderOut : LadderOut;
        port stoppedOut : StoppedOut;
        port gapOut : GapOut;
    }

    part blueTopoSource : BlueTopoSource {
        doc /*
        code: trid3nt_server/tools/fetchers/_router/hooks/bluetopo.py
        */
        port declarationIn : DeclarationIn;
        port provenanceOut : ProvenanceOut;
    }

    part blueTopoDeclaration : BedSourceDeclaration {
        doc /*
        code: trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/source.yaml
        */
        port declarationOut : DeclarationOut;
    }

    part coastalDeclaration : BedSourceDeclaration {
        doc /*
        code: trid3nt_server/tools/fetchers/ocean/fetch_topobathy/source.yaml
        */
        port classOut : ClassOut;
    }

    part coastalComposite : CoastalComposite {
        doc /*
        code: trid3nt_server/tools/fetchers/_router/hooks/topobathy.py
        */
        port stoppedIn : StoppedIn;
        port rungOut : RungOut;
        port ladderOut : LadderOut;
        port gapOut : GapOut;
    }

    part bedResultModel : BedResultModel {
        doc /*
        code: contracts/trid3nt_contracts/execution.py
        */
        port provenanceIn : ProvenanceIn;
    }

    // ----------------------------------------------------------------- //
    // Hops
    // ----------------------------------------------------------------- //

    interface bedSourceDeclared : BedSourceParams
        connect blueTopoDeclaration.declarationOut to blueTopoSource.declarationIn;

    interface bedProvenanceCarried : BedProvenance
        connect blueTopoSource.provenanceOut to bedResultModel.provenanceIn;

    interface classVocabularyDeclared : WaterBodyClassDeclaration
        connect coastalDeclaration.classOut to waterBodyClassifier.classIn;

    interface classRungDeclared : BedLadderRung
        connect waterBodyClassifier.rungOut to bedLadderRegistry.rungIn;

    interface compositeRungDeclared : BedLadderRung
        connect coastalComposite.rungOut to bedLadderRegistry.rungIn;

    interface classLadderRegistered : BedLadderDeclaration
        connect waterBodyClassifier.ladderOut to bedLadderRegistry.ladderIn;

    interface compositeLadderRegistered : BedLadderDeclaration
        connect coastalComposite.ladderOut to bedLadderRegistry.ladderIn;

    interface perRequestLadderChosen : PerRequestLadderChoice
        connect bedLadderRegistry.choiceOut to fetcherRouter.choiceIn;

    interface stoppedClassRefused : StoppedClassRefusal
        connect waterBodyClassifier.stoppedOut to coastalComposite.stoppedIn;

    interface classGapReported : PartialCoverGap
        connect waterBodyClassifier.gapOut to ladderWalker.gapIn;

    interface compositeGapReported : PartialCoverGap
        connect coastalComposite.gapOut to ladderWalker.gapIn;

    // ----------------------------------------------------------------- //
    // The signed decisions this seam is governed by
    // ----------------------------------------------------------------- //

    requirement def PerWaterBodyClassLadders {
        doc /*
        SIGNED DECISION - per-class ladders on the topobathy row (bathymetry
        methodology, M1). A bed source fits one KIND of water and not another,
        so the row has one ladder per class rather than one ladder: coastal or
        estuary takes BlueTopo above the CUDEM composite, and the other two
        classes take none - each stopped for a measured reason recorded under
        AStoppedRungNeverShips. Which ladder governs a request is resolved from
        the request itself, before the walk.

        A row that declares no class keeps the unclassed ladder. A class nobody
        declared is not a class anybody may assume, so the per-class ladders
        govern the row that states which water it is and nothing else.
        */
    }

    requirement def WaterBodyClassComesFromHeldData {
        doc /*
        SIGNED DECISION - the class comes from data the chain already holds (the
        reach, water and waterbody rows), never from a guess. A tidal FType
        among the mapped water makes the reach coastal; no mapped water surface
        at all makes it a small inland stream, because a channel too narrow to
        be mapped as an area is a flowline only and that absence is a real
        answer about the channel.

        Where the held rows CANNOT decide, the classifier REFUSES and names what
        was missing rather than falling to a conservative default. A mapped
        inland channel surface says the river is wide; the navigable class means
        a federally maintained navigation channel, and no row this chain holds
        says whether this one is - so it refuses and names that.
        */
    }

    requirement def SubstitutionIsDeclared {
        doc /*
        SIGNED DECISION - a cross-dataset substitution is a DECLARED rung taken
        loudly through the gate, never a fill a fetch performs on its own.
        Falling from BlueTopo to the CUDEM composite crosses datasets and wears
        that consequence, so the gate asks before it happens and the activation
        records which rung painted what share.

        A rung that served only part of the request says so as a gap carrying
        the measured share, which is what lets the next rung fill the rest under
        the gate instead of the first rung returning a half-painted bed as a
        whole one.
        */
    }

    requirement def BedSourceStatesItsDatum {
        doc /*
        SIGNED DECISION - the bed's vertical datum is stated in provenance, not
        assumed by a reader. BlueTopo publishes NAVD88 and says so twice in each
        tile; a tile that states neither is refused rather than merged, because
        the whole reason this source outranks the alternatives is that its datum
        is known. The datum, the tiles, the tiers and the measured coverage ride
        on the returned layer, since none of them survives in the raster bytes.
        */
    }

    requirement def AStoppedRungNeverShips {
        doc /*
        SIGNED DECISION - load-bearing unverified items are verified live before
        any rung ships, and a dead assumption stops its rung rather than
        shipping it. Two rungs the methodology names stopped on measured
        grounds and are absent here rather than declared:

        eHydro, the navigable primary: its queryable surface is one layer of
        survey-boundary polygons carrying a horizontal projection and no
        vertical datum field at all, with the soundings behind per-survey bulk
        archives on another host. A bed whose datum is unknowable from its index
        cannot state its datum, so it cannot be on this ladder. What the
        methodology leaves under it is BlueTopo alone, and one source is not a
        degradation path - so the navigable class has no ladder either and
        refuses, naming the stopped primary.

        NXSDB, the small-stream primary: its measurements carry depth below the
        water surface and no bed elevation and no vertical datum, published as
        one national GeoPackage on a host serving no range requests. That is a
        producer's input, not a bed anyone can fetch.

        So the small-stream class likewise has no rung and REFUSES, naming both
        gaps and the synthetic slot below them.

        That slot is DEFERRED BY RULING, not merely unbuilt (HAPPY PATH FIRST,
        SYNTHETIC DEFERRED, 2026-09-02, amending the signed methodology): no
        synthetic bathymetry is produced now, the Bieger regression the
        methodology named as the candidate does not build, and whether a
        fabricated bed may ever stand in for a survey is a USER decision rather
        than a gap for an implementation to close. Best-case behaviour is
        established first and never intertwined with sad-path interpolation.
        The slot is therefore STATED and EMPTY - an absence somebody decided, so
        that a later reader finds a ruling where they would otherwise find an
        oversight. Empty, it is a refusal, and the refusal is the honest floor
        working.
        */
    }

    requirement def ThalwegBurningIsNeverABedSource {
        doc /*
        SIGNED DECISION - thalweg and stream burning stay REJECTED as a bed
        source. Burning lowers DEM cells along a mapped network so flow routing
        honours it: the depth is chosen for ROUTING reasons and bears no
        relation to true bathymetry, and a burned surface is unsuited even for
        measuring local slope. It enforces direction and connectivity and says
        nothing about cross-sectional conveyance. Reading a routing offset as a
        channel depth is precisely the confusion the correct-data-class law
        forbids.

        The rule is written as a dependency: no module on this bed seam may
        reach for the hydrologic-conditioning family, which is where fill,
        resolve-flats and stream-network conditioning live. A bed source that
        imported them would be conditioning terrain and calling the result a
        survey.

        forbid: trid3nt_server.tools.fetchers._router.hooks.bluetopo -> trid3nt_server.tools.processing
        forbid: trid3nt_server.tools.fetchers._router.hooks.topobathy_class -> trid3nt_server.tools.processing
        forbid: trid3nt_server.tools.fetchers._router.hooks.topobathy -> trid3nt_server.tools.processing
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement PerWaterBodyClassLadders by waterBodyClassifier;
    satisfy requirement PerWaterBodyClassLadders by bedLadderRegistry;
    satisfy requirement PerWaterBodyClassLadders by fetcherRouter;
    verify requirement PerWaterBodyClassLadders by
        "tests/test_bathymetry_data_seam.py::test_the_coastal_ladder_puts_bluetopo_above_the_cudem_composite";
    verify requirement PerWaterBodyClassLadders by
        "tests/test_bathymetry_data_seam.py::test_every_declared_class_either_ladders_or_stops_by_name";
    verify requirement PerWaterBodyClassLadders by
        "tests/test_bathymetry_data_seam.py::test_an_undeclared_class_keeps_the_rows_unclassed_ladder";
    verify requirement PerWaterBodyClassLadders by
        "tests/test_bathymetry_data_seam.py::test_every_class_ladder_ends_at_refuse_with_the_rows_own_error_code";

    satisfy requirement WaterBodyClassComesFromHeldData by waterBodyClassifier;
    satisfy requirement WaterBodyClassComesFromHeldData by coastalDeclaration;
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/test_bathymetry_data_seam.py::test_a_tidal_ftype_classifies_the_reach_as_coastal_estuary";
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/test_bathymetry_data_seam.py::test_no_mapped_water_surface_classifies_the_reach_as_a_small_inland_stream";
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/test_bathymetry_data_seam.py::test_a_wide_inland_river_refuses_naming_what_would_have_decided_it";
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/test_bathymetry_data_seam.py::test_the_classifier_never_guesses_a_class_from_an_unknown_ftype";
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/test_bathymetry_data_seam.py::test_the_topobathy_row_declares_the_water_body_class_it_ladders_on";

    satisfy requirement SubstitutionIsDeclared by waterBodyClassifier;
    satisfy requirement SubstitutionIsDeclared by ladderWalker;
    satisfy requirement SubstitutionIsDeclared by coastalComposite;
    verify requirement SubstitutionIsDeclared by
        "tests/test_bathymetry_data_seam.py::test_falling_from_bluetopo_to_the_cudem_composite_is_a_declared_cross_dataset_rung";
    verify requirement SubstitutionIsDeclared by
        "tests/test_bathymetry_data_seam.py::test_a_partial_bluetopo_cover_is_reported_as_a_gap_not_a_whole_bed";
    verify requirement SubstitutionIsDeclared by
        "tests/test_fallback_ladder.py::test_declared_rung_fills_the_gap_and_splits_coverage";

    satisfy requirement BedSourceStatesItsDatum by blueTopoSource;
    satisfy requirement BedSourceStatesItsDatum by blueTopoDeclaration;
    satisfy requirement BedSourceStatesItsDatum by bedResultModel;
    verify requirement BedSourceStatesItsDatum by
        "tests/test_bathymetry_data_seam.py::test_a_tile_that_states_navd88_passes_the_datum_gate";
    verify requirement BedSourceStatesItsDatum by
        "tests/test_bathymetry_data_seam.py::test_a_tile_that_states_no_navd88_refuses_rather_than_merging";
    verify requirement BedSourceStatesItsDatum by
        "tests/test_bathymetry_data_seam.py::test_the_envelope_states_the_datum_in_provenance";
    verify requirement BedSourceStatesItsDatum by
        "tests/test_bathymetry_data_seam.py::test_the_bluetopo_spec_declares_the_delegate_hooks_and_the_result_model";

    satisfy requirement AStoppedRungNeverShips by waterBodyClassifier;
    satisfy requirement AStoppedRungNeverShips by coastalComposite;
    verify requirement AStoppedRungNeverShips by
        "tests/test_bathymetry_data_seam.py::test_a_small_inland_stream_has_no_ladder_and_refuses_naming_both_gaps";
    verify requirement AStoppedRungNeverShips by
        "tests/test_bathymetry_data_seam.py::test_a_navigable_river_has_no_ladder_and_refuses_naming_its_stopped_primary";
    verify requirement AStoppedRungNeverShips by
        "tests/test_bathymetry_data_seam.py::test_no_ladder_anywhere_ships_an_ehydro_rung";
    verify requirement AStoppedRungNeverShips by
        "tests/test_bathymetry_data_seam.py::test_a_stopped_class_refuses_before_the_cache_and_before_the_network";
    verify requirement AStoppedRungNeverShips by
        "tests/test_bathymetry_data_seam.py::test_a_tile_row_with_no_delivered_link_is_not_data";
    verify requirement AStoppedRungNeverShips by
        "tests/test_bathymetry_data_seam.py::test_an_aoi_no_delivered_tile_reaches_refuses_by_name";
    verify requirement AStoppedRungNeverShips by
        "tests/test_bathymetry_data_seam.py::test_the_synthetic_slot_is_stated_as_deferred_rather_than_forgotten";

    satisfy requirement ThalwegBurningIsNeverABedSource by blueTopoSource;
    satisfy requirement ThalwegBurningIsNeverABedSource by waterBodyClassifier;
    satisfy requirement ThalwegBurningIsNeverABedSource by coastalComposite;
    verify requirement ThalwegBurningIsNeverABedSource by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";
    verify requirement ThalwegBurningIsNeverABedSource by
        "tests/test_bathymetry_data_seam.py::test_the_selected_tiles_run_coarsest_first_so_the_finest_paints_last";
}
