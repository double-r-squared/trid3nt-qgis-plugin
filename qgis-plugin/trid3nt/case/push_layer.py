"""Push-layer support -- PURE PYTHON (no PyQGIS / PyQt imports) except the ONE
QGIS-only export helper at the bottom.

Bidirectional layer push (the reverse seam of ``case_export.py``'s
open-in-QGIS): the user's ACTIVE QGIS layer (vector or raster) is sent INTO
the current case as a first-class input layer, via the agent's HTTP listener
(``tool_catalog_http.py``, default ``http://127.0.0.1:8766``):

    1. POST /api/ingest-layer-file?filename=<name>  (raw request-body upload;
       Content-Type: application/octet-stream, NOT multipart/form-data -- this
       plugin has no boto3 and this codebase has no multipart parser anywhere,
       so a raw-body PUT-shaped POST is the simplest correct single-file
       upload). -> 200 {"s3_uri": "s3://..."}
    2. POST /api/ingest-layer {"case_id", "name", "kind", "s3_uri",
       "crs_authid"?, "make_aoi"?} -- registers the uploaded object onto the
       case. -> 200 {"status": "ok", "layer_id", "name", "layer_type", "uri",
       "bbox", "aoi_pinned", "feature_count"} | 4xx {"error": "<honest msg>"}

Pure-testable pieces: both HTTP calls (stdlib urllib against any host) and the
finished-note formatter. The ONE QGIS-touching piece --
``export_active_layer_to_tempfile`` -- is isolated at the bottom of this
module so it is the ONLY thing a headless test cannot drive; everything else
(``push_exported_file``, the orchestration used once a temp file already
exists) is plain file + network I/O and is exercised end-to-end in
``tests/headless_push_layer_proof.py`` against a stub HTTP server standing in
for the two routes above.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "DEFAULT_INGEST_TIMEOUT",
    "PushLayerRequestError",
    "export_active_layer_to_tempfile",
    "format_push_note",
    "post_ingest_layer",
    "push_active_layer",
    "push_exported_file",
    "upload_layer_bytes",
]

#: Generous default -- a pushed layer can legitimately be tens of MB (the
#: agent enforces the real 200 MB cap; this is just a client-side patience
#: budget for the upload + ingest round trip).
DEFAULT_INGEST_TIMEOUT = 120.0


class PushLayerRequestError(Exception):
    """The ingest API call (upload or register) failed -- carries the
    server's honest message, or an honest local description (unreadable
    layer, agent unreachable, non-JSON reply)."""


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """The server's own ``{"error": ...}`` message, prefixed with the HTTP
    status so a 404 vs 413 is distinguishable at a glance. Mirrors
    ``case_export._http_error_detail``."""
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", "replace"))
        if isinstance(payload, dict):
            detail = str(payload.get("error") or "")
    except Exception:  # noqa: BLE001 -- body may be anything
        pass
    return f"HTTP {exc.code}" + (f": {detail}" if detail else "")


def upload_layer_bytes(
    base_url: str, filename: str, data: bytes, timeout: float = DEFAULT_INGEST_TIMEOUT
) -> str:
    """``POST {base_url}/api/ingest-layer-file?filename=<filename>`` with
    ``data`` as the raw request body -> the staged ``s3://`` object URI.

    Blocking -- the caller runs this OFF the UI thread (see ``_PushLayerTask``
    in ``dock.py``, mirroring ``_ExportTask``).
    """
    if not data:
        raise PushLayerRequestError("nothing to upload -- the exported file is empty")
    url = (
        f"{base_url.rstrip('/')}/api/ingest-layer-file"
        f"?filename={urllib.parse.quote(filename)}"
    )
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise PushLayerRequestError(_http_error_detail(exc)) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise PushLayerRequestError(
            f"upload API unreachable at {url} ({exc}) -- is the local agent "
            "running with its HTTP listener?"
        ) from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PushLayerRequestError(f"upload API returned non-JSON: {exc}") from exc
    s3_uri = result.get("s3_uri") if isinstance(result, dict) else None
    if not isinstance(s3_uri, str) or not s3_uri:
        raise PushLayerRequestError("upload API returned no `s3_uri`")
    return s3_uri


def post_ingest_layer(
    base_url: str,
    case_id: str,
    name: str,
    kind: str,
    s3_uri: str,
    crs_authid: Optional[str] = None,
    make_aoi: bool = False,
    timeout: float = DEFAULT_INGEST_TIMEOUT,
) -> Dict[str, Any]:
    """``POST {base_url}/api/ingest-layer`` -> the registered layer's result
    dict. Blocking -- see ``upload_layer_bytes``."""
    url = f"{base_url.rstrip('/')}/api/ingest-layer"
    body_dict: Dict[str, Any] = {
        "case_id": case_id,
        "name": name,
        "kind": kind,
        "s3_uri": s3_uri,
        "make_aoi": bool(make_aoi),
    }
    if crs_authid:
        body_dict["crs_authid"] = crs_authid
    body = json.dumps(body_dict).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise PushLayerRequestError(_http_error_detail(exc)) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise PushLayerRequestError(
            f"ingest API unreachable at {url} ({exc}) -- is the local agent "
            "running with its HTTP listener?"
        ) from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PushLayerRequestError(f"ingest API returned non-JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise PushLayerRequestError("ingest API returned a non-object body")
    return result


def format_push_note(name: str, result: Dict[str, Any]) -> str:
    """The dock's success note for a completed push (design point 3: "<name>
    pushed to case (vector, N features)")."""
    kind = result.get("layer_type") or "layer"
    if kind == "vector":
        fc = result.get("feature_count")
        if isinstance(fc, int):
            plural = "" if fc == 1 else "s"
            detail = f"vector, {fc} feature{plural}"
        else:
            detail = "vector"
    elif kind == "raster":
        detail = "raster"
    else:
        detail = str(kind)
    note = f"'{name}' pushed to case ({detail})"
    if result.get("aoi_pinned"):
        note += " -- case AOI updated"
    return note


