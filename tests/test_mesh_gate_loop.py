"""Offline tests for the mesh gate loop.

No object store, no live session, no container: the reg_grid mesher builds in
process, a fake emitter stands in for the map, and a background driver answers
the gate card on the shared pending-confirmation spine.

Pins what the gate promises: agent tools GENERATED one per registered edit
action and mounted only while the session is open, AUTO building inline with no
card at all, and restart truncating the chain back to the DECLARED prefix
through the gate.

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
from trid3nt_server.workflows.mesh.meshers import MeshToolError, get_mesher
from trid3nt_server.workflows.mesh.session import MeshSession
from trid3nt_server.workflows.mesh.tool import tool

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


def _declaration(*, declared_resolution_m: float | None = None):
    declaration = tool.build_mesh(mesher="reg_grid", kind="structured_grid",
                                  extent=_AOI, resolution_m=400.0)
    if declared_resolution_m is not None:
        declaration = declaration.edit("set_resolution", declared_resolution_m)
    return declaration


def _session(tmp_path, **over) -> MeshSession:
    return MeshSession(_declaration(**over), workdir=tmp_path)


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
def test_no_mesh_tool_is_mounted_before_a_gate_opens():
    assert not MOUNTED_TOOLS
    assert "mesh_accept" not in TOOL_REGISTRY
    assert "mesh_edit_set_resolution" not in TOOL_REGISTRY


def test_gate_mounts_one_tool_per_registered_action(tmp_path):
    actions = set(get_mesher("reg_grid").actions)
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))

    expected = {mesh_gate.edit_tool_name(a) for a in actions} | {
        mesh_gate.ACCEPT_TOOL, mesh_gate.RESTART_TOOL}
    assert set(gate.tools) == expected
    assert expected <= set(MOUNTED_TOOLS)
    for name in expected:
        assert name in TOOL_REGISTRY
        assert TOOL_REGISTRY[name].metadata.cacheable is False

    mesh_gate.close_mesh_gate(gate)
    for name in expected:
        assert name not in TOOL_REGISTRY
        assert name not in MOUNTED_TOOLS


def test_generated_tool_signature_names_the_actions_declared_inputs(tmp_path):
    import inspect

    mesh_gate.open_mesh_gate(_session(tmp_path))
    fn = TOOL_REGISTRY[mesh_gate.edit_tool_name("set_resolution")].fn
    params = inspect.signature(fn).parameters
    assert list(params) == list(
        get_mesher("reg_grid").actions["set_resolution"].inputs)
    assert "resolution_m" in (fn.__doc__ or "")


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
# The agent lane: a mounted tool edits, re-presents, accepts.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_edit_tool_rebuilds_and_re_presents(tmp_path, monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    gate = mesh_gate.open_mesh_gate(_session(tmp_path))
    before = (await mesh_gate.present_mesh(gate))["probes"]["node_count"]

    out = await TOOL_REGISTRY[
        mesh_gate.edit_tool_name("set_resolution")].fn(resolution_m=800.0)

    assert out["probes"]["node_count"] < before
    assert out["probes"]["edits_applied"] == ["set_resolution"]
    assert out["recipe"][-1] == {"edit": "set_resolution", "resolution_m": 800.0}
    # The presentation is a MESH layer on the map, not a picture of one.
    assert fake.layers and fake.layers[-1].layer_type == "mesh"
    assert fake.layers[-1].style_preset == "mesh_wireframe"
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
    assert "set_resolution" in envelope.recommendation
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
async def test_gate_restart_truncates_to_the_declared_prefix(
        tmp_path, monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    session = _session(tmp_path, declared_resolution_m=250.0)
    driver = asyncio.create_task(_drive([
        ("narrow_scope", {"edit": "set_resolution", "resolution_m": 900.0}),
        ("narrow_scope", {"restart": True}),
        ("proceed", None),
    ]))

    art = await mesh_gate.gate_mesh_build(
        session, tool_name="telemac_river_dye", input_mode="user_gated")

    await driver
    # The DECLARED edit survives the truncation; the gate-time one does not.
    assert [e.action for e in session.chain] == ["set_resolution"]
    assert session.chain[0].inputs["resolution_m"] == 250.0
    assert art.provenance["edits"] == ["set_resolution"]
    assert len(fake.sent) == 3


@pytest.mark.asyncio
async def test_gate_refuses_a_revision_it_cannot_read(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    driver = asyncio.create_task(_drive([("narrow_scope", {"resolution_m": 900.0})]))

    with pytest.raises(MeshToolError) as excinfo:
        await mesh_gate.gate_mesh_build(
            _session(tmp_path), tool_name="telemac_river_dye",
            input_mode="user_gated")

    await driver
    assert excinfo.value.error_code == "MESH_GATE_REVISION_UNREADABLE"
    assert "set_resolution" in str(excinfo.value)
    assert not MOUNTED_TOOLS


@pytest.mark.asyncio
async def test_gate_stops_asking_after_its_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    driver = asyncio.create_task(_drive([("narrow_scope", {"restart": True})] * 2))

    with pytest.raises(MeshToolError) as excinfo:
        await mesh_gate.gate_mesh_build(
            _session(tmp_path), tool_name="telemac_river_dye",
            input_mode="user_gated", max_rounds=2)

    await driver
    assert excinfo.value.error_code == "MESH_GATE_NOT_APPROVED"


# --------------------------------------------------------------------------- #
# The revision channel the SHIPPED card can actually reach.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_card_carries_one_row_per_numeric_knob_plus_the_truncation(
        tmp_path, monkeypatch):
    """The gate's edit surface has to be a channel the client renders.

    A card the user can only proceed or cancel on makes the loop's third answer
    unreachable, and the model cannot stand in for it - it is blocked on the
    gate's own future while the card is open, so its mounted edit tools are no
    fallback. The param sheet is that channel: one editable row per numeric edit
    knob, named for the action it turns, plus the truncation row.
    """
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    driver = asyncio.create_task(_drive([("proceed", None)]))

    await mesh_gate.gate_mesh_build(
        _session(tmp_path), tool_name="telemac_river_dye",
        input_mode="user_gated")

    await driver
    _mtype, envelope = fake.sent[0]
    names = [row.name for row in envelope.param_sheet.rows]
    assert "set_resolution.resolution_m" in names
    assert "restart" in names
    # An action taking a geometry or a layer is not a knob a grid can carry, so
    # it stays on the mounted tools and off the card.
    assert not [n for n in names if n.startswith(("set_extent", "apply_layer"))]
    assert all(row.editable for row in envelope.param_sheet.rows)


@pytest.mark.asyncio
async def test_a_knob_edited_on_the_card_rebuilds_the_mesh(tmp_path, monkeypatch):
    """The client sends row name -> text; the loop turns that into the edit."""
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    session = _session(tmp_path)
    coarse = session.probes()["node_count"]
    driver = asyncio.create_task(_drive([
        ("narrow_scope", {"set_resolution.resolution_m": "900"}),
        ("proceed", None),
    ]))

    art = await mesh_gate.gate_mesh_build(
        session, tool_name="telemac_river_dye", input_mode="user_gated")

    await driver
    assert [e.action for e in session.chain] == ["set_resolution"]
    assert session.chain[0].inputs["resolution_m"] == 900.0
    assert art.node_count != coarse


@pytest.mark.asyncio
async def test_the_truncation_row_answers_yes_the_way_the_card_sends_it(
        tmp_path, monkeypatch):
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    session = _session(tmp_path, declared_resolution_m=250.0)
    driver = asyncio.create_task(_drive([
        ("narrow_scope", {"set_resolution.resolution_m": "900"}),
        ("narrow_scope", {"restart": "yes"}),
        ("proceed", None),
    ]))

    await mesh_gate.gate_mesh_build(
        session, tool_name="telemac_river_dye", input_mode="user_gated")

    await driver
    assert [e.action for e in session.chain] == ["set_resolution"]
    assert session.chain[0].inputs["resolution_m"] == 250.0


@pytest.mark.asyncio
async def test_a_knob_the_action_never_declared_refuses_by_name(
        tmp_path, monkeypatch):
    monkeypatch.setattr(pe, "current_emitter", lambda: _FakeEmitter())
    driver = asyncio.create_task(
        _drive([("narrow_scope", {"set_resolution.edge_length_m": "900"})]))

    with pytest.raises(MeshToolError) as excinfo:
        await mesh_gate.gate_mesh_build(
            _session(tmp_path), tool_name="telemac_river_dye",
            input_mode="user_gated")

    await driver
    assert excinfo.value.error_code == "MESH_GATE_REVISION_UNREADABLE"
    assert "edge_length_m" in str(excinfo.value)


def _card_rows(session) -> list[str]:
    """The gate card's row names for ``session``, built without a mesh.

    The sheet is assembled from the mesher's REGISTRY, so a mesher that only
    builds inside a container still answers what its card would carry.
    """
    sheet = mesh_gate._mesh_param_sheet(session, tool_name="build_mesh",
                                        round_idx=1, max_rounds=3)
    return [row.name for row in sheet.rows]


def test_a_vocabulary_knob_gets_a_row_and_names_its_roster(tmp_path):
    """A card that skipped every action mixing a shape with a word was restart-only.

    The library wrappers declare the open-boundary designation as a side and a
    type - two words off a roster - beside a drawn region, and skipping a whole
    action over the drawn input left their gate with nothing to answer on.
    """
    declaration = tool.build_mesh(mesher="om2d", kind="unstructured_tri",
                                  extent=_AOI, refine={"edge_length": 400.0})
    session = MeshSession(declaration, workdir=tmp_path)

    names = _card_rows(session)

    assert "set_boundary.side" in names
    assert "set_boundary.type" in names
    # The number beside a drawn region is a knob too; the region itself is not.
    assert "refine_region.edge_length" in names
    assert not [n for n in names if n.endswith((".geometry", ".layer"))]
    sheet = mesh_gate._mesh_param_sheet(session, tool_name="build_mesh",
                                        round_idx=1, max_rounds=3)
    side = next(r for r in sheet.rows if r.name == "set_boundary.side")
    for choice in get_mesher("om2d").actions["set_boundary"].inputs["side"].choices:
        assert choice in side.desc


def test_a_mesher_with_no_vocabulary_knob_keeps_the_card_it_had(tmp_path):
    """The corridor's knobs are numbers; per-input rows change nothing for it."""
    declaration = tool.build_mesh(
        mesher="corridor_tin", kind="unstructured_tri",
        domain={"reach": {"slug": "eel"}, "seed": {"lon": -117.0, "lat": 37.0}},
        extent_km=0.12, width_m=60.0, banks="nhd_area")
    session = MeshSession(declaration, workdir=tmp_path)

    assert _card_rows(session) == ["set_resolution.edge_length_m",
                                  "set_extent.extent_km", "restart"]


@pytest.mark.asyncio
async def test_a_word_off_the_declared_roster_refuses_by_name(tmp_path):
    """The roster is the declaration's; typing past it is refused, not passed on."""
    declaration = tool.build_mesh(mesher="om2d", kind="unstructured_tri",
                                  extent=_AOI)
    session = MeshSession(declaration, workdir=tmp_path)

    with pytest.raises(MeshToolError) as excinfo:
        await mesh_gate._apply_gate_revision(
            session, {"set_boundary.side": "sideways"})

    assert excinfo.value.error_code == "MESH_GATE_REVISION_UNREADABLE"
    assert "seaward" in str(excinfo.value)


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
    typed["set_resolution.resolution_m"] = "900"
    revised = client_gate.resolve_param_sheet_edits(sheet.rows, typed)
    assert revised == {"set_resolution.resolution_m": "900"}

    replayed = _session(tmp_path / "replay")
    await mesh_gate._apply_gate_revision(replayed, revised)
    assert [e.action for e in replayed.chain] == ["set_resolution"]
    assert replayed.chain[0].inputs["resolution_m"] == 900.0
