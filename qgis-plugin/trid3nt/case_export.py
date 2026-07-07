"""Open-case-in-QGIS support -- PURE PYTHON (no PyQGIS / PyQt imports).

Milestone 2 item 4: list the user's cases and open one in QGIS via the local
agent's export API (``tool_catalog_http.py``, default ``http://127.0.0.1:8766``):

    POST /api/export-qgis {"case_id": "..."}
      -> 200 {"status": "ok"|"partial", "qgz_path": str,
              "gpkg_path": str|None, "exported_vector_count": int,
              "exported_raster_count": int,
              "skipped": [{"name","reason"}...], "output_dir": str}
      -> 4xx {"error": "<honest message>"}

DESIGN DECISION (documented per the milestone kickoff): we ADD THE EXPORTED
LAYERS DIRECTLY to the user's current project instead of opening the returned
``project.qgz`` via ``QgsProject.instance().read()``. Reading a .qgz REPLACES
the whole open project -- unsaved user work, their layer tree, and the live
TRID3NT chat-session group would all be discarded. Adding the GeoPackage
tables + GeoTIFFs as ordinary layers is non-destructive, keeps the plugin's
own group intact, and QGIS users already know how to save/inspect them. The
.qgz path is still surfaced in the note so a user who WANTS the styled
project can open it manually.

The local agent runs on this same machine, so the returned paths are directly
readable; no file download round-trip is needed (the /api/export-qgis/file
route exists for remote clients and stays unused here).

Pure-testable pieces: the HTTP call (stdlib urllib against any host) and the
result -> layer plan (GeoPackage table listing via stdlib sqlite3
``gpkg_contents`` -- no OGR needed just to enumerate names; the raster scan is
a plain ``*.tif``/``*.tiff`` directory walk of ``output_dir``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

__all__ = [
    "DEFAULT_EXPORT_API",
    "ExportPlan",
    "ExportRequestError",
    "plan_export_layers",
    "post_export_case",
]

DEFAULT_EXPORT_API = "http://127.0.0.1:8766"


class ExportRequestError(Exception):
    """The export API call failed -- carries the server's honest message."""


def post_export_case(
    base_url: str, case_id: str, timeout: float = 300.0
) -> dict:
    """``POST {base_url}/api/export-qgis`` -> the export result dict.

    Raises ``ExportRequestError`` with the server's own ``error`` message on
    a 4xx/5xx (typed tool errors come back as ``{"error": ...}``), and on
    transport failures (connection refused = agent HTTP listener not up).
    Blocking -- the caller runs it OFF the UI thread.
    """
    url = f"{base_url.rstrip('/')}/api/export-qgis"
    body = json.dumps({"case_id": case_id}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            if isinstance(payload, dict):
                detail = str(payload.get("error") or "")
        except Exception:  # noqa: BLE001 -- body may be anything
            pass
        raise ExportRequestError(
            detail or f"export API returned HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ExportRequestError(
            f"export API unreachable at {url} ({exc}) -- is the local agent "
            "running with its HTTP listener?"
        ) from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportRequestError(f"export API returned non-JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise ExportRequestError("export API returned a non-object body")
    return result


@dataclass
class ExportPlan:
    """What to add to the QGIS project from one export result."""

    status: str = ""
    qgz_path: Optional[str] = None
    gpkg_path: Optional[str] = None
    vector_layers: List[str] = field(default_factory=list)  # gpkg table names
    raster_paths: List[str] = field(default_factory=list)  # local .tif files
    notes: List[str] = field(default_factory=list)  # honest skips/problems


def _gpkg_layer_names(gpkg_path: str) -> List[str]:
    """Feature-table names from a GeoPackage's ``gpkg_contents`` (stdlib
    sqlite3; read-only URI so we never lock or mutate the export)."""
    uri = f"file:{gpkg_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT table_name FROM gpkg_contents "
            "WHERE data_type = 'features' ORDER BY table_name"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r and isinstance(r[0], str)]


def plan_export_layers(result: dict) -> ExportPlan:
    """Turn an export result dict into an add-these-layers plan.

    Never raises on a malformed/partial result -- problems become honest
    notes (a bad export must not crash the dock).
    """
    plan = ExportPlan(status=str(result.get("status") or ""))
    qgz = result.get("qgz_path")
    plan.qgz_path = qgz if isinstance(qgz, str) and qgz else None

    gpkg = result.get("gpkg_path")
    if isinstance(gpkg, str) and gpkg:
        if os.path.isfile(gpkg):
            try:
                names = _gpkg_layer_names(gpkg)
            except sqlite3.Error as exc:
                names = []
                plan.notes.append(f"GeoPackage unreadable ({exc}): {gpkg}")
            if names:
                plan.gpkg_path = gpkg
                plan.vector_layers = names
            elif os.path.isfile(gpkg):
                plan.notes.append(f"GeoPackage has no feature tables: {gpkg}")
        else:
            plan.notes.append(f"GeoPackage missing on disk: {gpkg}")

    output_dir = result.get("output_dir")
    if isinstance(output_dir, str) and os.path.isdir(output_dir):
        for name in sorted(os.listdir(output_dir)):
            if name.lower().endswith((".tif", ".tiff")):
                plan.raster_paths.append(os.path.join(output_dir, name))
    expected_rasters = result.get("exported_raster_count")
    if (
        isinstance(expected_rasters, int)
        and expected_rasters > 0
        and len(plan.raster_paths) < expected_rasters
    ):
        plan.notes.append(
            f"export reported {expected_rasters} raster(s) but only "
            f"{len(plan.raster_paths)} .tif found in {output_dir!r}"
        )

    for row in result.get("skipped") or []:
        if isinstance(row, dict):
            plan.notes.append(
                f"skipped '{row.get('name')}': {row.get('reason')}"
            )
    return plan
