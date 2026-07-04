"""Tests for the session-scoped layer-URI registry (job-0263).

Layer-handle indirection kills the LLM-URI-mangling incident class. The
suite covers, per the kickoff:

1. registration on tool results (LayerURI models, dicts, bare gs:// strings,
   WMS URLs, composer observation hook);
2. all four resolution branches (exact pass / handle substitution / fuzzy
   mangle-match + WARNING / typed URI_HANDLE_UNRESOLVED error);
3. cross-session isolation;
4. the FIVE historical incident shapes, each replayed with the REAL logged
   values from the Stage 3 / demo evidence:

   - I1 runs/ prefix mangle      (job-0253 agent_restart_0253.log:475)
   - I2 layer_id-as-basename     (same call — assets_uri)
   - I3 hash-tail hallucination  (job-0257 report, 3/3 publishes)
   - I4 WMS-URL-as-hazard        (job-0255 agent_log_p5_turn.txt:170)
   - I5 invented cache hash      (same call — assets_uri)

5. server-seam wiring: ``_invoke_tool_via_emitter`` resolves params before
   dispatch, registers results after, and the typed error reaches Gemini as
   a structured retryable function_response listing the real handles.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from grace2_agent.uri_registry import (
    RESOLVABLE_URI_PARAMS,
    SessionUriRegistry,
    UriResolutionError,
    activate_registry,
    deactivate_registry,
    get_uri_registry,
    observe_published_layer,
    reset_uri_registries_for_tests,
)
from grace2_contracts.execution import LayerURI

# --------------------------------------------------------------------------- #
# Real logged values (verbatim from the evidence files)
# --------------------------------------------------------------------------- #

# job-0253 (Fort Myers flood -> Pelicun, session 01KTS5T50ET0FZZ1TWRMGCQTBA)
REAL_FLOOD_COG_0253 = (
    "gs://grace-2-hazard-prod-runs/01KTS5W9GTE7A7WPC3BNBE10EQ/flood_depth_peak.tif"
)
MANGLED_RUNS_PREFIX_0253 = (
    "gs://grace-2-hazard-prod-runs/runs/01KTS5W9GTE7A7WPC3BNBE10EQ/flood_depth_peak.tif"
)
REAL_NSI_FGB = (
    "gs://grace-2-hazard-prod-cache/cache/static-30d/usace_nsi/"
    "852a6cc379b18c865bf9d99ec1acaa35.fgb"
)
NSI_LAYER_ID = "usace-nsi--81.9126-26.5476--81.7511-26.6892"
MANGLED_NSI_LAYERID_BASENAME_0253 = (
    "gs://grace-2-hazard-prod-cache/cache/static-30d/usace_nsi/"
    f"{NSI_LAYER_ID}.fgb"
)

# job-0257 (hillshade demo, /tmp/agent_demo_ready.log) — 3/3 hash-tail mangles
HILLSHADE_REAL_CHICAGO = "090a4ff8d9a083f67c0b355caf40241a.tif"
HILLSHADE_MANGLED_CHICAGO_1 = "090a4ff8d9a083b28499252309d12999.tif"
HILLSHADE_MANGLED_CHICAGO_2 = "090a4ff8d9a08321a43a7a9437b0e51c.tif"
HILLSHADE_REAL_SEATTLE = "4007d642cb157d11f5db275a50286ae5.tif"
HILLSHADE_MANGLED_SEATTLE = "4007d642cb157d22b1113a4b912a2ee3.tif"
HILLSHADE_CACHE_DIR = "gs://grace-2-hazard-prod-cache/cache/static-30d/compute_hillshade"

# job-0255 (Fort Myers round 10, session 01KTS7QFMKWMKWG8V54D8GMH89)
REAL_FLOOD_COG_0255 = (
    "gs://grace-2-hazard-prod-runs/01KTS8H8RJT6311A2V4BKX6H8A/flood_depth_peak.tif"
)
FLOOD_LAYER_ID_0255 = "flood-depth-peak-01KTS8H8RJT6311A2V4BKX6H8A"
WMS_URL_0255 = (
    "https://grace-2-qgis-server-425352658356.us-central1.run.app/ogc/wms"
    "?MAP=/mnt/qgs/grace2-sample.qgs&LAYERS=flood-depth-peak-01KTS8H8RJT6311A2V4BKX6H8A"
)
MANGLED_NSI_INVENTED_HASH_0255 = (
    "gs://grace-2-hazard-prod-cache/cache/static-30d/usace_nsi/20240516140505.fgb"
)


@pytest.fixture(autouse=True)
def _clean_store():
    reset_uri_registries_for_tests()
    yield
    reset_uri_registries_for_tests()


def make_registry(session_id: str = "sess-test") -> SessionUriRegistry:
    return get_uri_registry(session_id)


# --------------------------------------------------------------------------- #
# 1. Registration
# --------------------------------------------------------------------------- #


class TestRegistration:
    def test_layeruri_model_registers_handle_and_uri(self) -> None:
        reg = make_registry()
        layer = LayerURI(
            layer_id=NSI_LAYER_ID,
            name="USACE NSI Structures",
            layer_type="vector",
            uri=REAL_NSI_FGB,
            style_preset="usace_nsi",
        )
        new = reg.register_tool_result("fetch_usace_nsi", layer)
        assert new == {NSI_LAYER_ID: REAL_NSI_FGB}
        assert reg.resolve_params("t", {"assets_uri": NSI_LAYER_ID}) == {
            "assets_uri": REAL_NSI_FGB
        }

    def test_dict_result_with_layer_id_uri_pair(self) -> None:
        reg = make_registry()
        reg.register_tool_result(
            "fetch_usace_nsi",
            {"layer_id": NSI_LAYER_ID, "uri": REAL_NSI_FGB, "feature_count": 9000},
        )
        assert reg.known_handles() == [NSI_LAYER_ID]

    def test_nested_envelope_registers_layers_and_bare_uris(self) -> None:
        reg = make_registry()
        envelope = {
            "envelope_id": "01HZZZ",
            "layers": [
                {
                    "layer_id": FLOOD_LAYER_ID_0255,
                    "uri": WMS_URL_0255,  # composer substitutes the WMS URL
                    "layer_type": "raster",
                }
            ],
            "provenance": {"data_sources": [REAL_FLOOD_COG_0255]},
        }
        reg.register_tool_result("run_model_flood_scenario", envelope)
        handles = reg.known_handles()
        assert FLOOD_LAYER_ID_0255 in handles
        # The bare COG string registered too (fuzzy-match inventory).
        assert (
            reg.resolve_params("t", {"hazard_raster_uri": REAL_FLOOD_COG_0255})[
                "hazard_raster_uri"
            ]
            == REAL_FLOOD_COG_0255
        )

    def test_wms_url_in_uri_slot_never_displaces_data_uri(self) -> None:
        reg = make_registry()
        reg.record(FLOOD_LAYER_ID_0255, uri=REAL_FLOOD_COG_0255, tool_name="publish")
        # Composer envelope re-registers the handle with the WMS display URL.
        reg.record(FLOOD_LAYER_ID_0255, uri=WMS_URL_0255, tool_name="composer")
        resolved = reg.resolve_params(
            "run_pelicun_damage_assessment",
            {"hazard_raster_uri": FLOOD_LAYER_ID_0255},
        )
        assert resolved["hazard_raster_uri"] == REAL_FLOOD_COG_0255

    def test_vsigs_normalized_to_gs(self) -> None:
        reg = make_registry()
        reg.record(
            "flood-x",
            uri="/vsigs/grace-2-hazard-prod-runs/01ABC/flood_depth_peak.tif",
        )
        assert (
            reg.resolve_params("t", {"hazard_raster_uri": "flood-x"})[
                "hazard_raster_uri"
            ]
            == "gs://grace-2-hazard-prod-runs/01ABC/flood_depth_peak.tif"
        )

    def test_observation_hook_requires_active_context(self) -> None:
        reg = make_registry()
        # No active registry — observation is a no-op (direct/test calls).
        observe_published_layer("h1", gcs_uri=REAL_FLOOD_COG_0253)
        assert reg.known_handles() == []
        token = activate_registry(reg)
        try:
            observe_published_layer(
                "flood-depth-peak-01KTS5W9GTE7A7WPC3BNBE10EQ",
                gcs_uri=REAL_FLOOD_COG_0253,
                wms_url="https://x/ogc/wms?MAP=p.qgs&LAYERS=flood-depth-peak-01KTS5W9GTE7A7WPC3BNBE10EQ",
            )
        finally:
            deactivate_registry(token)
        assert reg.known_handles() == ["flood-depth-peak-01KTS5W9GTE7A7WPC3BNBE10EQ"]

    def test_registration_never_raises_on_pathological_results(self) -> None:
        reg = make_registry()
        cyc: dict[str, Any] = {}
        cyc["self"] = cyc

        class Weird:
            def model_dump(self, mode: str = "json") -> None:
                raise RuntimeError("boom")

        reg.register_tool_result("t", cyc)
        reg.register_tool_result("t", Weird())
        reg.register_tool_result("t", None)
        reg.register_tool_result("t", 42)


# --------------------------------------------------------------------------- #
# 2. The four resolution branches
# --------------------------------------------------------------------------- #


class TestResolutionBranches:
    def test_branch1_exact_known_uri_passes_verbatim(self) -> None:
        reg = make_registry()
        reg.record(NSI_LAYER_ID, uri=REAL_NSI_FGB, tool_name="fetch_usace_nsi")
        out = reg.resolve_params("t", {"assets_uri": REAL_NSI_FGB})
        assert out["assets_uri"] == REAL_NSI_FGB

    def test_branch2_handle_substituted(self) -> None:
        reg = make_registry()
        reg.record(NSI_LAYER_ID, uri=REAL_NSI_FGB, tool_name="fetch_usace_nsi")
        out = reg.resolve_params("t", {"assets_uri": NSI_LAYER_ID})
        assert out["assets_uri"] == REAL_NSI_FGB

    def test_branch3_close_match_substituted_with_warning(self, caplog) -> None:
        reg = make_registry()
        reg.record(NSI_LAYER_ID, uri=REAL_NSI_FGB, tool_name="fetch_usace_nsi")
        with caplog.at_level("WARNING", logger="grace2_agent.uri_registry"):
            out = reg.resolve_params(
                "run_pelicun_damage_assessment",
                {"assets_uri": MANGLED_NSI_LAYERID_BASENAME_0253},
            )
        assert out["assets_uri"] == REAL_NSI_FGB
        assert any("resolved" in r.message for r in caplog.records)

    def test_branch4_unknown_managed_bucket_raises_typed_error(self) -> None:
        reg = make_registry()
        reg.record(NSI_LAYER_ID, uri=REAL_NSI_FGB, tool_name="fetch_usace_nsi")
        invented = "gs://grace-2-hazard-prod-cache/cache/static-30d/totally/made_up.tif"
        with pytest.raises(UriResolutionError) as exc_info:
            reg.resolve_params("t", {"layer_uri": invented})
        err = exc_info.value
        assert err.error_code == "URI_HANDLE_UNRESOLVED"
        assert err.retryable is True
        # The message TELLS Gemini which handles exist so it self-corrects.
        assert NSI_LAYER_ID in str(err)

    def test_branch4_empty_registry_message_says_run_producer_first(self) -> None:
        reg = make_registry()
        with pytest.raises(UriResolutionError) as exc_info:
            reg.resolve_params(
                "run_pelicun_damage_assessment",
                {"hazard_raster_uri": "gs://grace-2-hazard-prod-cache/cache/x.tif"},
            )
        assert "producing tool" in str(exc_info.value)

    def test_foreign_bucket_unknown_uri_fails_open(self) -> None:
        reg = make_registry()
        foreign = "gs://some-user-bucket/their/data.tif"
        out = reg.resolve_params("t", {"raster_uri": foreign})
        assert out["raster_uri"] == foreign

    def test_non_uri_params_and_non_strings_untouched(self) -> None:
        reg = make_registry()
        params = {"bbox": [1, 2, 3, 4], "style_preset": "blues", "layer_uri": 7}
        assert reg.resolve_params("t", params) == params

    def test_ambiguous_hash_prefix_refuses_to_guess(self) -> None:
        reg = make_registry()
        # Two cache keys sharing the same 14-char prefix — substitution would
        # be a coin flip; branch 4 (error + inventory) is the honest answer.
        a = f"{HILLSHADE_CACHE_DIR}/090a4ff8d9a083aaaaaaaaaaaaaaaaaa.tif"
        b = f"{HILLSHADE_CACHE_DIR}/090a4ff8d9a083bbbbbbbbbbbbbbbbbb.tif"
        reg.record("hillshade-a", uri=a)
        reg.record("hillshade-b", uri=b)
        with pytest.raises(UriResolutionError):
            reg.resolve_params(
                "publish_layer",
                {"layer_uri": f"{HILLSHADE_CACHE_DIR}/090a4ff8d9a083cccccccccccccccccc.tif"},
            )

    def test_handle_with_only_wms_face_raises_instead_of_handing_display_url(
        self,
    ) -> None:
        reg = make_registry()
        reg.record(FLOOD_LAYER_ID_0255, wms_url=WMS_URL_0255)
        with pytest.raises(UriResolutionError):
            reg.resolve_params(
                "run_pelicun_damage_assessment",
                {"hazard_raster_uri": FLOOD_LAYER_ID_0255},
            )


# --------------------------------------------------------------------------- #
# 2b. TiTiler tile-template DISPLAY URL recovery (D1 — the live SWMM run, case
#     01KVH4MZ9JF7GGHQ88D5PSWZVH: compute_layer_bounds UNKNOWN_LAYER_URI when the
#     LLM passes a frame's display tile URL to a *_uri param instead of the COG).
# --------------------------------------------------------------------------- #

# A SWMM depth-frame COG + its TiTiler display template (the `url=` param is the
# COG, URL-encoded, exactly as publish_layer.py:1763 builds it).
SWMM_FRAME_COG = (
    "s3://grace2-hazard-runs/01KVHKWETM1QMXH059QZGXP4V6/swmm_depth_frame_01.tif"
)
SWMM_FRAME_LAYER_ID = "swmm-depth-frame-01-01KVHKWETM1QMXH059QZGXP4V6"
SWMM_FRAME_TILE_TEMPLATE = (
    "https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    "?url=s3%3A%2F%2Fgrace2-hazard-runs%2F01KVHKWETM1QMXH059QZGXP4V6"
    "%2Fswmm_depth_frame_01.tif&rescale=0%2C2&colormap_name=blues"
)


class TestTitilerTemplateRecovery:
    def test_titiler_display_template_resolves_to_registered_cog(self) -> None:
        """The live failure: the LLM grabs a frame's display tile URL from
        loaded_layers and hands it to compute_layer_bounds(layer_uri=...).
        The resolver must recover the registered COG, not fail open."""
        reg = make_registry()
        # publish_layer registers both faces (observe_published_layer):
        reg.record(
            SWMM_FRAME_LAYER_ID,
            uri=SWMM_FRAME_COG,
            wms_url=SWMM_FRAME_TILE_TEMPLATE,
            tool_name="publish_layer",
        )
        out = reg.resolve_params(
            "compute_layer_bounds", {"layer_uri": SWMM_FRAME_TILE_TEMPLATE}
        )
        assert out["layer_uri"] == SWMM_FRAME_COG

    def test_titiler_template_unregistered_cog_returns_embedded_cog(self) -> None:
        """Even with NO registered record, the embedded `url=` COG is the real
        object key, so it is recovered verbatim (honest, deterministic)."""
        reg = make_registry()
        out = reg.resolve_params(
            "compute_layer_bounds", {"layer_uri": SWMM_FRAME_TILE_TEMPLATE}
        )
        assert out["layer_uri"] == SWMM_FRAME_COG

    def test_titiler_template_with_no_url_param_raises_typed_error(self) -> None:
        """A tile template lacking a recoverable `url=` COG raises the typed,
        retryable URI_HANDLE_UNRESOLVED so the LLM self-corrects."""
        reg = make_registry()
        reg.record(SWMM_FRAME_LAYER_ID, uri=SWMM_FRAME_COG)
        bad = "https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/3/2/1.png"
        with pytest.raises(UriResolutionError):
            reg.resolve_params("compute_layer_bounds", {"layer_uri": bad})

    def test_titiler_template_does_not_disturb_handle_path(self) -> None:
        """The already-working path (LLM passes the layer_id handle) still
        resolves directly — the new branch is additive."""
        reg = make_registry()
        reg.record(
            SWMM_FRAME_LAYER_ID,
            uri=SWMM_FRAME_COG,
            wms_url=SWMM_FRAME_TILE_TEMPLATE,
        )
        out = reg.resolve_params(
            "compute_layer_bounds", {"layer_uri": SWMM_FRAME_LAYER_ID}
        )
        assert out["layer_uri"] == SWMM_FRAME_COG


# --------------------------------------------------------------------------- #
# 3. Cross-session isolation
# --------------------------------------------------------------------------- #


class TestSessionIsolation:
    def test_handles_do_not_leak_across_sessions(self) -> None:
        reg_a = get_uri_registry("session-A")
        reg_b = get_uri_registry("session-B")
        reg_a.record(NSI_LAYER_ID, uri=REAL_NSI_FGB)
        # Session B never produced the layer: handle unresolved -> typed error.
        with pytest.raises(UriResolutionError):
            reg_b.resolve_params("t", {"assets_uri": MANGLED_NSI_LAYERID_BASENAME_0253})
        # And the same registry object comes back for the same session id
        # (reconnect-survival: the store is keyed by session_id).
        assert get_uri_registry("session-A") is reg_a
        assert get_uri_registry("session-A").known_handles() == [NSI_LAYER_ID]

    def test_bare_handle_in_other_session_unresolved(self) -> None:
        get_uri_registry("session-A").record(NSI_LAYER_ID, uri=REAL_NSI_FGB)
        reg_b = get_uri_registry("session-B")
        # A non-URI handle string that session B doesn't know just passes
        # through (fail-open for non-URI-shaped strings) — the tool's own
        # 404/typed error then feeds the retry loop.
        out = reg_b.resolve_params("t", {"assets_uri": "some-handle-from-elsewhere"})
        assert out["assets_uri"] == "some-handle-from-elsewhere"


# --------------------------------------------------------------------------- #
# 4. The five historical incidents — real logged values
# --------------------------------------------------------------------------- #


class TestHistoricalIncidents:
    def test_i1_runs_prefix_mangle_job0253(self) -> None:
        """gs://...-runs/runs/<ULID>/flood_depth_peak.tif -> the real COG."""
        reg = make_registry()
        token = activate_registry(reg)
        try:
            observe_published_layer(
                "flood-depth-peak-01KTS5W9GTE7A7WPC3BNBE10EQ",
                gcs_uri=REAL_FLOOD_COG_0253,
            )
        finally:
            deactivate_registry(token)
        # A SECOND flood run in the same session must not confuse the
        # tie-break (shared-path-segment overlap picks the right run ULID).
        reg.record(
            FLOOD_LAYER_ID_0255,
            uri=REAL_FLOOD_COG_0255,
            tool_name="publish_layer",
        )
        out = reg.resolve_params(
            "run_pelicun_damage_assessment",
            {"hazard_raster_uri": MANGLED_RUNS_PREFIX_0253},
        )
        assert out["hazard_raster_uri"] == REAL_FLOOD_COG_0253

    def test_i2_nsi_layer_id_as_basename_job0253(self) -> None:
        """assets_uri carrying <layer_id>.fgb in the cache dir -> real hash."""
        reg = make_registry()
        reg.register_tool_result(
            "fetch_usace_nsi",
            LayerURI(
                layer_id=NSI_LAYER_ID,
                name="USACE NSI Structures",
                layer_type="vector",
                uri=REAL_NSI_FGB,
                style_preset="usace_nsi",
            ),
        )
        out = reg.resolve_params(
            "run_pelicun_damage_assessment",
            {"assets_uri": MANGLED_NSI_LAYERID_BASENAME_0253},
        )
        assert out["assets_uri"] == REAL_NSI_FGB

    @pytest.mark.parametrize(
        ("real_base", "mangled_base"),
        [
            (HILLSHADE_REAL_CHICAGO, HILLSHADE_MANGLED_CHICAGO_1),
            (HILLSHADE_REAL_SEATTLE, HILLSHADE_MANGLED_SEATTLE),
            (HILLSHADE_REAL_CHICAGO, HILLSHADE_MANGLED_CHICAGO_2),
        ],
    )
    def test_i3_hash_tail_hallucination_x3_job0257(
        self, real_base: str, mangled_base: str
    ) -> None:
        """All three live hash-tail mangles resolve to the real cache object."""
        reg = make_registry()
        reg.register_tool_result(
            "compute_hillshade",
            LayerURI(
                layer_id=f"hillshade-{real_base.split('.')[0][:8]}-standard",
                name="Hillshade",
                layer_type="raster",
                uri=f"{HILLSHADE_CACHE_DIR}/{real_base}",
                style_preset="hillshade_standard",
            ),
        )
        # Both runs' rasters present (chicago + seattle) — prefix match must
        # still pick the right one.
        other = (
            HILLSHADE_REAL_SEATTLE
            if real_base == HILLSHADE_REAL_CHICAGO
            else HILLSHADE_REAL_CHICAGO
        )
        reg.record("hillshade-other", uri=f"{HILLSHADE_CACHE_DIR}/{other}")
        out = reg.resolve_params(
            "publish_layer",
            {"layer_uri": f"{HILLSHADE_CACHE_DIR}/{mangled_base}"},
        )
        assert out["layer_uri"] == f"{HILLSHADE_CACHE_DIR}/{real_base}"

    def test_i4_wms_url_as_hazard_job0255(self) -> None:
        """The QGIS display URL passed as hazard_raster_uri -> the gs:// COG."""
        reg = make_registry()
        # publish_layer (inside the composer) observed both faces:
        token = activate_registry(reg)
        try:
            observe_published_layer(
                FLOOD_LAYER_ID_0255,
                gcs_uri=REAL_FLOOD_COG_0255,
                wms_url=WMS_URL_0255,
            )
        finally:
            deactivate_registry(token)
        # ...then the composer's envelope re-registered the WMS URL face.
        reg.register_tool_result(
            "run_model_flood_scenario",
            {
                "layers": [
                    {
                        "layer_id": FLOOD_LAYER_ID_0255,
                        "uri": WMS_URL_0255,
                        "layer_type": "raster",
                    }
                ]
            },
        )
        # The NSI fetch ran earlier in the live session (its uri was correct
        # in the logged call) — register it as the session did.
        reg.record(NSI_LAYER_ID, uri=REAL_NSI_FGB, tool_name="fetch_usace_nsi")
        out = reg.resolve_params(
            "run_pelicun_damage_assessment",
            {
                "hazard_raster_uri": WMS_URL_0255,  # exact value from the log
                "assets_uri": REAL_NSI_FGB,
            },
        )
        assert out["hazard_raster_uri"] == REAL_FLOOD_COG_0255

    def test_i5_invented_cache_hash_job0255(self) -> None:
        """The timestamp-shaped invented .fgb basename -> unique same-dir match."""
        reg = make_registry()
        reg.register_tool_result(
            "fetch_usace_nsi",
            LayerURI(
                layer_id=NSI_LAYER_ID,
                name="USACE NSI Structures",
                layer_type="vector",
                uri=REAL_NSI_FGB,
                style_preset="usace_nsi",
            ),
        )
        out = reg.resolve_params(
            "run_pelicun_damage_assessment",
            {"assets_uri": MANGLED_NSI_INVENTED_HASH_0255},
        )
        assert out["assets_uri"] == REAL_NSI_FGB

    def test_i5_invented_hash_ambiguous_dir_errors_with_handles(self) -> None:
        """Two NSI fetches in the dir -> same-dir match is ambiguous -> error."""
        reg = make_registry()
        reg.record(NSI_LAYER_ID, uri=REAL_NSI_FGB, tool_name="fetch_usace_nsi")
        reg.record(
            "usace-nsi-tampa",
            uri=(
                "gs://grace-2-hazard-prod-cache/cache/static-30d/usace_nsi/"
                "ffffffffffffffffffffffffffffffff.fgb"
            ),
            tool_name="fetch_usace_nsi",
        )
        with pytest.raises(UriResolutionError) as exc_info:
            reg.resolve_params(
                "run_pelicun_damage_assessment",
                {"assets_uri": MANGLED_NSI_INVENTED_HASH_0255},
            )
        msg = str(exc_info.value)
        assert NSI_LAYER_ID in msg and "usace-nsi-tampa" in msg


