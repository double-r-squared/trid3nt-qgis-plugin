"""Offscreen harness for the persistent per-case bbox.

Run as a SUBPROCESS by its wrapper, which needs a real ``QgsMapCanvas`` and
``QgsRubberBand`` and skips honestly when absent. No agent, no network: the
bridge is a recorder so outbound case-commands can be inspected, and the extent
tool's handler is invoked directly, covering everything downstream of the drag."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.gui import QgsMapCanvas  # noqa: E402

# Never touch the real QGIS profile's QSettings from this harness.
QCoreApplication.setOrganizationName("trid3nt-case-bbox-harness")
QCoreApplication.setApplicationName("trid3nt-case-bbox-harness")

qgs = QgsApplication([], False)
qgs.initQgis()

from plugin.ui.dock import Trid3ntDock  # noqa: E402


class RecBridge:
    """Records outbound verbs the dock issues; reports itself as running."""

    def __init__(self) -> None:
        self.calls: list = []
        self.running = True

    def case_command(self, command, case_id=None, args=None) -> None:
        self.calls.append((command, case_id, args))

    def select_case(self, case_id) -> None:
        self.calls.append(("select", case_id, None))

    def send_chat(
        self, text, show_thinking=False, model_id="", aoi_bbox=None,
        tool_choice_mode="",
    ) -> None:
        # record the STRUCTURED per-message AOI exactly as sent.
        # (grew the bridge surface with tool_choice_mode -- accepted
        # here so the recorder mirrors the real AgentBridge signature; this
        # harness asserts the AOI leg only.)
        self.calls.append(("chat", text, aoi_bbox))

    def stop(self) -> None:
        self.calls.append(("stop", None, None))


class FakeIface:
    """Headed iface backed by a real offscreen 3857 canvas."""

    def __init__(self, canvas) -> None:
        self._canvas = canvas

    def mapCanvas(self):
        return self._canvas

    def activeLayer(self):
        return None

    # The layer-tree push action registration is best-effort (try/except in
    # the dock), so these can raise -- headless has no real layer tree.
    def addCustomActionForLayerType(self, *a, **k):
        raise RuntimeError("no layer tree in the harness")

    def removeCustomActionForLayerType(self, *a, **k):
        raise RuntimeError("no layer tree in the harness")


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _approx(got, want, tol=1e-3) -> bool:
    return got is not None and all(abs(g - w) <= tol for g, w in zip(got, want))


# 3857 canvas over a Fort-Myers-ish window.
canvas = QgsMapCanvas()
canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
to_3857 = QgsCoordinateTransform(
    QgsCoordinateReferenceSystem("EPSG:4326"),
    QgsCoordinateReferenceSystem("EPSG:3857"),
    QgsProject.instance().transformContext(),
)
canvas_extent_4326 = (-82.70, 26.50, -82.40, 26.80)
canvas.setExtent(
    to_3857.transformBoundingBox(QgsRectangle(*canvas_extent_4326))
)

dock = Trid3ntDock(FakeIface(canvas))
dock._auto_connect_done_this_show = True  # block showEvent auto-connect
dock.settings.auto_basemap = False        # no basemap fetch in the harness
rec = RecBridge()
dock.bridge = rec

# ---- 1. new_case creates BBOX-LESS ---------------------------------------- #
# The canvas-as-AOI seed is GONE: a fresh case starts with NO AOI until the
# user Sets one (the Set-AOI rectangle) or the LLM geocodes it -- new_case
# issues case_command("create", args=None) and leaves no local bbox behind.

dock.new_case()
create_calls = [c for c in rec.calls if c[0] == "create"]
if not create_calls:
    _fail("new_case did not issue a create case-command")
_cmd, _cid, cargs = create_calls[-1]
if cargs is not None:
    _fail(f"new_case must create BBOX-LESS (args=None), got args: {cargs!r}")
if dock._case_bbox is not None:
    _fail(f"new_case left a local bbox: {dock._case_bbox!r}")

# ---- 2. _on_aoi_extent_chosen: convert + state + overlay + persist --------- #

dock._case_id = "C1"
edit_bbox_4326 = (-82.62, 26.58, -82.50, 26.70)
edit_rect_3857 = to_3857.transformBoundingBox(QgsRectangle(*edit_bbox_4326))
rec.calls.clear()
dock._on_aoi_extent_chosen(edit_rect_3857)

if not _approx(dock._case_bbox, edit_bbox_4326, tol=1e-2):
    _fail(f"_on_aoi_extent_chosen set {_c := dock._case_bbox} != {edit_bbox_4326}")
if dock._aoi_rubber is None:
    _fail("_on_aoi_extent_chosen did not build the AOI overlay")
setbbox_calls = [c for c in rec.calls if c[0] == "set-bbox"]
if not setbbox_calls:
    _fail("_on_aoi_extent_chosen did not persist via case-command set-bbox")
_cmd, scid, sargs = setbbox_calls[-1]
if scid != "C1":
    _fail(f"set-bbox case_id {scid!r} != active case C1")
if not (isinstance(sargs, dict) and "bbox" in sargs
        and _approx(tuple(sargs["bbox"]), edit_bbox_4326, tol=1e-2)):
    _fail(f"set-bbox args.bbox wrong: {sargs!r}")

# ---- 3. case-open with a bbox sets state + renders overlay ----------------- #

dock._aoi_rubber = None  # force a fresh overlay build on the next render
open_bbox = (-82.60, 26.60, -82.55, 26.65)
dock._on_case_open_event(
    {"session_state": {"case": {
        "case_id": "C2", "title": "Open Case", "bbox": list(open_bbox)}}}
)
if dock._case_bbox != open_bbox:
    _fail(f"case-open bbox not adopted: {dock._case_bbox} != {open_bbox}")
if dock._aoi_rubber is None:
    _fail("case-open did not render the AOI overlay")

# ---- 4. _clear_messages (case switch) clears the bbox ---------------------- #

dock._clear_messages()
if dock._case_bbox is not None:
    _fail(f"_clear_messages left a stale bbox: {dock._case_bbox}")

# ---- 5. disconnect_agent clears the bbox ----------------------------------- #

dock._on_case_open_event(
    {"session_state": {"case": {
        "case_id": "C3", "title": "Case3", "bbox": list(open_bbox)}}}
)
if dock._case_bbox != open_bbox:
    _fail("setup for disconnect check failed to seed a bbox")
dock.disconnect_agent()
if dock._case_bbox is not None:
    _fail(f"disconnect_agent left a stale bbox: {dock._case_bbox}")

# ---- 6. case-open WITHOUT a bbox leaves _case_bbox None -------------------- #

dock._on_case_open_event(
    {"session_state": {"case": {"case_id": "C4", "title": "No AOI"}}}
)
if dock._case_bbox is not None:
    _fail(f"bbox-less case-open left a bbox: {dock._case_bbox}")

# ---- 7. _send with NO AOI: aoi_bbox=None + clean text
# C4 is open with no bbox -- the outgoing send_chat must carry aoi_bbox=None
# and the text must be exactly what the user typed.

rec.calls.clear()
dock.input_edit.setPlainText("plain message, no AOI")
dock._send()
chats = [c for c in rec.calls if c[0] == "chat"]
if len(chats) != 1:
    _fail(f"_send (no AOI) did not issue exactly one send_chat: {rec.calls!r}")
_verb, sent_text, sent_aoi = chats[-1]
if sent_text != "plain message, no AOI":
    _fail(f"no-AOI outgoing text not clean: {sent_text!r}")
if sent_aoi is not None:
    _fail(f"no-AOI send must ride aoi_bbox=None, got {sent_aoi!r}")

# ---- 8. _send rides the case bbox STRUCTURED + clean text
# A case-open with a bbox (the rehydrated drawn AOI), then a send: the bbox
# rides send_chat(aoi_bbox=[...]) -- NOT a bracketed prose line appended to
# the text -- and the set-time affordance (overlay) is still up afterwards.

dock._aoi_rubber = None  # force a fresh overlay build on the case-open render
dock._on_case_open_event(
    {"session_state": {"case": {
        "case_id": "C5", "title": "Send Case", "bbox": list(open_bbox)}}}
)
rec.calls.clear()
dock.input_edit.setPlainText("Fetch a DEM here")
dock._send()
chats = [c for c in rec.calls if c[0] == "chat"]
if len(chats) != 1:
    _fail(f"_send (AOI) did not issue exactly one send_chat: {rec.calls!r}")
_verb, sent_text, sent_aoi = chats[-1]
if sent_text != "Fetch a DEM here":
    _fail(f"legacy AOI prose leaked into the text: {sent_text!r}")
if sent_aoi is None or not _approx(tuple(sent_aoi), open_bbox, tol=1e-9):
    _fail(f"aoi_bbox missing/wrong on send_chat: {sent_aoi!r} != {open_bbox}")
if dock._aoi_rubber is None:
    _fail("AOI affordance (rubber-band overlay) gone after the send")

print("CASE-BBOX-OK")
qgs.exitQgis()
