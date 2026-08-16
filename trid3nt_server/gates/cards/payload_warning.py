"""Payload-warning gate card helpers: env thresholds + estimator resolution.

Pure env readers + estimator lookup keyed off a tool name. The
transport-coupled ``_maybe_gate_on_payload_warning`` orchestration stays in
``server``.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from trid3nt_contracts.payload_warning import (
    HARD_CAP_MB_DEFAULT,
    WARNING_THRESHOLD_MB_DEFAULT,
)

logger = logging.getLogger("trid3nt_server.gates.cards.payload_warning")


def _get_warning_threshold_mb() -> float:
    """Read the warning threshold from env, falling back to the default."""
    raw = os.environ.get("TRID3NT_PAYLOAD_WARNING_MB")
    if raw is None:
        return WARNING_THRESHOLD_MB_DEFAULT
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "TRID3NT_PAYLOAD_WARNING_MB=%r is not a float; using default %s",
            raw,
            WARNING_THRESHOLD_MB_DEFAULT,
        )
        return WARNING_THRESHOLD_MB_DEFAULT


def _get_hard_cap_mb() -> float:
    """Read the hard cap from env, falling back to the default."""
    raw = os.environ.get("TRID3NT_PAYLOAD_HARDCAP_MB")
    if raw is None:
        return HARD_CAP_MB_DEFAULT
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "TRID3NT_PAYLOAD_HARDCAP_MB=%r is not a float; using default %s",
            raw,
            HARD_CAP_MB_DEFAULT,
        )
        return HARD_CAP_MB_DEFAULT


def _resolve_payload_estimator(tool_name: str, estimator_name: str) -> Any | None:
    """Look up the named estimator callable on the tool's module.

    The ``AtomicToolMetadata.payload_mb_estimator_name`` field
    carries a Python identifier (not the callable itself) so the metadata
    stays serializable. Resolution at gate-time walks
    ``RegisteredTool.module`` to find the callable. Returns ``None`` if the
    module/attribute lookup fails — the gate then skips for this call.
    """
    try:
        from importlib import import_module

        from trid3nt_server.data import TOOL_REGISTRY

        entry = TOOL_REGISTRY.get(tool_name)
        if entry is None:
            return None
        mod = import_module(entry.module)
        fn = getattr(mod, estimator_name, None)
        if not callable(fn):
            return None
        return fn
    except Exception:  # noqa: BLE001 — defensive; gate must never raise
        logger.exception(
            "payload-warning: estimator lookup failed tool=%s name=%s",
            tool_name,
            estimator_name,
        )
        return None
