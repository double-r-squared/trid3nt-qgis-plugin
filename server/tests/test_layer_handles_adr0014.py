"""ADR 0014 — the LLM passes handles, never URIs (Lane S implementation).

Covers the four server-side mechanisms:

1. HANDLE MINT — the registry mints short per-case handles (``L1``, ``L2``,
   ...) the moment a record gains a data URI; monotonic per case; both
   directions (handle -> uri, uri -> handle) resolvable; the ``{L<n>: uri}``
   map export/import round-trips through the Case persistence seam so a
   reconnect/reopen resolves the SAME handles.
2. EMIT REWRITE — ``rewrite_result_for_llm`` swaps registered URI faces
   (data COG + WMS/tile display URL) for short handles in the LLM-facing
   function_response ONLY; the plugin-bound LayerURI emission
   (``emit_layer_uri``) keeps the real uri.
3. DISPATCH RESOLVE — ``resolve_params`` resolves ``L<n>``
   (case-insensitive, zero-pad tolerant) and dual-accepts verbatim
   registered URIs; UNKNOWN short handles and unregistered object-store
   URIs reject typed (``URI_HANDLE_UNRESOLVED``) with the handle-inventory
   hint; ``code_exec_request.layer_refs`` values (including list-valued
   refs) resolve the same way.
4. PERSISTENCE — ``Persistence.set/get_case_layer_handles`` store the map
   as a storage-only ``layer_handles`` field on the cases doc that
   ``upsert_case`` never clobbers and ``CaseSummary`` never carries.

Plus one end-to-end drive of ``_stream_gemini_reply`` (fake Gemini, real
emit seam) proving the function_response the model reads carries ``L<n>``
while the raw uri never reaches it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field as dc_field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_server.layer_uri_emit import emit_layer_uri
from trid3nt_server.uri_registry import (
    SHORT_HANDLE_RE,
    SessionUriRegistry,
    UriResolutionError,
    get_uri_registry,
    reset_uri_registries_for_tests,
)

COG_A = "s3://trid3nt-runs/01KX00000000000000000000A1/flood_depth_peak.tif"
COG_B = "s3://trid3nt-runs/01KX00000000000000000000B2/hillshade.tif"
FRAME_COG = "s3://trid3nt-runs/01KX00000000000000000000A1/depth_frame_07.tif"
TILE_FACE_A = (
    "https://tiles.example.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    "?url=s3%3A%2F%2Ftrid3nt-runs%2F01KX00000000000000000000A1"
    "%2Fflood_depth_peak.tif&rescale=0%2C2"
)


@pytest.fixture(autouse=True)
def _clean_store():
    reset_uri_registries_for_tests()
    yield
    reset_uri_registries_for_tests()


def make_registry(session_id: str = "sess-adr14") -> SessionUriRegistry:
    return get_uri_registry(session_id)


def _layer(layer_id: str, uri: str, layer_type: str = "raster") -> LayerURI:
    return LayerURI(
        layer_id=layer_id,
        name=layer_id,
        layer_type=layer_type,  # type: ignore[arg-type]
        uri=uri,
        style_preset="",
    )


# --------------------------------------------------------------------------- #
# 1. HANDLE MINT
# --------------------------------------------------------------------------- #


class TestHandleMint:
    def test_mint_is_monotonic_per_registration_order(self) -> None:
        reg = make_registry()
        reg.register_tool_result("run_model_flood_scenario", _layer("flood-a", COG_A))
        reg.register_tool_result("compute_hillshade", _layer("hill-b", COG_B))
        assert reg.short_for_uri(COG_A) == "L1"
        assert reg.short_for_uri(COG_B) == "L2"
        assert reg.uri_for_short("L1") == COG_A
        assert reg.uri_for_short("L2") == COG_B

    def test_mint_is_idempotent_per_uri(self) -> None:
        reg = make_registry()
        reg.record("flood-a", uri=COG_A, tool_name="publish_layer")
        reg.record("flood-a", uri=COG_A, tool_name="publish_layer")
        assert reg.short_for_uri(COG_A) == "L1"
        assert reg.export_short_handles() == {"L1": COG_A}

    def test_bare_object_store_strings_mint_too(self) -> None:
        """Run-frame / artifact URIs (bare s3 strings in results) get handles
        so the emit rewrite can hide them from the LLM."""
        reg = make_registry()
        reg.register_tool_result(
            "list_run_frames", {"frames": [{"uri": FRAME_COG, "t": 7}]}
        )
        short = reg.short_for_uri(FRAME_COG)
        assert short is not None and SHORT_HANDLE_RE.match(short)
        out = reg.resolve_params("t", {"raster_uri": short})
        assert out["raster_uri"] == FRAME_COG

    def test_display_face_maps_to_the_data_uris_handle(self) -> None:
        reg = make_registry()
        reg.record("flood-a", uri=COG_A, wms_url=TILE_FACE_A, tool_name="publish_layer")
        assert reg.short_for_uri(TILE_FACE_A) == reg.short_for_uri(COG_A) == "L1"

    def test_export_import_round_trip_resolves_same_handles(self) -> None:
        """The persist round-trip: reopen restores the SAME L<n> numbers and
        the counter resumes PAST the persisted maximum."""
        reg = make_registry("sess-old")
        reg.register_tool_result("run_model_flood_scenario", _layer("flood-a", COG_A))
        reg.register_tool_result("compute_hillshade", _layer("hill-b", COG_B))
        exported = reg.export_short_handles()
        assert exported == {"L1": COG_A, "L2": COG_B}

        fresh = get_uri_registry("sess-reconnect")
        fresh.replace_from_layers(
            [
                {"layer_id": "flood-a", "uri": COG_A},
                {"layer_id": "hill-b", "uri": COG_B},
            ],
            short_handles=exported,
        )
        # Same handles resolve to the same URIs...
        assert fresh.resolve_params("t", {"raster_uri": "L1"})["raster_uri"] == COG_A
        assert fresh.resolve_params("t", {"raster_uri": "L2"})["raster_uri"] == COG_B
        # ...the import is not marked dirty (it just came FROM persistence)...
        assert fresh.shorts_dirty is False
        # ...and a NEW layer mints past the persisted maximum.
        fresh.record("new-layer", uri="s3://trid3nt-runs/01KXNEW/new.tif")
        assert fresh.short_for_uri("s3://trid3nt-runs/01KXNEW/new.tif") == "L3"
        assert fresh.shorts_dirty is True

    def test_case_switch_clears_shorts(self) -> None:
        reg = make_registry()
        reg.record("flood-a", uri=COG_A)
        assert reg.short_for_uri(COG_A) == "L1"
        reg.replace_from_layers([{"layer_id": "hill-b", "uri": COG_B}])
        # Case B's first layer is L1 again (per-case counter), and Case A's
        # uri no longer has a handle.
        assert reg.short_for_uri(COG_B) == "L1"
        assert reg.short_for_uri(COG_A) is None

    def test_zero_padded_and_lowercase_import_normalizes(self) -> None:
        reg = make_registry()
        reg.import_short_handles({"L07": COG_A})
        assert reg.uri_for_short("l7") == COG_A
        reg.record("next", uri=COG_B)
        assert reg.short_for_uri(COG_B) == "L8"  # counter resumed past 7


# --------------------------------------------------------------------------- #
# 2. EMIT REWRITE (LLM face only; plugin envelope untouched)
# --------------------------------------------------------------------------- #


class TestEmitRewrite:
    def test_exact_uri_values_become_short_handles(self) -> None:
        reg = make_registry()
        reg.register_tool_result("run_model_flood_scenario", _layer("flood-a", COG_A))
        summary = {
            "tool": "run_model_flood_scenario",
            "status": "ok",
            "layer": {"layer_id": "flood-a", "uri": COG_A, "name": "Flood depth"},
        }
        rewritten = reg.rewrite_result_for_llm(summary)
        assert rewritten["layer"]["uri"] == "L1"
        assert rewritten["layer"]["name"] == "Flood depth"
        # The input summary is NOT mutated.
        assert summary["layer"]["uri"] == COG_A

    def test_uri_embedded_in_a_message_string_is_replaced(self) -> None:
        reg = make_registry()
        reg.record("flood-a", uri=COG_A)
        msg = f"Published the peak-depth layer at {COG_A} for the AOI."
        out = reg.rewrite_result_for_llm({"message": msg})
        assert COG_A not in out["message"]
        assert "L1" in out["message"]

    def test_display_face_rewrites_to_the_same_handle(self) -> None:
        reg = make_registry()
        reg.record("flood-a", uri=COG_A, wms_url=TILE_FACE_A)
        out = reg.rewrite_result_for_llm({"uri": TILE_FACE_A})
        assert out["uri"] == "L1"

    def test_unregistered_strings_pass_through(self) -> None:
        reg = make_registry()
        reg.record("flood-a", uri=COG_A)
        external = "https://waterdata.usgs.gov/monitoring-location/02323500/"
        out = reg.rewrite_result_for_llm(
            {"citation": external, "note": "no layer refs here"}
        )
        assert out == {"citation": external, "note": "no layer refs here"}

    def test_plugin_envelope_keeps_the_real_uri(self) -> None:
        """The divergence contract: the LLM face shows L<n>; the LayerURI the
        plugin renders from (the emit_layer_uri seam) keeps the raw uri."""
        reg = make_registry()
        layer = _layer("flood-a", COG_A)
        reg.register_tool_result("run_model_flood_scenario", layer)
        # LLM face:
        assert reg.rewrite_result_for_llm({"uri": COG_A})["uri"] == "L1"
        # Plugin face (raster + s3:// passes the guardrail untouched):
        emitted = emit_layer_uri(layer)
        assert emitted is not None
        assert emitted.uri == COG_A

    def test_rewrite_never_raises_on_pathological_input(self) -> None:
        reg = make_registry()
        reg.record("flood-a", uri=COG_A)
        cyc: dict[str, Any] = {}
        cyc["self"] = cyc  # depth cap absorbs the cycle
        out = reg.rewrite_result_for_llm(cyc)
        assert isinstance(out, dict)


# --------------------------------------------------------------------------- #
# 3. DISPATCH RESOLVE (dual-accept + typed rejects + layer_refs)
# --------------------------------------------------------------------------- #


class TestDispatchResolve:
    def _reg(self) -> SessionUriRegistry:
        reg = make_registry()
        reg.register_tool_result("run_model_flood_scenario", _layer("flood-a", COG_A))
        return reg

    def test_short_handle_resolves(self) -> None:
        reg = self._reg()
        out = reg.resolve_params("publish_layer", {"layer_uri": "L1"})
        assert out["layer_uri"] == COG_A

    @pytest.mark.parametrize("form", ["l1", " L1 ", "L01"])
    def test_short_handle_case_and_padding_tolerant(self, form: str) -> None:
        reg = self._reg()
        out = reg.resolve_params("publish_layer", {"layer_uri": form})
        assert out["layer_uri"] == COG_A

    def test_verbatim_registered_uri_dual_accepts(self) -> None:
        reg = self._reg()
        out = reg.resolve_params("publish_layer", {"layer_uri": COG_A})
        assert out["layer_uri"] == COG_A

    def test_layer_id_handle_still_resolves(self) -> None:
        reg = self._reg()
        out = reg.resolve_params("publish_layer", {"layer_uri": "flood-a"})
        assert out["layer_uri"] == COG_A

    def test_unknown_short_handle_rejects_typed_with_inventory(self) -> None:
        reg = self._reg()
        with pytest.raises(UriResolutionError) as exc_info:
            reg.resolve_params("publish_layer", {"layer_uri": "L99"})
        err = exc_info.value
        assert err.error_code == "URI_HANDLE_UNRESOLVED"
        assert err.retryable is True
        # The inventory names BOTH the short handle and the layer_id.
        msg = str(err)
        assert "L1" in msg and "flood-a" in msg

    def test_unregistered_object_store_uri_rejects_typed(self) -> None:
        reg = self._reg()
        with pytest.raises(UriResolutionError):
            reg.resolve_params(
                "publish_layer",
                {"layer_uri": "s3://trid3nt-runs/01KXNOPE/never_made.tif"},
            )

    def test_code_exec_layer_refs_values_resolve(self) -> None:
        reg = self._reg()
        reg.register_tool_result(
            "list_run_frames", {"frames": [{"uri": FRAME_COG}]}
        )
        frame_short = reg.short_for_uri(FRAME_COG)
        assert frame_short is not None
        out = reg.resolve_params(
            "code_exec_request",
            {
                "python_code": "result = peak.read(1).max()",
                "layer_refs": {
                    "peak": "L1",  # short handle
                    "frame": FRAME_COG,  # verbatim registered uri
                    "frames": [frame_short, FRAME_COG],  # list-valued ref
                },
            },
        )
        refs = out["layer_refs"]
        assert refs["peak"] == COG_A
        assert refs["frame"] == FRAME_COG
        assert refs["frames"] == [FRAME_COG, FRAME_COG]
        # python_code is never touched.
        assert out["python_code"] == "result = peak.read(1).max()"

    def test_code_exec_layer_refs_unknown_handle_rejects_typed(self) -> None:
        reg = self._reg()
        with pytest.raises(UriResolutionError) as exc_info:
            reg.resolve_params(
                "code_exec_request",
                {"layer_refs": {"peak": "L42"}},
            )
        assert "layer_refs[peak]" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 4. PERSISTENCE — storage-only field on the cases doc
# --------------------------------------------------------------------------- #


def _mk_case(case_id: str):
    from datetime import datetime, timezone

    from trid3nt_contracts.case import CaseSummary

    now = datetime.now(timezone.utc)
    return CaseSummary(
        case_id=case_id, title="ADR-0014 case", created_at=now, updated_at=now
    )


@pytest.mark.asyncio
async def test_persistence_layer_handles_round_trip(tmp_path) -> None:
    from trid3nt_server.persistence import FileMCPClient, Persistence

    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case_id = new_ulid()
    case = _mk_case(case_id)
    await p.upsert_case(case)

    handles = {"L1": COG_A, "L2": COG_B}
    await p.set_case_layer_handles(case_id, handles)
    assert await p.get_case_layer_handles(case_id) == handles

    # upsert_case ($set of named fields) must NOT clobber the storage-only map.
    await p.upsert_case(case.model_copy(update={"title": "renamed"}))
    assert await p.get_case_layer_handles(case_id) == handles

    # The wire CaseSummary never carries it (contract stays narrow).
    read_back = await p.get_case(case_id)
    assert read_back is not None
    assert "layer_handles" not in read_back.model_dump()


@pytest.mark.asyncio
async def test_persistence_layer_handles_missing_case_is_noop(tmp_path) -> None:
    from trid3nt_server.persistence import FileMCPClient, Persistence

    p = Persistence(FileMCPClient(base_dir=tmp_path))
    ghost = new_ulid()
    # upsert=False: never resurrects a deleted/never-created Case.
    await p.set_case_layer_handles(ghost, {"L1": COG_A})
    assert await p.get_case(ghost) is None
    assert await p.get_case_layer_handles(ghost) is None


@pytest.mark.asyncio
async def test_server_persist_and_seed_helpers_round_trip(tmp_path) -> None:
    """_persist_case_layer_handles writes the dirty map; _seed_registry_for_case
    restores it on a FRESH session (the reconnect/reopen path)."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.persistence import FileMCPClient, Persistence
    from trid3nt_server.server import SessionState

    p = Persistence(FileMCPClient(base_dir=tmp_path))
    case_id = new_ulid()
    await p.upsert_case(_mk_case(case_id))

    with patch.object(agent_server, "_PERSISTENCE", p):
        state = SessionState(session_id=new_ulid())
        reg = get_uri_registry(state.session_id)
        reg.register_tool_result(
            "run_model_flood_scenario", _layer("flood-a", COG_A)
        )
        assert reg.shorts_dirty is True
        await agent_server._persist_case_layer_handles(state, case_id=case_id)
        assert reg.shorts_dirty is False
        assert await p.get_case_layer_handles(case_id) == {"L1": COG_A}

        # A brand-new session (reconnect) seeds from the Case and resolves
        # the SAME short handle.
        state2 = SessionState(session_id=new_ulid())
        await agent_server._seed_registry_for_case(
            state2, case_id, [{"layer_id": "flood-a", "uri": COG_A}]
        )
        reg2 = get_uri_registry(state2.session_id)
        assert (
            reg2.resolve_params("t", {"raster_uri": "L1"})["raster_uri"] == COG_A
        )


