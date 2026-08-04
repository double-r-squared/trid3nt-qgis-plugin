"""Guard: every forwarded Qt signal pair in the plugin's ``ws_bridge.py`` has
MATCHING signatures (worker signal -> bridge signal).

The live bug this locks out (commit 650e575): ``AgentBridge.connected`` was
declared ``pyqtSignal(str, bool)`` while the worker emitted
``pyqtSignal(str, bool, str, str)``. Qt silently DROPS the extra args when a
2-arg slot is connected to a 4-arg signal, so the advertised ``http_base`` /
``data_base`` never reached the dock and every tailnet client fell back to
localhost layer fetches -- with no error anywhere.

This test parses ``ws_bridge.py`` with ``ast`` (no Qt / no ``qgis.PyQt``
import needed, so it runs in the offline server suite): it reads the
``pyqtSignal(...)`` declarations of ``AgentWorker`` and ``AgentBridge`` and the
``self._worker.<x>.connect(self.<y>)`` wiring in ``AgentBridge.start``, then
asserts each connected pair's argument-type lists are identical -- and that
``connected`` is specifically the 4-arg ``(str, bool, str, str)``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_WS_BRIDGE = (
    Path(__file__).resolve().parents[2]
    / "qgis-plugin"
    / "trid3nt"
    / "net"
    / "ws_bridge.py"
)

# The forwarded signals wired in AgentBridge.start (worker -> bridge). Sanity
# floor: the wiring parse below must recover at least these, so a refactor that
# silently drops the connect wiring is caught too.
_EXPECTED_FORWARDED = {
    "connected",
    "case_ready",
    "agent_event",
    "failed",
    "closed",
    "reconnecting",
    "resumed",
    "auth_expired",
}


def _signal_signatures(class_node: ast.ClassDef) -> dict[str, tuple[str, ...]]:
    """Map ``name -> (arg-type, ...)`` for every ``name = pyqtSignal(...)``
    class-level assignment. Positional args only (the type list); a ``name=``
    kwarg overload label is ignored."""
    sigs: dict[str, tuple[str, ...]] = {}
    for node in class_node.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        call = node.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "pyqtSignal"
        ):
            sigs[target.id] = tuple(ast.unparse(a) for a in call.args)
    return sigs


def _forwarded_pairs(start_fn: ast.FunctionDef) -> list[tuple[str, str]]:
    """Recover ``(worker_signal, bridge_signal)`` from every
    ``self._worker.<a>.connect(self.<b>)`` call in ``start``. Only the
    signal->signal forwards are returned; ``.connect(self._thread.quit)`` and
    ``.connect(self._worker.run)`` (slot connects, not forwards) are skipped."""
    pairs: list[tuple[str, str]] = []
    for node in ast.walk(start_fn):
        if not (isinstance(node, ast.Call) and _is_attr(node.func, "connect")):
            continue
        emitter = node.func.value  # self._worker.<a>
        if not (
            isinstance(emitter, ast.Attribute)
            and isinstance(emitter.value, ast.Attribute)
            and emitter.value.attr == "_worker"
        ):
            continue
        if len(node.args) != 1:
            continue
        slot = node.args[0]  # want self.<b>
        if not (
            isinstance(slot, ast.Attribute)
            and isinstance(slot.value, ast.Name)
            and slot.value.id == "self"
        ):
            continue
        pairs.append((emitter.attr, slot.attr))
    return pairs


def _is_attr(node: ast.AST, attr: str) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == attr


def _class(module: ast.Module, name: str) -> ast.ClassDef:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found in ws_bridge.py")


def _method(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"method {name} not found on {class_node.name}")


@pytest.fixture(scope="module")
def parsed():
    module = ast.parse(_WS_BRIDGE.read_text(encoding="utf-8"))
    worker = _class(module, "AgentWorker")
    bridge = _class(module, "AgentBridge")
    return {
        "worker_sigs": _signal_signatures(worker),
        "bridge_sigs": _signal_signatures(bridge),
        "pairs": _forwarded_pairs(_method(bridge, "start")),
    }


def test_forwarded_wiring_recovered(parsed):
    forwarded_worker_names = {a for a, _ in parsed["pairs"]}
    missing = _EXPECTED_FORWARDED - forwarded_worker_names
    assert not missing, f"forwarding wiring not found for: {sorted(missing)}"


def test_every_forwarded_pair_has_matching_signature(parsed):
    worker_sigs = parsed["worker_sigs"]
    bridge_sigs = parsed["bridge_sigs"]
    mismatches = []
    for worker_name, bridge_name in parsed["pairs"]:
        assert worker_name in worker_sigs, f"worker signal {worker_name} not declared"
        assert bridge_name in bridge_sigs, f"bridge signal {bridge_name} not declared"
        if worker_sigs[worker_name] != bridge_sigs[bridge_name]:
            mismatches.append(
                f"{worker_name}{worker_sigs[worker_name]} -> "
                f"{bridge_name}{bridge_sigs[bridge_name]}"
            )
    assert not mismatches, "signal signature drift (Qt would drop args): " + "; ".join(
        mismatches
    )


def test_connected_is_four_arg(parsed):
    # The exact 650e575 regression: connected MUST carry
    # (user_id, is_anonymous, http_base, data_base) on BOTH sides.
    expected = ("str", "bool", "str", "str")
    assert parsed["worker_sigs"].get("connected") == expected
    assert parsed["bridge_sigs"].get("connected") == expected
