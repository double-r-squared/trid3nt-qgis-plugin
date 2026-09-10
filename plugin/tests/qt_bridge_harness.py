"""Regression harness for the REAL Qt bridge start path.

Run as a SUBPROCESS by its wrapper test, which needs ``qgis.PyQt`` and skips
honestly when the interpreter lacks it. A pyqtSignal named for a C++ virtual
shadows it, so the first delivered QEvent aborts the host process - only a real
Qt object tree catches that class of bug. ``argv[1]`` is the stub agent's url."""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402

from plugin.net.ws_bridge import AgentBridge  # noqa: E402


def main() -> int:
    url = sys.argv[1]
    app = QCoreApplication([])  # noqa: F841 -- the event loop Qt needs
    bridge = AgentBridge()

    seen = {"connected": False, "case": None, "turn": False, "kinds": []}
    bridge.connected.connect(lambda _u, _a: seen.__setitem__("connected", True))
    bridge.case_ready.connect(lambda cid: seen.__setitem__("case", cid))
    bridge.failed.connect(lambda m: print("FAILED:", m, flush=True))

    def on_event(kind: str, _data: object) -> None:
        seen["kinds"].append(kind)
        if kind == "turn-complete":
            seen["turn"] = True

    bridge.agent_event.connect(on_event)

    # The statement the shadowed-signal bug aborted on (QThread(self) ->
    # ChildAdded QEvent -> QObject.event lookup).
    bridge.start(url)

    def pump(seconds: float, until) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline and not until():
            QCoreApplication.processEvents()
            time.sleep(0.02)

    pump(20, lambda: seen["case"] is not None)
    assert seen["connected"], "connected signal never fired"
    assert seen["case"], "case_ready signal never fired"

    # One full chat round trip through the cross-thread queued signals.
    bridge.send_chat("hello from the qt harness")
    pump(20, lambda: seen["turn"])
    assert seen["turn"], f"no turn-complete; kinds={seen['kinds']}"
    assert "chunk" in seen["kinds"], seen["kinds"]
    assert "session-state" in seen["kinds"], seen["kinds"]

    bridge.stop()
    print(
        f"QT-BRIDGE-OK case={seen['case']} kinds={len(seen['kinds'])}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