# --------------------------------------------------------------------------- #
# 5. End-to-end: the function_response the model reads shows L<n>, never the
#    raw uri (fake Gemini, REAL emit seam in _stream_gemini_reply).
# --------------------------------------------------------------------------- #


def _make_fake_chunk_with_function_call(name: str, args: dict, call_id: str = "c1"):
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    fake_part = MagicMock()
    fake_part.function_call = fn_call
    fake_part.text = None
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


def _make_fake_chunk_with_text(text: str):
    fake_part = MagicMock()
    fake_part.function_call = None
    fake_part.text = text
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


@dataclass
class _FakeSocket:
    sent: list[str] = dc_field(default_factory=list)

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


def _function_response_payloads(contents_per_turn):
    out = []
    for contents in contents_per_turn:
        for content in contents:
            for part in content.parts:
                fr = getattr(part, "function_response", None)
                if fr is not None and not isinstance(fr, MagicMock):
                    out.append((fr.name, dict(fr.response)))
    return out


@pytest.mark.asyncio
async def test_emit_seam_llm_sees_handle_not_uri() -> None:
    from trid3nt_server import server as agent_server
    from trid3nt_server.adapter import GeminiSettings
    from trid3nt_server.main import _import_tools_registry
    from trid3nt_server.server import SessionState

    _import_tools_registry()

    async def _fake_invoke(_ws, state, name, args):
        result = {"layer_id": "flood-a", "uri": COG_A, "name": "Flood depth"}
        agent_server.get_uri_registry(state.session_id).register_tool_result(
            name, result
        )
        return result

    turn_chunks = [
        [
            _make_fake_chunk_with_function_call(
                "run_model_flood_scenario",
                {"location_query": "Cedar Rapids, Iowa"},
                "call-flood",
            )
        ],
        [_make_fake_chunk_with_text("Done.")],
    ]
    turn_responses = iter([iter(chunks) for chunks in turn_chunks])
    contents_per_turn: list[list[Any]] = []

    def _capture_and_stream(**kwargs):
        contents_per_turn.append(list(kwargs["contents"]))
        return next(turn_responses)

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = _capture_and_stream

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="test", location="us-central1",
        use_vertex=True,
    )
    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Model a flood for Cedar Rapids", "research",
        )

    payloads = _function_response_payloads(contents_per_turn)
    assert payloads, "no function_response reached the second turn"
    _name, payload = payloads[0]
    flat = json.dumps(payload)
    # The raw uri NEVER reaches the LLM; the short handle does.
    assert COG_A not in flat
    assert payload["result"]["uri"] == "L1"
    # The announcement maps layer name -> short handle (no raw URIs).
    assert payload.get("layer_handles") == {"flood-a": "L1"}
    note = payload.get("layer_handles_note", "")
    assert "NOT visible on the user's map" in note
    assert "Do NOT construct or echo gs:// paths" in note
