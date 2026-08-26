"""One flood layer, never two, and it is styled by DECLARATION.

A single flood request must put exactly ONE map row of the peak-depth data on
the canvas. Three independent things can break that, and each gets a class:

PRIMARY (``adapter.summarize_tool_result``): a scenario wrapper's
already-published LayerURI is summarized with explicit ``published`` /
``on_map`` / ``wms_url`` signals, so the model recognizes the layer is already
on the map and does not issue a second publish of the same COG.

THE STYLE BOUNDARY (``publish.style_preset_for_publish``): a layer publishes
under the preset its PRODUCER DECLARED - an explicit preset, else the declared
quantity resolved through the style contract, else the neutral ramp. A preset
is never inferred from a filename or a layer id.

THE DEDUP RULE (``pipeline_emitter.add_loaded_layer``): two publishes of the
SAME underlying COG under different display URLs collapse to ONE loaded layer,
while two genuinely distinct COGs must still coexist as two rows.

Every appended layer also carries a stable, monotonic ``z_index``, and an
in-place re-publish reuses the superseded layer's slot rather than renumbering.
"""

from __future__ import annotations

import inspect

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.adapters.adapter import (
    _layer_uri_is_published,
    _published_scenario_tool_names,
    summarize_tool_result,
)
from trid3nt_server.emission.pipeline_emitter import PipelineEmitter, _layer_identity_key
from trid3nt_server.emission.publish import style_preset_for_publish
from trid3nt_server.emission.styles import NEUTRAL_FALLBACK_PRESET


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


class _Sink:
    async def __call__(self, text: str) -> None:  # noqa: D401 - swallow frames
        return None


def _published_flood_layer_uri(run_id: str) -> LayerURI:
    """The single styled, ALREADY-PUBLISHED peak-depth LayerURI a flood scenario
    wrapper returns on success (uri is the renderable TiTiler tile template)."""
    cog = f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif"
    return LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Peak flood depth",
        layer_type="raster",
        uri=(
            "https://titiler.example/cog/tiles/{z}/{x}/{y}.png"
            f"?url={cog}&rescale=0,3&colormap_name=blues"
        ),
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
        bbox=(-85.4, 35.0, -85.2, 35.2),
    )


# --------------------------------------------------------------------------- #
# (c) PRIMARY - the scenario function_response carries the already-published
#     signal so the LLM does not re-publish.
# --------------------------------------------------------------------------- #


class TestScenarioPublishedSignal:
    def test_flood_scenario_summary_signals_already_published(self) -> None:
        result = _published_flood_layer_uri("RUN123")
        summary = summarize_tool_result("sfincs_flood", result)

        assert summary["status"] == "ok"
        # The explicit already-published signals the LLM keys on.
        assert summary["published"] is True
        assert summary["on_map"] is True
        assert summary["publish_status"] == "published"
        # The prompt's escape clause also keys on a "wms_url" field - it must be
        # present and carry the renderable URL.
        assert summary["wms_url"] == result.uri
        # The canonical handle + metadata the loop needs to narrate.
        assert summary["layer_id"] == result.layer_id
        assert summary["handle"] == result.layer_id
        assert summary["style_preset"] == "continuous_flood_depth"
        assert summary["bbox"] == [-85.4, 35.0, -85.2, 35.2]
        # A human-readable do-not-republish note for the model.
        assert "publish_layer" in summary["already_published_note"]

    def test_every_scenario_wrapper_is_recognized(self) -> None:
        # All flood + plume scenario wrappers carry the published signal.
        for tool in (
            "sfincs_flood",
            "modflow_contaminant_plume",
        ):
            assert tool in _published_scenario_tool_names(), tool
            summary = summarize_tool_result(tool, _published_flood_layer_uri("R"))
            assert summary.get("published") is True, tool
            assert summary.get("on_map") is True, tool

    def test_non_scenario_layer_uri_is_not_flagged_published(self) -> None:
        """A LayerURI from a NON-scenario tool (e.g. a fetcher) must NOT get the
        published signal - it falls through to the normal summary path."""
        layer = _published_flood_layer_uri("R")
        summary = summarize_tool_result("fetch_wdpa_protected_areas", layer)
        assert "published" not in summary
        assert "on_map" not in summary

    def test_raw_gs_cog_not_flagged_published(self) -> None:
        """A scenario result whose uri is a RAW gs:// COG (storage, not on the
        map) must NOT be flagged published - only an http(s) WMS uri is."""
        raw = LayerURI(
            layer_id="flood-depth-peak-R",
            name="Peak flood depth",
            layer_type="raster",
            uri="gs://legacy-cloud-runs/R/flood_depth_peak.tif",
            style_preset="continuous_flood_depth",
            role="primary",
        )
        assert _layer_uri_is_published(raw) is False
        summary = summarize_tool_result("sfincs_flood", raw)
        assert "published" not in summary

    def test_failed_scenario_envelope_unaffected(self) -> None:
        """A FAILED modeled envelope (empty layers, honesty floor) must still
        surface status=error - the published-signal branch must not swallow it."""
        failed = {
            "envelope_type": "modeled",
            "layers": [],
            "workflow_name": "model_flood_scenario:FAILED:SOLVER_TIMEOUT",
        }
        summary = summarize_tool_result("sfincs_flood", failed)
        assert summary["status"] == "error"
        assert "published" not in summary


