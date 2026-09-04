"""The snippet's engine room: a staged payload in, one result envelope out.

Runs INSIDE the box and imports nothing from the server package - the mount is
the only thing that connects them. There is no network here and nothing to
fetch: every path the payload carries was staged before the container started,
so a ref that is still a URI is a staging miss and opens as one.
"""

from __future__ import annotations

import base64
import builtins
import errno
import io
import json
import socket
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

import numpy as np
import pandas as pd
from matplotlib.figure import Figure

MAX_OUTPUT_CHARS = 65536
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_DATAFRAME_ROWS = 5000

#: The errnos a denied network wears. The box holds no interfaces, so a snippet
#: reaching for the world fails at the kernel; naming those failures lets the
#: envelope say "blocked" rather than dressing an egress attempt up as a bug.
_NET_ERRNOS = frozenset({errno.ENETUNREACH, errno.EHOSTUNREACH,
                         errno.ENETDOWN, errno.EADDRNOTAVAIL})

_RASTER_EXTS = (".tif", ".tiff", ".cog", ".vrt")
_VECTOR_EXTS = (".geojson", ".json", ".fgb", ".gpkg", ".shp")


class _Bounded(io.StringIO):
    """A capture stream that stops growing, and says that it did."""

    truncated = False

    def write(self, s: str) -> int:
        room = MAX_OUTPUT_CHARS - self.tell()
        if len(s) > room:
            self.truncated = True
            super().write(s[:max(room, 0)])
            return len(s)
        return super().write(s)


def _var(name: str) -> str:
    base = name.rsplit("/", 1)[-1].split(".", 1)[0]
    clean = "".join(c if (c.isalnum() or c == "_") else "_" for c in base)
    return clean if clean and not clean[0].isdigit() else f"layer_{clean}"


def _open(path: str) -> Any:
    lower = str(path).lower()
    if lower.endswith(_RASTER_EXTS):
        import rasterio
        return rasterio.open(path)
    if lower.endswith(".parquet"):
        import geopandas
        return geopandas.read_parquet(path)
    if lower.endswith(_VECTOR_EXTS):
        import geopandas
        return geopandas.read_file(path)
    return path


def _handles(layer_refs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """The staged refs as open handles, plus the reason each unopened one failed.

    A list ref is an ordered frame set and opens as a list, so a snippet iterates
    frames. An open failure hands back the ref string rather than crashing: the
    snippet decides what a missing layer means.
    """
    handles: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, ref in (layer_refs or {}).items():
        many = isinstance(ref, list)
        frames = list(ref) if many else [ref]
        opened: list[Any] = []
        for i, one in enumerate(frames):
            try:
                opened.append(_open(one))
            except Exception as exc:
                opened.append(one)
                errors[f"{name}[{i}]" if many else name] = f"{type(exc).__name__}: {exc}"
        handles[_var(name)] = opened if many else opened[0]
        handles[f"{_var(name)}_uri"] = frames[0] if frames else None
    handles["layers"] = {_var(n): handles.get(_var(n)) for n in (layer_refs or {})}
    return handles, errors


def _descriptor(value: Any) -> dict[str, Any]:
    if value is None:
        return {"kind": "none", "value": None}
    fig = value if isinstance(value, Figure) else getattr(value, "figure", None)
    if isinstance(fig, Figure):
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        return {"kind": "chart",
                "title": next((t for t in (ax.get_title() for ax in fig.get_axes())
                               if t), "Sandbox figure"),
                "png_base64": base64.b64encode(buf.getvalue()).decode("ascii")}
    frame = value.to_frame() if isinstance(value, pd.Series) else value
    if isinstance(frame, pd.DataFrame):
        head = frame.head(MAX_DATAFRAME_ROWS)
        return {"kind": "dataframe", "columns": [str(c) for c in frame.columns],
                "records": json.loads(head.to_json(orient="records",
                                                   date_format="iso")),
                "row_count": int(len(frame)), "returned_rows": int(len(head)),
                "truncated": len(frame) > MAX_DATAFRAME_ROWS}
    if isinstance(value, np.generic):
        return {"kind": "scalar", "value": value.item()}
    if isinstance(value, np.ndarray):
        small = value.size <= MAX_DATAFRAME_ROWS
        return {"kind": "array", "shape": list(value.shape), "truncated": not small,
                "value": value.tolist() if small else None}
    try:
        json.dumps(value)
        return {"kind": "json",
                "value": list(value) if isinstance(value, tuple) else value}
    except (TypeError, ValueError):
        return {"kind": "repr", "value": repr(value)[:MAX_OUTPUT_CHARS]}


def _bound(descriptor: dict[str, Any]) -> dict[str, Any]:
    """A descriptor cut to what the wire carries, stating the cut it made."""
    try:
        size = len(json.dumps(descriptor).encode("utf-8"))
    except (TypeError, ValueError):
        size = MAX_RESULT_BYTES + 1
    if size <= MAX_RESULT_BYTES:
        return descriptor
    kind, value = descriptor.get("kind"), descriptor.get("value")
    if isinstance(value, str):
        kept = value.encode("utf-8")[:MAX_RESULT_BYTES - 4096].decode("utf-8", "ignore")
        return {"kind": kind, "truncated": True, "original_bytes": size,
                "value": f"{kept}...[truncated {len(value) - len(kept)} chars]"}
    return {"kind": "too_large", "original_kind": kind, "value": None,
            "truncated": True, "original_bytes": size,
            "max_result_bytes": MAX_RESULT_BYTES, "repr_head": repr(value)[:1024]}


def _is_network(exc: BaseException | None) -> bool:
    """Whether the denial reached the snippet, however far up it was rewrapped."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, socket.gaierror) or (
                isinstance(exc, OSError) and exc.errno in _NET_ERRNOS):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def run(python_code: str, layer_refs: dict[str, Any]) -> dict[str, Any]:
    """One snippet run -> the envelope stating what it printed, produced and hit."""
    handles, layer_errors = _handles(layer_refs)
    namespace: dict[str, Any] = {"__builtins__": builtins,
                                 "__name__": "__trid3nt_sandbox__", **handles}
    out, err = _Bounded(), _Bounded()
    status, error = "ok", None
    result: dict[str, Any] = {"kind": "none", "value": None}
    try:
        with redirect_stdout(out), redirect_stderr(err):
            exec(compile(python_code, "<sandbox>", "exec"), namespace)
        result = _bound(_descriptor(namespace.get("result")))
    except SystemExit as exc:
        status = "ok" if exc.code in (0, None) else "error"
        error = None if status == "ok" else f"SystemExit({exc.code})"
    except BaseException as exc:
        status = "blocked" if _is_network(exc) else "error"
        error = f"{type(exc).__name__}: {exc}"
        err.write("\n" + "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)))
    envelope = {"stdout": out.getvalue(), "stderr": err.getvalue(),
                "result": result, "status": status, "error": error,
                "stdout_truncated": out.truncated, "stderr_truncated": err.truncated}
    if layer_errors:
        envelope["layer_errors"] = layer_errors
    return envelope


def main(payload_path: str, result_path: str) -> int:
    with open(payload_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    envelope = run(payload.get("python_code", ""), payload.get("layer_refs") or {})
    with open(result_path, "w", encoding="utf-8") as fh:
        json.dump(envelope, fh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