# --------------------------------------------------------------------------- #
# 5. Server-seam wiring (_invoke_tool_via_emitter)
# --------------------------------------------------------------------------- #


class MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        self.sent.append(json.loads(raw))


@pytest.fixture()
def _dummy_uri_tool():
    """Register two dummy tools: a producer (returns LayerURI) + a consumer."""
    from grace2_contracts.tool_registry import AtomicToolMetadata
    from grace2_agent.tools import TOOL_REGISTRY, RegisteredTool

    captured: dict[str, Any] = {}

    def produce_layer(**kwargs: Any) -> LayerURI:
        return LayerURI(
            layer_id=NSI_LAYER_ID,
            name="USACE NSI Structures",
            layer_type="vector",
            uri=REAL_NSI_FGB,
            style_preset="usace_nsi",
        )

    def consume_layer(assets_uri: str, **kwargs: Any) -> dict:
        captured["assets_uri"] = assets_uri
        return {"ok": True}

    saved = dict(TOOL_REGISTRY)
    for fn, name in ((produce_layer, "produce_layer_t"), (consume_layer, "consume_layer_t")):
        TOOL_REGISTRY[name] = RegisteredTool(
            fn=fn,
            metadata=AtomicToolMetadata(
                name=name,
                ttl_class="live-no-cache",
                source_class="api_fetch",
                cacheable=False,
            ),
            module=__name__,
        )
    try:
        yield captured
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(saved)


