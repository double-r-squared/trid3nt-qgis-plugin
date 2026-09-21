// plane: workflow | system: runtime -> assembler
//
// The module surface: a TELEMAC module's own keyword table, the wrapper it
// makes, the sheet a body fills, and the one writer of the steering format.
//
// SysML v2 TEXTUAL notation, restricted to the subset the project checker
// reads. Conventions as the other seams: ``code:`` binds a block to the module
// it IS, ``forbid:`` states a dependency rule, ``constructor:`` names a
// function that builds a contract.
//
// This is the runtime system's first modeled seam. The plan language beside it
// - steps, gates, the interpreter that walks them - is unmodeled and stated so
// in the index, because it is the half this surface is replacing.

package SteeringSurface {

    doc /*
    The engine publishes its whole keyword surface: every module's dictionary
    names each keyword, its type, its allowed values, its default and its help.
    Transcribing that by hand into f-strings and comments is what this seam
    exists to stop. The dictionary is extracted in the image, committed as data
    and audited against the image; the wrapper it makes is the analog of the
    engine's own defaults and asserts NOTHING of its own.

    Opinion lives one layer up, in templates, which stay Python because people
    read them. A body asserts raw keywords under the identifiers the image
    itself spells them by, and a sheet records per slot WHERE its value came
    from - engine default, shared body, template, fill, producer - so nothing
    inherited or derived hides behind a number.

    Two acts on the sheet, and only two. Fill is repeatable and decides nothing.
    Run is explicit, on a complete sheet, and it is where execution stops being
    held: the sheet is serialized by telapy - the one writer of the format -
    read straight back by the engine's own parser, staged, and handed to the box.

    The module is the unit the whole surface is built around. A question is a
    template: values over one module - a body of keywords, DATA rows, the reads
    it PLACES, captions - and nothing below it knows the question exists. What a
    run WRITES is the module's own output table, and the reads over it are
    primitives named from that table; the run record carries the engine and the
    module, every filled slot carries one of six origins, and the adapters build
    their turns from an IR of this repo's own. A point anywhere in the system is
    one typed Point.
    */

    // ----------------------------------------------------------------- //
    // Blocks
    // ----------------------------------------------------------------- //

    part def Dictionary {
        doc /*
        The in-image extractor: the module dictionaries read through the
        engine's own DAMOCLES reader, trimmed to what a slot is filled and
        refused by, and written as one committed JSON per module.
        */
    }

    part def Slot {
        doc /*
        One keyword as the dictionary describes it - raw name, identifier, type,
        list-ness, help, choices, engine default, level, rubrique, file role -
        and the check that says whether a value is one it takes.

        Beside the dictionary's own row it carries the PLAUSIBILITY BOUNDS a
        value of the keyword is taken inside, read from a sidecar because the
        dictionary itself is the image's and is compared to it byte for byte.
        Those bounds are where a deleted Param's own range went: one range for
        every value the keyword holds, or one per element in the keyword's order,
        and a value outside refuses by the keyword's name at fill. It is the one
        table a calibration reads its priors and its ranges from.
        */
    }

    part def Module {
        doc /*
        The wrapper class the dictionary makes. It holds the dictionary, the
        composites and the outputs, and it asserts no value: there is
        deliberately no hook here for a default.
        */
    }

    part def TemporalValue {
        doc /*
        The runtime's stated value that is not one number: instants on the run's
        own clock, in one unit.
        */
    }

    part def Alignment {
        doc /*
        The ONE move a measured record may make onto the clock a reader reads it
        on: its quantity class picks the method, its unit is converted, and a
        hole wider than the bound refuses.
        */
    }

    part def ObservationSlot {
        doc /*
        The ingestion behind the measured-value slots: one reading, and the
        window the record reported it inside.
        */
    }

    part def Composite {
        doc /*
        One value standing for several slots and the file they name - a release
        becoming the sources keywords plus the series file, a gradation becoming
        the class table a mixture IS. It lives on the wrapper because the keyword
        group is the module's, not a template's private code.
        */
    }

    part def Output {
        doc /*
        One variable the module writes: the name its result file carries it
        under, its unit, the style row it draws under and whether it varies in
        time. The table of those rows is the module's whole statement about
        what a run of it produces.
        */
    }

    part def Sheet {
        doc /*
        A module's slots as they stand: what is filled and by whom, what a
        composite produced, and which mandatory slots are still open. Fill and
        run are the two acts on it.
        */
    }

    part def Serializer {
        doc /*
        The sheet resolved into the engine's own steering file. It holds the
        resolution and the two measured caveats of the format; telapy holds the
        writing.
        */
    }

    part def SteeringDriver {
        doc /*
        The in-container half: telapy writes the file and the DAMOCLES reader
        reads it back. The only place either happens.
        */
    }

    part def SteeringGate {
        doc /*
        The round trip's host end - the one door to the image, in both
        directions - and the refusal that names the file and the keyword.
        */
    }

    part def CanvasGate {
        doc /*
        The canvas ask: one value drawn on the map, or a typed reason there is
        none. It is the same gate a typed value passes through, so the drawn
        vocabulary and the typed vocabulary cannot drift.
        */
    }

    part def EngineWorkflow {
        doc /*
        The engine's own workflow class, which reads a template MODULE by its
        own names - the STEERING body, the data rows, the outputs and the
        answer - and builds the plan off them. It decides nothing: the sequence
        it builds is the same for every question, and no object stands between
        the declaration and the plan.
        */
    }

    part def Stager {
        doc /*
        An authored run directory becomes a staged run: everything uploaded
        beside the mesh, the manifest naming the case written last.
        */
    }

    part def RunRecord {
        doc /*
        One line per completed run: the engine and the module it ran under, the
        sheet with its provenance, the raw keyword floor it was pinned by, the
        answer. The template is not a field of the line; it survives as the
        provenance of the slots it filled.
        */
    }

    part def TypedInput {
        doc /*
        A Point: lon, lat and a name, ingested once from every form a user hands
        it in - the pick, a pair, a point layer, a geocoded place - and validated
        against the mesh by the slot that asked for it.
        */
    }

    part def MessageIR {
        doc /*
        The five models a turn is built from - a message, its parts, a tool
        call, a tool response, a tool declaration - owned by the contracts
        package, so no provider SDK is carried for its data classes.
        */
    }

    // ----------------------------------------------------------------- //
    // Ports
    // ----------------------------------------------------------------- //

    port def DictionaryOut;
    port def DictionaryIn;
    port def SurfaceOut;
    port def SurfaceIn;
    port def DeckOut;
    port def DeckIn;
    port def WriteOut;
    port def WriteIn;
    port def ParseOut;
    port def ParseIn;
    port def StageOut;
    port def StageIn;
    port def DrawOut;
    port def DrawIn;

    // ----------------------------------------------------------------- //
    // Interfaces - the seam contracts, by their real key names
    // ----------------------------------------------------------------- //

    interface def KeywordDictionary {
        doc /*
        One dictionary entry as the dictionary carries it. ``keyword`` is the
        engine's own name and is never rewritten; ``identifier`` is the spelling
        a class body writes it under, taken from the map eficas ships in the
        image rather than from a rule guessed at outside it. ``help`` is the
        dictionary's own prose, de-LaTeXed, and it is what a reader and the
        model are given in place of a hand-written description.

        constructor: extract
        */
        item keyword : String;
        item identifier : String;
        item type : String;
        item size : Integer;
        item unbounded : Boolean;
        item help : String;
        item rubrique : List;
        item is_file : Boolean;
        item level : Integer [0..1];
        item default : Any [0..1];
        item choices : Any [0..1];
        item mnemo : String [0..1];
        item file_role : String [0..1];
        item file_mandatory : Boolean [0..1];
    }

    interface def WrapperSurface {
        doc /*
        What the wrapper offers a body and a fill: the module it wraps, its
        whole keyword table, the composites registered on it, and what THIS body
        asserts - empty on the wrapper itself, by law.
        */
        item MODULE : String;
        item MODULE_INPUT : Map;
        item COMPOSITES : Map;
        item ASSERTED : Map;
    }

    interface def ResolvedSheet {
        doc /*
        What the serializer receives: the module, the keywords the deck states
        with their values, and the files a composite named. An engine default is
        not among them - the dictionary already supplies it, and writing it back
        would make the deck claim a choice nobody made.
        */
        item module : String;
        item resolved : List;
        item files : Map;
    }

    interface def SteeringWrite {
        doc /*
        The write the driver performs. ``values`` is keyed by the RAW keyword,
        because that is what the dictionary is keyed by; a string among them is
        spelled in the engine's own form on the way out, since Python's own repr
        reaches for a double-quote delimiter as soon as a value holds an
        apostrophe and a double-quoted value derails DAMOCLES on the first space
        inside it.
        */
        item write : Map;
        item module : String;
        item values : Map;
    }

    interface def SteeringParse {
        doc /*
        The round trip back. ``ok`` is the honest terminal: a file that does not
        parse against its own dictionary carries the engine's own words in
        ``error`` rather than a message this code wrote about it. This is where
        a value outside a keyword's CHOIX is caught.
        */
        item steering : Map;
        item module : String;
        item ok : Boolean;
        item keywords : Integer;
        item error : String [0..1];
    }

    interface def DrawnValue {
        doc /*
        What the canvas gave back. ``drawn`` is the honest terminal: no live
        session, a decline, or a wait that ran out all come back with a
        ``reason`` and no ``value``, and the sheet turns that into a refusal
        naming the slot that stayed empty rather than inventing a geometry.
        */
        item value : Any;
        item reason : String [0..1];
        item drawn : Boolean;
    }

    interface def StageRequest {
        doc /*
        What a complete sheet hands the stager. The run directory is already
        authored; what travels here is which engine reads which file, where the
        run stages, and the sheet itself as the run's record of what was asked.

        constructor: stage_run
        */
        item module : String;
        item steering : String;
        item results : List;
        item outputs : List;
        item mesh_inputs : List;
        item prefix : String;
        item sheet : Map;
        item result_basename : String;
        item server_facts : Map;
    }

    // ----------------------------------------------------------------- //
    // Parts - bound to the modules they ARE
    // ----------------------------------------------------------------- //

    part dictionaryExtractor : Dictionary {
        doc /*
        code: workers/telemac/scripts/dico.py
        */
        port dictionaryOut : DictionaryOut;
    }

    part slot : Slot {
        doc /*
        code: trid3nt_server/workflows/telemac/modules/module.py
        */
        port dictionaryIn : DictionaryIn;
    }

    part wrapper : Module {
        doc /*
        code: trid3nt_server/workflows/telemac/modules/module.py
        */
        port dictionaryIn : DictionaryIn;
        port surfaceOut : SurfaceOut;
    }

    part composite : Composite {
        doc /*
        The composites are the WRAPPER's, so they live in the wrapper files -
        one value standing for the keyword group the module reads it as. The
        coupled modules carry their own beside them (waqtel.py, gaia.py), and a
        capability the engine offers only as keywords on one of those decks is
        a composite there rather than a surface of its own. The one composite
        two wrappers register is coupling, which both hydrodynamic carriers take
        from coupling.py; the water column is all that separates their reading
        of it.
        code: trid3nt_server/workflows/telemac/modules/telemac2d.py
        code: trid3nt_server/workflows/telemac/modules/coupling.py
        */
        port surfaceOut : SurfaceOut;
    }

    part temporalValue : TemporalValue {
        doc /*
        code: trid3nt_server/workflows/runtime/temporal.py
        */
    }

    part alignment : Alignment {
        doc /*
        code: trid3nt_server/inputs/series.py
        */
    }

    part observationSlot : ObservationSlot {
        doc /*
        code: trid3nt_server/inputs/observation.py
        */
    }

    part output : Output {
        doc /*
        code: trid3nt_server/workflows/telemac/modules/telemac2d.py
        */
        port surfaceOut : SurfaceOut;
    }

    part sheet : Sheet {
        doc /*
        code: trid3nt_server/workflows/telemac/modules/sheet.py
        */
        port surfaceIn : SurfaceIn;
        port deckOut : DeckOut;
        port stageOut : StageOut;
        port drawIn : DrawIn;
    }

    part canvasGate : CanvasGate {
        doc /*
        code: trid3nt_server/gates/draw_input.py
        */
        port drawOut : DrawOut;
    }

    part serializer : Serializer {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/serializer.py
        */
        port deckIn : DeckIn;
        port writeOut : WriteOut;
    }

    part steeringGate : SteeringGate {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/cas_validate.py
        */
        port writeOut : WriteOut;
        port parseIn : ParseIn;
    }

    part steeringDriver : SteeringDriver {
        doc /*
        code: workers/telemac/scripts/cas.py
        */
        port writeIn : WriteIn;
        port parseOut : ParseOut;
    }

    part stager : Stager {
        doc /*
        code: trid3nt_server/workflows/telemac/authoring/assembler.py
        */
        port stageIn : StageIn;
    }

    part engineWorkflow : EngineWorkflow {
        doc /*
        code: trid3nt_server/workflows/telemac/workflow.py
        */
    }

    part runRecord : RunRecord {
        doc /*
        code: trid3nt_server/workflows/runtime/journal.py
        */
    }

    part point : TypedInput {
        doc /*
        code: trid3nt_server/inputs/point.py
        */
    }

    part messageIR : MessageIR {
        doc /*
        code: contracts/trid3nt_contracts/message.py
        */
    }

    // ----------------------------------------------------------------- //
    // Hops
    // ----------------------------------------------------------------- //

    interface dictionaryExtracted : KeywordDictionary
        connect dictionaryExtractor.dictionaryOut to slot.dictionaryIn;

    interface dictionaryLoaded : KeywordDictionary
        connect dictionaryExtractor.dictionaryOut to wrapper.dictionaryIn;

    interface surfaceOffered : WrapperSurface
        connect wrapper.surfaceOut to sheet.surfaceIn;

    interface sheetResolved : ResolvedSheet
        connect sheet.deckOut to serializer.deckIn;

    interface writeRequested : SteeringWrite
        connect serializer.writeOut to steeringGate.writeOut {
        doc /*
        pass-through: steeringGate
        */
    }

    interface writePerformed : SteeringWrite
        connect steeringGate.writeOut to steeringDriver.writeIn {
        doc /*
        pass-through: steeringGate
        */
    }

    interface parseReturned : SteeringParse
        connect steeringDriver.parseOut to steeringGate.parseIn;

    interface runStaged : StageRequest
        connect sheet.stageOut to stager.stageIn;

    interface canvasAnswered : DrawnValue
        connect canvasGate.drawOut to sheet.drawIn;

    // ----------------------------------------------------------------- //
    // The laws this seam is governed by
    // ----------------------------------------------------------------- //

    requirement def KeywordNamesAreRaw {
        doc /*
        A slot is spelled the engine's way. The dictionary carries the dictionary's
        own keyword verbatim, and the identifier a class body writes it under is
        the image's own map - which matters because a keyword can open on a
        digit or carry a hyphen or a parenthesis, and a spelling rule invented
        here would drift from the one eficas ships.

        The pay-off is that a template reads as the engine reads: no second
        vocabulary to learn, no table mapping friendly names onto real ones, and
        a misspelling answered at import with the nearest real keyword.
        */
    }

    requirement def EverySlotDescribed {
        doc /*
        Every slot carries the dictionary's own help, rendered as plain words.
        The description is not decoration: it is what the model and the reader
        are given in place of a docstring, and 1,311 keywords cannot be
        described any other way. Markup surviving into it is markup in the
        surface, so no backslash may remain in any dictionary.
        */
    }

    requirement def EngineDefaultSurfaced {
        doc /*
        An unset slot is never a black box. The dictionary's default is on the
        slot and can be shown; a slot the dictionary gives NO default for is a
        question, and it is exactly those the sheet reports as open. The deck
        writes neither: an engine default written back would make the file claim
        a choice nobody made.
        */
    }

    requirement def TheOpenSetIsCompleteAndRequiredIsTheObligFiles {
        doc /*
        The sheet states two different things and never conflates them.

        OPEN is every keyword the dictionary gives no default for and nothing has
        set - LISTS INCLUDED. A list the dictionary writes no DEFAUT for is empty
        until something states it, and an emptiness the engine substitutes for is
        exactly what a reader has to be able to see. The set is informational and
        COMPLETE; it refuses nothing.

        REQUIRED is the dictionary's own OBLIG files, and it is the only thing a
        run refuses on. Which OTHER keyword a particular deck cannot run without
        is the engine's to say: LECDON demands it by name in the listing, and
        that sentence is carried out to the caller. A required set invented here
        would refuse runs the engine would have taken, and inventing one is the
        thing this requirement forbids.
        */
    }

    requirement def ANoDefaultKeywordIsTheTemplatesToState {
        doc /*
        A keyword the dictionary gives NO default for is a choice somebody
        makes. Left unstated it is not silence - the engine substitutes
        something of its own (SCHEME FOR ADVECTION OF TRACERS falls back to
        whatever the VELOCITIES are advected by) and the deck records no choice
        at all, so nobody can read what the run was solved under.

        Such a keyword is the TEMPLATE's to state, never the wrapper's, and it
        states three things with it: the value, the BASIS for the value where the
        LLM and the human both read it, and a way to override it on the sheet. A
        value the template derives rather than asserts names the producer it came
        from, and reuses the one that already exists.
        */
    }

    requirement def ATemplateNumberIsMetToTheMeshsPrecision {
        doc /*
        A template declares ONE number for a physical thing - the obstacle
        half-width says where the cut is - and the deck's own statement about
        that thing is read off the same number. Two numbers would let the cut
        and the deck disagree about where a structure is.

        What a node is MEASURED AGAINST is that number's outline plus the
        ACCEPTED MESH'S OWN EDGE. The edge is a property of the mesh - what a
        relaxation places a node on a locked outline to - and not a second
        tolerance the template gets to pick. Measured both ways on the same
        harbour: a band sized off the element edge ALONE sat entirely inside the
        cut, matched zero boundary nodes, and the run solved with its breakwater
        as absorbing shore; an equality at the half-width exactly halved a
        population sitting 9-11 m off a 20 m structure.

        A value derived that way is DERIVED on the sheet, not template: the
        template says which measurement the slot takes, and the number is the
        accepted artifact's.
        */
    }

    requirement def WrapperHasNoOpinion {
        doc /*
        The wrapper asserts nothing and offers nowhere to. It is the analog of
        the engine's own defaults; variance lives in templates, where a person
        reads it. A defaults hook here would put an opinion in the one place
        nobody would think to look for one, and it would be inherited by every
        template silently.

        A COUPLED BODY is on the wrapper too, and a constant inside one is the
        same opinion by a longer route: it is not a class body, so the frozen
        ASSERTED never sees it, and every template that names the body inherits
        it. What a coupled body states is what its caller handed in. The only
        values it may repeat across two calls sharing no argument are the file
        the wrapper names, the transport mode the body IS, a slot the dictionary
        states no default for, and the dictionary's own default at this body's
        own class count.
        */
    }

    requirement def EverythingOverridable {
        doc /*
        Resolution runs lowest to highest - engine default, shared body,
        template, fill - and every layer above the engine is overridable by
        plain assignment. Two dye releases is a longer list, not a new template.
        A fill is repeatable, so an edit is another fill, and the row says which
        layer answered.

        A value drawn on the canvas is a fill like any other, reached through
        the same gate a typed value rides. A canvas that answers nothing refuses
        by name; nothing is invented from a decline.

        The wire half of it is the RAW KEYWORD FLOOR every template carries: a
        caller states the dictionary's own name and the fill sets that slot,
        beating the template because that is what it was stated to do. A name
        the module has no keyword for refuses naming the nearest one it does,
        and a value outside the dictionary's choices refuses naming them - both
        while a person can still read what they asked for.

        A floor name is resolved across the RUN'S BODIES in deck order, not the
        carrier's dictionary alone: a qualified "waqtel: K2 REAERATION
        COEFFICIENT" against that body, a bare name where exactly one body
        spells it, and a collision refuses naming both qualified spellings. A
        coupled value is stored on that body's own slots and checked against its
        own dictionary, and the card shows it under its body with its unit and
        its bounds - so a keyword on a coupled deck has the same user surface
        the carrier's own has.

        The floor is resolved ONCE, BEFORE any stage runs. A stage settled from
        the deck's class attribute would describe a different run than the deck
        writes, so the early readers - the settle's clock, the open channel's
        friction law and coefficient, the wave the boundary file is stamped at,
        the outlet's rating curve - read the resolved floor at run time. A read
        placed on a keyword INSIDE a body reads the FILLED SHEET instead, which
        is the same value after a revision at the gate.

        A value whose term the engine gates on ARMS that term where nothing has
        stated it: a rate reaching the engine with its switch off is a number
        nothing reads, and arming is the value's own ingestion rather than a
        second thing the caller has to know.
        */
    }

    requirement def DictionaryMatchesImage {
        doc /*
        The committed dictionary is the image's dictionaries, not a copy that once
        was. The suite re-extracts from the image and compares, and skips saying
        so when the image is absent - it never passes on absence.
        */
    }

    requirement def CompositesLiveInWrappers {
        doc /*
        A composite is the MODULE's, registered on its wrapper: the sources
        keyword group belongs to telemac2d whichever template releases into it.
        It may not shadow a keyword, and a name no wrapper registered refuses at
        fill rather than being absorbed as something the module might mean.
        */
    }

    requirement def CompositesSetOnlyPresence {
        doc /*
        A composite expands a value into the keywords that value IS. Two
        literals a presence defines: the ARMING keyword the input implies (WIND
        = YES when a wind is given) and the NAME of the file the composite
        itself writes. A CHOICE among the alternatives the dictionary offers -
        the restart format, the runoff model, the wind option - is not one of
        them: a composite picking it puts an opinion in the one place a reader
        of the template would not look, and inherits it into every template
        that ever passes the composite a value.

        Such a choice is the TEMPLATE's assertion where the template needs a
        non-default, and the engine's own default by omission where it does not.

        Nor may an ARGUMENT re-name a keyword the dictionary already carries: a
        composite takes what the dictionary LACKS - a file, a placement, a
        gradation, a series - and every keyword beside it is stated on the body
        under its own name, so a reader of the deck sees the value and a caller
        overrides it by the name the engine itself publishes. What survives as an
        argument is a COERCION the keyword's ingestion owes its reader: a common
        alternate unit the question is asked in - microns for a diameter,
        milligrams per litre for a source load, a compass bearing for a wind -
        converted at the line that states the engine's own.
        */
    }

    requirement def AStatedValueIsANumberOrASeries {
        doc /*
        A value the engine takes BOTH as a constant keyword and as a file of
        instants is stated as whichever the record measured. The transform says
        which keyword has the twin - PRESCRIBED FLOWRATES and PRESCRIBED
        ELEVATIONS to the LIQUID BOUNDARIES FILE, the source rates to the
        SOURCES FILE, the weather to the ASCII ATMOSPHERIC DATA FILE - and a
        number is what a run states where nothing measured a window.

        The Series is the RUNTIME's: instants on the run's own clock in one
        unit, frozen, arriving through a slot's own ingestion off a fetched
        record and never through a fetch of its own. It rides the unit
        conversion and the datum shift the reading rode, is read on the clock
        the run opens at rather than the record's, and is thinned to the
        engine's own step only where the record is finer than one.

        The twin is written per BOUNDARY, not per run: the engine looks for one
        column named from each boundary's own number and reads the steering
        list for every column the file does not carry, so one deck states a
        measured series where it has one and a constant where it does not.
        */
    }

    requirement def BoundsRideBesideTheDictionaryRow {
        doc /*
        A keyword's PLAUSIBILITY BOUNDS live beside its dictionary row, in a
        sidecar the module reads at import and the slot carries: the dictionary
        itself is the engine image's and is compared to it byte for byte, so
        nothing of ours may be written into it.

        The bounds are where a deleted Param's own range went - one range for
        the value the keyword holds, or one per element in the keyword's own
        order - and a value outside them REFUSES at fill by the KEYWORD's name,
        because that is the name the user set it under. It is the one table a
        calibration reads its priors and its ranges from, and a keyword with no
        row in it is taken at whatever the dictionary's own check allows.
        */
    }

    requirement def ACapabilityOnAHostDeckIsAComposite {
        doc /*
        A module the engine gives its own dictionary, its own deck and its own
        results is a WRAPPER. A capability it expresses as KEYWORDS ON A HOST'S
        DECK is a COMPOSITE on that host's wrapper and never a wrapper of its
        own: there is no dictionary for a second surface to be the keyword table
        of, and one written by hand would be the transcription this seam exists
        to stop.

        WHICH host, where more than one deck spells the same keywords, is the
        engine's to decide as well. The dredge's keywords stand on the
        hydrodynamic deck and on the sediment deck, and only the sediment path
        puts the material it moves through the per-class mass evolution the bed
        evolution and the sediment balance are computed from - the other adds a
        thickness to the bottom with no stock, no grain classes and no
        accounting behind it. The composite lives on the deck whose own closure
        carries the result, and the other path is not exposed.
        */
    }

    requirement def NoBodyExtendsABody {
        doc /*
        A body extends its module's wrapper and nothing else. A value two
        templates share is restated in each, under the template that states
        it, so a keyword that means something else in a new setting is seen
        rather than inherited into silence.
        */
    }

    requirement def ProvenanceNamesTheLayerThatAnswered {
        doc /*
        A filled slot says which of the six layers answered it. Engine default
        is ABSENCE - the slot is not on the sheet at all and the card badges it
        under the advanced fold. The other five are on the row: a template
        names itself, a fill names the user, a
        composite names the producer that expanded it, and a value the RUN
        MEASURED reads DERIVED.

        Derived is not the same statement as template. A body states WHICH
        measurement a slot takes - the boundary walk the accepted mesh reported,
        the normal depth of the reach, the time step the mesh's own CFL allows,
        the stage-discharge curve fitted to the outlet - and the number itself
        is the artifact's. Badging it template would say an author wrote a value
        nobody wrote down. A declared PARAM is the invocation's own answer and
        stays the template's.
        */
    }

    requirement def BodiesAreStatic {
        doc /*
        A body reads no value a fill produced. Its assertions are data, fixed
        when the module is imported - a literal the dictionary checked, or a
        description of a read the fill substitutes later - so two fills of the
        same body start from the same statement and a body cannot branch on what
        a run resolved.
        */
    }

    requirement def OneTemplatePerQuestion {
        doc /*
        A structural fork of the deck is a TEMPLATE, never a switch on a param.
        A tracer, an oil slick, a moving bed and a settling class fill DIFFERENT
        slots, and the arity of the carrier's own tracer surface moves with the
        fork - so each body states that surface itself rather than letting a
        composite own a carrier slot out of sight. What varies WITHIN one
        question is a composite that states nothing when it is given nothing.

        The rule holds on the READING side too. Each template LISTS the reads
        its question PLACES, off the module's own READS registry and, for a
        coupled module's own result, through that module's wrapper; a single
        publisher branching on a substance-class string put the fork back in the
        products, one level down from where a person reads it.
        */
    }

    requirement def AStepsKwargsAreItsRunnersCall {
        doc /*
        A plan is built once at import and its steps are not called until a run
        reaches them, so a step whose kwargs the runner cannot take is a
        declaration that imports, registers and validates - and dies at the
        stage it was written for, on a live run, after the mesh was built. The
        kwargs a step declares ARE the call: every parameter the runner requires
        is among them, and nothing it does not take is.
        */
    }

    requirement def ReviewIsTheWorkflowsView {
        doc /*
        The sheet is STATE. The review is how the workflow renders what fill
        returned - the set slots with their provenance and the mandatory ones
        still open - and it HOLDS there until the user runs; an edit is another
        fill. No gate concept survives in the plan: a card in front of the fill
        would revise a sheet the fill never reads.

        What is SET and what is OPEN AND MANDATORY are the review; the whole
        rest of the module folds under advanced, grouped by the dictionary's
        own rubrique and carrying the engine default it would otherwise run on.
        The same declaration is what the tool's own docstring names - the
        module, the rubriques the body touches, the mandatory slots it leaves
        open - so the prose and the card cannot claim a surface the body does
        not state.
        */
    }

    requirement def RunIsHeld {
        doc /*
        Nothing executes until the user runs. Fill produces a sheet and no work
        beyond the producers the canvas shows; run is the separate, explicit act
        - and it refuses a sheet whose REQUIRED slots are unanswered BY NAME
        first, because a run started without the files the dictionary marks
        OBLIG fails inside Fortran minutes later, blaming a keyword rather than
        the gap.

        It refuses the same way on the CADENCE: a module that marches in time
        names the keyword saying how often its output table is written, and the
        dictionary's own default for that keyword is every step. A deck stating
        none would inherit an animation nobody sized, so each states its own
        period in steps as its opinion, and the user overrides it by the
        keyword's name.
        */
    }

    requirement def ModuleIsTheUnit {
        doc /*
        The engine's module is the unit. A template is values over one module
        and composes nothing, so nothing is frozen: a body that states one
        tracer is two on the fill. A wrapper's reads are primitives named from
        the module's own output table and never from a question. A run is recorded
        under its engine and its module, with no template field; every filled
        slot carries one of the six origins the card renders. The adapters
        build a turn from the contracts IR and import no provider SDK for it.

        ONE wrapper serves every role its module has: a template's own steering
        base where the engine gives the module a deck of its own, and a coupled
        body where a host names it, off the same dictionary and the same output
        table. What a coupled module hands its host in memory rather than on the
        host's result is felt only under a host switch, so stating the coupling
        ARMS that switch the way a stated rate arms the term that reads it, and
        a host deck that states the switch itself keeps what it said. The user
        code each deck of a run names is staged off that deck, never off the
        host's alone.

        A MODULE STATES ITS OWN SPELLINGS. The keyword a deck names its result
        in, and the keywords its run LENGTH is spelled in, are the module's
        statements exactly as its output table is: one module names the window
        itself, another names a step and a count of them whose product it is,
        and a module that does not march in time names none. Every stage that
        needs either - the settle the water opens under, the window a matched
        series source is filtered on, the file the primitives read - asks the
        module, so no reader carries one module's word for all of them.
        */
    }

    requirement def OutputsAreTheModules {
        doc /*
        What a run writes, how each variable is drawn and what it is called are
        the MODULE's, stated once in its output table and never by the template
        that asked the question. The printouts keyword is generated from that
        table as the deck is serialized - for the host and for every coupled
        module - with each token checked against the keyword's own choices; a
        template restating it, or naming a style of its own, is refused.

        EVERY variable a result file WROTE is published as ONE layer: the
        TEMPORAL layer where the row that names it varies in time, the final
        frame where it does not. The RESULT FILE is the list and the table is
        what draws it - a variable no row names is published under the spelling
        the file itself carries, in the unit the record itself carries, with a
        journal line saying so - because a real result omitted is as wrong as a
        slot the engine never wrote published. A still beside a time series
        would be a copy of a frame that layer already carries, so a picture of
        one instant is a render of the temporal layer rather than a second
        product. That layer's legend is ranged over EVERY frame of the record,
        never over the last frame alone - a variable that peaks and flushes
        would otherwise be ranged on an empty field and paint blank while the
        run's own data reached the top of its scale. There is no leading layer
        and no ranking among the rows, because every group rides the one mesh
        the run solved on and the camera takes its extent; the order is the
        dictionary's, then what the files carry past it. A row the result does
        not carry is skipped rather than refused, and a row a module prints in
        its listing or derives over its own variables is read or published but
        never asked of the engine.

        A row may exist only UNDER a keyword: the engine allocates the variable
        with that keyword true and stops the solve on a request for one it did
        not allocate, so the generated table carries the row where the deck
        states its keyword and nowhere else. Nothing arms it - a deck that says
        nothing about the keyword is a deck the row is not written for.

        The SPELLING of the generated value is the engine's own. A table longer
        than the line the engine's parser reads is truncated by the engine, so
        past that column it is written in the wildcard the engine's own variable
        matching answers to, over prefixes that reach exactly the rows the table
        carries and no other variable the keyword spells; a table no wildcard
        can shorten enough refuses by name.

        A module may write a result NO primitive reads: one over a grid that is
        not the domain, such as a spectrum over frequency and direction. It is
        not a row and nothing paints it, and it is not thrown away either - a
        deck that names one has the run declare it, check it landed and keep it
        for download, which is the whole of what the module states about it
        until a question asks for a read over its own grid.

        What a template keeps is the QUESTION: the keywords, the reads that need
        a place the user gives, the answer, and the captions for those.
        */
    }

    requirement def InputsAreTypedPrimitives {
        doc /*
        Every point in the system is a Point. A slot that asks for one fills
        from each of its sources - the pick, a pair, a point layer - under the
        same ingestion, keeps the name the source carried, refuses by name a
        point outside the domain, and moves a dry landing to the nearest wet
        node. Neither the user nor the model names it twice.

        An ingestion RESOLVES what it was handed and never fetches: a bare place
        NAME is refused, retryably, naming the geocoder the model calls first,
        so no network call hides under a slot nobody asked to wait on.
        */
    }

    requirement def TheSteeringFormatHasOneWriter {
        doc /*
        telapy writes the steering file and the serializer is the only thing
        that hands it a sheet. A keyword spelled and assigned in a string
        anywhere else is a second author of the format, and a second author is
        what the surface replaced: the hand-written one wrote the deck line by
        line and its comments were the only description a keyword had.

        The gate is over the dictionary's own names, so a new writer is caught
        by what it spells rather than by a list of files somebody keeps.
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement BoundsRideBesideTheDictionaryRow by dictionaryExtractor;
    satisfy requirement BoundsRideBesideTheDictionaryRow by slot;
    verify requirement BoundsRideBesideTheDictionaryRow by
        "tests/telemac/test_telemac_do_sag.py::test_a_keyword_outside_the_modules_own_bounds_refuses_by_its_name";

    satisfy requirement KeywordNamesAreRaw by dictionaryExtractor;
    satisfy requirement KeywordNamesAreRaw by slot;
    verify requirement KeywordNamesAreRaw by
        "tests/telemac/test_telemac_module_surface.py::test_every_slot_carries_the_dictionary_s_own_name_and_help";
    verify requirement KeywordNamesAreRaw by
        "tests/telemac/test_telemac_module_surface.py::test_the_identifier_is_the_keyword_and_nothing_invented";
    verify requirement KeywordNamesAreRaw by
        "tests/telemac/test_telemac_module_surface.py::test_a_keyword_the_module_does_not_have_refuses_naming_the_nearest";

    satisfy requirement EverySlotDescribed by dictionaryExtractor;
    satisfy requirement EverySlotDescribed by slot;
    verify requirement EverySlotDescribed by
        "tests/telemac/test_telemac_module_surface.py::test_every_slot_carries_the_dictionary_s_own_name_and_help";
    verify requirement EverySlotDescribed by
        "tests/scripts/test_telemac_dictionary_drift.py::test_the_help_carries_no_markup_into_the_surface";

    satisfy requirement EngineDefaultSurfaced by slot;
    satisfy requirement EngineDefaultSurfaced by sheet;
    satisfy requirement EngineDefaultSurfaced by engineWorkflow;
    verify requirement EngineDefaultSurfaced by
        "tests/telemac/test_telemac_module_surface.py::test_an_open_slot_under_the_fold_says_the_dictionary_answers_it_for_nobody";
    verify requirement EngineDefaultSurfaced by
        "tests/telemac/test_telemac_module_surface.py::test_the_engine_default_is_on_the_slot_or_the_slot_is_a_question";
    verify requirement EngineDefaultSurfaced by
        "tests/telemac/test_telemac_module_surface.py::test_an_engine_default_is_never_written_into_the_deck";
    verify requirement EngineDefaultSurfaced by
        "tests/telemac/test_telemac_module_surface.py::test_the_bare_sheet_opens_every_keyword_the_dictionary_answers_for_nobody";

    satisfy requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by engineWorkflow;
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_telemac_module_surface.py::test_every_required_file_is_the_template_s_own_statement";
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_telemac_module_surface.py::test_the_artemis_forcing_composite_carries_the_file_and_not_its_name";
    satisfy requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by slot;
    satisfy requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by sheet;
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_telemac_module_surface.py::test_the_bare_sheet_opens_every_keyword_the_dictionary_answers_for_nobody";
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_telemac_module_surface.py::test_a_defaulted_oblig_file_is_not_a_question_the_sheet_asks";
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_telemac_module_surface.py::test_the_open_row_says_whether_the_run_cannot_begin_without_it";
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_telemac_module_surface.py::test_run_does_not_refuse_an_open_keyword_the_engine_may_yet_default";
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_telemac_run_reads.py::test_the_engine_s_own_demand_is_read_by_name_out_of_the_listing";
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_telemac_engine.py::test_classify_exit_names_the_keyword_lecdon_asked_for";
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_telemac_engine.py::test_classify_exit_invents_no_demand_where_the_engine_made_none";

    satisfy requirement ANoDefaultKeywordIsTheTemplatesToState by slot;
    satisfy requirement ANoDefaultKeywordIsTheTemplatesToState by sheet;
    verify requirement ANoDefaultKeywordIsTheTemplatesToState by
        "tests/telemac/test_telemac_module_surface.py::test_the_basin_states_how_its_tracer_is_carried_and_under_what_ceiling";
    verify requirement ANoDefaultKeywordIsTheTemplatesToState by
        "tests/telemac/test_telemac_module_surface.py::test_the_engine_default_is_on_the_slot_or_the_slot_is_a_question";

    satisfy requirement WrapperHasNoOpinion by wrapper;
    verify requirement WrapperHasNoOpinion by
        "tests/telemac/test_telemac_module_surface.py::test_a_wrapper_asserts_nothing_and_has_no_hook_to";
    verify requirement WrapperHasNoOpinion by
        "tests/telemac/test_telemac_module_surface.py::test_a_wrapper_is_a_declaration_and_refuses_to_be_a_value";
    verify requirement WrapperHasNoOpinion by
        "tests/telemac/test_telemac_module_surface.py::test_a_coupled_body_states_only_what_its_caller_handed_it";

    satisfy requirement EverythingOverridable by runRecord;
    satisfy requirement EverythingOverridable by wrapper;
    satisfy requirement EverythingOverridable by sheet;
    satisfy requirement EverythingOverridable by canvasGate;
    satisfy requirement EverythingOverridable by engineWorkflow;
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_raw_keyword_on_the_wire_fills_the_slot_it_names";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_raw_keyword_the_module_does_not_have_refuses_naming_the_nearest";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_raw_keyword_value_outside_the_choices_refuses_naming_the_choices";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_two_sources_on_the_floor_are_two_sources_in_the_deck";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_every_template_wire_carries_the_raw_keyword_floor";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_floor_that_is_not_a_mapping_refuses_by_name";
    verify requirement EverythingOverridable by
        "tests/runtime/test_run_journal.py::test_the_record_carries_the_raw_keyword_floor_the_run_was_pinned_by";
    verify requirement EverythingOverridable by
        "tests/runtime/test_declarative_library.py::test_the_raw_keyword_floor_rides_out_on_the_runs_own_result";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_resolution_order_is_engine_then_template_then_fill";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_fill_is_repeatable_and_the_later_one_stands";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_drawn_point_is_a_fill";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_canvas_that_answers_nothing_refuses_and_invents_nothing";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_do_sag.py::test_a_coupled_keyword_is_stated_by_its_own_body_name";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_do_sag.py::test_a_bare_coupled_keyword_only_one_body_spells_resolves_there";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_do_sag.py::test_a_name_two_bodies_spell_refuses_naming_both_qualified_spellings";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_do_sag.py::test_a_value_the_coupled_dictionary_refuses_refuses_at_the_fill";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_do_sag.py::test_the_card_groups_the_coupled_deck_under_its_own_body";
    verify requirement EverythingOverridable by
        "tests/telemac/test_workflow_owns_its_stages.py::test_the_floor_is_resolved_once_before_any_stage_runs";
    verify requirement EverythingOverridable by
        "tests/telemac/test_workflow_owns_its_stages.py::test_the_settle_step_reads_the_files_the_deck_itself_names";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_water_temperature.py::test_a_stated_duration_sizes_the_weather_record";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_rain_on_grid_template.py::test_a_stated_friction_law_derives_the_rating_curve";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_read_placed_on_a_keyword_resolves_through_the_filled_sheet";
    verify requirement EverythingOverridable by
        "tests/telemac/test_agitation_template.py::test_the_transect_is_measured_off_the_structure_the_question_asks_about";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_rate_stated_by_name_arms_the_term_that_reads_it";
    verify requirement EverythingOverridable by
        "tests/runtime/test_run_journal.py::test_the_record_carries_the_slots_the_run_was_supplied";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_khione.py::test_the_qualified_floor_reaches_the_coupled_body";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_khione.py::test_a_keyword_two_bodies_spell_names_the_body_it_belongs_to";

    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_tomawac.py::test_the_qualified_floor_reaches_the_coupled_body";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_tomawac.py::test_a_keyword_both_bodies_spell_names_the_body_it_belongs_to";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_tomawac.py::test_a_deck_that_states_the_switch_off_keeps_it_off";
    satisfy requirement DictionaryMatchesImage by dictionaryExtractor;
    verify requirement DictionaryMatchesImage by
        "tests/scripts/test_telemac_dictionary_drift.py::test_the_committed_dictionary_is_what_the_image_says_today";
    verify requirement DictionaryMatchesImage by
        "tests/scripts/test_telemac_dictionary_drift.py::test_every_exposed_module_has_a_committed_dictionary";

    satisfy requirement CompositesLiveInWrappers by composite;
    satisfy requirement CompositesLiveInWrappers by wrapper;
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_module_surface.py::test_a_composite_becomes_several_slots_and_the_file_they_name";
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_module_surface.py::test_a_composite_lives_on_the_wrapper_and_may_not_shadow_a_keyword";
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_module_surface.py::test_a_composite_the_wrapper_never_registered_refuses_by_name";
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_module_surface.py::test_the_sources_file_is_written_from_the_keywords_the_deck_states_by_name";
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_module_surface.py::test_a_coupling_states_only_what_the_carrier_names_it_by";
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_module_surface.py::test_a_coupled_body_is_checked_against_its_own_module_s_dictionary";
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_khione.py::test_a_three_dimensional_host_couples_khione_and_states_its_column";
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_ice_cover.py::test_the_ice_deck_states_the_five_keywords_this_question_has_an_opinion_on";

    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_tomawac.py::test_both_hosts_couple_the_waves_through_the_seam_they_share";
    satisfy requirement OneTemplatePerQuestion by engineWorkflow;
    verify requirement OneTemplatePerQuestion by
        "tests/telemac/test_telemac_module_surface.py::test_a_structural_fork_is_a_template_and_never_a_switch";
    verify requirement OneTemplatePerQuestion by
        "tests/telemac/test_telemac_module_surface.py::test_no_flipped_body_branches_on_anything";
    verify requirement OneTemplatePerQuestion by
        "tests/search/test_door_dissolution.py::test_all_templates_registered_and_callable";
    verify requirement OneTemplatePerQuestion by
        "tests/telemac/test_telemac_module_surface.py::test_a_template_reads_its_answer_through_the_primitives_its_module_binds";
    verify requirement OneTemplatePerQuestion by
        "tests/telemac/test_telemac_outputs.py::test_a_primitive_names_the_coupled_module_whose_result_it_reads";
    verify requirement OneTemplatePerQuestion by
        "tests/telemac/test_telemac_outputs.py::test_a_series_at_a_point_is_measured_at_that_point";

    satisfy requirement AStepsKwargsAreItsRunnersCall by engineWorkflow;
    satisfy requirement AStepsKwargsAreItsRunnersCall by stager;
    verify requirement AStepsKwargsAreItsRunnersCall by
        "tests/runtime/test_step_signatures.py::test_every_step_binds_against_its_own_runners_signature";

    satisfy requirement ReviewIsTheWorkflowsView by engineWorkflow;
    verify requirement ReviewIsTheWorkflowsView by
        "tests/telemac/test_telemac_module_surface.py::test_the_card_shows_what_is_set_and_open_and_folds_the_rest_by_rubrique";
    verify requirement ReviewIsTheWorkflowsView by
        "tests/telemac/test_telemac_module_surface.py::test_the_fill_docstring_names_the_module_its_rubriques_and_its_open_slots";
    verify requirement ReviewIsTheWorkflowsView by
        "tests/telemac/test_telemac_module_surface.py::test_the_review_is_the_workflows_view_and_never_a_step_of_its_own";
    verify requirement ReviewIsTheWorkflowsView by
        "tests/runtime/test_declarative_library.py::test_do_sag_declares_no_gate_in_front_of_its_self_gating_review";

    satisfy requirement CompositesSetOnlyPresence by composite;
    satisfy requirement CompositesSetOnlyPresence by wrapper;
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_module_surface.py::test_a_composite_sets_only_what_its_value_s_presence_defines";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_module_surface.py::test_a_wind_from_the_north_reaches_the_engines_own_speed_and_direction_pair";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_module_surface.py::test_a_continuation_names_the_file_and_nothing_else";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_module_surface.py::test_a_curve_number_field_names_no_model_and_the_template_does";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_atmosphere.py::test_a_series_left_out_writes_no_column_and_shifts_none_of_the_others";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_atmosphere.py::test_a_host_handed_no_weather_states_no_file";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_atmosphere.py::test_the_thermal_body_states_only_the_keywords_it_was_given";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_micropollutant.py::test_a_constant_nobody_states_is_left_to_the_engine";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_eutrophication.py::test_a_body_states_only_what_it_was_handed";

    satisfy requirement AStatedValueIsANumberOrASeries by composite;
    satisfy requirement AStatedValueIsANumberOrASeries by temporalValue;
    satisfy requirement AStatedValueIsANumberOrASeries by observationSlot;
    verify requirement AStatedValueIsANumberOrASeries by
        "tests/runtime/test_series.py::test_a_series_prints_as_its_shape_rather_than_its_points";
    satisfy requirement AStatedValueIsANumberOrASeries by alignment;
    verify requirement AStatedValueIsANumberOrASeries by
        "tests/inputs/test_series_align.py::test_a_rate_keeps_its_total_across_a_coarser_clock";
    verify requirement AStatedValueIsANumberOrASeries by
        "tests/inputs/test_series_align.py::test_a_clock_no_coarser_than_the_record_leaves_it_as_measured";
    verify requirement AStatedValueIsANumberOrASeries by
        "tests/inputs/test_series_align.py::test_a_hole_wider_than_the_bound_refuses_rather_than_being_bridged";
    verify requirement AStatedValueIsANumberOrASeries by
        "tests/inputs/test_observation.py::test_the_whole_window_is_kept_beside_the_reading";
    verify requirement AStatedValueIsANumberOrASeries by
        "tests/inputs/test_observation.py::test_a_record_that_reported_one_moment_carries_no_window";
    verify requirement AStatedValueIsANumberOrASeries by
        "tests/telemac/test_telemac_liquid_boundaries.py::test_a_measured_window_writes_its_column_and_the_list_still_stands";
    verify requirement AStatedValueIsANumberOrASeries by
        "tests/telemac/test_telemac_liquid_boundaries.py::test_a_run_nothing_measured_a_window_at_writes_no_file";
    verify requirement AStatedValueIsANumberOrASeries by
        "tests/telemac/test_telemac_liquid_boundaries.py::test_the_file_opens_where_a_continued_run_s_clock_opens";

    satisfy requirement ACapabilityOnAHostDeckIsAComposite by composite;
    satisfy requirement ACapabilityOnAHostDeckIsAComposite by wrapper;
    verify requirement ACapabilityOnAHostDeckIsAComposite by
        "tests/telemac/test_telemac_nestor_dredge.py::test_the_dredge_arms_nestor_and_names_the_files_it_wrote";
    verify requirement ACapabilityOnAHostDeckIsAComposite by
        "tests/telemac/test_telemac_nestor_dredge.py::test_the_carrier_deck_has_no_dredge_of_its_own";
    verify requirement ACapabilityOnAHostDeckIsAComposite by
        "tests/telemac/test_telemac_nestor_dredge.py::test_a_dredge_over_a_bed_with_no_stock_refuses_by_name";
    verify requirement ACapabilityOnAHostDeckIsAComposite by
        "tests/telemac/test_telemac_atmosphere.py::test_both_hosts_name_the_same_file_and_state_no_unit_of_their_own";
    verify requirement ACapabilityOnAHostDeckIsAComposite by
        "tests/telemac/test_telemac_atmosphere.py::test_each_host_writes_the_columns_its_own_source_term_reads";
    verify requirement ACapabilityOnAHostDeckIsAComposite by
        "tests/telemac/test_telemac_atmosphere.py::test_a_run_whose_own_terms_read_the_weather_writes_those_columns_alone";
    verify requirement ACapabilityOnAHostDeckIsAComposite by
        "tests/telemac/test_telemac_ice_cover.py::test_the_weather_file_carries_what_the_ice_reads_and_no_solar_column";
    verify requirement ACapabilityOnAHostDeckIsAComposite by
        "tests/telemac/test_telemac_micropollutant.py::test_the_body_names_the_process_and_the_carrier_states_it";
    verify requirement ACapabilityOnAHostDeckIsAComposite by
        "tests/telemac/test_telemac_eutrophication.py::test_the_carrier_states_the_process_and_the_steering_file";

    satisfy requirement NoBodyExtendsABody by wrapper;
    verify requirement NoBodyExtendsABody by
        "tests/telemac/test_telemac_module_surface.py::test_a_body_extends_the_wrapper_and_never_a_body";

    satisfy requirement ATemplateNumberIsMetToTheMeshsPrecision by engineWorkflow;
    satisfy requirement ATemplateNumberIsMetToTheMeshsPrecision by sheet;
    verify requirement ATemplateNumberIsMetToTheMeshsPrecision by
        "tests/telemac/test_open_water_domains.py::test_the_footprint_and_the_solid_faces_are_cut_at_the_same_width";
    verify requirement ATemplateNumberIsMetToTheMeshsPrecision by
        "tests/telemac/test_open_water_domains.py::test_a_boundary_node_is_on_the_structure_when_it_stands_on_the_punched_outline";
    verify requirement ATemplateNumberIsMetToTheMeshsPrecision by
        "tests/telemac/test_open_water_domains.py::test_a_lone_node_between_two_of_another_kind_is_not_a_face";

    satisfy requirement ProvenanceNamesTheLayerThatAnswered by sheet;
    satisfy requirement ProvenanceNamesTheLayerThatAnswered by engineWorkflow;
    verify requirement ProvenanceNamesTheLayerThatAnswered by
        "tests/telemac/test_telemac_module_surface.py::test_a_value_the_run_measured_reads_as_derived_not_as_the_body_that_named_it";
    verify requirement ProvenanceNamesTheLayerThatAnswered by
        "tests/telemac/test_telemac_module_surface.py::test_a_measured_value_a_fill_overrides_still_reads_as_the_users";

    satisfy requirement BodiesAreStatic by sheet;
    verify requirement BodiesAreStatic by
        "tests/telemac/test_telemac_module_surface.py::test_a_body_is_static_and_no_fill_changes_what_it_asserts";
    verify requirement BodiesAreStatic by
        "tests/telemac/test_telemac_module_surface.py::test_a_param_a_body_reads_binds_from_the_sheet_the_invocation_resolved";

    satisfy requirement RunIsHeld by sheet;
    satisfy requirement RunIsHeld by serializer;
    satisfy requirement RunIsHeld by stager;
    verify requirement RunIsHeld by
        "tests/telemac/test_telemac_module_surface.py::test_run_refuses_an_incomplete_sheet_naming_the_required_file";
    verify requirement RunIsHeld by
        "tests/telemac/test_telemac_module_surface.py::test_run_serializes_then_stages_then_dispatches";
    verify requirement RunIsHeld by
        "tests/telemac/test_telemac_module_surface.py::test_run_refuses_a_deck_that_states_no_cadence";
    verify requirement RunIsHeld by
        "tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement TheSteeringFormatHasOneWriter by serializer;
    satisfy requirement TheSteeringFormatHasOneWriter by steeringDriver;
    verify requirement TheSteeringFormatHasOneWriter by
        "tests/telemac/test_telemac_module_surface.py::test_the_serializer_is_the_only_module_that_writes_a_keyword_into_a_deck";

    satisfy requirement ModuleIsTheUnit by wrapper;
    satisfy requirement ModuleIsTheUnit by output;
    satisfy requirement ModuleIsTheUnit by sheet;
    satisfy requirement ModuleIsTheUnit by engineWorkflow;
    satisfy requirement ModuleIsTheUnit by runRecord;
    satisfy requirement ModuleIsTheUnit by messageIR;
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_run_identity.py::test_every_template_is_values_over_a_module_of_the_one_engine";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_run_identity.py::test_the_run_record_carries_the_engine_and_the_module_and_no_template";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_run_identity.py::test_the_vocabulary_is_the_six_the_card_renders";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_run_identity.py::test_every_slot_of_a_template_s_default_fill_carries_one_of_the_six";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_module_surface.py::test_a_fill_with_two_tracers_is_a_two_tracer_deck";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_module_surface.py::test_every_wrapper_binds_the_primitive_set_and_nothing_question_named";
    verify requirement ModuleIsTheUnit by
        "tests/adapters/test_message_ir.py::test_the_conversation_is_built_from_the_ir_alone";
    verify requirement ModuleIsTheUnit by
        "tests/adapters/test_message_ir.py::test_no_provider_sdk_is_imported_for_the_ir";

    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_tomawac.py::test_a_standalone_deck_stands_on_the_wrapper_and_names_its_own_files";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_tomawac.py::test_the_wrapper_writes_its_own_wave_field_beside_the_host_s";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_tomawac.py::test_the_user_fortran_of_every_deck_of_the_run_is_staged";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_khione.py::test_the_table_is_written_in_the_engine_s_own_wildcard_and_fits_its_line";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_khione.py::test_a_row_under_a_keyword_is_written_unless_the_deck_switches_it_off";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_khione.py::test_one_frazil_row_per_class_the_deck_counts_on_the_host_and_the_ice_file";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_khione.py::test_the_ice_file_rows_nothing_the_budget_did_not_allocate";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_khione.py::test_the_temperature_is_appended_to_every_host_and_the_rest_ride_a_switch";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_khione.py::test_every_row_the_table_carries_is_a_mnemonic_the_dictionary_spells";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_khione.py::test_the_wrapper_writes_its_own_result_beside_the_host_s";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_ice_cover.py::test_every_answer_is_read_off_the_ice_modules_own_result";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_ice_cover.py::test_the_module_declares_the_tracers_and_the_deck_declares_none";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_tomawac.py::test_the_table_is_the_engine_s_forty_less_the_slot_it_never_writes";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_tomawac.py::test_the_table_is_written_in_the_engine_s_own_wildcard_and_fits_its_line";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_tomawac.py::test_a_deep_deck_does_not_ask_for_the_bottom_velocity_the_engine_leaves";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_tomawac.py::test_the_spectra_are_kept_as_result_files_and_never_rowed_as_a_mesh";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_tomawac.py::test_the_module_appends_no_tracer_and_arms_the_switch_it_is_felt_through";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_spectrum_read.py::test_the_polar_grid_decodes_into_frequencies_and_directions";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_spectrum_read.py::test_the_measures_are_the_energy_the_stated_spectrum_carries";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_spectrum_read.py::test_the_point_asked_about_is_answered_by_the_nearest_printout_point";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_spectrum_read.py::test_no_module_whose_result_is_a_mesh_reads_a_spectrum";
    satisfy requirement OutputsAreTheModules by wrapper;
    satisfy requirement OutputsAreTheModules by output;
    satisfy requirement OutputsAreTheModules by sheet;
    satisfy requirement OutputsAreTheModules by serializer;
    satisfy requirement OutputsAreTheModules by engineWorkflow;
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_no_template_states_a_style_or_a_printout_list";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_the_printouts_keyword_is_the_table_and_the_run_s_own_tracers";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_a_token_the_keyword_does_not_spell_refuses_before_the_engine_reads_it";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_a_varying_row_is_one_temporal_layer_and_a_still_row_its_frame";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_a_temporal_layer_is_ranged_over_the_record_not_its_last_frame";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_a_row_the_result_does_not_carry_is_skipped_and_a_placed_read_refuses";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_every_variable_the_result_wrote_is_published_even_where_no_row_names_it";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_a_coupled_module_states_the_tracers_it_appends_and_its_own_table";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_atmosphere.py::test_the_thermal_process_appends_its_temperature_to_a_carrier_with_no_tracer";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_atmosphere.py::test_a_temperature_the_carrier_declares_is_adopted_rather_than_appended";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_atmosphere.py::test_the_thermal_rows_are_declared_data_on_the_wrapper";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_micropollutant.py::test_the_rows_are_declared_data_on_the_wrapper";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_micropollutant.py::test_the_dissolved_row_a_carrier_declares_is_ADOPTED_never_appended";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_eutrophication.py::test_eutro_appends_the_eight_tracers_in_the_engines_order";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_eutrophication.py::test_biomass_appends_the_first_five";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_the_card_carries_every_variable_each_deck_writes";
    verify requirement OutputsAreTheModules by
        "tests/telemac/test_telemac_module_outputs.py::test_the_published_order_is_the_table_order_across_host_and_coupled";

    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_tomawac.py::test_the_module_states_the_keyword_its_own_result_is_named_in";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_telemac_tomawac.py::test_the_module_states_how_its_own_run_length_is_spelled";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_nearshore_waves_template.py::test_the_run_length_is_the_product_the_module_spells_it_as";
    verify requirement ModuleIsTheUnit by
        "tests/telemac/test_nearshore_waves_template.py::test_the_run_is_read_off_the_file_the_module_spells_its_results_in";

    satisfy requirement InputsAreTypedPrimitives by point;
    satisfy requirement InputsAreTypedPrimitives by canvasGate;
    satisfy requirement InputsAreTypedPrimitives by sheet;
    verify requirement InputsAreTypedPrimitives by
        "tests/inputs/test_point.py::test_a_pick_carries_its_coordinates_and_its_name";
    verify requirement InputsAreTypedPrimitives by
        "tests/inputs/test_point.py::test_a_pair_is_lon_lat_with_no_name";
    verify requirement InputsAreTypedPrimitives by
        "tests/inputs/test_point.py::test_a_selected_point_layer_gives_its_first_point_and_its_name";
    verify requirement InputsAreTypedPrimitives by
        "tests/inputs/test_point.py::test_a_place_name_refuses_and_names_the_geocoder";
    verify requirement InputsAreTypedPrimitives by
        "tests/inputs/test_point.py::test_a_point_outside_the_domain_is_refused_by_name_and_names_the_fix";
    verify requirement InputsAreTypedPrimitives by
        "tests/inputs/test_point.py::test_a_point_landing_on_a_dry_node_moves_to_the_nearest_wet_one";
    verify requirement InputsAreTypedPrimitives by
        "tests/telemac/test_telemac_module_surface.py::test_a_drawn_point_is_a_fill";
}