# --------------------------------------------------------------------------- #
# (a) THE STYLE BOUNDARY - the preset comes from what the producer DECLARED,
#     and a filename is not a declaration.
# --------------------------------------------------------------------------- #


class TestPublishBoundaryPreset:
    def test_declared_flood_depth_quantity_gets_the_flood_preset(self) -> None:
        preset = style_preset_for_publish(
            style_preset="", quantity="flood_depth"
        )
        assert preset == "continuous_flood_depth"

    def test_the_hyphenated_spelling_of_the_same_quantity_agrees(self) -> None:
        assert style_preset_for_publish(
            style_preset=None, quantity="swmm-depth"
        ) == "continuous_flood_depth"

    def test_explicit_preset_is_honored(self) -> None:
        # The producer named its own ramp; a declared quantity never overrides it.
        preset = style_preset_for_publish(
            style_preset="continuous_dem", quantity="flood_depth"
        )
        assert preset == "continuous_dem"

    def test_undeclared_quantity_publishes_neutral(self) -> None:
        # No preset and no quantity: the layer's physical meaning is unknown, so
        # it gets the neutral ramp over its own range, not a physical band.
        preset = style_preset_for_publish(style_preset="", quantity=None)
        assert preset == NEUTRAL_FALLBACK_PRESET
        assert preset != "continuous_flood_depth"

    def test_unregistered_quantity_publishes_neutral(self) -> None:
        preset = style_preset_for_publish(
            style_preset=None, quantity="no_such_quantity"
        )
        assert preset == NEUTRAL_FALLBACK_PRESET

    def test_the_boundary_cannot_see_a_filename(self) -> None:
        """The anti-guess pin: a file name and a layer id are NAMES, not
        measurements, so the boundary is not given either one. A COG called
        ``flood_depth_peak.tif`` cannot acquire a flood ramp here, because
        nothing here is told what the file is called."""
        params = set(inspect.signature(style_preset_for_publish).parameters)
        assert params == {"style_preset", "quantity"}, params

    def test_a_flood_named_layer_with_no_declaration_stays_neutral(self) -> None:
        # The layer the old filename guess styled as flood depth. Its producer
        # declared nothing, so it publishes neutral.
        layer = LayerURI(
            layer_id="flood-depth-peak-R",
            name="flood_depth_peak.tif",
            layer_type="raster",
            uri="s3://runs/R/flood_depth_peak.tif",
            style_preset="",
        )
        assert style_preset_for_publish(
            style_preset=layer.style_preset
        ) == NEUTRAL_FALLBACK_PRESET


# --------------------------------------------------------------------------- #
# (b) SAFETY NET - two publishes of the SAME underlying COG (different display
#     URLs) dedup to ONE loaded_layer.
# --------------------------------------------------------------------------- #


