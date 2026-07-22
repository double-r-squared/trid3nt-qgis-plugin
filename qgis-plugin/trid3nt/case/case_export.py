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

LOCAL mode: the agent runs on this same machine, so the returned paths are
directly readable; no download round-trip is needed.

REMOTE mode (milestone 3 item 1): the returned paths live on the REMOTE box,
so the artifacts are downloaded through ``GET /api/export-qgis/file?path=<abs>``
into a local temp dir first (``localize_remote_export``). That route serves
ONLY ``.qgz``/``.gpkg`` under the agent's export root (Content-Disposition
attachment; 400 missing param / 403 wrong type or outside root / 404 missing
-- see the server's ``tool_catalog_http.py`` + its route tests). GeoTIFF
rasters are therefore NOT downloadable remotely: they become an honest
skipped note, never a silent gap (the raster is still viewable through its
published tile layer). The HTTP API base is derived from the WS URL
(``wss://host/ws`` -> ``https://host`` -- CloudFront routes ``/api/*`` to the
agent's HTTP listener at the origin root).

Pure-testable pieces: the HTTP calls (stdlib urllib against any host), the
WS->HTTP base derivation, the remote-result localization, and the result ->
layer plan (GeoPackage table listing via stdlib sqlite3 ``gpkg_contents`` --
no OGR needed just to enumerate names; the raster scan is a plain
``*.tif``/``*.tiff`` directory walk of ``output_dir``).

Raster styling (the black-flood-raster fix): the export tool writes a sidecar
``<stem>.qml`` next to every GeoTIFF (the same TiTiler-derived pseudocolor
ramp its ``project.qgz`` embeds inline) and lists them as ``qml_paths`` in the
result JSON. ``plan_export_layers`` joins qml to raster by filename stem
(result list first, same-stem disk sidecar as fallback) into
``ExportPlan.raster_styles`` so the materializer can ``loadNamedStyle`` each
raster after adding it -- without this the plugin-added GeoTIFFs rendered
default grayscale (near-black flood frames).

Mesh outputs (MDAL phase 1): the export result additionally carries a
``mesh`` list (``export_case_to_qgis`` on the agent side) -- ONE entry per
SFINCS run whose ``sfincs_map.nc`` sits alongside a flood-depth layer in the
runs bucket, each ``{"kind": "mesh", "format": "sfincs_map_netcdf",
"s3_uri": str, "crs_authid": str|None, "name": str}``. Unlike GeoTIFFs/the
GeoPackage, the agent never copies the mesh into the export's ``output_dir``
-- it is only referenced by ``s3_uri``, so EVERY mode needs its own download
step: ``localize_mesh_entries`` fetches it via the local MinIO http form
(``s3_to_http`` from ``trid3nt_client`` -- the SAME translation
``layers.py``'s live vector materialization uses for ``s3://`` uris). This
only works when MinIO is directly network-reachable (local mode); remote
mode has no presigned-fetch path yet (same gap as the live vector/raster
carve-outs above), so a mesh entry there is left without a ``local_path`` and
``plan_export_layers`` turns that into an honest skip note -- never a crash.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:  # package context (real plugin runtime: net/case are sibling subpackages)
    from ..net.trid3nt_client import s3_to_http
except ImportError:  # flat/test context (tests sys.path.insert the trid3nt/ dir directly)
    from net.trid3nt_client import s3_to_http  # type: ignore[no-redef]

__all__ = [
    "DEFAULT_EXPORT_API",
    "ExportPlan",
    "ExportRequestError",
    "download_export_file",
    "download_mesh_file",
    "localize_mesh_entries",
    "localize_remote_export",
    "plan_export_layers",
    "post_export_case",
    "ws_url_to_http_base",
]

DEFAULT_EXPORT_API = "http://127.0.0.1:8766"


class ExportRequestError(Exception):
    """The export API call failed -- carries the server's honest message."""


def ws_url_to_http_base(ws_url: str) -> str:
    """Derive the HTTP(S) API base from an agent WebSocket URL.

    ``wss://host/ws`` -> ``https://host``; ``ws://127.0.0.1:8765/ws`` ->
    ``http://127.0.0.1:8765``. The ``/api/*`` routes live at the origin root
    (CloudFront routes ``/api/*`` to the agent's HTTP listener), so the WS
    path and any query string are dropped; the port is preserved.
    """
    parts = urllib.parse.urlsplit((ws_url or "").strip())
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme, parts.scheme or "http")
    netloc = parts.netloc or parts.path.split("/", 1)[0]
    return f"{scheme}://{netloc}"


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """The server's own ``{"error": ...}`` message, prefixed with the HTTP
    status so a 403 vs 404 is distinguishable at a glance."""
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        if isinstance(payload, dict):
            detail = str(payload.get("error") or "")
    except Exception:  # noqa: BLE001 -- body may be anything
        pass
    return f"HTTP {exc.code}" + (f": {detail}" if detail else "")


def download_export_file(
    base_url: str, remote_path: str, dest_dir: str, timeout: float = 300.0
) -> str:
    """``GET {base_url}/api/export-qgis/file?path=<remote_path>`` -> local path.

    Downloads one export artifact into ``dest_dir`` (named by the remote
    basename) and returns the local path. The route serves ONLY ``.qgz`` /
    ``.gpkg`` under the agent's export root; a 403 (wrong type / outside
    root) or 404 (missing) surfaces as an ``ExportRequestError`` carrying the
    status code + the server's honest message. Blocking -- the caller runs it
    OFF the UI thread.
    """
    url = (
        f"{base_url.rstrip('/')}/api/export-qgis/file"
        f"?path={urllib.parse.quote(remote_path, safe='')}"
    )
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        raise ExportRequestError(
            f"download of {os.path.basename(remote_path)!r} failed "
            f"({_http_error_detail(exc)})"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ExportRequestError(
            f"export file download unreachable at {url} ({exc})"
        ) from exc
    name = os.path.basename(remote_path.rstrip("/")) or "export.bin"
    local_path = os.path.join(dest_dir, name)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


def download_mesh_file(
    minio_endpoint: str, s3_uri: str, dest_dir: str, timeout: float = 300.0
) -> str:
    """Fetch one mesh artifact's ``s3://`` object directly off the local
    MinIO http form (``s3_to_http``) into ``dest_dir`` -- returns the local
    path. Unlike ``download_export_file`` this does NOT go through the
    agent's ``/api/export-qgis/file`` route: the export tool never copies
    ``sfincs_map.nc`` into its ``output_dir`` (see the module docstring), so
    there is no in-export-root file for that route to serve; MinIO is read
    straight over HTTP instead. Raises ``ExportRequestError`` (a bad
    ``s3_uri``, or MinIO unreachable/404) -- callers convert per-entry
    failures into honest notes, never a hard stop. Blocking -- run OFF the
    UI thread.
    """
    http_url = s3_to_http(s3_uri, minio_endpoint)
    if not http_url:
        raise ExportRequestError(f"mesh uri {s3_uri!r} is not a valid s3:// uri")
    request = urllib.request.Request(http_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as exc:
        raise ExportRequestError(
            f"mesh download of {os.path.basename(s3_uri)!r} failed (HTTP {exc.code})"
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ExportRequestError(
            f"mesh download unreachable at {http_url} ({exc})"
        ) from exc
    name = os.path.basename(s3_uri.rstrip("/")) or "mesh.nc"
    local_path = os.path.join(dest_dir, name)
    with open(local_path, "wb") as f:
        f.write(data)
    return local_path


def localize_mesh_entries(result: dict, minio_endpoint: str, dest_dir: str) -> dict:
    """Download every ``result["mesh"]`` entry's ``sfincs_map.nc`` via the
    local MinIO http form, attaching ``local_path`` to each entry.

    Only meaningful when MinIO is directly network-reachable (local mode --
    see ``download_mesh_file``); callers should skip invoking this in remote
    mode, where entries simply keep no ``local_path`` and
    ``plan_export_layers`` honestly notes them as unavailable. A per-entry
    download failure is recorded as an ``error`` string on that entry, never
    raised -- one bad mesh must not lose the rest of the export.
    """
    localized = dict(result)
    entries = []
    for entry in result.get("mesh") or []:
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        s3_uri = entry.get("s3_uri")
        if isinstance(s3_uri, str) and s3_uri:
            try:
                entry["local_path"] = download_mesh_file(minio_endpoint, s3_uri, dest_dir)
            except ExportRequestError as exc:
                entry["local_path"] = None
                entry["error"] = str(exc)
        else:
            entry["local_path"] = None
            entry["error"] = "mesh entry has no s3_uri"
        entries.append(entry)
    localized["mesh"] = entries
    return localized


def localize_remote_export(base_url: str, result: dict, dest_dir: str) -> dict:
    """Rewrite a remote ``/api/export-qgis`` result into a locally-planable one.

    Downloads the ``.gpkg`` (vector tables) and ``.qgz`` (styled project)
    through the file route into ``dest_dir`` and points the result's paths at
    the local copies (``output_dir`` = ``dest_dir``). Rasters are NOT
    downloadable (the route serves only .qgz/.gpkg), so a nonzero
    ``exported_raster_count`` becomes an honest ``skipped`` row and the count
    is zeroed. Per-file failures (403/404) also become skipped rows -- this
    never raises, so one bad artifact cannot lose the rest.
    """
    localized = dict(result)
    skipped = [row for row in (result.get("skipped") or []) if isinstance(row, dict)]
    for key in ("gpkg_path", "qgz_path"):
        remote = result.get(key)
        localized[key] = None
        if isinstance(remote, str) and remote:
            try:
                localized[key] = download_export_file(base_url, remote, dest_dir)
            except ExportRequestError as exc:
                skipped.append(
                    {"name": os.path.basename(remote), "reason": str(exc)}
                )
    raster_count = result.get("exported_raster_count")
    if isinstance(raster_count, int) and raster_count > 0:
        skipped.append(
            {
                "name": f"{raster_count} raster(s)",
                "reason": (
                    "remote export serves only .qgz/.gpkg -- GeoTIFFs stay on "
                    "the remote box (view them via the published tile layers)"
                ),
            }
        )
    localized["exported_raster_count"] = 0
    # Raster style sidecars live next to the rasters on the remote box; with
    # no rasters localized there is nothing to style locally.
    localized["qml_paths"] = []
    localized["skipped"] = skipped
    localized["output_dir"] = dest_dir
    return localized


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
    raster_styles: Dict[str, str] = field(default_factory=dict)  # tif -> .qml
    # {"name": str, "local_path": str|None, "crs_authid": str|None} per mesh
    # (MDAL phase 1); local_path is None when the download did not happen
    # (remote mode) or failed -- the materializer skips those with a note.
    mesh_entries: List[dict] = field(default_factory=list)
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

    # Sidecar .qml styles declared by the export result, keyed by stem.
    qml_by_stem = {}
    for qml in result.get("qml_paths") or []:
        if isinstance(qml, str) and qml.lower().endswith(".qml") and os.path.isfile(qml):
            qml_by_stem[os.path.splitext(os.path.basename(qml))[0]] = qml

    output_dir = result.get("output_dir")
    if isinstance(output_dir, str) and os.path.isdir(output_dir):
        for name in sorted(os.listdir(output_dir)):
            if name.lower().endswith((".tif", ".tiff")):
                path = os.path.join(output_dir, name)
                plan.raster_paths.append(path)
                # Style join: result-declared qml first, same-stem disk
                # sidecar as fallback (covers pre-qml_paths result shapes).
                stem = os.path.splitext(name)[0]
                qml = qml_by_stem.get(stem)
                if qml is None:
                    candidate = os.path.join(output_dir, stem + ".qml")
                    if os.path.isfile(candidate):
                        qml = candidate
                if qml is not None:
                    plan.raster_styles[path] = qml
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

    # Mesh entries (MDAL phase 1) -- defensive: a malformed/absent "mesh" key
    # is simply an empty list, never an error (older agent builds predate it).
    for entry in result.get("mesh") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        local_path = entry.get("local_path")
        if isinstance(local_path, str) and local_path and os.path.isfile(local_path):
            plan.mesh_entries.append(
                {
                    "name": name,
                    "local_path": local_path,
                    "crs_authid": entry.get("crs_authid")
                    if isinstance(entry.get("crs_authid"), str)
                    else None,
                }
            )
        else:
            reason = entry.get("error") or (
                "not downloaded (remote export does not yet fetch MDAL "
                "netCDF meshes -- local mode only for now)"
            )
            plan.mesh_entries.append({"name": name, "local_path": None, "crs_authid": None})
            plan.notes.append(f"mesh '{name}': {reason}")

    for row in result.get("skipped") or []:
        if isinstance(row, dict):
            plan.notes.append(
                f"skipped '{row.get('name')}': {row.get('reason')}"
            )
    return plan
