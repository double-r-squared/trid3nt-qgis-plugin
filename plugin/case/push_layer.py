"""Push-layer support -- the two agent ingest routes, as pure python.

The user's active QGIS layer goes INTO the current case in two calls against the
agent's HTTP listener: a raw-body (never multipart) file upload returning an
``s3://`` URI, then a JSON register that returns the case layer's row. The
layer's export to a file is ``render.export_layer``'s, on both sides of the seam."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from ..render.export_layer import export_to_tempfile

__all__ = [
    "DEFAULT_INGEST_TIMEOUT",
    "PushLayerRequestError",
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
    status so a 404 and a 413 are distinguishable at a glance."""
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
    BLOCKING: the caller runs this off the UI thread."""
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
    """The dock's success note for a completed push."""
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
    Deletes ``local_path`` on success AND on failure; never partially
    registers, because a failed upload never reaches the ingest POST."""
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
    it, and register it on ``case_id``. BLOCKING, and the one entry point a
    caller needs."""
    local_path, kind = export_to_tempfile(layer)
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

