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
    */

    // ----------------------------------------------------------------- //
    // Blocks
    // ----------------------------------------------------------------- //

    part def Catalog {
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
        The wrapper class the catalog makes. It holds the catalog, the
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

    part def Stager {
        doc /*
        An authored run directory becomes a staged run: everything uploaded
        beside the mesh, the manifest naming the case written last.
        */
    }

    // ----------------------------------------------------------------- //
    // Ports
    // ----------------------------------------------------------------- //

    port def CatalogOut;
    port def CatalogIn;
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

    interface def KeywordCatalog {
        doc /*
        One dictionary entry as the catalog carries it. ``keyword`` is the
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
        item CATALOG : Map;
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

    part catalogExtractor : Catalog {
        doc /*
        code: trid3nt_server/workflows/mesh/meshers/drivers/telemac_dico_driver.py
        */
        port catalogOut : CatalogOut;
    }

    part slot : Slot {
        doc /*
        code: trid3nt_server/workflows/telemac/modules/module.py
        */
        port catalogIn : CatalogIn;
    }

    part wrapper : Module {
        doc /*
        code: trid3nt_server/workflows/telemac/modules/module.py
        */
        port catalogIn : CatalogIn;
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

    // ----------------------------------------------------------------- //
    // Hops
    // ----------------------------------------------------------------- //

    interface catalogExtracted : KeywordCatalog
        connect catalogExtractor.catalogOut to slot.catalogIn;

    interface catalogLoaded : KeywordCatalog
        connect catalogExtractor.catalogOut to wrapper.catalogIn;

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
        A slot is spelled the engine's way. The catalog carries the dictionary's
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
        surface, so no backslash may remain in any catalog.
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

    requirement def WrapperHasNoOpinion {
        doc /*
        The wrapper asserts nothing and offers nowhere to. It is the analog of
        the engine's own defaults; variance lives in templates, where a person
        reads it. A defaults hook here would put an opinion in the one place
        nobody would think to look for one, and it would be inherited by every
        template silently.
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
        */
    }

    requirement def CatalogMatchesImage {
        doc /*
        The committed catalog is the image's dictionaries, not a copy that once
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

    requirement def SharedBodyHasTwoUsers {
        doc /*
        A shared body exists because a good portion is shared, which means at
        least two templates LIST it as a part. One user folds back into its
        template; a body with one is an indirection, not a sharing.
        */
    }

    requirement def CompositionNotInheritance {
        doc /*
        A body reuses another body by listing it, never by extending it. Parts
        merge in the listed order and per-slot provenance names the part, so a
        keyword that means something else in a new setting is seen rather than
        inherited into silence; a keyword two parts both set refuses by name
        unless the template settles it itself.
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

    requirement def RunIsHeld {
        doc /*
        Nothing executes until the user runs. Fill produces a sheet and no work
        beyond the producers the canvas shows; run is the separate, explicit act
        - and it refuses an incomplete sheet BY NAME first, because a run
        started on an unanswered mandatory slot fails inside Fortran minutes
        later, blaming a keyword rather than the gap.

        The module surface never reaches the writer it replaces: while both
        stand, the substrate is dark beside it, not entangled with it.

        forbid: trid3nt_server.workflows.telemac.modules -> trid3nt_server.workflows.telemac.authoring.author
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement KeywordNamesAreRaw by catalogExtractor;
    satisfy requirement KeywordNamesAreRaw by slot;
    verify requirement KeywordNamesAreRaw by
        "tests/test_telemac_module_surface.py::test_every_slot_carries_the_dictionary_s_own_name_and_help";
    verify requirement KeywordNamesAreRaw by
        "tests/test_telemac_module_surface.py::test_the_identifier_is_the_keyword_and_nothing_invented";
    verify requirement KeywordNamesAreRaw by
        "tests/test_telemac_module_surface.py::test_a_keyword_the_module_does_not_have_refuses_naming_the_nearest";

    satisfy requirement EverySlotDescribed by catalogExtractor;
    satisfy requirement EverySlotDescribed by slot;
    verify requirement EverySlotDescribed by
        "tests/test_telemac_module_surface.py::test_every_slot_carries_the_dictionary_s_own_name_and_help";
    verify requirement EverySlotDescribed by
        "tests/test_telemac_catalog_drift.py::test_the_help_carries_no_markup_into_the_surface";

    satisfy requirement EngineDefaultSurfaced by slot;
    satisfy requirement EngineDefaultSurfaced by sheet;
    verify requirement EngineDefaultSurfaced by
        "tests/test_telemac_module_surface.py::test_the_engine_default_is_on_the_slot_or_the_slot_is_a_question";
    verify requirement EngineDefaultSurfaced by
        "tests/test_telemac_module_surface.py::test_an_engine_default_is_never_written_into_the_deck";
    verify requirement EngineDefaultSurfaced by
        "tests/test_telemac_module_surface.py::test_the_bare_sheet_asks_the_three_questions_the_engine_has_no_answer_for";

    satisfy requirement WrapperHasNoOpinion by wrapper;
    verify requirement WrapperHasNoOpinion by
        "tests/test_telemac_module_surface.py::test_a_wrapper_asserts_nothing_and_has_no_hook_to";
    verify requirement WrapperHasNoOpinion by
        "tests/test_telemac_module_surface.py::test_a_wrapper_is_a_declaration_and_refuses_to_be_a_value";

    satisfy requirement EverythingOverridable by wrapper;
    satisfy requirement EverythingOverridable by sheet;
    satisfy requirement EverythingOverridable by canvasGate;
    verify requirement EverythingOverridable by
        "tests/test_telemac_module_surface.py::test_resolution_order_is_engine_then_the_parts_then_template_then_fill";
    verify requirement EverythingOverridable by
        "tests/test_telemac_module_surface.py::test_a_composed_slot_says_which_part_asserted_it";
    verify requirement EverythingOverridable by
        "tests/test_telemac_module_surface.py::test_a_fill_is_repeatable_and_the_later_one_stands";
    verify requirement EverythingOverridable by
        "tests/test_telemac_module_surface.py::test_a_drawn_point_is_a_fill";
    verify requirement EverythingOverridable by
        "tests/test_telemac_module_surface.py::test_a_canvas_that_answers_nothing_refuses_and_invents_nothing";

    satisfy requirement CatalogMatchesImage by catalogExtractor;
    verify requirement CatalogMatchesImage by
        "tests/test_telemac_catalog_drift.py::test_the_committed_catalog_is_what_the_image_says_today";
    verify requirement CatalogMatchesImage by
        "tests/test_telemac_catalog_drift.py::test_every_exposed_module_has_a_committed_catalog";

    satisfy requirement CompositesLiveInWrappers by composite;
    satisfy requirement CompositesLiveInWrappers by wrapper;
    verify requirement CompositesLiveInWrappers by
        "tests/test_telemac_module_surface.py::test_a_composite_becomes_several_slots_and_the_file_they_name";
    verify requirement CompositesLiveInWrappers by
        "tests/test_telemac_module_surface.py::test_a_composite_lives_on_the_wrapper_and_may_not_shadow_a_keyword";
    verify requirement CompositesLiveInWrappers by
        "tests/test_telemac_module_surface.py::test_a_composite_the_wrapper_never_registered_refuses_by_name";
    verify requirement CompositesLiveInWrappers by
        "tests/test_telemac_module_surface.py::test_a_release_becomes_the_source_keywords_and_the_series_they_name";
    verify requirement CompositesLiveInWrappers by
        "tests/test_telemac_module_surface.py::test_a_coupling_states_only_what_the_carrier_names_it_by";
    verify requirement CompositesLiveInWrappers by
        "tests/test_telemac_module_surface.py::test_a_coupled_body_is_checked_against_its_own_module_s_dictionary";

    satisfy requirement SharedBodyHasTwoUsers by sheet;
    verify requirement SharedBodyHasTwoUsers by
        "tests/test_telemac_module_surface.py::test_every_shared_body_has_at_least_two_users";

    satisfy requirement CompositionNotInheritance by sheet;
    verify requirement CompositionNotInheritance by
        "tests/test_telemac_module_surface.py::test_a_body_is_reused_by_composition_and_never_by_extension";
    verify requirement CompositionNotInheritance by
        "tests/test_telemac_module_surface.py::test_the_parts_merge_in_the_listed_order";
    verify requirement CompositionNotInheritance by
        "tests/test_telemac_module_surface.py::test_a_keyword_two_parts_both_set_refuses_unless_the_template_settles_it";
    verify requirement CompositionNotInheritance by
        "tests/test_telemac_module_surface.py::test_a_composed_slot_says_which_part_asserted_it";

    satisfy requirement BodiesAreStatic by sheet;
    verify requirement BodiesAreStatic by
        "tests/test_telemac_module_surface.py::test_a_body_is_static_and_no_fill_changes_what_it_asserts";
    verify requirement BodiesAreStatic by
        "tests/test_telemac_module_surface.py::test_a_param_a_body_reads_binds_from_the_sheet_the_invocation_resolved";

    satisfy requirement RunIsHeld by sheet;
    satisfy requirement RunIsHeld by serializer;
    satisfy requirement RunIsHeld by stager;
    verify requirement RunIsHeld by
        "tests/test_telemac_module_surface.py::test_run_refuses_an_incomplete_sheet_naming_what_is_open";
    verify requirement RunIsHeld by
        "tests/test_telemac_module_surface.py::test_run_serializes_then_stages_then_dispatches";
    verify requirement RunIsHeld by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";
}
