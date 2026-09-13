"""Real-QGIS harness for the retitle, run as a SUBPROCESS by its wrapper.

Proved on the INSTALLED QGIS: a layer row reaches the canvas under the name it
carries, and a LATER row for the same layer_id under a new name renames the
layer already on the map rather than adding a second one - which is what a
restyle carrying a title does, since the rename rides the same session-state
row the materializer already reads.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.core import QgsApplication, QgsProject  # noqa: E402

_LAYER_ID = "L1"
_CASE = "01KX5TEZ20BK86EE6DG8PSVFJK"

_POINT = {
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "properties": {},
                  "geometry": {"type": "Point", "coordinates": [-71.5, 41.4]}}],
}


def _session_state(name: str) -> dict:
    """The wire frame a publish - or a restyle carrying a title - sends."""
    return {"loaded_layers": [{
        "layer_id": _LAYER_ID, "name": name, "layer_type": "vector",
        "uri": "s3://trid3nt-runs/case/point.geojson", "visible": True,
        "role": "input", "inline_geojson": _POINT}]}


def _on_the_map():
    return [layer for layer in QgsProject.instance().mapLayers().values()
            if layer.customProperty("trid3nt/layer_id") == _LAYER_ID]


#: The QGIS application, held at module scope for the life of the process: Qt
#: aborts the moment it is released, and this harness never tears it down.
_APP: "QgsApplication | None" = None


def main() -> None:
    global _APP

    _APP = QgsApplication([], False)
    _APP.initQgis()
    from plugin.net.trid3nt_client import parse_layer_events
    from plugin.plugin_settings import PluginSettings
    from plugin.render.layers import LayerMaterializer

    materializer = LayerMaterializer(PluginSettings())
    materializer.set_case(_CASE, "rename proof")

    notes = materializer.materialize(parse_layer_events(_session_state("Depth")))
    print(f"add notes: {notes}", flush=True)
    landed = _on_the_map()
    assert len(landed) == 1, f"{len(landed)} layers carry {_LAYER_ID}"
    assert landed[0].name() == "Depth", landed[0].name()

    renamed = materializer.materialize(
        parse_layer_events(_session_state("Depth at the wharf")))
    print(f"rename notes: {renamed}", flush=True)
    assert renamed == [], f"a rename re-added the layer: {renamed}"
    after = _on_the_map()
    assert len(after) == 1, f"the rename added a second layer: {len(after)}"
    assert after[0].name() == "Depth at the wharf", after[0].name()
    assert after[0].id() == landed[0].id(), "the rename replaced the layer"

    materializer.cleanup_session()
    print("QT-LAYER-RENAME-OK", flush=True)


if __name__ == "__main__":
    # QGIS's own C++ teardown faults on this build with the harness layers still
    # alive, and a segfault at exit says nothing about what was measured. The
    # verdict is the flushed token, so the process ends on the measurement.
    try:
        main()
        _rc = 0
    except BaseException:  # noqa: BLE001 -- the traceback IS the failure report
        import traceback

        traceback.print_exc()
        _rc = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)
