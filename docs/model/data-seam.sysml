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
// without and cannot check for itself. Every requirement below is a decision
// from the bathymetry methodology (docs/local/research/bathymetry-sources.md), named
// in its doc, allocated to the code that embodies it and verified by a test
// that exists.

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

    part def CatalogRasterReader {
        doc /*
        The reader a catalog-published raster row is served by. It takes the
        row's catalog coordinates - which collection, which asset, at which
        cell, signed how, rendered how - and returns the window. It knows
        nothing about the DATA: the vertical datum, the resolution and the style
        stay on the source row, because a library that fetches bytes cannot say
        what they are referenced to.
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

    part def BedPainter {
        doc /*
        The op that puts a source's elevations onto a mesh's nodes: it asks the
        source row for its datum before it fetches, journals what actually
        painted, and refuses a bed that came back on two vertical references.
        */
    }

    part def BedSourceDeclaration {
        doc /*
        A bed source's declaration - its params, its hooks, its caveats and the
        native cells it actually publishes. Data, not code.
        */
    }

    part def FreeSurfaceReader {
        doc /*
        The slot ingestion that turns an observed water level into the elevation
        a run opens at: it ranks the reporting sites against the domain, takes
        the nearest reading in the unit the keyword reads, and says on the run
        journal which site reported it and when.
        */
    }

    part def CoastalComposite {
        doc /*
        The CUDEM-first coastal topo-bathy composite: the row's original
        producer, and the rung the coastal ladder falls to when the primary
        cannot cover the AOI.
        */
    }

    part def WaterCut {
        doc /*
        The derive that turns a box and a mapped coastline into the WATER inside
        it: the faces the ways split the box into, classified by the direction
        each way was drawn, with the wet ones returned as one polygon.
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

    interface def CatalogReadDeclaration {
        doc /*
        What a source row tells a catalog reader. Every key is a coordinate INTO
        a catalog, never a claim about the pixels: the reader can find the asset
        and put it on a grid, and it still cannot say which datum the numbers
        are on. That is why a bed source published through a catalog states its
        datum on its own row and the reader carries none.
        */
        item collection : String;
        item data_asset : String;
        item native_cell_m : Real;
        item render : String;
        item sign : String;
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

    part stacRasterReader : CatalogRasterReader {
        doc /*
        code: trid3nt_server/tools/fetchers/_router/executors/stac_raster.py
        */
        port declarationIn : DeclarationIn;
    }

    part copernicusDeclaration : BedSourceDeclaration {
        doc /*
        code: trid3nt_server/tools/fetchers/terrain/fetch_copernicus_dem/source.yaml
        */
        port declarationOut : DeclarationOut;
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
        code: trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/hooks.py
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

    part waterCut : WaterCut {
        doc /*
        code: trid3nt_server/workflows/mesh/water.py
        */
    }

    part coastlineDeclaration : BedSourceDeclaration {
        doc /*
        code: trid3nt_server/tools/fetchers/ocean/fetch_osm_coastline/source.yaml
        */
    }

    part bedPainter : BedPainter {
        doc /*
        code: trid3nt_server/workflows/mesh/shared/primitives.py
        */
    }

    part def DomainSlot {
        doc /*
        The one row a run is solved over: a closed polygon, geometry only.
        Drawn, picked, fetched or purely surveyed, it reads the same afterwards.
        */
    }

    part def BedSlot {
        doc /*
        The one row every node's elevation comes from, over every shape a user
        can have it in: a surface, a layer of soundings, or a stated depth.
        */
    }

    part def BedComposer {
        doc /*
        The derive that lays a narrow measurement over the wider surface under
        it: the primary wins every cell it measured, the fallback paints what is
        left, and a sidecar records which won where. It is also where two
        surfaces that did not count the same way are brought onto ONE zero - a
        primary counting DEPTHS read as elevations up from its own datum,
        through the shift that survey publishes about itself - because a merge
        of two conventions is a floor with a step in it.
        */
    }

    part def BoundaryRunSlot {
        doc /*
        The named stretches of the domain's edge, each two points and a type.
        Zero of them is an answer: a closed body's edge is solid wall whole.
        */
    }

    part def LineSlot {
        doc /*
        The polyline a placed read is measured ALONG: the centerline a domain's
        producer measured beside its polygon, or the line a user draws over a
        body whose producer measured none. Optional by construction - a run
        solves without one - and read by purpose on the canvas when it is not.
        */
    }

    part def ExtentSlot {
        doc /*
        The RECTANGLE a question is asked inside - the window a domain is cut
        out of - however the caller names it: a box picked on the canvas, four
        numbers, a place, or a layer's bounds.
        */
    }

    part def VerticalFrame {
        doc /*
        The ONE zero a run counts every elevation from, and the coercion that
        brings a source published on another onto it.
        */
    }

    part def SourceCoverage {
        doc /*
        A source's ONLY statement of what it covers: the class of thing it
        measures, how it came by its numbers, where and how far one station
        serves, over what time, at what cell, on what zero, and the unit of each
        value column. Stated from the dataset's own documentation.
        */
    }

    part def SlotNeed {
        doc /*
        What a slot asks the world for: one class of the coarse vocabulary. The
        place, the window and the frame are the RUN's and are read off it, so a
        question states none of them.
        */
    }

    part def SourceMatch {
        doc /*
        The one function that reads a need against every coverage row: class and
        place hard, the window hard for a series and a sort key for a surface,
        the datum flagged. Pure over declarations; the caller probes.
        */
    }

    part def RankedSourceList {
        doc /*
        ONE object in three views: the card renders it with the pick
        highlighted, the tool result carries the rows on a tie, and the sheet
        stores the pick and its reason.
        */
    }

    part def ContextRow {
        doc /*
        A producer row whose absence is legal, and SAID: the run continues and
        the sheet carries the sentence the row states.
        */
    }

    part domainSlot : DomainSlot {
        doc /*
        code: trid3nt_server/inputs/domain.py
        */
    }

    part bedSlot : BedSlot {
        doc /*
        code: trid3nt_server/inputs/bed.py
        */
    }

    part bedComposer : BedComposer {
        doc /*
        code: trid3nt_server/tools/derive/derive_merge_rasters/derive_merge_rasters.py
        */
    }

    part boundaryRunSlot : BoundaryRunSlot {
        doc /*
        code: trid3nt_server/inputs/boundary.py
        */
    }

    part slotDoor : DomainSlot {
        doc /*
        code: trid3nt_server/inputs/slots.py
        */
    }

    part lineSlot : LineSlot {
        doc /*
        code: trid3nt_server/inputs/line.py
        */
    }

    part extentSlot : ExtentSlot {
        doc /*
        code: trid3nt_server/inputs/extent.py
        */
    }

    part contextRow : ContextRow {
        doc /*
        code: trid3nt_server/workflows/runtime/data.py
        */
    }

    part coverageContract : SourceCoverage {
        doc /*
        code: contracts/trid3nt_contracts/coverage.py
        */
    }

    part gaugeDeclaration : SourceCoverage {
        doc /*
        code: trid3nt_server/tools/fetchers/hydrology/fetch_usgs_nwis_gauges/source.yaml
        */
    }

    part needDeclaration : SlotNeed {
        doc /*
        code: trid3nt_server/workflows/runtime/data.py
        */
    }

    part sourceMatch : SourceMatch {
        doc /*
        code: trid3nt_server/tools/search/match.py
        */
    }

    part matchedSlot : SourceMatch {
        doc /*
        code: trid3nt_server/workflows/runtime/interpreter.py
        */
    }

    part modelMatch : SourceMatch {
        doc /*
        code: trid3nt_server/tools/search/find_sources/find_sources.py
        */
    }

    part rankedListCard : RankedSourceList {
        doc /*
        code: trid3nt_server/workflows/telemac/workflow.py
        */
    }

    part rankedListRecord : RankedSourceList {
        doc /*
        code: trid3nt_server/workflows/runtime/journal.py
        */
    }

    part seriesOnTheRunsClock : FreeSurfaceReader {
        doc /*
        code: trid3nt_server/workflows/runtime/temporal.py
        */
    }

    part verticalFrame : VerticalFrame {
        doc /*
        code: trid3nt_server/inputs/vertical_datum.py
        */
    }

    part frameLever : VerticalFrame {
        doc /*
        code: trid3nt_server/workflows/runtime/levers.py
        */
    }

    part lakeDeclaration : BedSourceDeclaration {
        doc /*
        code: trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_bathymetry/source.yaml
        */
    }

    part lakeLevelDeclaration : BedSourceDeclaration {
        doc /*
        code: trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_water_level/source.yaml
        */
    }

    part freeSurfaceReader : FreeSurfaceReader {
        doc /*
        code: trid3nt_server/inputs/observation.py
        */
    }

    part offsetRowDeclaration : VerticalFrame {
        doc /*
        code: trid3nt_server/workflows/runtime/interpreter.py
        */
    }

    part rowLadderWalker : LadderWalker {
        doc /*
        code: trid3nt_server/workflows/runtime/interpreter.py
        */
    }

    part unreadContextAsk : ContextRow {
        doc /*
        code: trid3nt_server/workflows/runtime/interpreter.py
        */
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

    interface catalogBedDeclared : CatalogReadDeclaration
        connect copernicusDeclaration.declarationOut to stacRasterReader.declarationIn;

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

    requirement def ACatalogReaderCarriesNoProvenance {
        doc /*
        The STAC rows read through a maintained catalog library rather than
        hand-written search-and-warp code. The library owns the socket, the
        grid and the pixel fuse; it owns nothing else. Provenance facts - the
        vertical datum, the resolution and the style row - stay on the source
        row whatever library performs the fetch, so a bed published through a
        catalog is as mergeable after the fold as before it.
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

        The same loudness holds one level up, on a DATA row's own declared
        ladder: when a rung other than the primary answers, the interpreter
        writes a note ON THE RUN naming the row, what each earlier rung refused
        with, and which runner answered. The packet carries that note, so a
        reader of the answer sees that a different dataset produced the
        artifact; a log line would die with the process.
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

    requirement def OneSourceReadsOnOneStatedDatum {
        doc /*
        Every bed source states its VERTICAL DATUM on its own source row, taken
        from the dataset's own documentation and never inferred from the bytes
        - a raster carries numbers, and what they are counted from is not among
        them. A row that cannot state one carries none, and a row carrying none
        is refused as a bed rather than painted and read later as if it were on
        whatever the run assumed.

        A mosaic that mixes datums is therefore not a bed source. The NCEI
        DEM_all endpoint serves the lake-datum bathymetry, the NAVD88 coastal
        tiles, the same tiles on MHW and the EGM2008 global bases merged under
        one name, so the row that reads it as a bed pins the ONE product it
        means and states that product's datum.

        The stated datum is the WHOLE guard. A post-paint population test over
        the elevations is not one: real terrain puts an outlier as far from its
        neighbours as a datum offset does, so such a test reads the shape of the
        ground rather than the reference it is counted from.

        The datum rides onto the layer's provenance card beside the acquisition
        instant and the native cell, since a bed the user is shown to refine is
        a bed they may stitch another source onto, and that is the metadata the
        two have to be compared on.
        */
    }

    requirement def AFreeSurfaceAndABedShareOneStatedDatum {
        doc /*
        A water body's LEVEL is an observation, fetched the way a river's
        discharge is - the nearest gauge to the AOI, at the day the run is
        about - and the run's free surface opens at it. Data is assumed true;
        nothing thresholds the reading.

        WHAT MAKES THE ARITHMETIC LEGAL is that both documents state their zero.
        The gauge row and the bed row each state a vertical datum, and the
        subtraction between a level and an elevation is only defined when the
        two statements agree. Two rows stating different zeros, with no offset
        stated anywhere, REFUSE BY NAME - both names and both datums - rather
        than adding numbers that are not on one axis. A row stating none refuses
        for the reason OneSourceReadsOnOneStatedDatum already gives.

        Left at the dictionary's own zero the free surface sits ON the chart
        datum, which is where the bed is counted from, so the rim of a surveyed
        basin carries no water at all. Clipping that rim away would be a
        threshold on values; giving it its real water is the observation.
        */
    }

    requirement def TheRunHasOneVerticalFrame {
        doc /*
        A run counts every elevation it ingests from ONE zero, and that zero is
        the RUNTIME's: a lever beside the compute class, NAVD88 by default,
        never a row a question writes. A template that named its own datum would
        make the frame a property of the question rather than of the run, and
        two questions over one water body would then answer on two axes.

        A source published on another frame reaches the run's through a shift
        somebody MEASURED, in one order: the shift the source publishes about
        itself - a district's project datum states in the survey's own metadata
        how far it sits above a national frame, and no service serves that -
        else the offset a DATA ROW produces between the two frames at the point
        the run stands on. That row is the RUNTIME's: it is declared where an
        elevation slot is handed a source on another frame, walked and journaled
        like any other producer, and the slot's coercion reads its value and
        fetches nothing - a coercion may call a derive, never a fetch. A pair
        nothing measures refuses naming BOTH frames rather than laying one over
        the other, because a shift nobody measured is indistinguishable from a
        measurement to every reader downstream.

        The same order holds wherever two surfaces meet before a slot does: a
        merge reads its primary on the fallback's zero through the offset the
        call states, else the one the primary publishes, and refuses naming both
        when neither exists. A surface counting DEPTHS down from its zero is one
        axis with a surface counting elevations up only through that shift, so
        it is read the other way round on the way - on its own grid, before
        either is read onto the common one.

        What a slot does with a source that states NO datum is that slot's own
        statement and not this one's: a surface of depths has to know the zero
        they are counted below and refuses without it, while an elevation
        surface somebody handed this run stands on the run's frame.
        */
    }

    requirement def ASourceStatesWhatItCovers {
        doc /*
        A fetcher's coverage row is its ONLY statement of coverage: the class of
        thing it measures, the place it reaches as a coarse polygon or a station
        set or "read from the service", the time its records span, the cell it
        publishes at, the zero its elevations are counted from, and the unit of
        each value column the record carries. The prose that used to say these
        things in the caveats goes, because two statements of one fact are how
        the two drift apart and no filter can read a sentence.

        The row is stated from the dataset's own documentation, never inferred
        from a fetched body: a guessed extent is indistinguishable from a
        measured one to every reader downstream, and this row is what decides
        which source fills a slot. A source that states none is never matched -
        it stays model-callable, and nothing here can say whether it reaches
        this place.

        The UNIT is per value column, because one record carries more than one
        quantity: a gauge reports a discharge in cubic feet per second beside a
        stage in feet, and a slot that read either in the unit it wanted would
        be reading a different river. A source that serves two classes carries
        ONE ROW PER CLASS AND KIND rather than a hybrid word, and each row names
        the columns its value, its window and the zero it is counted from arrive
        under - a slot that states a need cannot name a column of a source it
        did not choose. A gauge that reports a measured series and a predicted
        one of the same class carries a row for each, and each row carries the
        request values it is fetched under, because the row is what the match
        weighed and a spec default answers for whichever row it was written
        around. A unit or a zero the RECORD carries per feature is stated as
        "record" and read off the feature.

        A STATION SET LISTS ITS STATIONS - inline where the set is small, read
        from a bounded listing that is cached once, or discovered by box inside
        the region where the network has no listing at all - because a ring
        alone says a place is inside the region and nothing about whether a
        station stands anywhere near it, which is the distance the reach is
        measured against. A set that lists them is measured from the nearest
        listed station; a discovered network is measured from the region and
        the probe drops it on an empty answer. Rings alone under a station set
        are refused.
        */
    }

    requirement def ASlotStatesANeedAndTheMatchFillsIt {
        doc /*
        A slot either NAMES its producer, with its ladder and its transforms, or
        states the CLASS it needs; a row that names a fetcher is a PIN and is
        honoured as it is. A need is one class of a coarse vocabulary of about
        seventeen, and the place, the window and the frame it is matched under
        are the RUN's - the domain's centre, the deck's own duration from the
        moment the run asks at, the vertical lever - so a question states none
        of them.

        The filter is mechanical: class and place are hard, the window is hard
        for a SERIES source and never for a static surface - a 2014 survey
        answers a 2026 question and recency ranks it - and a differing datum is
        FLAGGED rather than filtered, because the offset row is what refuses at.
        A station set states the REACH one station serves, read against the
        nearest station the row lists, and a place further from it than that is
        not a survivor at all. A DISCHARGE is the water passing one section, so
        its place filter is the domain itself: the gauge has to stand on this
        domain's own water - inside the polygon the run drew, or its box, within
        the cell the mesh resolves - and a gauge on another river is dropped by
        name however near it stands. A level propagates up a reach, so the reach
        stays its rule. What the top survivor is CALLED with closes
        through the match: the run's box and window, the matched row's own
        request values, and the nearest listed station where the source is
        called by a station name. A source whose required params the row can
        answer for is a survivor; one whose it cannot is dropped by name. The sort is on facts, in the
        order they decide: how the numbers were come by - measured, then
        predicted, then modelled - how far the place is from them, the cell
        against the mesh, how current the record is, and whether the source is
        already on the run's frame. The top
        survivor is CALLED and dropped on an empty source, so the list a reader
        sees says what the world answered rather than what the sort preferred,
        and a refusal NAMES every source and what each one had to say.

        One run-level statement loosens one filter - a stand-in window, a datum
        taken unconverted - and one names the PICK for a slot among the
        survivors; both show as the user's choice, never as the match's own
        judgement, and a named source the filters excluded is a refusal rather
        than a way past them.

        The bed is the one slot whose physics rule the runtime owns, stated
        ONCE: the measurement where it measured, the terrain under the rest, the
        soundings gridded at the run's own edge before the merge reads them, and
        the terrain alone where nothing measured this domain.
        */
    }

    requirement def TheRankedListIsOneObjectInThreeViews {
        doc /*
        The list a slot was filled from is ONE object. The card renders it with
        the pick highlighted; the tool result carries the rows only on a TIE,
        where several sources rank equal on every fact the sort reads and the
        model or the user picks by row; the sheet stores the pick and its
        reason.

        The sentence the model is given IS the sentence the card shows. It is
        authored once, in the match, because a second rendering of the same
        facts is a second chance to say something the run did not do.
        */
    }

    requirement def ASeriesIsAlignedToTheRunsEventTime {
        doc /*
        A run opens at the moment it asks about, INSIDE the record: t=0 is that
        instant and the readings before it carry negative times, rather than the
        record's first sample standing in for a moment nobody asked about. A
        record that does not span the window the run solves over refuses, naming
        the nearest sample it holds and the statement that would take it as a
        stand-in - reading a record flat past its last sample is a forcing
        nobody measured.

        A record is read in the unit it was MEASURED in, off its source's
        coverage row where the record carries no unit column of its own; a
        record that states none refuses rather than being read in the unit the
        slot wanted. And a gauge publishes a HEIGHT above its own zero, which is
        a different surface from the one a bed is painted on: the run reaches
        that surface by adding the zero's own elevation, which the site record
        publishes, and a gauge that publishes none refuses.
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
        archives on another host. A LADDER RUNG has to state its datum on the
        source row, before it fetches, so it cannot be one - and what the
        methodology leaves under it is BlueTopo alone, which is not a
        degradation path either. The navigable class therefore has no ladder and
        refuses, naming the stopped primary.

        That is a statement about LADDERS, not about eHydro. A survey states its
        own zero on its own features - a local project datum on many rivers -
        and that is read where the soundings are, so eHydro is a DATA row of its
        own: the row states the datum per feature, the surface derived from it
        carries what those features stated, and the merge that places it over a
        terrain surface refuses two different zeros BY NAME. A per-source zero
        and a per-row zero are both stated; neither is ever assumed.

        NXSDB, the small-stream primary: its measurements carry depth below the
        water surface and no bed elevation and no vertical datum, published as
        one national GeoPackage on a host serving no range requests. That is a
        producer's input, not a bed anyone can fetch.

        So the small-stream class likewise has no rung and REFUSES, naming both
        gaps and the synthetic slot below them.

        That slot is DEFERRED, not merely unbuilt: no synthetic bathymetry is
        produced, the Bieger regression the methodology named as the candidate
        does not build, and whether a fabricated bed may ever stand in for a
        survey is a USER decision rather than a gap for an implementation to
        close. Best-case behaviour is established first and never intertwined
        with sad-path interpolation. The slot is therefore STATED and EMPTY -
        an absence somebody decided, so that a later reader finds a decision
        where they would otherwise find an oversight. Empty, it is a refusal,
        and the refusal is the honest floor working.
        */
    }

    requirement def TheShorelineIsTheDomainsOwnEdge {
        doc /*
        A shoreline is where the water meets land. The DOMAIN is the closed
        polygon the equations are solved over, so its own boundary - islands
        included - is that shoreline, minus the stretches a run of type inflow,
        outflow, open or rating_curve names, which are the edge the water
        CROSSES. Every sizing function measures what is left, on any domain: a
        drawn pond, a reach section, a harbour basin.

        There is therefore ONE mesher path and no box extent: a box is not a
        domain, and cutting the water out of one is a derive over that box and a
        mapped coastline, useful on its own and written as a DATA row by the
        template that wants it. A domain whose whole edge is crossed has no
        shore for a sizing function to measure and is refused by name.
        */
    }

    requirement def TheDomainIsOneSlotHoweverItIsFilled {
        doc /*
        A template solves over any body of water: drawn, picked, fetched, or
        purely surveyed. The DOMAIN is one row - a closed polygon, geometry only
        - and nothing downstream branches on where it came from, because the
        moment a consumer can tell, half the ways of filling it stop working.

        A slot that names a producer is still on the WIRE: what the user hands in
        supersedes the source the template preferred, and that is the whole of
        how a body of water no fetcher knows gets modelled. Fetchers are one way
        to fill a slot, never the linchpin.

        A domain read as TEXT is the name it was given, else the word: the mesh
        session, the layer titles and the run's own name are all written from
        it, so a domain that read as its geometry named every one of them after
        its coordinates.
        */
    }

    requirement def OneBedOverEverySourceAUserCanHave {
        doc /*
        The bed is one seam fed by whatever the user has: a fetched DEM, a
        bathymetry or survey raster, a layer of soundings, or a stated depth
        below the free surface. The slot takes ONE source, and it is one row.

        A bed built from a survey over a wider surface is EXISTING COMPOSITION,
        not new vocabulary: a survey row declared context, a row that grids it
        into a surface, a terrain row, and a merged row that is a derive over
        the two - primary over fallback by coverage, with a sidecar recording
        which input won at each cell, and the terrain passing through unchanged
        where no survey was published. The derive is useful outside any slot,
        which is why it is a tool rather than a second argument on the mesh op
        or on the slot, and a fetcher's ladder - which is about a source being
        unavailable - is never spelled as one. The COMPOSITION IS ROWS: the slot
        takes the one layer the merge produced and knows nothing of what went
        into it.

        A node the source does not cover REFUSES by name. Filling the hole with
        the mean of what was covered is a bed nobody measured, and it reads as
        data everywhere downstream.
        */
    }

    requirement def ABoundaryRunIsTwoPointsAndAType {
        doc /*
        A run is two Points on the domain's edge and a type - wall by default,
        inflow, outflow, open. A template or a producer states zero or more, and
        STATING NONE IS AN ANSWER: a closed body has no liquid boundary and its
        mesh has only walls, so nothing is prescribed where nothing is named.

        A PRODUCER that cut its polygon between two faces returns those faces as
        rows of the same artifact, and the domain slot reads them off it: the
        producer names which row is which in its OWN vocabulary, and the slot
        reads that rather than asking a fetcher to speak the slot's. A row
        naming a stretch that prescribes nothing is the wall the edge already
        is, and is not a run.

        A template declares the slot and nothing else about it. What fills it,
        in order: what the caller supplied, the runs the DOMAIN row carries
        because its producer measured them, what the user draws under the
        boundary-run purpose, and then nothing - which is a closed body. A
        question that needs a run and was given none refuses by naming the run
        it lacked, never by opening on a condition nobody stated.
        */
    }

    requirement def AContextRowsAbsenceContinuesTheRun {
        doc /*
        A producer row declared as CONTEXT continues the run when its source held
        nothing, and the sheet carries the sentence the row states. Every other
        producer row still fails typed: the difference is declared, not inferred,
        because a row that is load-bearing and a row that is background look
        identical from inside the fetch.

        ONLY AN EMPTY SOURCE IS AN ABSENCE. A window, a bbox or a unit the
        caller stated wrong is the ASK being wrong and refuses, because a run
        that swallowed it would report that nothing was there about a question
        nobody managed to put.

        The refusal for a producer row declared .optional() stands: absence with
        nothing said about it is the failure mode this row was written to end.

        A tail read off a row that is WHOLLY ABSENT reads as nothing, exactly as
        a field that is present and empty does - otherwise the keyword reading a
        context row refuses the very run the row was declared to let continue.

        THE SENTENCE IS THE PRODUCT, so a context row no step and no sibling row
        dereferences is asked all the same, before the run works: a row declared
        to say what the run stood beside and then never asked says nothing at
        all. Every other declared artifact nothing reads still costs no fetch.
        */
    }

    requirement def ABoxIsNotADomainAndIsItsOwnSlot {
        doc /*
        A box has no shoreline, so it is not a domain and the mesher refuses one
        by name. What it IS is the window a question is asked inside, and a
        question that cuts its domain out of one declares that window as its own
        slot - which is what puts a canvas ask back where the domain's producer
        took it away: a slot with a producer is never drawn, so a harbour whose
        water is CUT would otherwise have nothing a user with nothing can fill.

        One role, one ingestion, one draw purpose: a rectangle picked on the
        canvas, four numbers, a place or a layer's bounds all read as ``.bbox``
        afterwards, and the raster engines fill the same slot with the grid
        window they solve on.
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

        forbid: trid3nt_server.tools.fetchers.ocean.fetch_bluetopo.hooks -> trid3nt_server.tools.derive
        forbid: trid3nt_server.tools.fetchers._router.hooks.topobathy_class -> trid3nt_server.tools.derive
        forbid: trid3nt_server.tools.fetchers._router.hooks.topobathy -> trid3nt_server.tools.derive
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement PerWaterBodyClassLadders by waterBodyClassifier;
    satisfy requirement PerWaterBodyClassLadders by bedLadderRegistry;
    satisfy requirement PerWaterBodyClassLadders by fetcherRouter;
    verify requirement PerWaterBodyClassLadders by
        "tests/fetchers/test_bathymetry_data_seam.py::test_the_coastal_ladder_puts_bluetopo_above_the_cudem_composite";
    verify requirement PerWaterBodyClassLadders by
        "tests/fetchers/test_bathymetry_data_seam.py::test_every_declared_class_either_ladders_or_stops_by_name";
    verify requirement PerWaterBodyClassLadders by
        "tests/fetchers/test_bathymetry_data_seam.py::test_an_undeclared_class_keeps_the_rows_unclassed_ladder";
    verify requirement PerWaterBodyClassLadders by
        "tests/fetchers/test_bathymetry_data_seam.py::test_every_class_ladder_ends_at_refuse_with_the_rows_own_error_code";

    satisfy requirement ACatalogReaderCarriesNoProvenance by stacRasterReader;
    satisfy requirement ACatalogReaderCarriesNoProvenance by copernicusDeclaration;
    verify requirement ACatalogReaderCarriesNoProvenance by
        "tests/fetchers/test_router_stac_raster.py::test_float_render_applies_scale_offset_and_fill";
    verify requirement ACatalogReaderCarriesNoProvenance by
        "tests/fetchers/test_router_stac_raster.py::test_mosaic_fuse_is_first_valid_in_search_order";
    verify requirement ACatalogReaderCarriesNoProvenance by
        "tests/fetchers/test_bathymetry_data_seam.py::test_every_bed_capable_source_row_states_its_vertical_datum";

    satisfy requirement WaterBodyClassComesFromHeldData by waterBodyClassifier;
    satisfy requirement WaterBodyClassComesFromHeldData by coastalDeclaration;
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/fetchers/test_bathymetry_data_seam.py::test_a_tidal_ftype_classifies_the_reach_as_coastal_estuary";
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/fetchers/test_bathymetry_data_seam.py::test_no_mapped_water_surface_classifies_the_reach_as_a_small_inland_stream";
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/fetchers/test_bathymetry_data_seam.py::test_a_wide_inland_river_refuses_naming_what_would_have_decided_it";
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/fetchers/test_bathymetry_data_seam.py::test_the_classifier_never_guesses_a_class_from_an_unknown_ftype";
    verify requirement WaterBodyClassComesFromHeldData by
        "tests/fetchers/test_bathymetry_data_seam.py::test_the_topobathy_row_declares_the_water_body_class_it_ladders_on";

    satisfy requirement SubstitutionIsDeclared by waterBodyClassifier;
    satisfy requirement SubstitutionIsDeclared by ladderWalker;
    satisfy requirement SubstitutionIsDeclared by coastalComposite;
    satisfy requirement SubstitutionIsDeclared by rowLadderWalker;
    verify requirement SubstitutionIsDeclared by
        "tests/fetchers/test_bathymetry_data_seam.py::test_falling_from_bluetopo_to_the_cudem_composite_is_a_declared_cross_dataset_rung";
    verify requirement SubstitutionIsDeclared by
        "tests/fetchers/test_bathymetry_data_seam.py::test_a_partial_bluetopo_cover_is_reported_as_a_gap_not_a_whole_bed";
    verify requirement SubstitutionIsDeclared by
        "tests/fetchers/test_fallback_ladder.py::test_declared_rung_fills_the_gap_and_splits_coverage";
    verify requirement SubstitutionIsDeclared by
        "tests/runtime/test_declarative_library.py::test_a_declared_ladder_walks_its_rungs_and_records_which_answered";

    satisfy requirement AFreeSurfaceAndABedShareOneStatedDatum by lakeLevelDeclaration;
    satisfy requirement AFreeSurfaceAndABedShareOneStatedDatum by lakeDeclaration;
    satisfy requirement AFreeSurfaceAndABedShareOneStatedDatum by freeSurfaceReader;
    verify requirement AFreeSurfaceAndABedShareOneStatedDatum by
        "tests/telemac/test_telemac3d_stratified_flow.py::test_the_level_the_free_surface_opens_at_is_one_measured_value";
    verify requirement AFreeSurfaceAndABedShareOneStatedDatum by
        "tests/telemac/test_telemac3d_stratified_flow.py::test_an_ungauged_body_of_water_continues_the_run";
    verify requirement AFreeSurfaceAndABedShareOneStatedDatum by
        "tests/inputs/test_slot_inputs.py::test_each_slot_reads_through_the_ingestion_its_role_names";
    verify requirement AFreeSurfaceAndABedShareOneStatedDatum by
        "tests/telemac/test_open_water_domains.py::test_the_thermocline_is_stated_below_the_water_top_not_the_datum";
    verify requirement AFreeSurfaceAndABedShareOneStatedDatum by
        "tests/derive/test_derive_merge_rasters.py::test_two_datums_refuse_by_name";
    verify requirement AFreeSurfaceAndABedShareOneStatedDatum by
        "tests/derive/test_derive_merge_rasters.py::test_an_unstated_datum_refuses";
    verify requirement AFreeSurfaceAndABedShareOneStatedDatum by
        "tests/inputs/test_vertical_datum.py::test_a_layer_states_its_own_datum_and_a_name_reads_its_source_row";

    satisfy requirement BedSourceStatesItsDatum by blueTopoSource;
    satisfy requirement BedSourceStatesItsDatum by blueTopoDeclaration;
    satisfy requirement BedSourceStatesItsDatum by bedResultModel;
    verify requirement BedSourceStatesItsDatum by
        "tests/fetchers/test_bathymetry_data_seam.py::test_a_tile_that_states_navd88_passes_the_datum_gate";
    verify requirement BedSourceStatesItsDatum by
        "tests/fetchers/test_bathymetry_data_seam.py::test_a_tile_that_states_no_navd88_refuses_rather_than_merging";
    verify requirement BedSourceStatesItsDatum by
        "tests/fetchers/test_bathymetry_data_seam.py::test_the_envelope_states_the_datum_in_provenance";
    verify requirement BedSourceStatesItsDatum by
        "tests/fetchers/test_bathymetry_data_seam.py::test_the_bluetopo_spec_declares_the_delegate_hooks_and_the_result_model";

    satisfy requirement OneSourceReadsOnOneStatedDatum by bedPainter;
    satisfy requirement OneSourceReadsOnOneStatedDatum by lakeDeclaration;
    satisfy requirement OneSourceReadsOnOneStatedDatum by coastalDeclaration;
    satisfy requirement OneSourceReadsOnOneStatedDatum by blueTopoDeclaration;
    verify requirement OneSourceReadsOnOneStatedDatum by
        "tests/fetchers/test_bathymetry_data_seam.py::test_every_bed_capable_source_row_states_its_vertical_datum";
    verify requirement OneSourceReadsOnOneStatedDatum by
        "tests/fetchers/test_bathymetry_data_seam.py::test_the_lake_row_pins_one_product_of_the_mixed_mosaic";
    verify requirement OneSourceReadsOnOneStatedDatum by
        "tests/mesh/test_mesh_topology_and_bed.py::test_a_source_row_that_states_no_datum_is_not_a_bed";
    verify requirement OneSourceReadsOnOneStatedDatum by
        "tests/mesh/test_mesh_topology_and_bed.py::test_the_bed_card_states_the_datum_and_the_native_cell_of_its_source";
    verify requirement OneSourceReadsOnOneStatedDatum by
        "tests/fetchers/test_emit_on_fetch_seam.py::test_input_layer_name_shape_and_purpose";

    satisfy requirement ASourceStatesWhatItCovers by coverageContract;
    satisfy requirement ASourceStatesWhatItCovers by gaugeDeclaration;
    satisfy requirement ASourceStatesWhatItCovers by lakeDeclaration;
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_coverage_rows.py::test_every_spec_loads_and_its_rows_validate";
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_coverage_rows.py::test_a_source_with_no_row_says_it_is_model_callable";
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_coverage_rows.py::test_a_station_row_lists_its_stations";
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_coverage_rows.py::test_the_tide_gauge_states_the_record_and_the_prediction_apart";
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_coverage_rows.py::test_the_nearest_station_is_what_a_listed_set_is_measured_from";
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_coverage_rows.py::test_every_series_source_states_the_unit_of_its_value_columns";
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_coverage_rows.py::test_the_gauge_network_states_each_column_on_its_own_row";
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_coverage_rows.py::test_a_lake_row_covers_its_lake_and_not_the_open_coast";
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_router_spec_loader.py::test_a_coverage_row_naming_a_class_outside_the_vocabulary_is_refused";
    verify requirement ASourceStatesWhatItCovers by
        "tests/fetchers/test_router_spec_loader.py::test_a_spec_states_no_coverage_by_default";

    satisfy requirement ASlotStatesANeedAndTheMatchFillsIt by needDeclaration;
    satisfy requirement ASlotStatesANeedAndTheMatchFillsIt by sourceMatch;
    satisfy requirement ASlotStatesANeedAndTheMatchFillsIt by matchedSlot;
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_one_survivor_fills_the_slot_and_the_sentence_names_the_facts";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_gauge_on_another_river_is_not_this_domain_s_discharge";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_gauge_on_the_reach_survives_the_place_filter";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_the_reach_still_answers_for_a_water_level_gauge";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_source_too_coarse_for_the_mesh_ranks_below_every_source_that_resolves_it";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_source_already_on_the_run_s_frame_outranks_one_that_needs_a_shift";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_place_no_source_reaches_refuses_naming_what_each_filter_excluded";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_static_survey_older_than_the_run_is_never_dropped_by_the_window";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_series_outside_the_window_refuses_with_the_nearest_record_named";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_one_run_level_statement_loosens_the_window_as_the_user_s_choice";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_measured_gauge_at_the_place_outranks_a_modelled_grid_over_it";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_measured_gauge_outranks_a_prediction_a_hundred_kilometres_away";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_station_beyond_its_reach_is_not_a_survivor_at_all";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_run_that_picks_a_survivor_takes_it_as_the_user_s_choice";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/search/test_source_match.py::test_a_run_that_picks_a_source_the_filters_excluded_is_refused";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/runtime/test_matched_slots.py::test_the_bed_is_the_measurement_gridded_over_the_terrain_under_it";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/runtime/test_matched_slots.py::test_no_measurement_over_this_domain_leaves_the_terrain_as_the_whole_bed";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/runtime/test_matched_slots.py::test_the_next_survivor_takes_its_turn_when_the_top_one_held_nothing";
    verify requirement ASlotStatesANeedAndTheMatchFillsIt by
        "tests/runtime/test_matched_slots.py::test_a_need_and_a_producer_on_one_row_is_refused_at_declaration";

    satisfy requirement TheRankedListIsOneObjectInThreeViews by rankedListCard;
    satisfy requirement TheRankedListIsOneObjectInThreeViews by rankedListRecord;
    satisfy requirement TheRankedListIsOneObjectInThreeViews by coverageContract;
    verify requirement TheRankedListIsOneObjectInThreeViews by
        "tests/runtime/test_matched_slots.py::test_the_ranked_list_is_one_object_the_card_and_the_sheet_both_read";
    verify requirement TheRankedListIsOneObjectInThreeViews by
        "tests/search/test_source_match.py::test_several_equal_on_every_fact_present_as_a_tie";
    verify requirement TheRankedListIsOneObjectInThreeViews by
        "tests/search/test_source_match.py::test_the_ranked_list_carries_at_most_five_rows";

    satisfy requirement ASeriesIsAlignedToTheRunsEventTime by seriesOnTheRunsClock;
    satisfy requirement ASeriesIsAlignedToTheRunsEventTime by freeSurfaceReader;
    satisfy requirement ASeriesIsAlignedToTheRunsEventTime by gaugeDeclaration;
    satisfy requirement ASeriesIsAlignedToTheRunsEventTime by matchedSlot;
    verify requirement ASeriesIsAlignedToTheRunsEventTime by
        "tests/inputs/test_observation.py::test_the_run_opens_at_its_own_moment_inside_the_record";
    verify requirement ASeriesIsAlignedToTheRunsEventTime by
        "tests/inputs/test_observation.py::test_a_record_that_stops_before_the_run_does_refuses_naming_the_nearest";
    verify requirement ASeriesIsAlignedToTheRunsEventTime by
        "tests/inputs/test_observation.py::test_a_window_in_no_stated_unit_refuses_rather_than_reading_the_slot_s";
    verify requirement ASeriesIsAlignedToTheRunsEventTime by
        "tests/inputs/test_observation.py::test_the_coverage_row_s_column_unit_is_what_a_bare_record_is_read_in";
    verify requirement ASeriesIsAlignedToTheRunsEventTime by
        "tests/inputs/test_observation.py::test_a_gage_height_reaches_the_run_s_frame_over_the_gauge_s_own_zero";
    verify requirement ASeriesIsAlignedToTheRunsEventTime by
        "tests/inputs/test_observation.py::test_a_gauge_that_publishes_no_zero_refuses_rather_than_reading_a_height";
    verify requirement ASeriesIsAlignedToTheRunsEventTime by
        "tests/fetchers/test_router_nwis.py::test_parse_iv_window_publishes_the_stage_series_beside_the_discharge";
    verify requirement ASeriesIsAlignedToTheRunsEventTime by
        "tests/fetchers/test_router_nwis.py::test_parse_iv_instantaneous_carries_the_gauge_s_own_zero";
    verify requirement ASeriesIsAlignedToTheRunsEventTime by
        "tests/runtime/test_matched_slots.py::test_a_run_series_is_asked_for_the_window_the_deck_will_solve";

    satisfy requirement TheRunHasOneVerticalFrame by verticalFrame;
    satisfy requirement TheRunHasOneVerticalFrame by frameLever;
    satisfy requirement TheRunHasOneVerticalFrame by bedSlot;
    satisfy requirement TheRunHasOneVerticalFrame by bedComposer;
    satisfy requirement TheRunHasOneVerticalFrame by freeSurfaceReader;
    satisfy requirement TheRunHasOneVerticalFrame by offsetRowDeclaration;
    verify requirement TheRunHasOneVerticalFrame by
        "tests/derive/test_derive_merge_rasters.py::test_a_primary_of_depths_is_read_as_elevations_on_the_fallbacks_zero";
    verify requirement TheRunHasOneVerticalFrame by
        "tests/derive/test_derive_merge_rasters.py::test_a_depth_surface_whose_zero_nothing_bridges_refuses_by_name";
    verify requirement TheRunHasOneVerticalFrame by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_every_elevation_slot_is_read_on_the_runs_own_vertical_frame";
    verify requirement TheRunHasOneVerticalFrame by
        "tests/inputs/test_vertical_datum.py::test_a_survey_reaches_the_runs_frame_through_the_shift_it_publishes";
    verify requirement TheRunHasOneVerticalFrame by
        "tests/inputs/test_vertical_datum.py::test_a_source_publishing_no_shift_owes_an_offset_row";
    verify requirement TheRunHasOneVerticalFrame by
        "tests/inputs/test_vertical_datum.py::test_the_coercion_reads_the_rows_value_and_fetches_nothing";
    verify requirement TheRunHasOneVerticalFrame by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_the_runtime_declares_the_offset_row_a_differing_source_owes";
    verify requirement TheRunHasOneVerticalFrame by
        "tests/inputs/test_vertical_datum.py::test_a_pair_nothing_measures_refuses_naming_both_frames";
    verify requirement TheRunHasOneVerticalFrame by
        "tests/inputs/test_slot_inputs.py::test_the_bed_slot_says_which_shape_it_was_handed";

    satisfy requirement AStoppedRungNeverShips by waterBodyClassifier;
    satisfy requirement AStoppedRungNeverShips by coastalComposite;
    verify requirement AStoppedRungNeverShips by
        "tests/fetchers/test_bathymetry_data_seam.py::test_a_small_inland_stream_has_no_ladder_and_refuses_naming_both_gaps";
    verify requirement AStoppedRungNeverShips by
        "tests/fetchers/test_bathymetry_data_seam.py::test_a_navigable_river_has_no_ladder_and_refuses_naming_its_stopped_primary";
    verify requirement AStoppedRungNeverShips by
        "tests/fetchers/test_bathymetry_data_seam.py::test_no_ladder_anywhere_ships_an_ehydro_rung";
    verify requirement AStoppedRungNeverShips by
        "tests/fetchers/test_bathymetry_data_seam.py::test_a_stopped_class_refuses_before_the_cache_and_before_the_network";
    verify requirement AStoppedRungNeverShips by
        "tests/fetchers/test_bathymetry_data_seam.py::test_a_tile_row_with_no_delivered_link_is_not_data";
    verify requirement AStoppedRungNeverShips by
        "tests/fetchers/test_bathymetry_data_seam.py::test_an_aoi_no_delivered_tile_reaches_refuses_by_name";
    verify requirement AStoppedRungNeverShips by
        "tests/fetchers/test_bathymetry_data_seam.py::test_the_synthetic_slot_is_stated_as_deferred_rather_than_forgotten";

    satisfy requirement TheShorelineIsTheDomainsOwnEdge by waterCut;
    satisfy requirement TheShorelineIsTheDomainsOwnEdge by coastlineDeclaration;
    verify requirement TheShorelineIsTheDomainsOwnEdge by
        "tests/mesh/test_mesh_polygon_domain.py::test_the_shoreline_the_sizing_functions_measure_is_the_polygons_own_edge";
    verify requirement TheShorelineIsTheDomainsOwnEdge by
        "tests/mesh/test_mesh_polygon_domain.py::test_a_reach_with_two_open_runs_drops_those_stretches_from_the_shore";
    verify requirement TheShorelineIsTheDomainsOwnEdge by
        "tests/mesh/test_mesh_polygon_domain.py::test_an_island_ring_is_shore_too";
    verify requirement TheShorelineIsTheDomainsOwnEdge by
        "tests/mesh/test_mesh_polygon_domain.py::test_a_domain_whose_whole_edge_is_crossed_refuses_by_name";
    verify requirement TheShorelineIsTheDomainsOwnEdge by
        "tests/mesh/test_mesh_om2d.py::test_a_box_extent_refuses_and_names_the_cut_that_makes_it_a_domain";
    verify requirement TheShorelineIsTheDomainsOwnEdge by
        "tests/mesh/test_mesh_water.py::test_the_water_is_the_side_the_way_does_not_have_its_land_on";
    verify requirement TheShorelineIsTheDomainsOwnEdge by
        "tests/mesh/test_mesh_water.py::test_reversing_the_way_moves_the_water_to_the_other_side";
    verify requirement TheShorelineIsTheDomainsOwnEdge by
        "tests/mesh/test_mesh_water.py::test_a_way_that_ends_inside_the_box_divides_nothing_and_refuses";

    satisfy requirement TheDomainIsOneSlotHoweverItIsFilled by domainSlot;
    satisfy requirement TheDomainIsOneSlotHoweverItIsFilled by slotDoor;
    satisfy requirement TheDomainIsOneSlotHoweverItIsFilled by contextRow;
    verify requirement TheDomainIsOneSlotHoweverItIsFilled by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_domain_row_is_one_slot_however_it_is_filled";
    verify requirement TheDomainIsOneSlotHoweverItIsFilled by
        "tests/inputs/test_slot_inputs.py::test_a_drawn_ring_a_geojson_polygon_and_a_collection_read_the_same";
    verify requirement TheDomainIsOneSlotHoweverItIsFilled by
        "tests/inputs/test_slot_inputs.py::test_several_polygons_are_one_domain_with_parts";
    verify requirement TheDomainIsOneSlotHoweverItIsFilled by
        "tests/inputs/test_slot_inputs.py::test_a_shape_that_is_not_closed_refuses_rather_than_being_squared_off";
    verify requirement TheDomainIsOneSlotHoweverItIsFilled by
        "tests/telemac/test_workflow_owns_its_stages.py::test_the_domain_and_the_bed_reach_the_wire_as_the_slots_they_are";
    verify requirement TheDomainIsOneSlotHoweverItIsFilled by
        "tests/inputs/test_slot_inputs.py::test_a_domain_says_its_own_name_when_it_is_read_as_text";

    satisfy requirement OneBedOverEverySourceAUserCanHave by bedSlot;
    satisfy requirement OneBedOverEverySourceAUserCanHave by bedPainter;
    satisfy requirement OneBedOverEverySourceAUserCanHave by bedComposer;
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/inputs/test_slot_inputs.py::test_the_bed_slot_takes_one_source_and_composes_nothing";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/telemac/test_workflow_owns_its_stages.py::test_the_bed_op_takes_the_one_row_the_merge_derive_produced";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/derive/test_derive_merge_rasters.py::test_an_absent_survey_leaves_the_terrain_as_the_whole_bed";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/inputs/test_slot_inputs.py::test_the_bed_slot_says_which_shape_it_was_handed";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/mesh/test_bed_sources_and_runs.py::test_a_stated_depth_is_a_flat_bed_below_the_free_surface";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/mesh/test_bed_sources_and_runs.py::test_the_bed_is_one_source_and_two_are_merged_before_the_op";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/derive/test_derive_merge_rasters.py::test_the_primary_wins_where_it_measured_and_the_fallback_fills_the_rest";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/derive/test_derive_merge_rasters.py::test_the_sidecar_says_which_input_painted_each_cell";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/derive/test_derive_merge_rasters.py::test_an_absent_primary_passes_the_other_surface_through";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/mesh/test_bed_sources_and_runs.py::test_a_domain_no_source_covers_refuses_by_name";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/mesh/test_bed_sources_and_runs.py::test_a_layer_of_soundings_goes_through_the_derive_at_the_meshs_own_scale";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/mesh/test_bed_sources_and_runs.py::test_a_tree_without_the_soundings_derive_says_which_one_is_missing";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/telemac/test_workflow_owns_its_stages.py::test_the_bed_op_takes_the_one_row_the_merge_derive_produced";
    verify requirement OneBedOverEverySourceAUserCanHave by
        "tests/telemac/test_workflow_owns_its_stages.py::test_a_second_bed_row_refuses_by_name";

    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/inputs/test_slot_inputs.py::test_a_domain_producer_hands_over_the_runs_it_cut_the_polygon_between";
    satisfy requirement ABoundaryRunIsTwoPointsAndAType by boundaryRunSlot;
    satisfy requirement ABoundaryRunIsTwoPointsAndAType by bedPainter;
    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/inputs/test_slot_inputs.py::test_a_run_is_two_points_on_the_edge_and_a_type";
    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/inputs/test_slot_inputs.py::test_a_drawn_line_is_the_run_between_its_two_ends";
    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/inputs/test_slot_inputs.py::test_a_closed_body_states_no_run_and_prescribes_nothing";
    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/mesh/test_bed_sources_and_runs.py::test_boundary_runs_prescribe_the_roles_their_types_name";
    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/mesh/test_bed_sources_and_runs.py::test_a_closed_body_states_no_run_and_the_mesh_has_only_walls";
    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_the_runs_slot_is_filled_by_the_domains_own_producer";
    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_domain_that_carries_no_runs_leaves_the_slot_empty";
    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_run_the_user_hands_over_supersedes_the_producers_own";
    verify requirement ABoundaryRunIsTwoPointsAndAType by
        "tests/telemac/test_telemac_open_channel.py::test_a_domain_with_no_inflow_run_has_no_channel_to_open";

    satisfy requirement ABoxIsNotADomainAndIsItsOwnSlot by extentSlot;
    satisfy requirement ABoxIsNotADomainAndIsItsOwnSlot by slotDoor;
    verify requirement ABoxIsNotADomainAndIsItsOwnSlot by
        "tests/inputs/test_slot_inputs.py::test_an_extent_slot_reads_a_box_however_the_caller_named_it";
    verify requirement ABoxIsNotADomainAndIsItsOwnSlot by
        "tests/inputs/test_slot_inputs.py::test_only_the_slots_a_user_can_draw_are_offered_on_the_canvas";
    verify requirement ABoxIsNotADomainAndIsItsOwnSlot by
        "tests/telemac/test_agitation_template.py::test_the_box_is_an_extent_slot_the_canvas_offers_a_rectangle_for";
    verify requirement ABoxIsNotADomainAndIsItsOwnSlot by
        "tests/telemac/test_agitation_template.py::test_the_mesh_is_cut_from_the_domain_polygon_and_not_from_a_box";

    satisfy requirement AContextRowsAbsenceContinuesTheRun by contextRow;
    satisfy requirement AContextRowsAbsenceContinuesTheRun by unreadContextAsk;
    verify requirement AContextRowsAbsenceContinuesTheRun by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_context_row_no_step_reads_is_asked_all_the_same";
    verify requirement AContextRowsAbsenceContinuesTheRun by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_context_rows_absence_continues_the_run_and_says_so";
    verify requirement AContextRowsAbsenceContinuesTheRun by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_hard_producer_row_still_refuses_when_its_source_is_empty";
    verify requirement AContextRowsAbsenceContinuesTheRun by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_malformed_ask_on_a_context_row_refuses_rather_than_reading_absent";
    verify requirement AContextRowsAbsenceContinuesTheRun by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_malformed_value_handed_to_a_context_rows_slot_refuses_too";
    verify requirement AContextRowsAbsenceContinuesTheRun by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_context_needs_a_producer_and_optional_still_refuses_one";
    verify requirement AContextRowsAbsenceContinuesTheRun by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_context_row_states_what_the_sheet_says_when_its_source_is_empty";
    verify requirement AContextRowsAbsenceContinuesTheRun by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_a_tail_read_off_a_wholly_absent_row_is_nothing";
    verify requirement AContextRowsAbsenceContinuesTheRun by
        "tests/runtime/test_domain_bed_and_context_slots.py::test_the_sheet_reads_a_wholly_absent_row_as_nothing";

    satisfy requirement ThalwegBurningIsNeverABedSource by blueTopoSource;
    satisfy requirement ThalwegBurningIsNeverABedSource by waterBodyClassifier;
    satisfy requirement ThalwegBurningIsNeverABedSource by coastalComposite;
    verify requirement ThalwegBurningIsNeverABedSource by
        "tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree";
    verify requirement ThalwegBurningIsNeverABedSource by
        "tests/fetchers/test_bathymetry_data_seam.py::test_the_selected_tiles_run_coarsest_first_so_the_finest_paints_last";
}
