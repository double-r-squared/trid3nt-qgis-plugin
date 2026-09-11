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
    template: values over one module - a body of keywords, DATA rows, an outputs
    list, captions - and nothing below it knows the question exists. A wrapper's
    outputs are primitives named from the module's variable vocabulary, the
    run record carries the engine and the module, every filled slot carries one
    of six origins, and the adapters build their turns from an IR of this
    repo's own. A point anywhere in the system is one typed Point.
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
        */
    }

    part def Module {
        doc /*
        The wrapper class the dictionary makes. It holds the dictionary, the
        composites and the outputs, and it asserts no value: there is
        deliberately no hook here for a default.
        */
    }

    part def Composite {
        doc /*
        One value standing for several slots and the file they name - a release
        becoming the sources keywords plus the series file, wind becoming the
        five wind keywords. It lives on the wrapper because the keyword group is
        the module's, not a template's private code.
        */
    }

    part def Output {
        doc /*
        A module's output bound to the reader that publishes it. A binding, not
        a new reader.
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

    part def Door {
        doc /*
        What a template hands its declarations to instead of a plan: it names
        the world the sheet is filled FROM, the STEERING body itself, and the
        wrapper output that publishes the solved file. It decides nothing - the
        sequence it builds is the same for every question.
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
        sheet with its provenance, the answer. The template is not a field of
        the line; it survives as the provenance of the slots it filled.
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
        item DICTIONARY : Map;
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
        code: trid3nt_server/workflows/mesh/meshers/drivers/telemac_dico_driver.py
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
        coupled modules carry their own beside them (waqtel.py, gaia.py).
        code: trid3nt_server/workflows/telemac/modules/telemac2d.py
        */
        port surfaceOut : SurfaceOut;
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
        code: trid3nt_server/workflows/mesh/meshers/drivers/telemac_cas_driver.py
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

    part door : Door {
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
        code: trid3nt_server/workflows/inputs/point.py
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

        The rule holds on the READING side too. Each template LISTS the
        primitives its question reads, off the module's own OUTPUTS bindings
        and, for a coupled module's own result, through that module's wrapper;
        a single publisher branching on a substance-class string put the fork
        back in the products, one level down from where a person reads it.
        */
    }

    requirement def ReviewIsTheDoorsView {
        doc /*
        The sheet is STATE. The review is how the door renders what fill
        returned - the set slots with their provenance and the mandatory ones
        still open - and it HOLDS there until the user runs; an edit is another
        fill. No gate concept survives in the plan: a card in front of the door
        would revise a sheet the door never reads.

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
        */
    }

    requirement def ModuleIsTheUnit {
        doc /*
        The engine's module is the unit. A template is values over one module
        and composes nothing, so nothing is frozen: a body that states one
        tracer is two on the fill. A wrapper's outputs are primitives named from
        the module's own variables and never from a question. A run is recorded
        under its engine and its module, with no template field; every filled
        slot carries one of the six origins the card renders. The adapters
        build a turn from the contracts IR and import no provider SDK for it.
        */
    }

    requirement def InputsAreTypedPrimitives {
        doc /*
        Every point in the system is a Point. A slot that asks for one fills
        from each of its sources - the pick, a pair, a point layer, a geocoded
        name - under the same ingestion, keeps the name the source carried,
        refuses by name a point outside the domain, and moves a dry landing to
        the nearest wet node. Neither the user nor the model names it twice.
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
    satisfy requirement EngineDefaultSurfaced by door;
    verify requirement EngineDefaultSurfaced by
        "tests/telemac/test_telemac_module_surface.py::test_an_open_slot_under_the_fold_says_the_dictionary_answers_it_for_nobody";
    verify requirement EngineDefaultSurfaced by
        "tests/telemac/test_telemac_module_surface.py::test_the_engine_default_is_on_the_slot_or_the_slot_is_a_question";
    verify requirement EngineDefaultSurfaced by
        "tests/telemac/test_telemac_module_surface.py::test_an_engine_default_is_never_written_into_the_deck";
    verify requirement EngineDefaultSurfaced by
        "tests/telemac/test_telemac_module_surface.py::test_the_bare_sheet_opens_every_keyword_the_dictionary_answers_for_nobody";

    satisfy requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by door;
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
        "tests/telemac/test_run_telemac_chain.py::test_classify_exit_names_the_keyword_lecdon_asked_for";
    verify requirement TheOpenSetIsCompleteAndRequiredIsTheObligFiles by
        "tests/telemac/test_run_telemac_chain.py::test_classify_exit_invents_no_demand_where_the_engine_made_none";

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

    satisfy requirement EverythingOverridable by wrapper;
    satisfy requirement EverythingOverridable by sheet;
    satisfy requirement EverythingOverridable by canvasGate;
    satisfy requirement EverythingOverridable by door;
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_raw_keyword_on_the_wire_fills_the_slot_it_names";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_raw_keyword_the_module_does_not_have_refuses_naming_the_nearest";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_raw_keyword_value_outside_the_choices_refuses_naming_the_choices";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_two_releases_on_the_floor_are_two_sources_in_the_deck";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_every_template_wire_carries_the_raw_keyword_floor";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_floor_that_is_not_a_mapping_refuses_by_name";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_resolution_order_is_engine_then_template_then_fill";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_fill_is_repeatable_and_the_later_one_stands";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_drawn_point_is_a_fill";
    verify requirement EverythingOverridable by
        "tests/telemac/test_telemac_module_surface.py::test_a_canvas_that_answers_nothing_refuses_and_invents_nothing";

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
        "tests/telemac/test_telemac_module_surface.py::test_a_release_becomes_the_source_keywords_and_the_series_they_name";
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_module_surface.py::test_a_coupling_states_only_what_the_carrier_names_it_by";
    verify requirement CompositesLiveInWrappers by
        "tests/telemac/test_telemac_module_surface.py::test_a_coupled_body_is_checked_against_its_own_module_s_dictionary";

    satisfy requirement OneTemplatePerQuestion by door;
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

    satisfy requirement ReviewIsTheDoorsView by door;
    verify requirement ReviewIsTheDoorsView by
        "tests/telemac/test_telemac_module_surface.py::test_the_card_shows_what_is_set_and_open_and_folds_the_rest_by_rubrique";
    verify requirement ReviewIsTheDoorsView by
        "tests/telemac/test_telemac_module_surface.py::test_the_fill_docstring_names_the_module_its_rubriques_and_its_open_slots";
    verify requirement ReviewIsTheDoorsView by
        "tests/telemac/test_telemac_module_surface.py::test_the_review_is_the_doors_view_and_never_a_step_of_its_own";
    verify requirement ReviewIsTheDoorsView by
        "tests/runtime/test_declarative_library.py::test_do_sag_declares_no_gate_in_front_of_its_self_gating_review";

    satisfy requirement CompositesSetOnlyPresence by composite;
    satisfy requirement CompositesSetOnlyPresence by wrapper;
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_module_surface.py::test_a_composite_sets_only_what_its_value_s_presence_defines";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_module_surface.py::test_a_wind_from_the_north_drives_the_water_south";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_module_surface.py::test_a_continuation_names_the_file_and_leaves_its_format_to_the_template";
    verify requirement CompositesSetOnlyPresence by
        "tests/telemac/test_telemac_module_surface.py::test_a_curve_number_field_names_no_model_and_the_template_does";

    satisfy requirement NoBodyExtendsABody by wrapper;
    verify requirement NoBodyExtendsABody by
        "tests/telemac/test_telemac_module_surface.py::test_a_body_extends_the_wrapper_and_never_a_body";

    satisfy requirement ATemplateNumberIsMetToTheMeshsPrecision by door;
    satisfy requirement ATemplateNumberIsMetToTheMeshsPrecision by sheet;
    verify requirement ATemplateNumberIsMetToTheMeshsPrecision by
        "tests/telemac/test_open_water_domains.py::test_the_footprint_and_the_solid_faces_are_cut_at_the_same_width";
    verify requirement ATemplateNumberIsMetToTheMeshsPrecision by
        "tests/telemac/test_open_water_domains.py::test_a_boundary_node_is_on_the_structure_when_it_stands_on_the_punched_outline";
    verify requirement ATemplateNumberIsMetToTheMeshsPrecision by
        "tests/telemac/test_open_water_domains.py::test_a_lone_node_between_two_of_another_kind_is_not_a_face";

    satisfy requirement ProvenanceNamesTheLayerThatAnswered by sheet;
    satisfy requirement ProvenanceNamesTheLayerThatAnswered by door;
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
        "tests/model/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement TheSteeringFormatHasOneWriter by serializer;
    satisfy requirement TheSteeringFormatHasOneWriter by steeringDriver;
    verify requirement TheSteeringFormatHasOneWriter by
        "tests/telemac/test_telemac_module_surface.py::test_the_serializer_is_the_only_module_that_writes_a_keyword_into_a_deck";

    satisfy requirement ModuleIsTheUnit by wrapper;
    satisfy requirement ModuleIsTheUnit by output;
    satisfy requirement ModuleIsTheUnit by sheet;
    satisfy requirement ModuleIsTheUnit by door;
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
        "tests/inputs/test_point.py::test_a_place_name_is_geocoded_and_keeps_the_name";
    verify requirement InputsAreTypedPrimitives by
        "tests/inputs/test_point.py::test_a_point_outside_the_domain_is_refused_by_name_and_names_the_fix";
    verify requirement InputsAreTypedPrimitives by
        "tests/inputs/test_point.py::test_a_point_landing_on_a_dry_node_moves_to_the_nearest_wet_one";
    verify requirement InputsAreTypedPrimitives by
        "tests/telemac/test_telemac_module_surface.py::test_a_drawn_point_is_a_fill";
}