class TestDedupByIdentity:
    @pytest.mark.asyncio
    async def test_same_cog_two_display_urls_merge_to_one(self) -> None:
        emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
        cog = "s3://trid3nt-runs/RUN/flood_depth_peak.tif"

        # 1. The workflow's internal styled publish (continuous_flood_depth ramp).
        workflow_layer = LayerURI(
            layer_id="flood-depth-peak-RUN",
            name="Peak flood depth",
            layer_type="raster",
            uri=(
                "https://titiler.example/cog/tiles/{z}/{x}/{y}.png"
                f"?url={cog}&rescale=0,3&colormap_name=blues"
            ),
            style_preset="continuous_flood_depth",
        )
        await emitter.add_loaded_layer(workflow_layer)

        # 2. A redundant LLM re-publish of the SAME COG - DIFFERENT display URL
        #    (different tile-template query order / id) AND a different layer_id.
        llm_republish = LayerURI(
            layer_id="chattanooga-100-year",
            name="chattanooga-100-year",
            layer_type="raster",
            uri=(
                "https://titiler.example/cog/tiles/{z}/{x}/{y}.png"
                f"?url={cog}&rescale=0,5&colormap_name=viridis"
            ),
            style_preset="continuous_flood_depth",
        )
        await emitter.add_loaded_layer(llm_republish)

        # Exactly ONE loaded layer - the two publishes of the same COG merged.
        layers = emitter.loaded_layers
        assert len(layers) == 1, [(l.layer_id, l.uri) for l in layers]
        # The later publish supersedes in place (its id is what the row carries).
        assert layers[0].layer_id == "chattanooga-100-year"

    @pytest.mark.asyncio
    async def test_identity_key_extracts_shared_cog(self) -> None:
        cog = "s3://runs/RUN/flood_depth_peak.tif"
        a = (
            "https://titiler.example/cog/tiles/{z}/{x}/{y}.png"
            f"?url={cog}&rescale=0,3&colormap_name=blues"
        )
        b = (
            "https://titiler.example/cog/tiles/{z}/{x}/{y}.png"
            f"?url={cog}&rescale=0,5&colormap_name=viridis"
        )
        assert _layer_identity_key(a) == _layer_identity_key(b) == cog

    @pytest.mark.asyncio
    async def test_plain_cog_keys_to_itself(self) -> None:
        # A bare gs:// COG (no query string) keys to its own uri - legacy behavior.
        assert _layer_identity_key("gs://b/dem.tif") == "gs://b/dem.tif"

    @pytest.mark.asyncio
    async def test_distinct_cogs_do_not_merge_f97_guard(self) -> None:
        """F97 coexistence guard: two genuinely DIFFERENT layers (distinct COGs /
        distinct display URLs that share only a generic WMS LAYERS name) must
        STILL coexist as two rows. The identity key must not collapse them."""
        emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())

        # Two WDPA fetches: same generic LAYERS=wdpa name, but distinct (n=) ->
        # genuinely distinct layers that MUST remain independently deletable.
        first = LayerURI(
            layer_id="wdpa-aaaaaaaaaaaaaaaaaaaaaaaaaa",
            name="Protected Areas",
            layer_type="raster",
            uri="https://qgis.example/ogc/wms?LAYERS=wdpa&n=1",
            style_preset="wdpa_protected_areas",
        )
        second = LayerURI(
            layer_id="wdpa-bbbbbbbbbbbbbbbbbbbbbbbbbb",
            name="Protected Areas",
            layer_type="raster",
            uri="https://qgis.example/ogc/wms?LAYERS=wdpa&n=2",
            style_preset="wdpa_protected_areas",
        )
        await emitter.add_loaded_layer(first)
        await emitter.add_loaded_layer(second)

        layers = emitter.loaded_layers
        assert len(layers) == 2, [(l.layer_id, l.uri) for l in layers]
        ids = {l.layer_id for l in layers}
        assert ids == {first.layer_id, second.layer_id}


# --------------------------------------------------------------------------- #
# z-index-fix - every appended layer carries a STABLE, MONOTONIC z_index, and
# an in-place re-publish REUSES the superseded layer's slot (no renumbering).
# --------------------------------------------------------------------------- #


