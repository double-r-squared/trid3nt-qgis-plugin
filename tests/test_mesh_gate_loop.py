"""Offline tests for the mesh gate loop.

No object store, no live session, no container: the reg_grid mesher builds in
process, a fake emitter stands in for the map, and a background driver answers
the gate card on the shared pending-confirmation spine.

Pins what the gate promises: the three loop tools mounted only while a session is
open, ONE generic card path for every mesher, AUTO building inline with no card
at all, ``mesh_op`` as the whole runtime edit surface, and reset putting the
recipe back to the declaration through the gate.

ASCII only.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload
from trid3nt_server.emission import pipeline_emitter as pe
from trid3nt_server.gates import pending
from trid3nt_server.tools import MOUNTED_TOOLS, TOOL_REGISTRY, mount_tool
from trid3nt_server.workflows.mesh import gate as mesh_gate
from trid3nt_server.workflows.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.mesh.meshers import MeshToolError
from trid3nt_server.workflows.mesh.session import MeshSession
from trid3nt_server.workflows.mesh.tool import mesh_op, tool

_AOI = (-83.50, 35.00, -83.40, 35.09)


class _FakeEmitter:
    """Minimal emitter: records envelopes, layers and map commands."""

    def __init__(self, session_id: str = "sess-mesh-gate") -> None:
        self.session_id = session_id
        self.sent: list[tuple[str, object]] = []
        self.layers: list[object] = []
        self.commands: list[tuple[str, object]] = []

    async def send_envelope(self, message_type: str, payload: object) -> None:
        self.sent.append((message_type, payload))

    async def add_loaded_layer(self, layer: object) -> None:
        self.layers.append(layer)

    async def emit_map_command(self, name: str, payload: object) -> None:
        self.commands.append((name, payload))


def _recipe(**over):
    ask = {"mesher": "reg_grid", "kind": "structured_grid", "extent": _AOI,
           "resolution_m": 400.0}
    ask.update(over)
    return tool.build_mesh(**ask)


def _session(tmp_path, **over) -> MeshSession:
    return MeshSession(_recipe(**over), workdir=tmp_path)


async def _drive(script, *, seen=None, appear_timeout=5.0) -> None:
    """Answer each fresh gate card in turn with the next scripted decision."""
    seen = seen if seen is not None else set()
    for decision, revised in script:
        for _ in range(int(appear_timeout / 0.005)):
            fresh = [(wid, fut)
                     for wid, (_sess, fut) in pending._PENDING_CONFIRMATIONS.items()
                     if wid not in seen and not fut.done()]
            if fresh:
                wid, fut = fresh[0]
                seen.add(wid)
                fut.set_result(PayloadConfirmationEnvelopePayload(
                    warning_id=wid, decision=decision, revised_args=revised))
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("no fresh pending confirmation appeared")


@pytest.fixture(autouse=True)
def _closed_gates(monkeypatch):
    """Every test starts and ends with no gate open and nothing mounted."""
    monkeypatch.delenv("TRID3NT_INPUT_GATE_MODE", raising=False)
    for open_gate in mesh_gate.open_mesh_gates():
        mesh_gate.close_mesh_gate(open_gate)
    yield
    for open_gate in mesh_gate.open_mesh_gates():
        mesh_gate.close_mesh_gate(open_gate)
    assert not MOUNTED_TOOLS


# --------------------------------------------------------------------------- #
# Mount / unmount lifecycle.
# --------------------------------------------------------------------------- #
def test_no_gate_tool_is_mounted_before_a_gate_opens():
    assert not MOUNTED_TOOLS
    assert "mesh_accept" not in TOOL_REGISTRY
    assert "mesh_reset" not in TOOL_REGISTRY


def test_the_gate_mounts_the_three_loop_tools_and_no_per_mesher_ones(tmp_path):
    """Every mesher gets the SAME gate: nothing here knows what a library does."""
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))

    expected = {mesh_gate.ACCEPT_TOOL, mesh_gate.RESET_TOOL, mesh_gate.ADOPT_TOOL}
    assert set(gate.tools) == expected
    assert expected <= set(MOUNTED_TOOLS)
    for name in expected:
        assert name in TOOL_REGISTRY
        assert TOOL_REGISTRY[name].metadata.cacheable is False

    mesh_gate.close_mesh_gate(gate)
    for name in expected:
        assert name not in TOOL_REGISTRY
        assert name not in MOUNTED_TOOLS


def test_mesh_op_is_registered_rather_than_mounted_per_mesher():
    """ONE atomic tool: it is in the catalog whether or not a gate is open, and
    it says so when nothing is under construction."""
    assert "mesh_op" in TOOL_REGISTRY
    with pytest.raises(MeshToolError) as excinfo:
        asyncio.run(TOOL_REGISTRY["mesh_op"].fn(fn="laplacian2"))
    assert excinfo.value.error_code == "MESH_NO_ACTIVE_SESSION"


def test_a_mounted_tool_refuses_once_its_gate_is_closed(tmp_path):
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))
    fn = TOOL_REGISTRY[mesh_gate.ACCEPT_TOOL].fn
    mesh_gate.close_mesh_gate(gate)
    with pytest.raises(MeshToolError) as excinfo:
        asyncio.run(fn())
    assert excinfo.value.error_code == "MESH_SESSION_CLOSED"


def test_opening_a_gate_supersedes_the_previous_one(tmp_path):
    first = mesh_gate.open_mesh_gate(_session(tmp_path / "one"))
    second = mesh_gate.open_mesh_gate(_session(tmp_path / "two"))
    assert mesh_gate.open_mesh_gates() == (second,)
    assert first.tools == ()
    assert set(second.tools) <= set(MOUNTED_TOOLS)
    mesh_gate.close_mesh_gate(second)


def test_a_mounted_tool_never_shadows_a_registered_one(tmp_path):
    from trid3nt_contracts.tool_registry import AtomicToolMetadata
    from trid3nt_server.tools import ToolRegistrationError

    with pytest.raises(ToolRegistrationError):
        mount_tool(AtomicToolMetadata(name="build_mesh",
                                      ttl_class="live-no-cache",
                                      cacheable=False), lambda: None)
    assert "build_mesh" not in MOUNTED_TOOLS


def test_mounted_tools_ride_the_retrieval_floor(tmp_path):
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    gate = mesh_gate.open_mesh_gate(_session(tmp_path))
    visible = retrieve_visible_tools("what is the weather", None, 8)
    assert set(gate.tools) <= visible
    mesh_gate.close_mesh_gate(gate)
    assert not set(gate.tools) & retrieve_visible_tools(
        "what is the weather", None, 8)


# --------------------------------------------------------------------------- #
# The agent lane: mesh_op edits the RECIPE, regenerates, re-presents.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mesh_op_appends_regenerates_and_re_presents(tmp_path, monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))
    await mesh_gate.present_mesh(gate)

    out = await TOOL_REGISTRY["mesh_op"].fn(fn="set_boundary_roles")

    assert out["ops"] == ["0: mesh_op('set_boundary_roles')"]
    assert out["recipe"]["ops"] == [{"op": "set_boundary_roles"}]
    # The presentation is a MESH layer on the map, not a picture of one.
    assert fake.layers and fake.layers[-1].layer_type == "mesh"
    assert fake.layers[-1].style_preset == "mesh_wireframe"
    mesh_gate.close_mesh_gate(gate)


@pytest.mark.asyncio
async def test_mesh_op_alters_and_removes_by_index(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))

    await TOOL_REGISTRY["mesh_op"].fn(fn="set_boundary_roles")
    await TOOL_REGISTRY["mesh_op"].fn(fn="set_boundary_roles", at=0)
    out = await TOOL_REGISTRY["mesh_op"].fn(at=0, remove=True)

    assert out["ops"] == []
    assert gate.session.recipe.ops == ()
    mesh_gate.close_mesh_gate(gate)


@pytest.mark.asyncio
async def test_mesh_op_refuses_an_unknown_name_with_the_nearest_matches(
        tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))

    with pytest.raises(MeshToolError) as excinfo:
        await TOOL_REGISTRY["mesh_op"].fn(fn="set_bedd")

    assert excinfo.value.error_code == "MESH_OP_UNKNOWN"
    assert "set_bed" in str(excinfo.value)
    assert gate.session.recipe.ops == ()
    mesh_gate.close_mesh_gate(gate)


@pytest.mark.asyncio
async def test_removing_without_an_index_names_the_numbered_recipe(
        tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))
    await TOOL_REGISTRY["mesh_op"].fn(fn="set_boundary_roles")

    with pytest.raises(MeshToolError) as excinfo:
        await TOOL_REGISTRY["mesh_op"].fn(remove=True)

    assert excinfo.value.error_code == "MESH_OP_INDEX"
    assert "0: mesh_op('set_boundary_roles')" in str(excinfo.value)
    mesh_gate.close_mesh_gate(gate)


def test_mesh_op_surfaces_in_top8():
    """The runtime refinement loop must be reachable from its own phrasings."""
    from pathlib import Path

    import yaml

    import trid3nt_server.tools as t
    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    dd._get_index()
    corpus_path = (Path(t.__file__).resolve().parents[1] / "workflows" / "mesh"
                   / "corpus.yaml")
    queries = (yaml.safe_load(corpus_path.read_text()) or {})["mesh_op"]
    assert queries
    assert any("mesh_op" in retrieve_visible_tools(q, None, 8) for q in queries), (
        "mesh_op surfaces in NO top-8 for any of its corpus queries")


@pytest.mark.asyncio
async def test_reset_tool_puts_the_recipe_back_and_re_presents(
        tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))
    await TOOL_REGISTRY["mesh_op"].fn(fn="set_boundary_roles")

    out = await TOOL_REGISTRY[mesh_gate.RESET_TOOL].fn()

    assert out["ops"] == []
    assert gate.session.recipe == gate.session.declared
    mesh_gate.close_mesh_gate(gate)


@pytest.mark.asyncio
async def test_accept_tool_freezes_the_mesh_and_unmounts(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))
    names = gate.tools

    summary = await TOOL_REGISTRY[mesh_gate.ACCEPT_TOOL].fn()

    assert summary["mesher"] == "reg_grid"
    assert summary["element_count"] > 0
    assert isinstance(gate.accepted, MeshArtifact)
    assert not MOUNTED_TOOLS
    assert all(name not in TOOL_REGISTRY for name in names)


@pytest.mark.asyncio
async def test_adopting_a_hand_edited_layer_flags_the_mesh(tmp_path, monkeypatch):
    import numpy as np

    from trid3nt_server.emission.mesh_display import write_2dm
    from trid3nt_server.workflows.mesh.meshers import Mesh

    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    edited = tmp_path / "edited.2dm"
    edited.write_text(write_2dm(Mesh(
        points=np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]),
        cells=np.array([[0, 1, 2], [1, 3, 2]]), crs_authid="EPSG:4326")))
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))

    out = await TOOL_REGISTRY[mesh_gate.ADOPT_TOOL].fn(layer=str(edited))

    assert out["probes"]["element_count"] == 2
    assert "regen" in out["regen_note"]
    mesh_gate.close_mesh_gate(gate)


# --------------------------------------------------------------------------- #
# AUTO builds inline: no card, no mounted tools.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_auto_mode_builds_inline_with_no_gate(tmp_path, monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)

    art = await mesh_gate.gate_mesh_build(
        _session(tmp_path), tool_name="telemac_river_dye", input_mode="auto")

    assert isinstance(art, MeshArtifact)
    assert fake.sent == []
    assert not MOUNTED_TOOLS
    assert mesh_gate.open_mesh_gates() == ()


@pytest.mark.asyncio
async def test_user_gated_with_no_session_builds_inline(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: None)

    art = await mesh_gate.gate_mesh_build(
        _session(tmp_path), tool_name="telemac_river_dye",
        input_mode="user_gated")

    assert isinstance(art, MeshArtifact)
    assert not MOUNTED_TOOLS


@pytest.mark.asyncio
async def test_session_lever_turns_the_gate_on(tmp_path, monkeypatch):
    monkeypatch.setenv("TRID3NT_INPUT_GATE_MODE", "user_gated")
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    driver = asyncio.create_task(_drive([("proceed", None)]))

    art = await mesh_gate.gate_mesh_build(
        _session(tmp_path), tool_name="telemac_river_dye", input_mode=None)

    await driver
    assert isinstance(art, MeshArtifact)
    assert [m for m, _ in fake.sent] == ["tool-payload-warning"]


# --------------------------------------------------------------------------- #
# The demanded lane: present, decide, accept.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gate_presents_probes_then_accepts(tmp_path, monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    driver = asyncio.create_task(_drive([("proceed", None)]))

    art = await mesh_gate.gate_mesh_build(
        _session(tmp_path), tool_name="telemac_river_dye",
        input_mode="user_gated")

    await driver
    assert isinstance(art, MeshArtifact)
    _mtype, envelope = fake.sent[0]
    assert envelope.options == ["proceed", "cancel", "narrow_scope"]
    assert "nodes" in envelope.recommendation
    assert "mesh_op" in envelope.recommendation
    assert envelope.tool_args["mesh_id"]
    assert fake.layers  # the editable mesh layer went to the map
    # The session closed behind the accept.
    assert not MOUNTED_TOOLS
    assert not pending._PENDING_CONFIRMATIONS


@pytest.mark.asyncio
async def test_gate_cancel_refuses_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    driver = asyncio.create_task(_drive([("cancel", None)]))

    with pytest.raises(MeshToolError) as excinfo:
        await mesh_gate.gate_mesh_build(
            _session(tmp_path), tool_name="telemac_river_dye",
            input_mode="user_gated")

    await driver
    assert excinfo.value.error_code == "MESH_GATE_DECLINED"
    assert not MOUNTED_TOOLS


@pytest.mark.asyncio
async def test_gate_reset_puts_the_recipe_back_to_the_declaration(
        tmp_path, monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    session = _session(tmp_path)
    driver = asyncio.create_task(_drive([
        ("narrow_scope", {"resolution_m": 900.0}),
        ("narrow_scope", {"reset": True}),
        ("proceed", None),
    ]))

    art = await mesh_gate.gate_mesh_build(
        session, tool_name="telemac_river_dye", input_mode="user_gated")

    await driver
    assert session.recipe == session.declared
    assert art.provenance["recipe"]["resolution_m"] == 400.0
    assert len(fake.sent) == 3


@pytest.mark.asyncio
async def test_gate_refuses_a_revision_it_cannot_read(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    driver = asyncio.create_task(_drive([("narrow_scope", {"gradation": 0.2})]))

    with pytest.raises(MeshToolError) as excinfo:
        await mesh_gate.gate_mesh_build(
            _session(tmp_path), tool_name="telemac_river_dye",
            input_mode="user_gated")

    await driver
    assert excinfo.value.error_code == "MESH_GATE_REVISION_UNREADABLE"
    assert "mesh_op" in str(excinfo.value)
    assert not MOUNTED_TOOLS


@pytest.mark.asyncio
async def test_gate_stops_asking_after_its_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    driver = asyncio.create_task(_drive([("narrow_scope", {"reset": True})] * 2))

    with pytest.raises(MeshToolError) as excinfo:
        await mesh_gate.gate_mesh_build(
            _session(tmp_path), tool_name="telemac_river_dye",
            input_mode="user_gated", max_rounds=2)

    await driver
    assert excinfo.value.error_code == "MESH_GATE_NOT_APPROVED"


# --------------------------------------------------------------------------- #
# ONE card path: the agnostic params, the numbered recipe, the revert.
# --------------------------------------------------------------------------- #
def _card_rows(session) -> list[str]:
    """The gate card's row names for ``session``, built without a mesh.

    The sheet is assembled from the RECIPE, so a mesher that only builds inside a
    container still answers what its card would carry.
    """
    sheet = mesh_gate._mesh_param_sheet(session, tool_name="build_mesh",
                                        round_idx=1, max_rounds=3)
    return [row.name for row in sheet.rows]


def test_every_mesher_gets_the_same_card(tmp_path):
    """GENERALITY: a lattice and a triangulation render through one path, and
    nothing on the card is a name any particular library knows."""
    lattice = MeshSession(_recipe(), workdir=tmp_path / "grid")
    assert _card_rows(lattice) == ["resolution_m", "reset"]

    triangulated = MeshSession(
        tool.build_mesh(mesher="om2d", kind="unstructured_tri", extent=_AOI,
                        resolution_m=60.0,
                        ops=[mesh_op("laplacian2"),
                             mesh_op("set_bed", source="fetch_topobathy")]),
        workdir=tmp_path / "tri")
    assert _card_rows(triangulated) == [
        "resolution_m", "op[0]", "op[1]", "reset"]


def test_no_mesher_has_card_code_of_its_own():
    """The sweep guard, as source rather than as intent.

    Two files carry a mesh gate card: the one that ASSEMBLES it and the one the
    dock RENDERS it with. Neither may name a mesher - a card that knows one
    library's name is the first branch, and the second is the per-mesher card
    path this loop exists to not have.
    """
    import pathlib

    from trid3nt_server.workflows.mesh.meshers import registered_meshers

    repo = pathlib.Path(__file__).resolve().parents[1]
    offenders = {
        f"{path.name}: {name}"
        for path in (pathlib.Path(mesh_gate.__file__), repo / "plugin/ui/gate.py")
        for name in registered_meshers()
        if name in path.read_text()
    }
    assert not offenders, (
        "a mesh gate card names a mesher, so it is no longer ONE card path for "
        f"every mesher: {sorted(offenders)}")


def test_the_ops_are_numbered_on_the_card_because_an_index_is_what_targets_one(
        tmp_path):
    session = MeshSession(
        tool.build_mesh(mesher="om2d", kind="unstructured_tri", extent=_AOI,
                        resolution_m=60.0,
                        ops=[mesh_op("enforce_mesh_gradation", gradation=0.2)]),
        workdir=tmp_path)
    sheet = mesh_gate._mesh_param_sheet(session, tool_name="build_mesh",
                                        round_idx=1, max_rounds=3)
    row = next(r for r in sheet.rows if r.name == "op[0]")
    assert "enforce_mesh_gradation" in row.value
    assert row.user_lever is False
    assert "mesh_op" in row.desc


@pytest.mark.asyncio
async def test_the_one_size_word_is_the_row_a_card_can_move(tmp_path, monkeypatch):
    """The client sends row name -> text; the loop turns that into a rebuild."""
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    session = _session(tmp_path)
    coarse = session.probes()["node_count"]
    driver = asyncio.create_task(_drive([
        ("narrow_scope", {"resolution_m": "900"}),
        ("proceed", None),
    ]))

    art = await mesh_gate.gate_mesh_build(
        session, tool_name="telemac_river_dye", input_mode="user_gated")

    await driver
    assert session.recipe.resolution_m == 900.0
    assert art.node_count != coarse


@pytest.mark.asyncio
async def test_a_row_that_is_not_a_number_refuses_naming_the_row(tmp_path):
    session = _session(tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        await mesh_gate._apply_gate_revision(session, {"resolution_m": "coarse"})
    assert excinfo.value.error_code == "MESH_GATE_REVISION_UNREADABLE"
    assert "resolution_m" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_revert_row_answers_yes_the_way_the_card_sends_it(
        tmp_path, monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    session = _session(tmp_path)
    driver = asyncio.create_task(_drive([
        ("narrow_scope", {"resolution_m": "900"}),
        ("narrow_scope", {"reset": "yes"}),
        ("proceed", None),
    ]))

    await mesh_gate.gate_mesh_build(
        session, tool_name="telemac_river_dye", input_mode="user_gated")

    await driver
    assert session.recipe.resolution_m == 400.0


@pytest.mark.asyncio
async def test_the_shipped_client_parses_the_card_and_its_reply_routes_home(
        tmp_path, monkeypatch):
    """The reachability check, end to end through the CLIENT's own parser.

    The plugin's gate helpers are pure python, so the card the server emits is
    parsed here by the exact code the dock runs, its editors are typed into, and
    what it would send back is fed to the loop - which is the only proof that the
    channel is reachable rather than merely present on the envelope.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from plugin.ui import gate as client_gate

    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    session = _session(tmp_path)
    driver = asyncio.create_task(_drive([("proceed", None)]))
    await mesh_gate.gate_mesh_build(
        session, tool_name="telemac_river_dye", input_mode="user_gated")
    await driver

    _mtype, envelope = fake.sent[0]
    payload = envelope.model_dump(mode="json")
    sheet = client_gate.parse_param_sheet(payload)
    assert sheet is not None, "the shipped client renders no card for this envelope"

    typed = {row.name: row.display() for row in sheet.rows}
    typed["resolution_m"] = "900"
    revised = client_gate.resolve_param_sheet_edits(sheet.rows, typed)
    assert revised == {"resolution_m": "900"}

    replayed = _session(tmp_path / "replay")
    await mesh_gate._apply_gate_revision(replayed, revised)
    assert replayed.recipe.resolution_m == 900.0
