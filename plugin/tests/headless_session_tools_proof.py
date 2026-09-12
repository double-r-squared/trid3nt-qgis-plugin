"""Live proof: the two session tools through the daemon and this headless session.

The REAL plugin client connects to the running daemon, a DEM is fetched into a
throwaway case, and this process - a headless QgsApplication standing in for
the user's session - answers every processing-request the way the dock does:
``native:slope`` runs over the fetched DEM by its canvas name and the summary
rides back; ``run_pyqgis`` is refused when the card is denied and runs when it
is approved. Evidence lands as JSON at ``argv[1]``."""

from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer  # noqa: E402

from plugin.net import trid3nt_client as tc  # noqa: E402
from plugin.render.layers import configure_store_access  # noqa: E402
from plugin.render.processing import run_processing_request  # noqa: E402

WS_URL = os.environ.get("TRID3NT_WS_URL", "ws://127.0.0.1:8765/ws")
#: A small hill-country bbox the 3DEP fetch answers quickly.
BBOX = [-85.33, 35.03, -85.29, 35.06]
EVIDENCE: dict = {"steps": [], "events": []}


def _log(step_name: str, **detail) -> None:
    EVIDENCE["steps"].append({"step": step_name, **detail})
    print(f"[proof] {step_name}: {json.dumps(detail, default=str)[:400]}", flush=True)


