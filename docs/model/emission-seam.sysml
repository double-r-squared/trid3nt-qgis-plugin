// plane: workflow | system: fetcher -> products -> the user plane's canvas
//
// The emission seam as a checkable model: how a produced layer becomes a
// picture, and where its picture is decided.
//
// SysML v2 TEXTUAL notation, restricted to the subset the project checker
// reads. Conventions as the other seams: ``code:`` binds a block to the module
// it IS, ``forbid:`` states a dependency rule, ``constructor:`` names a
// function that builds a contract, and ``pass-through:`` marks an end that
// forwards the contract VERBATIM and therefore owes no item evidence.
//
// This seam runs ACROSS planes, which is why its far end is the first modeled
// edge of the user plane: it starts at a dataset's own declaration in the
// workflow plane and ends at the canvas the reader looks at. Everything else
// the user plane holds - the dock, the chat, the cards - is unmodeled, and the
// index says so rather than letting this one edge stand for the plane.

package EmissionSeam {

    doc /*
    The platform underneath already renders, styles, plays a time series and
    reads a remote store. What is OURS is narrower than it used to be: which
    layers exist, where their bytes are, what each one is a picture OF, and the
    honesty of the legend. This seam is that narrow part.

    Two things decide a picture, and they are declared in different places.
    A DATASET says how it draws itself, because that is a fact about the data -
    a curve number is classed, elevation is a grey ramp over metres, a road is
    a line somebody drew rather than a quantity somebody measured. A workflow
    says nothing about it at all: a ramp moves no water, so a declaration that
    carried one would be describing the picture instead of the run. Everything
    a reader wants to change afterwards is DISPLAY STATE and lives at runtime,
    on one surface, where changing it recomputes nothing.

    Emission itself is not a decision. A produced layer appears; the reader
    takes off what they do not want to see. There is no verb for "show this",
    so there is no way to produce a layer and forget to.
    */

    // ----------------------------------------------------------------- //
    // Blocks
    // ----------------------------------------------------------------- //

    part def StyleDeclaration {
        doc /*
        A dataset's own account of how it draws: the ``style:`` row beside the
        source that produces it, in the same file as its endpoints and its
        units. One of the shipped specs stands here for the shape every one of
        them carries.
        */
    }

    part def StyleRowSchema {
        doc /*
        The row's schema, and the place the preset family's vocabulary is
        closed. A kind outside the four is refused when the source registers,
        not when a reader is waiting for a picture.
        */
    }

    part def FetcherRouter {
        doc /*
        The declared row resolved for ONE call - the per-parameter overlay laid
        over the base - and stamped on the layer the fetch emits. It reads the
        row and forwards it; it decides nothing about it.
        */
    }

    part def EmissionSeamPoint {
        doc /*
        The one place a client-bound layer passes through. It publishes every
        renderable raster on the way past and drops the ones carrying a uri
        nothing can open, so a broken row never reaches the map wearing the
        look of a working one.

        A layer with no band to read - a vector's features, a mesh's dataset
        groups - has its declared row resolved HERE, through the same
        resolution the raster arm runs inside its publish. One resolution,
        four kinds, and nothing on the canvas is left to a default nobody
        declared.
        */
    }

    part def PublishPath {
        doc /*
        Write, register, notify. Overviews are enforced on the object, the
        handle is bound to its one uri, and the declared row is resolved
        against the raster's own bytes - the ONE resolution point, so the
        legend and the paint can never come from two different reads.

        The probes that read those bytes are questions only a raster can
        answer, so they are asked only of a raster; every other kind resolves
        through the same call with nothing read at all.
        */
    }

    part def PresetFamily {
        doc /*
        Four renderer shapes and one style-document writer. A quantity is a
        PARAMETER of one of the four - its units, its title, the one range it
        is read on - never a fifth shape. The document written here is QGIS's
        own format: their format, our writer, their validator.
        */
    }

    part def RestyleSurface {
        doc /*
        The whole presentation surface. Re-paint, retitle, re-scale, re-shape,
        or take a layer off the canvas - and nothing here can put a layer on
        one, because that already happened.
        */
    }

    part def RestyleTool {
        doc /*
        The ad hoc ask, in the vocabulary a reader uses: rescale that, stretch
        this, put these two on one range, hide it. It creates nothing and
        recomputes nothing.
        */
    }

    part def LayerRecord {
        doc /*
        One id bound to one uri. With a single scheme there is nothing to
        translate between, so what remains is the handle indirection that keeps
        a model from having to echo a store path back.
        */
    }

    part def PipelineEmitter {
        doc /*
        The emit record: the produced layer becomes the session's layer row and
        goes out on the wire, carrying its resolved style with it.
        */
    }

    part def OutputsSeamPoint {
        doc /*
        The solve-side half of automatic emission. A solver leg writes its
        outputs manifest; every entry becomes a registered, styled layer, with
        its picture derived from what the entry says it IS - the kind, the
        quantity, the units - so no worker ever bakes a style.
        */
    }

    part def ManifestRegistrar {
        doc /*
        The register-only arm: a worker already wrote display-ready objects and
        the statistics to scale them by, so the agent binds the handles and
        resolves the legend without reading a single COG back.
        */
    }

    part def LayerMaterializer {
        doc /*
        The canvas end, in the other process. It opens the store uri natively,
        loads the style document it was handed, and stamps the clock the row
        declares. It computes no colours of its own.

        It loads that document ONCE, when the layer is first created in the
        case. A case reopened adopts the layers the project already holds -
        with whatever styling they now carry - so a preset is the look a layer
        is BORN with and never a repaint of a choice made since.
        */
    }

    // ----------------------------------------------------------------- //
    // Ports
    // ----------------------------------------------------------------- //

    port def RowOut;
    port def RowIn;
    port def KindOut;
    port def KindIn;
    port def LayerOut;
    port def LayerIn;
    port def ResolvedOut;
    port def ResolvedIn;
    port def RecordOut;
    port def RecordIn;
    port def AskOut;
    port def AskIn;
    port def StashOut;
    port def StashIn;
    port def WireOut;
    port def WireIn;

    // ----------------------------------------------------------------- //
    // Interfaces - the seam contracts, by their real key names
    // ----------------------------------------------------------------- //

    interface def PresetKindVocabulary {
        doc /*
        The four kinds, named identically where they are validated and where
        they are drawn. Four names and no fifth: a kind the schema admits is a
        kind the writer has a renderer for, and the two must not drift apart -
        a row that passed registration and then found no shape would fail at
        the one moment a reader is waiting.

        The closure is the point. Presets keyed by QUANTITY multiply without
        limit and mislabel each other; keyed by DATA KIND there are four, and
        everything a quantity contributes is a parameter of one of them.
        */
        item kind : String;
        item continuous : String;
        item classed : String;
        item reference : String;
        item mesh : String;
    }

    interface def DeclaredStyleRow {
        doc /*
        How a layer draws, declared with the DATA and never with the run. The
        row picks one of the four shapes and parameterises it: which ramp,
        what the numbers mean, what the legend calls them, and the one scale
        they are read on.

        A row is optional in the tree and total here: an absent row is the
        kind's bare default, which is a complete answer rather than a gap.
        Nothing in it changes a simulation, which is exactly why it is declared
        beside the source instead of inside a workflow.

        ``scale`` carries the policy and, under a fixed policy, its range;
        ``classes`` are the declared breaks that make a classed row classed;
        ``geometry`` is the symbol shape a reference layer needs, and getting
        it wrong loads cleanly and then draws nothing.

        constructor: resolve_style_row
        */
        item kind : String;
        item ramp : String;
        item units : String;
        item label : String;
        item scale : Map;
        item classes : Map [0..1];
        item geometry : String [0..1];
        item color : String [0..1];
    }

    interface def EmittedLayer {
        doc /*
        A produced layer, as the record of it. There is no ``emit`` verb
        anywhere: a tool that produced something renderable has produced a
        layer, and this is what it produced.

        ``uri`` is the ONE reference - a single object in a single store,
        which is what makes remote parity an endpoint value rather than a
        second code path. ``role`` separates what was asked for from what was
        read on the way; ``bbox`` is what the camera flies to.

        ``style`` rides here as the producer DECLARED it, unresolved: the
        resolution happens once, on the publish path, against the layer's own
        bytes. ``legend`` is that resolution once it exists.

        constructor: build_layer_uri
        */
        item layer_id : String;
        item name : String;
        item layer_type : String;
        item uri : String;
        item role : String;
        item bbox : RealList;
        item style : Map [0..1];
        item legend : Map [0..1];
        item units : String [0..1];
        item crs_authid : String [0..1];
        item reference_time : String [0..1];
    }

    interface def ResolvedStyle {
        doc /*
        One preset resolved against one layer: the concrete range, what the
        legend says about it, and the style document that paints it. The
        colourbar and the raster come from THIS resolution, both of them, so
        there is no second range to drift.

        ``qml`` is the resolved preset in QGIS's own format and it is what the
        map loads; it is absent for a layer whose file already carries its
        colours, because nothing may override those. ``colormap`` is optional
        precisely because the paint travels IN the document - the canvas needs
        no ramp table of its own to draw what it was handed.
        */
        item kind : String;
        item vmin : Real;
        item vmax : Real;
        item label : String;
        item qml : String;
        item colormap : String [0..1];
        item units : String [0..1];
        item classes : Map [0..1];
        item value_field : String [0..1];
    }

    interface def LegendStash {
        doc /*
        The resolved style, keyed by the uri it was resolved for. An atomic
        publish returns a bare uri and no envelope, so the resolution reaches
        the emit record by the ONE thing both ends agree on. A key that did not
        match would lose the legend silently and paint an unstyled layer that
        looks like a styled one.
        */
        item uri : String;
        item legend : Map;
    }

    interface def LayerRecordEntry {
        doc /*
        The whole store record: an id, and the object it names. One store and
        one scheme leave nothing to translate - no display face to unwrap back
        into a data uri, no second scheme to accept beside this one - so what
        used to be a translation layer is this.

        constructor: observe_published_layer
        */
        item layer_id : String;
        item uri : String;
    }

    interface def RestyleAsk {
        doc /*
        What a reader wants changed about a layer already on the map. Every
        field is an OVERRIDE laid over what the data declared, and a field
        nobody asked about is left alone rather than re-asserted as a default -
        which is what makes a user's choice beat the preset instead of racing
        it.

        ``hidden`` is the un-emit and its inverse: taking a layer off the
        canvas is a presentation act like any other, so it lives here rather
        than as a second emission verb.

        ``shared`` is the comparison case - one range resolved across several
        layers at once, because two layers a reader is comparing on two ranges
        is a picture of two colour maps rather than of a difference.
        */
        item layer_id : String;
        item hidden : Boolean;
        item kind : String;
        item ramp : String;
        item label : String;
        item units : String;
        item policy : String;
        item value_range : RealList;
        item transform : String;
        item clip : RealList;
        item shared : RealList;
    }

    interface def SessionLayerRow {
        doc /*
        One row of the session's layer state, which is what the canvas is
        rebuilt from on every emit. ``uri`` is the same single store reference
        the producer wrote - the client opens it natively, so no publish ever
        mints a second face for the same bytes.

        ``visible`` is where the un-emit lands: a row the canvas has already
        seen arriving with it flipped takes the layer off the map and back on.

        ``reference_time`` is the mesh clock's origin. A SELAFIN records the
        offsets and not the instant they count from, so without this the
        scrubber plays the run in 1900; the row states when zero was.
        */
        item layer_id : String;
        item name : String;
        item layer_type : String;
        item uri : String;
        item visible : Boolean;
        item legend : Map;
        item temporal : Boolean;
        item crs_authid : String;
        item reference_time : String;
    }

    // ----------------------------------------------------------------- //
    // Parts - bound to the modules they ARE
    // ----------------------------------------------------------------- //

    part styleDeclaration : StyleDeclaration {
        doc /*
        code: trid3nt_server/tools/fetchers/weather/fetch_mrms_qpe/source.yaml
        */
        port rowOut : RowOut;
    }

    part styleRowSchema : StyleRowSchema {
        doc /*
        code: contracts/trid3nt_contracts/source_spec.py
        */
        port kindOut : KindOut;
    }

    part fetcherRouter : FetcherRouter {
        doc /*
        code: trid3nt_server/tools/fetchers/_router/router.py
        */
        port rowIn : RowIn;
        port layerOut : LayerOut;
    }

    part emissionSeamPoint : EmissionSeamPoint {
        doc /*
        code: trid3nt_server/emission/layer_uri_emit.py
        */
        port layerIn : LayerIn;
        port layerOut : LayerOut;
        port rowOut : RowOut;
    }

    part publishPath : PublishPath {
        doc /*
        code: trid3nt_server/emission/publish.py
        */
        port rowIn : RowIn;
        port rowOut : RowOut;
        port resolvedIn : ResolvedIn;
        port recordOut : RecordOut;
        port stashOut : StashOut;
    }

    part presetFamily : PresetFamily {
        doc /*
        code: trid3nt_server/emission/presets.py
        */
        port kindIn : KindIn;
        port rowIn : RowIn;
        port resolvedOut : ResolvedOut;
    }

    part restyleSurface : RestyleSurface {
        doc /*
        code: trid3nt_server/emission/restyle.py
        */
        port askIn : AskIn;
        port rowOut : RowOut;
    }

    part restyleTool : RestyleTool {
        doc /*
        code: trid3nt_server/tools/display/restyle_layer/restyle_layer.py
        */
        port askOut : AskOut;
    }

    part layerRecord : LayerRecord {
        doc /*
        code: trid3nt_server/emission/uri_registry.py
        */
        port recordIn : RecordIn;
    }

    part pipelineEmitter : PipelineEmitter {
        doc /*
        code: trid3nt_server/emission/pipeline_emitter.py
        */
        port layerIn : LayerIn;
        port stashIn : StashIn;
        port resolvedOut : ResolvedOut;
        port wireOut : WireOut;
    }

    part outputsSeamPoint : OutputsSeamPoint {
        doc /*
        code: trid3nt_server/emission/outputs_seam.py
        */
        port layerOut : LayerOut;
        port recordOut : RecordOut;
    }

    part manifestRegistrar : ManifestRegistrar {
        doc /*
        code: trid3nt_server/workflows/shared/register_published_manifest.py
        */
        port recordOut : RecordOut;
    }

    part layerMaterializer : LayerMaterializer {
        doc /*
        code: plugin/render/layers.py
        */
        port wireIn : WireIn;
        port resolvedIn : ResolvedIn;
    }

    // ----------------------------------------------------------------- //
    // Hops
    // ----------------------------------------------------------------- //

    interface kindVocabularyShared : PresetKindVocabulary
        connect styleRowSchema.kindOut to presetFamily.kindIn;

    interface styleDeclared : DeclaredStyleRow
        connect styleDeclaration.rowOut to fetcherRouter.rowIn {
        doc /*
        pass-through: fetcherRouter
        */
    }

    interface styleHandedToPublish : DeclaredStyleRow
        connect emissionSeamPoint.rowOut to publishPath.rowIn {
        doc /*
        pass-through: emissionSeamPoint
        */
    }

    interface rowResolvedAgainstTheLayer : DeclaredStyleRow
        connect publishPath.rowOut to presetFamily.rowIn;

    interface restyledRowRepublished : DeclaredStyleRow
        connect restyleSurface.rowOut to publishPath.rowIn;

    interface layerProduced : EmittedLayer
        connect fetcherRouter.layerOut to emissionSeamPoint.layerIn;

    interface layerTracked : EmittedLayer
        connect emissionSeamPoint.layerOut to pipelineEmitter.layerIn;

    interface solvedLayerEmitted : EmittedLayer
        connect outputsSeamPoint.layerOut to pipelineEmitter.layerIn;

    interface styleResolved : ResolvedStyle
        connect presetFamily.resolvedOut to publishPath.resolvedIn;

    interface legendStashedByUri : LegendStash
        connect publishPath.stashOut to pipelineEmitter.stashIn;

    interface legendReachesTheCanvas : ResolvedStyle
        connect pipelineEmitter.resolvedOut to layerMaterializer.resolvedIn {
        doc /*
        pass-through: pipelineEmitter
        */
    }

    interface layerOnTheCanvas : SessionLayerRow
        connect pipelineEmitter.wireOut to layerMaterializer.wireIn;

    interface recordFromPublish : LayerRecordEntry
        connect publishPath.recordOut to layerRecord.recordIn;

    interface recordFromSolve : LayerRecordEntry
        connect outputsSeamPoint.recordOut to layerRecord.recordIn;

    interface recordFromWorkerManifest : LayerRecordEntry
        connect manifestRegistrar.recordOut to layerRecord.recordIn;

    interface restyleAsked : RestyleAsk
        connect restyleTool.askOut to restyleSurface.askIn;

    // ----------------------------------------------------------------- //
    // The laws this seam is governed by
    // ----------------------------------------------------------------- //

    requirement def PresentationNeverInDeclarations {
        doc /*
        A workflow declaration carries no presentation. A ramp, a range, a
        title and a visibility change nothing a solver computes, so a plan that
        stated them would be describing the picture instead of the run - and
        the picture would then be frozen at authoring time, where the reader
        who wants it different is not.

        A DATASET declaring how it draws is the opposite case and is allowed
        for the same reason: that a curve number is classed and an elevation is
        a ramp over metres is a fact about the DATA, true before any run, and
        it belongs beside the source's endpoints and units rather than in a
        mapping table somebody has to keep in step.

        The ad hoc case lives at runtime on the restyle surface, where it costs
        nothing and can be undone.

        The preset family knowing nothing about workflows is the structural
        half: a style writer that could reach the declaration machinery is one
        import away from a plan that styles.

        forbid: trid3nt_server.emission.presets -> trid3nt_server.workflows
        */
    }

    requirement def OneScalePerQuantity {
        doc /*
        A quantity is read on ONE scale for the length of a packet. The range
        is resolved once, over the whole run, and every frame, plane and legend
        uses that one range - resolved per frame, the same colour means a
        different number in the next one, and an animation of a receding flood
        looks identical to an advancing one.

        A comparison set is the same rule across layers instead of across time:
        several layers a reader asked to compare resolve their range TOGETHER,
        and the legend says a shared range produced it rather than naming
        percentiles nobody read.

        The legend states the policy as well as the range, because a fixed
        domain scale and one read off this run's own values look the same on
        the canvas and mean different things.
        */
    }

    requirement def UserChoiceBeatsPreset {
        doc /*
        What a reader chose outranks what the data declared. The override wins
        FIELD BY FIELD, so asking for a range does not silently re-assert a
        default ramp, and an ask nobody made leaves the declaration untouched
        rather than merging an empty one over it.

        The same rule holds outside this code entirely: a reader restyles a
        layer natively in QGIS, and per-case durability keeps that rather than
        repainting it from the preset on reopen.
        */
    }

    requirement def EmissionIsAutomatic {
        doc /*
        A produced layer appears. There is no verb for showing one, on either
        arm - a fetch emits what it fetched, a solve emits every entry its
        outputs manifest names - and there is no opt-out, so an intermediate is
        a layer too and the reader hides what they did not want to see.

        This is why there is no way to produce a layer and forget to display
        it: the display is not a second step somebody has to remember.

        The un-emit is the same surface running the other way: a visibility
        flip on a row the canvas already holds, not a second emission verb.
        A layer nobody published refuses rather than being quietly created.
        */
    }

    requirement def StyleIsDeclaredOrKindDefault {
        doc /*
        Every emitted layer ships styled - by the row its data declared, or by
        its kind's bare default. There is no third case and no unstyled one:
        an unknown quantity gets ONE ramp over its own range, never the colours
        of a quantity somebody assumed it was.

        The kind vocabulary is closed at registration, so a row naming a fifth
        shape is a refused source rather than a layer that reaches the canvas
        with nothing to draw it.

        The document that paints it is QGIS's own format, written by us and
        validated by QGIS: the canvas loads it and does not compute colours of
        its own, which is what keeps one style writer instead of two that
        disagree.

        forbid: plugin.render.layers -> trid3nt_server
        */
    }

    // ----------------------------------------------------------------- //
    // Allocations
    // ----------------------------------------------------------------- //

    satisfy requirement PresentationNeverInDeclarations by styleDeclaration;
    satisfy requirement PresentationNeverInDeclarations by fetcherRouter;
    satisfy requirement PresentationNeverInDeclarations by restyleSurface;
    satisfy requirement PresentationNeverInDeclarations by restyleTool;
    verify requirement PresentationNeverInDeclarations by
        "tests/test_declarative_library.py::test_a_declaration_carries_no_presentation_vocabulary";
    verify requirement PresentationNeverInDeclarations by
        "tests/test_router_hrrr.py::test_units_and_style_by_param";
    verify requirement PresentationNeverInDeclarations by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";

    satisfy requirement OneScalePerQuantity by presetFamily;
    satisfy requirement OneScalePerQuantity by publishPath;
    verify requirement OneScalePerQuantity by
        "tests/test_presets.py::test_one_range_spans_a_compared_set";
    verify requirement OneScalePerQuantity by
        "tests/test_presets.py::test_a_shared_range_beats_a_read_and_the_legend_stops_naming_percentiles";
    verify requirement OneScalePerQuantity by
        "tests/test_presets.py::test_a_data_policy_reads_the_range_off_the_layer_and_says_so";
    verify requirement OneScalePerQuantity by
        "tests/test_animation_legend_stability.py::test_the_scale_is_resolved_over_the_whole_run_never_one_frame";
    verify requirement OneScalePerQuantity by
        "tests/test_animation_legend_stability.py::test_the_animation_paints_what_the_published_raster_of_that_quantity_paints";
    verify requirement OneScalePerQuantity by
        "tests/test_publish_layer_legend.py::test_the_layer_ships_the_qml_the_map_loads_over_that_same_range";
    verify requirement OneScalePerQuantity by
        "tests/test_outputs_seam.py::test_a_mesh_entry_paints_the_group_it_declares_on_the_published_range";
    verify requirement OneScalePerQuantity by
        "tests/test_animation_legend_stability.py::test_the_published_range_is_found_by_quantity_not_by_the_title_it_was_painted_under";

    satisfy requirement UserChoiceBeatsPreset by presetFamily;
    satisfy requirement UserChoiceBeatsPreset by restyleSurface;
    satisfy requirement UserChoiceBeatsPreset by restyleTool;
    verify requirement UserChoiceBeatsPreset by
        "tests/test_presets.py::test_the_override_wins_field_by_field_over_the_declaration";
    verify requirement UserChoiceBeatsPreset by
        "tests/test_restyle_surface.py::test_the_ask_beats_the_declared_row_field_by_field";
    verify requirement UserChoiceBeatsPreset by
        "tests/test_restyle_surface.py::test_an_ask_nobody_made_leaves_the_declaration_alone";
    verify requirement UserChoiceBeatsPreset by
        "tests/test_restyle_surface.py::test_a_comparison_paints_every_layer_on_the_one_range";
    verify requirement UserChoiceBeatsPreset by
        "plugin/tests/test_raster_render.py::test_a_case_reopen_adopts_its_own_layers_and_loads_no_style_over_them";
    verify requirement UserChoiceBeatsPreset by
        "plugin/tests/test_raster_render.py::test_a_different_case_sweeps_the_group_it_did_not_open";

    satisfy requirement EmissionIsAutomatic by emissionSeamPoint;
    satisfy requirement EmissionIsAutomatic by outputsSeamPoint;
    satisfy requirement EmissionIsAutomatic by pipelineEmitter;
    satisfy requirement EmissionIsAutomatic by restyleSurface;
    verify requirement EmissionIsAutomatic by
        "tests/test_auto_publish_droppable_raster.py::test_intermediate_publishes_too_there_is_no_opt_out";
    verify requirement EmissionIsAutomatic by
        "tests/test_auto_publish_droppable_raster.py::test_publish_for_emission_is_the_only_seam";
    verify requirement EmissionIsAutomatic by
        "tests/test_restyle_surface.py::test_hiding_a_layer_is_the_un_emit_and_unhiding_puts_it_back";
    verify requirement EmissionIsAutomatic by
        "tests/test_restyle_surface.py::test_hiding_a_layer_nobody_published_refuses_rather_than_creating_one";
    verify requirement EmissionIsAutomatic by
        "plugin/tests/test_raster_render.py::test_a_visibility_flip_on_a_row_already_seen_reaches_the_layer_tree";

    satisfy requirement StyleIsDeclaredOrKindDefault by styleDeclaration;
    satisfy requirement StyleIsDeclaredOrKindDefault by styleRowSchema;
    satisfy requirement StyleIsDeclaredOrKindDefault by presetFamily;
    satisfy requirement StyleIsDeclaredOrKindDefault by publishPath;
    satisfy requirement StyleIsDeclaredOrKindDefault by layerMaterializer;
    verify requirement StyleIsDeclaredOrKindDefault by
        "tests/test_presets.py::test_the_family_is_four_kinds_and_every_one_of_them_writes_a_document";
    verify requirement StyleIsDeclaredOrKindDefault by
        "tests/test_presets.py::test_a_declaration_that_names_no_parameters_gets_its_kinds_bare_default";
    verify requirement StyleIsDeclaredOrKindDefault by
        "tests/test_presets.py::test_a_quantity_parameterises_the_preset_it_never_mints_one";
    verify requirement StyleIsDeclaredOrKindDefault by
        "tests/test_router_spec_loader.py::test_a_style_row_naming_a_kind_outside_the_family_is_refused_at_registration";
    verify requirement StyleIsDeclaredOrKindDefault by
        "tests/test_router_spec_loader.py::test_a_style_row_naming_a_geometry_outside_the_family_is_refused";
    verify requirement StyleIsDeclaredOrKindDefault by
        "plugin/tests/test_raster_render.py::test_the_legends_qml_reaches_load_named_style";
    verify requirement StyleIsDeclaredOrKindDefault by
        "plugin/tests/test_raster_render.py::test_a_vectors_declared_symbol_is_what_the_read_back_reports";
    verify requirement StyleIsDeclaredOrKindDefault by
        "tests/test_publish_layer_legend.py::test_a_vector_row_resolves_through_the_same_seam_without_reading_the_object";
    verify requirement StyleIsDeclaredOrKindDefault by
        "tests/test_publish_layer_legend.py::test_a_mesh_row_resolves_bound_to_the_group_it_declares";
    verify requirement StyleIsDeclaredOrKindDefault by
        "tests/test_publish_layer_legend.py::test_the_emission_seam_stamps_the_resolved_row_on_a_vector_it_passes";
    verify requirement StyleIsDeclaredOrKindDefault by
        "tests/test_model_conformance.py::test_the_model_conforms_to_the_tree";
}
