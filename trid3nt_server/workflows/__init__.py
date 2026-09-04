"""Deterministic workflows that compose atomic tools.

Workflows are orchestrator-style Python functions composing the atomic tools
under ``trid3nt_server/tools/`` into deterministic chains. They are LLM-free and
stable-signature: the same inputs author the same run.

A workflow is not itself an atomic tool -- it carries no ``AtomicToolMetadata``,
and the cache shim (``tools/cache.py``) mediates only atomic-tool calls, so a
workflow composes already-cached, already-emitted results.

The LLM reaches a workflow through a thin atomic-tool wrapper (the engine
template) that declares ``cacheable=False``, ``ttl_class="live-no-cache"`` and
``source_class="workflow_dispatch"``, forwards its arguments verbatim to the
workflow body, and returns the workflow's ``AssessmentEnvelope`` unchanged.

The engine packages here are TELEMAC plus the shared spine it runs on: ``runtime``
(the declaration/plan/journal machinery), ``mesh``, ``shared`` and ``solver``.
"""

from __future__ import annotations

# Import the workflow modules so their @register_tool decorators fire at
# package import time and the LLM-facing wrappers land in TOOL_REGISTRY.
from .telemac import run_telemac as _run_telemac  # noqa: F401  -- registers the TELEMAC local-docker solve specs (SOLVER_WORKFLOW_REGISTRY + LOCAL_SOLVER_SPEC_REGISTRY); the LLM templates are imported by tools/__init__.py

__all__: list[str] = []