def push_exported_file(
    base_url: str,
    case_id: str,
    local_path: str,
    kind: str,
    name: str,
    crs_authid: Optional[str] = None,
    make_aoi: bool = False,
    timeout: float = DEFAULT_INGEST_TIMEOUT,
) -> Dict[str, Any]:
    """Upload an ALREADY-exported local file and register it on ``case_id``.

    The pure (no PyQGIS) half of the push flow -- everything past "a file
    exists on disk". Deletes ``local_path`` when done, success or failure
    (the temp export is single-use). Raises ``PushLayerRequestError`` on any
    failure; never partially registers (the upload step's failure never
    reaches the ingest POST).
    """
    try:
        with open(local_path, "rb") as f:
            data = f.read()
    except OSError as exc:
        raise PushLayerRequestError(f"could not read exported file: {exc}") from exc
    try:
        filename = os.path.basename(local_path)
        s3_uri = upload_layer_bytes(base_url, filename, data, timeout=timeout)
        return post_ingest_layer(
            base_url,
            case_id,
            name,
            kind,
            s3_uri,
            crs_authid=crs_authid,
            make_aoi=make_aoi,
            timeout=timeout,
        )
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass


def push_active_layer(
    base_url: str,
    case_id: str,
    layer: Any,
    make_aoi: bool = False,
    timeout: float = DEFAULT_INGEST_TIMEOUT,
) -> Dict[str, Any]:
    """Full push: export ``layer`` (a ``QgsMapLayer``) to a temp file, upload
    it, and register it on ``case_id``. The single entry point
    ``_PushLayerTask`` (dock.py) calls off the UI thread.
    """
    local_path, kind = export_active_layer_to_tempfile(layer)
    crs_authid = None
    try:
        crs_authid = layer.crs().authid() or None
    except Exception:  # noqa: BLE001 -- best-effort hint only
        crs_authid = None
    name = ""
    try:
        name = layer.name() or ""
    except Exception:  # noqa: BLE001
        name = ""
    name = name.strip() or os.path.basename(local_path)
    return push_exported_file(
        base_url,
        case_id,
        local_path,
        kind,
        name,
        crs_authid=crs_authid,
        make_aoi=make_aoi,
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
# QGIS-only: export the active layer to a temp file. NOT unit-testable
# headless -- everything above this line is.
# --------------------------------------------------------------------------- #


def export_active_layer_to_tempfile(layer: Any) -> Tuple[str, str]:
    """Export ``layer`` (the active QGIS layer) to a temp file.

    Returns ``(local_path, kind)`` where ``kind`` is ``"vector"`` (written as
    a GeoPackage via ``QgsVectorFileWriter``) or ``"raster"`` (written as a
    GeoTIFF via the ``gdal:translate`` Processing algorithm -- simpler and
    more robust across raster provider types than hand-building a
    ``QgsRasterFileWriter`` pipe). Raises ``PushLayerRequestError`` when
    ``layer`` is ``None``, of an unsupported type, or the export itself
    fails.
    """
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    if layer is None:
        raise PushLayerRequestError(
            "no active layer -- select a layer in the QGIS Layers panel first"
        )
    if isinstance(layer, QgsVectorLayer):
        return _export_vector_to_tempfile(layer), "vector"
    if isinstance(layer, QgsRasterLayer):
        return _export_raster_to_tempfile(layer), "raster"
    raise PushLayerRequestError(
        f"active layer '{layer.name()}' is not a vector or raster layer"
    )


def _export_vector_to_tempfile(layer: Any) -> str:
    import tempfile

    from qgis.core import QgsProject, QgsVectorFileWriter

    fd, path = tempfile.mkstemp(suffix=".gpkg", prefix="trid3nt_push_")
    os.close(fd)
    os.unlink(path)  # QgsVectorFileWriter creates the file itself

    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.fileEncoding = "UTF-8"
    transform_context = QgsProject.instance().transformContext()
    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, transform_context, options
    )
    # writeAsVectorFormatV3 returns (WriterError, errorMessage[, ...]).
    err = result[0] if isinstance(result, tuple) else result
    if err != QgsVectorFileWriter.NoError:
        detail = result[1] if isinstance(result, tuple) and len(result) > 1 else str(err)
        raise PushLayerRequestError(f"could not export vector layer: {detail}")
    return path


def _export_raster_to_tempfile(layer: Any) -> str:
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_push_")
    os.close(fd)
    try:
        import processing

        processing.run(
            "gdal:translate",
            {
                "INPUT": layer,
                "TARGET_CRS": None,
                "NODATA": None,
                "COPY_SUBDATASETS": False,
                "OPTIONS": "",
                "EXTRA": "",
                "DATA_TYPE": 0,
                "OUTPUT": path,
            },
        )
    except Exception as exc:  # noqa: BLE001 -- surfaced, never silent
        raise PushLayerRequestError(f"could not export raster layer: {exc}") from exc
    return path