def _drive(client, on_event, timeout_s: float) -> None:
    """Pump events until turn-complete, handing each to ``on_event``."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ev = client.next_event(timeout=1.0)
        if ev is None:
            continue
        kind = ev.kind if ev.kind != "raw" else f"raw:{ev.data.get('type')}"
        EVIDENCE["events"].append(kind)
        if kind in ("tool-io", "raw:tool-call-complete", "raw:tool-call-failed", "error"):
            EVIDENCE.setdefault("payloads", []).append({"kind": kind, "data": ev.data})
        if ev.kind == "payload-warning":
            client.confirm_payload(ev.data.get("warning_id"), "proceed", None)
            continue
        if on_event(ev):
            return
        if ev.kind == "turn-complete":
            return
    raise AssertionError(f"turn did not complete within {timeout_s}s")


def _step_of(ev, tool_name: str):
    """The pipeline step for ``tool_name`` off a pipeline event, as the dock
    renders it: a direct invocation reports its outcome on the step state."""
    if ev.kind != "pipeline":
        return None
    for step in ev.data.get("steps") or []:
        if step.tool_name == tool_name:
            return {"state": step.state, "error_message": step.error_message,
                    "duration_ms": step.duration_ms}
    return None


def main(evidence_path: str) -> int:
    QgsApplication.setPrefixPath("/usr", True)
    app = QgsApplication([], False)
    app.initQgis()
    sys.path.append(os.path.join(app.prefixPath(), "share", "qgis", "python", "plugins"))
    from processing.core.Processing import Processing

    Processing.initialize()
    note = configure_store_access(
        os.environ["AWS_ENDPOINT_URL"], os.environ["AWS_ACCESS_KEY_ID"],
        os.environ["AWS_SECRET_ACCESS_KEY"], os.environ.get("AWS_REGION", "us-east-1"),
    )
    assert note is None, note

    client = tc.AgentClient(WS_URL)
    client.connect()
    case_id = client.create_case("session-tools proof")
    _log("case", case_id=case_id)
    project = QgsProject.instance()
    try:
        # 1. A DEM fetched into the case, then opened in THIS session by name.
        fetched: dict = {}

        def on_fetch(ev) -> bool:
            step = _step_of(ev, "fetch_dem")
            if step:
                fetched.update(step)
            return False

        client.send_dev_tool_invoke("fetch_dem", {"bbox": BBOX}, raw_text="!run fetch_dem")
        _drive(client, on_fetch, 300)
        assert fetched.get("state") == "complete", fetched
        layer_row = next(
            (l for l in tc.parse_layer_events(client.last_session_state or {})
             if l.layer_type == "raster"), None)
        assert layer_row is not None, "no raster layer on the case after fetch_dem"
        vsis3 = tc.s3_to_vsis3(layer_row.uri)
        dem = QgsRasterLayer(vsis3, layer_row.name)
        assert dem.isValid(), f"could not open {vsis3}"
        project.addMapLayer(dem)
        _log("dem", name=layer_row.name, uri=layer_row.uri, width=dem.width(), height=dem.height())

        # 2. native:slope over that layer by its canvas name, run HERE.
        answered: list = []
        result: dict = {}

        def on_algorithm(ev) -> bool:
            if ev.kind == "processing-request":
                response = run_processing_request(ev.data, iface=None)
                answered.append(response)
                client.send_processing_response(**response)
            step = _step_of(ev, "run_qgis_algorithm")
            if step:
                result.update(step)
            return False

        client.send_dev_tool_invoke(
            "run_qgis_algorithm",
            {"algorithm": "native:slope", "params": {"INPUT": layer_row.name}},
            raw_text="!run run_qgis_algorithm",
        )
        _drive(client, on_algorithm, 300)
        assert answered and answered[0]["status"] == "ok", answered
        assert result.get("state") == "complete", result
        summary = answered[0]["result"]
        assert "layer_name" in summary and summary.get("kind") == "raster", summary
        names = [l.name() for l in project.mapLayers().values()]
        assert summary["layer_name"] in names, (summary, names)
        slope = project.mapLayer(summary["layer_id"])
        stats = slope.dataProvider().bandStatistics(1)
        _log("slope", step=result, summary={k: summary[k] for k in ("layer_name", "kind", "crs", "band_count", "width", "height")},
             slope_mean_deg=round(stats.mean, 3), slope_max_deg=round(stats.maximumValue, 3))

        # 3. run_pyqgis denied at the card: refused, nothing runs here.
        seen: dict = {"card": 0, "processing": 0}
        refusal: dict = {}

        def on_denied(ev) -> bool:
            if ev.kind == "code-exec-request":
                seen["card"] += 1
                client.confirm_payload(ev.data["code_exec_id"], "cancel", None)
            if ev.kind == "processing-request":
                seen["processing"] += 1
            if ev.kind == "error" and "CODE_EXEC_CANCELLED" in json.dumps(ev.data):
                refusal.update(ev.data)
            return False

        client.send_dev_tool_invoke(
            "run_pyqgis", {"code": "result = 1 + 1", "rationale": "proof"},
            raw_text="!run run_pyqgis",
        )
        _drive(client, on_denied, 120)
        assert seen["card"] == 1 and seen["processing"] == 0, seen
        assert refusal and refusal.get("retryable") is False, refusal
        assert "the code did not run" in refusal.get("message", ""), refusal
        _log("pyqgis_denied", refusal=refusal, seen=seen)

        # 4. run_pyqgis approved at the card: the code runs HERE, joined to the card.
        seen = {"card": 0, "processing": 0}
        card_id: dict = {}
        ran: list = []
        outcome: dict = {}

        def on_approved(ev) -> bool:
            if ev.kind == "code-exec-request":
                seen["card"] += 1
                card_id["id"] = ev.data["code_exec_id"]
                client.confirm_payload(ev.data["code_exec_id"], "proceed", None)
            if ev.kind == "processing-request":
                seen["processing"] += 1
                assert ev.data.get("kind") == "code" and ev.data.get("code_exec_id") == card_id["id"], ev.data
                response = run_processing_request(ev.data, iface=None)
                ran.append(response)
                client.send_processing_response(**response)
            step = _step_of(ev, "run_pyqgis")
            if step:
                outcome.update(step)
            return False

        client.send_dev_tool_invoke(
            "run_pyqgis", {"code": "result = 1 + 1", "rationale": "proof"},
            raw_text="!run run_pyqgis",
        )
        _drive(client, on_approved, 120)
        assert seen == {"card": 1, "processing": 1}, seen
        assert ran and ran[0]["status"] == "ok" and ran[0]["result"] == {"value": 2}, ran
        assert outcome.get("state") == "complete", outcome
        _log("pyqgis_approved", step=outcome, ran=ran[0])
        EVIDENCE["all_passed"] = True
        return 0
    finally:
        try:
            client.case_command("delete", case_id=case_id)
            time.sleep(1.0)
        except Exception:  # noqa: BLE001 -- cleanup never fails the proof
            pass
        client.close()
        with open(evidence_path, "w") as f:
            json.dump(EVIDENCE, f, indent=1, default=str)
        app.exitQgis()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