class TestStableMonotonicZIndex:
    def _distinct_layer(self, n: int) -> LayerURI:
        """A genuinely-distinct layer (own COG -> own identity key) so the
        dedup-by-identity rule appends rather than merges."""
        return LayerURI(
            layer_id=f"layer-{n}",
            name=f"Layer {n}",
            layer_type="raster",
            uri=(
                "https://titiler.example/cog/tiles/{z}/{x}/{y}.png"
                f"?url=s3://runs/RUN/dem_{n}.tif"
            ),
            style_preset="continuous_dem",
        )

    @pytest.mark.asyncio
    async def test_three_appends_get_increasing_distinct_z_index(self) -> None:
        emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
        for n in (1, 2, 3):
            await emitter.add_loaded_layer(self._distinct_layer(n))

        layers = emitter.loaded_layers
        assert len(layers) == 3, [(l.layer_id, l.z_index) for l in layers]
        zs = [l.z_index for l in layers]
        # Every layer carries a real z_index (no more all-``None`` column).
        assert all(z is not None for z in zs), zs
        # Strictly increasing -> distinct AND monotonic (top = highest).
        assert zs == sorted(zs)
        assert len(set(zs)) == 3, zs
        # First-added is the lowest slot; last-added is the top of the stack.
        by_id = {l.layer_id: l.z_index for l in layers}
        assert by_id["layer-1"] < by_id["layer-2"] < by_id["layer-3"]

    @pytest.mark.asyncio
    async def test_in_place_replace_reuses_z_index(self) -> None:
        emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
        for n in (1, 2, 3):
            await emitter.add_loaded_layer(self._distinct_layer(n))

        before = {l.layer_id: l.z_index for l in emitter.loaded_layers}
        z2_before = before["layer-2"]

        # Re-publish layer 2 (SAME underlying COG -> same identity key) under a
        # DIFFERENT display URL + a different layer_id. This collides on the COG
        # identity and REPLACES the existing row in place.
        republish = LayerURI(
            layer_id="layer-2-restyled",
            name="Layer 2 (restyled)",
            layer_type="raster",
            uri=(
                "https://titiler.example/cog/tiles/{z}/{x}/{y}.png"
                "?url=s3://runs/RUN/dem_2.tif&colormap_name=viridis"
            ),
            style_preset="continuous_dem",
        )
        await emitter.add_loaded_layer(republish)

        layers = emitter.loaded_layers
        # Still three rows - the re-publish merged into layer 2's slot.
        assert len(layers) == 3, [(l.layer_id, l.z_index) for l in layers]
        by_id = {l.layer_id: l.z_index for l in layers}
        assert "layer-2" not in by_id  # superseded id is gone
        # The re-publish REUSES the superseded slot - it does NOT jump to the top
        # and does NOT renumber any sibling.
        assert by_id["layer-2-restyled"] == z2_before
        assert by_id["layer-1"] == before["layer-1"]
        assert by_id["layer-3"] == before["layer-3"]

    @pytest.mark.asyncio
    async def test_reset_resumes_counter_past_seeded_z(self) -> None:
        """A Case reopen seeds from persisted layers carrying z_index; the next
        append must take a FRESH slot above every seeded one (no collision)."""
        emitter = PipelineEmitter(session_id=new_ulid(), sink=_Sink())
        emitter.reset_loaded_layers(
            [
                {
                    "layer_id": "seed-a",
                    "name": "Seed A",
                    "layer_type": "raster",
                    "uri": "https://qgis.example/ogc/wms?LAYERS=a",
                    "style_preset": "continuous_dem",
                    "visible": True,
                    "role": "context",
                    "temporal": False,
                    "z_index": 5,
                },
                {
                    "layer_id": "seed-b",
                    "name": "Seed B",
                    "layer_type": "raster",
                    "uri": "https://qgis.example/ogc/wms?LAYERS=b",
                    "style_preset": "continuous_dem",
                    "visible": True,
                    "role": "context",
                    "temporal": False,
                    "z_index": 9,
                },
            ]
        )
        await emitter.add_loaded_layer(self._distinct_layer(1))

        by_id = {l.layer_id: l.z_index for l in emitter.loaded_layers}
        # The new layer's z_index is strictly above the max seeded slot (9).
        assert by_id["layer-1"] == 10, by_id
        # Seeded slots are preserved untouched.
        assert by_id["seed-a"] == 5
        assert by_id["seed-b"] == 9
