"""Live proof: TELEMAC river-dye MESH animates in the plugin (P3, 2026-07-14).

Proves the river-dye render path END TO END against REAL data, with ZERO new
render infra beyond one additive dataset-group selector:

  AGENT HALF (done offline, values pinned below): the REAL agent-side
  ``export_case_to_qgis._mesh_entry_for_layer`` discovered the SELAFIN mesh
  sibling of a real solved run's dye COG in the real MinIO runs bucket --
  format ``telemac_selafin``, ``s3_uri`` .../r2d_river.slf, ``crs_authid``
  EPSG:32611 (read from telemac_metrics.json). See the session evidence /
  Phase-1 discovery run; re-verify live after the next agent restart.

  PLUGIN HALF (this script): drives the REAL plugin code
  (``case_export.download_mesh_file`` + ``plan_export_layers`` +
  ``layers.LayerMaterializer.materialize_export``, unmodified from what ships)
  against the mesh entry. It:
    - fetches the REAL ``r2d_river.slf`` from the REAL MinIO endpoint,
    - loads it as a NATIVE ``QgsMeshLayer(..., "mdal")`` inside offscreen QGIS,
    - asserts the mesh is valid, its CRS was set to EPSG:32611, it carries a
      DYE dataset group, that DYE group is TIME-VARYING (> 1 dataset = a
      playable time series), the mesh's temporal properties are ACTIVE (the
      Temporal Controller can play it), and DYE is the ACTIVE scalar group
      (the plugin's additive tracer selector, so the plume -- not VELOCITY U --
      is the default-rendered field),
    - renders HONEST MAP PIXELS with ``QgsMapRendererSequentialJob`` at the
      PEAK dye frame and saves the screenshot, and proves the animation is real
      by rendering an EARLY frame too and asserting the pixels DIFFER.

Run:  QT_QPA_PLATFORM=offscreen python3 tests/headless_telemac_dye_proof.py
Env:  TRID3NT_MINIO_ENDPOINT (default http://100.92.163.46:9000),
      TRID3NT_RUN_ID (default the real dev run this proof was authored against),
      TRID3NT_MESH_ENTRY_JSON (optional path to a Phase-1 discovery entry).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PLUGIN_PATH = os.environ.get(
    "TRID3NT_PLUGIN_PATH",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
sys.path.insert(0, PLUGIN_PATH)

MINIO_ENDPOINT = os.environ.get("TRID3NT_MINIO_ENDPOINT", "http://100.92.163.46:9000")
RUN_ID = os.environ.get("TRID3NT_RUN_ID", "01KXHGVVSW3Y8SV8EQMTXNK8NS")
PROOF_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "docs", "proof")
)
SHOT = os.path.join(PROOF_DIR, "telemac_p3_plugin_dye.png")

# The Phase-1 discovery entry (independently produced by the REAL agent-side
# export_case_to_qgis._mesh_entry_for_layer against real MinIO). Overridable via
# a Phase-1 JSON dump so the two halves stay a single source of truth.
MESH_ENTRY = {
    "kind": "mesh",
    "format": "telemac_selafin",
    "s3_uri": f"s3://trid3nt-runs/{RUN_ID}/r2d_river.slf",
    "crs_authid": "EPSG:32611",
    "name": f"TELEMAC dye mesh ({RUN_ID[:8]})",
}
_entry_json = os.environ.get("TRID3NT_MESH_ENTRY_JSON")
if _entry_json and os.path.isfile(_entry_json):
    MESH_ENTRY.update(json.load(open(_entry_json)))

failures: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        failures.append(label)


from trid3nt.case import case_export  # noqa: E402

print(f"[proof] downloading {MESH_ENTRY['s3_uri']} via MinIO", flush=True)
mesh_dir = tempfile.mkdtemp(prefix="trid3nt_telemac_proof_")
local_slf = case_export.download_mesh_file(MINIO_ENDPOINT, MESH_ENTRY["s3_uri"], mesh_dir, timeout=60)
check("SELAFIN .slf downloaded from real MinIO", os.path.isfile(local_slf), local_slf)
check("downloaded .slf non-trivially sized", os.path.getsize(local_slf) > 50_000, f"{os.path.getsize(local_slf)} bytes")

result = {"status": "ok", "mesh": [dict(MESH_ENTRY, local_path=local_slf)]}
plan = case_export.plan_export_layers(result)
check("plan_export_layers parsed the mesh entry",
      len(plan.mesh_entries) == 1 and plan.mesh_entries[0]["local_path"] == local_slf)
check("plan resolved crs_authid EPSG:32611",
      bool(plan.mesh_entries) and plan.mesh_entries[0].get("crs_authid") == "EPSG:32611")

from qgis.core import QgsApplication  # noqa: E402

qgs = QgsApplication([], False)
qgs.initQgis()

from trid3nt.render.layers import LayerMaterializer  # noqa: E402
from trid3nt.plugin_settings import PluginSettings  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsMeshDatasetIndex,
    QgsProject,
)

materializer = LayerMaterializer(settings=PluginSettings())
notes = materializer.materialize_export(plan, group_label=RUN_ID[:8])
print("[proof] materialize_export notes:", flush=True)
for n in notes:
    print(f"         - {n}", flush=True)

mesh_layers = [l for l in materializer.last_added_layers if l.__class__.__name__ == "QgsMeshLayer"]
check("exactly one QgsMeshLayer materialized", len(mesh_layers) == 1, str(len(mesh_layers)))

if mesh_layers:
    ml = mesh_layers[0]
    check("mesh layer is valid (MDAL opened the .slf)", ml.isValid())
    check("mesh CRS set to EPSG:32611",
          ml.crs().isValid() and ml.crs().authid() == "EPSG:32611", ml.crs().authid())

    # enumerate dataset groups
    groups = {}
    for i in range(ml.datasetGroupCount()):
        try:
            groups[i] = ml.datasetGroupMetadata(QgsMeshDatasetIndex(i, 0)).name()
        except Exception:
            pass
    print(f"[proof] dataset groups: {groups}", flush=True)
    dye_idx = next((i for i, n in groups.items() if "dye" in (n or "").lower()), None)
    check("mesh carries a DYE dataset group", dye_idx is not None, str(groups))

    if dye_idx is not None:
        n_dye = ml.datasetCount(QgsMeshDatasetIndex(dye_idx, 0))
        check("DYE group is TIME-VARYING (> 1 dataset = playable time series)",
              n_dye > 1, f"{n_dye} datasets")
        tp = ml.temporalProperties()
        check("mesh temporal properties ACTIVE (Temporal Controller can play it)",
              bool(tp.isActive()), str(tp.isActive()))
        active = ml.rendererSettings().activeScalarDatasetGroup()
        check("DYE is the ACTIVE scalar group (additive tracer selector applied)",
              active == dye_idx, f"active={active} ({groups.get(active)!r})")

        # --- honest map pixels: render the PEAK dye frame, and prove distinct
        #     frames animate (early vs peak render must DIFFER) --------------- #
        # peak frame = the DYE dataset with the largest maximum().
        peak_ds, peak_max = 0, -1.0
        for d in range(n_dye):
            mx = ml.datasetMetadata(QgsMeshDatasetIndex(dye_idx, d)).maximum()
            if mx > peak_max:
                peak_max, peak_ds = mx, d
        print(f"[proof] DYE datasets={n_dye} peak dataset index={peak_ds} max={peak_max:.3f}", flush=True)

        from qgis.core import (
            QgsMapRendererSequentialJob,
            QgsMapSettings,
            QgsDateTimeRange,
        )
        from qgis.PyQt.QtCore import QSize
        from qgis.PyQt.QtGui import QColor

        # Give the DYE scalar an explicit 0..peak ramp so the plume renders in
        # visible colour (not a faint auto-range) -- honest range from the data.
        rs = ml.rendererSettings()
        rs.setActiveScalarDatasetGroup(dye_idx)
        ss = rs.scalarSettings(dye_idx)
        try:
            ss.setClassificationMinimumMaximum(0.0, float(peak_max) or 1.0)
            shader = ss.colorRampShader()
            shader.setMinimumValue(0.0)
            shader.setMaximumValue(float(peak_max) or 1.0)
            shader.classifyColorRamp(5, -1)
            ss.setColorRampShader(shader)
        except Exception as exc:  # noqa: BLE001 -- ramp is cosmetic, never a gate
            print(f"[proof] ramp set note: {exc}", flush=True)
        rs.setScalarSettings(dye_idx, ss)
        ml.setRendererSettings(rs)

        # Render exactly what the Temporal Controller shows at time T: drive the
        # map's temporal range across the mesh's own time extent (datasets are
        # evenly spaced by the constant graphic-printout period).
        te = tp.timeExtent()
        begin, end = te.begin(), te.end()
        span_ms = begin.msecsTo(end)

        def render_at(frac: float, path: str | None):
            qdt = begin.addMSecs(int(frac * span_ms))
            ms = QgsMapSettings()
            ms.setLayers([ml])
            ms.setDestinationCrs(ml.crs())
            ext = ml.extent()
            ext.scale(1.15)
            ms.setExtent(ext)
            ms.setOutputSize(QSize(900, 700))
            ms.setBackgroundColor(QColor(235, 240, 245))
            ms.setIsTemporal(True)
            ms.setTemporalRange(QgsDateTimeRange(qdt, qdt.addSecs(1)))
            job = QgsMapRendererSequentialJob(ms)
            job.start()
            job.waitForFinished()
            img = job.renderedImage()
            if path:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                img.save(path)
            return img

        peak_frac = peak_ds / max(n_dye - 1, 1)
        early_img = render_at(0.0, None)
        peak_img = render_at(peak_frac, SHOT)
        check("peak-dye screenshot written (QgsMapRendererSequentialJob map pixels)",
              os.path.isfile(SHOT) and os.path.getsize(SHOT) > 2000, SHOT)

        # distinct-frame check: count differing pixels between early and peak.
        diff = 0
        w, h = peak_img.width(), peak_img.height()
        step = 5
        for yy in range(0, h, step):
            for xx in range(0, w, step):
                if early_img.pixel(xx, yy) != peak_img.pixel(xx, yy):
                    diff += 1
        check("animation is real: early vs peak frame render DIFFERENT pixels",
              diff > 20, f"{diff} differing sample pixels")

    export_group = QgsProject.instance().layerTreeRoot().findGroup(f"TRID3NT export {RUN_ID[:8]}")
    check("mesh sits inside the case's TRID3NT export group",
          export_group is not None
          and ml.id() in {c.layer().id() for c in export_group.children() if hasattr(c, "layer")})

qgs.exitQgis()

if failures:
    print(f"\n[proof] DONE -- {len(failures)} FAILURE(S): {failures}", flush=True)
    sys.exit(1)
print(f"\n[proof] DONE -- ALL CHECKS PASSED (screenshot {SHOT})", flush=True)
sys.exit(0)
