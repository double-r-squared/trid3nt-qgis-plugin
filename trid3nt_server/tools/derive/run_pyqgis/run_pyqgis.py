"""``run_pyqgis`` - a PyQGIS snippet executed in the user's QGIS session.

The tool body REFUSES without ``confirmed=True``: the dispatch layer strips a
model-supplied flag, presents the approval card, and re-dispatches with the flag
only on the user's approval. A direct programmatic caller passing
``confirmed=True`` is the one documented bypass; there is no hidden auto-approve.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.server.processing import SessionProcessingFailedError, run_in_session
from trid3nt_server.tools import register_tool

__all__ = ["run_pyqgis", "CodeExecConfirmationRequired"]

_METADATA = AtomicToolMetadata(
    name="run_pyqgis",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


class CodeExecConfirmationRequired(RuntimeError):
    """Raised when ``run_pyqgis`` is invoked without ``confirmed=True`` - the
    fail-closed guard. ``retryable=False``: no retry gets past a missing user
    approval, the gate has to run."""

    error_code: str = "CODE_EXEC_CONFIRMATION_REQUIRED"
    retryable: bool = False

    def __init__(self, code_exec_id: str | None = None) -> None:
        super().__init__(
            "run_pyqgis requires user confirmation before running: the server "
            "emits a code-exec-request card and awaits approval, then re-dispatches "
            "with confirmed=True. This call had confirmed=False"
            + (f" (code_exec_id={code_exec_id})" if code_exec_id else "")
            + "."
        )
        self.code_exec_id = code_exec_id


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_pyqgis(
    code: str,
    rationale: str | None = None,
    *,
    confirmed: bool = False,
    code_exec_id: str | None = None,
    # absorb model-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Run a PyQGIS snippet in the user's QGIS session; the user approves the exact code first.

    ROUTING: what the QGIS session can do that no tool and no single Processing
    algorithm covers - read or edit the features and attributes of a canvas
    layer, chain several algorithms, style or group layers, compute a number or
    a table over a layer's own data, drive the temporal controller. NOT for
    fetching data (fetch_* tools) and NOT for one standard algorithm
    (run_qgis_algorithm).

    The code runs in the session's Python with `iface`, `QgsProject`,
    `processing` and the qgis.core names available; reach a layer by its canvas
    name through QgsProject.instance().mapLayersByName(name). Assign the answer
    to `result` (a JSON-serializable value); what the snippet prints comes back
    as stdout. A short `rationale` captions the approval card.

    Returns {status, result, stdout} or the session's error verbatim. A denied
    card is a refusal: never re-issue the same snippet unless the user asks.
    """
    if not confirmed:
        raise CodeExecConfirmationRequired(code_exec_id)
    if not isinstance(code, str) or not code.strip():
        raise SessionProcessingFailedError("code must be a non-empty Python snippet")
    cx_id = code_exec_id or new_ulid()
    response = await run_in_session(kind="code", code=code, code_exec_id=cx_id)
    return {
        "status": "ok",
        "result": (response.result or {}).get("value"),
        "stdout": response.stdout,
        "code_exec_id": cx_id,
    }