def test_invoke_seam_registers_then_resolves(_dummy_uri_tool) -> None:
    """End-to-end through the real dispatch seam: produce -> mangle -> consume."""
    from grace2_agent.server import SessionState, _invoke_tool_via_emitter

    from grace2_contracts.common import new_ulid

    async def run() -> None:
        ws = MockWebSocket()
        state = SessionState(session_id=new_ulid())
        # 1. Producer result registers the handle.
        await _invoke_tool_via_emitter(ws, state, "produce_layer_t", {})
        # 2. Consumer dispatched with incident-I2's mangled assets_uri — the
        #    seam must hand the REAL fgb to the tool body.
        await _invoke_tool_via_emitter(
            ws,
            state,
            "consume_layer_t",
            {"assets_uri": MANGLED_NSI_LAYERID_BASENAME_0253},
        )
        assert _dummy_uri_tool["assets_uri"] == REAL_NSI_FGB

    asyncio.run(run())


def test_invoke_seam_unresolved_raises_typed_error(_dummy_uri_tool) -> None:
    """Branch 4 propagates as a typed retryable error the loop summarizes."""
    from grace2_agent.adapter import summarize_tool_result
    from grace2_agent.server import SessionState, _invoke_tool_via_emitter

    from grace2_contracts.common import new_ulid

    async def run() -> None:
        ws = MockWebSocket()
        state = SessionState(session_id=new_ulid())
        # F97: the dispatch mints a UNIQUE layer_id for the produced layer, so the
        # registered handle the unresolved-error must list is that MINTED id (not
        # the tool's source-derived NSI_LAYER_ID). Capture it from the result.
        produced = await _invoke_tool_via_emitter(ws, state, "produce_layer_t", {})
        assert isinstance(produced, LayerURI)
        minted_handle = produced.layer_id
        assert minted_handle != NSI_LAYER_ID  # the mint actually replaced the id
        with pytest.raises(UriResolutionError) as exc_info:
            await _invoke_tool_via_emitter(
                ws,
                state,
                "consume_layer_t",
                {
                    "assets_uri": (
                        "gs://grace-2-hazard-prod-cache/cache/static-30d/x/invented.fgb"
                    )
                },
            )
        summary = summarize_tool_result("consume_layer_t", None, error=exc_info.value)
        assert summary["status"] == "error"
        assert summary["error_code"] == "URI_HANDLE_UNRESOLVED"
        assert summary["retryable"] is True
        # The error lists the session's known handle so the LLM self-corrects.
        assert minted_handle in summary["message"]

    asyncio.run(run())


def test_resolvable_param_allowlist_excludes_server_owned_params() -> None:
    """project_qgs_uri (server-injected) and output destinations never resolve."""
    assert "project_qgs_uri" not in RESOLVABLE_URI_PARAMS
    assert "output_uri" not in RESOLVABLE_URI_PARAMS
    # The headline consumers from the 5 incidents ARE covered.
    for name in ("hazard_raster_uri", "assets_uri", "layer_uri"):
        assert name in RESOLVABLE_URI_PARAMS
