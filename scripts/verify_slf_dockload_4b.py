#!/usr/bin/env python
"""wave 4b dock-load verification (the plugin .nc-bug fix, proven live).

Runs the REAL QGIS/MDAL load the plugin's ``_add_mesh`` performs
(``QgsMeshLayer(local_path, name, "mdal")`` -> ``isValid()`` ->
``datasetGroupCount()``), against a real solved result SELAFIN staged TWO ways:

  * ``<base>.slf``  -- the post-fix staging (source extension preserved),
  * ``<base>.nc``   -- the pre-fix hardcoded staging (SELAFIN mislabeled netCDF).

Proves the fix mattered: the ``.slf``-staged file OPENS valid and reports
dataset groups; the SAME BYTES staged ``.nc`` are rejected / misreport
(MDAL's driver selection is extension-sensitive).

MUST run under a PyQGIS-capable interpreter (the system /usr/bin/python3 that
has QGIS 3.40, NOT the agent venv). Offscreen:
    QT_QPA_PLATFORM=offscreen /usr/bin/python3 scripts/verify_slf_dockload_4b.py <result.slf>
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication, QgsMeshLayer, Qgis  # noqa: E402


def _load(path: str, name: str) -> dict:
    """The exact plugin _add_mesh load call + its validity/group probe."""
    layer = QgsMeshLayer(path, name, "mdal")
    valid = bool(layer.isValid())
    groups = int(layer.datasetGroupCount()) if valid else 0
    gnames = []
    if valid:
        for i in range(groups):
            try:
                meta = layer.datasetGroupMetadata(i)
                gnames.append(meta.name())
            except Exception:  # noqa: BLE001
                pass
    return {"path": os.path.basename(path), "isValid": valid,
            "datasetGroupCount": groups, "group_names": gnames}


def main() -> int:
    src = sys.argv[1]
    qgs = QgsApplication([], False)
    qgs.initQgis()
    try:
        tmp = tempfile.mkdtemp(prefix="4b_dockload_")
        base = os.path.splitext(os.path.basename(src))[0]
        slf_path = os.path.join(tmp, base + ".slf")
        nc_path = os.path.join(tmp, base + ".nc")
        shutil.copyfile(src, slf_path)
        shutil.copyfile(src, nc_path)  # identical bytes, wrong extension

        as_slf = _load(slf_path, "results-mesh (.slf staging, post-fix)")
        as_nc = _load(nc_path, "results-mesh (.nc staging, pre-fix)")

        fix_proven = (as_slf["isValid"] and as_slf["datasetGroupCount"] > 0
                      and (not as_nc["isValid"]
                           or as_nc["datasetGroupCount"] == 0
                           or as_nc["datasetGroupCount"]
                              != as_slf["datasetGroupCount"]))
        result = {
            "verification_path": "PyQGIS real MDAL (QgsMeshLayer, QGIS "
                                 + str(Qgis.QGIS_VERSION) + ")",
            "source_selafin": src,
            "staged_as_slf": as_slf,
            "staged_as_nc": as_nc,
            "fix_proven": bool(fix_proven),
        }
        print(json.dumps(result, indent=2))
        shutil.rmtree(tmp, ignore_errors=True)
        return 0 if fix_proven else 1
    finally:
        qgs.exitQgis()


if __name__ == "__main__":
    sys.exit(main())
