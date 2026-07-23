"""Per-Case ``.qgs`` lazy-init helpers (job-0121, FR-MP-6 + OQ-62-QGS-MUTATION-CONFLICT).

This module is the single home for the lazy-init policy that resolves
OQ-62-QGS-MUTATION-CONFLICT: rather than every Case mutating a single
shared canonical ``.qgs`` (a hidden global), each Case would get its own
``{case_id}.qgs``, copied from the template on first publish.

Why this lives in its own module (not in ``server.py``):

- The lifecycle is purely-functional state on ``CaseSummary.qgs_project_uri``
  with no WebSocket coupling. Extracting it makes the per-Case .qgs policy
  testable without a live WebSocket.
- The server's role is to *call into* this module when ``publish_layer`` is
  invoked inside an active Case context — see ``server._invoke_tool_via_emitter``
  for the call site.

Lazy-init contract:

1. On first publish inside a Case context (``active_case_id`` set):

   - Read the persisted ``CaseSummary`` via ``Persistence.get_case``.
   - If ``case.qgs_project_uri`` is already set: return it directly (no
     re-provisioning).
   - Otherwise: per-Case ``.qgs`` provisioning is NOT implemented on the
     local build — raise the typed ``CaseLifecycleError`` below. The caller
     (``server._invoke_tool_via_emitter``) catches this and falls back to
     the single-tenant default ``.qgs`` (``DEFAULT_PROJECT_QGS_URI``),
     exactly as it does for any out-of-case publish.

2. Out-of-case publishes (no ``active_case_id``): this module is NOT called;
   ``publish_layer`` falls through to its existing single-tenant default.
   This preserves the M1 demo path verbatim.

History: an earlier revision minted a per-Case ``gs://<bucket>/<case_id>.qgs``
target URI and copied the template to it via a pluggable ``set_gcs_copy(...)``
DI seam. That seam was never bound in production (``main.py`` never called
``set_gcs_copy``), so every lazy-init attempt already failed honestly at the
"no copier bound" check — the DI plumbing and the gs:// URI minting were
dead weight around a fail-fast that always fired. Removed outright rather
than reshimmed: this module now fails fast directly, with no unreachable
success path to maintain.

Invariants:

- **9. No cost theater.** No cost / quota / quote field anywhere on this seam.
- **3. Engine registration, not modification.** This module knows ``publish_layer``
  needs a ``.qgs`` URI per Case; it does NOT special-case any hazard or
  engine. The seam is engine-agnostic — a future ``publish_vector_layer``
  uses the same resolver.
- **MongoDB MCP canonical persistence (job-0115).** All Case reads here go
  through ``Persistence`` — no custom Mongo wrapper, no direct driver.
"""

from __future__ import annotations

import logging

from .persistence import Persistence

logger = logging.getLogger("trid3nt_server.case_lifecycle")


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class CaseLifecycleError(RuntimeError):
    """Raised when per-Case ``.qgs`` lazy-init cannot complete.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code so the pipeline strip
    can surface a useful failure narration:

    - ``CASE_NOT_FOUND`` — the requested Case does not exist in Persistence.
    - ``PER_CASE_QGS_UNAVAILABLE`` — the Case has no ``qgs_project_uri`` yet
      and per-Case ``.qgs`` provisioning is not implemented on the local
      build. Callers fall back to the single-tenant default ``.qgs``.
    - ``PERSISTENCE_UNBOUND`` — caller passed ``None`` for the Persistence
      instance; the lazy-init seam requires a real persistence layer.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# Lazy-init entry point
# --------------------------------------------------------------------------- #


async def ensure_case_qgs(persistence: Persistence | None, case_id: str) -> str:
    """Resolve the per-Case ``.qgs`` URI, or fail-fast honestly.

    - If the Case already has ``qgs_project_uri`` set (a prior explicit
      assignment): return it directly. NO re-provisioning.
    - Otherwise: per-Case ``.qgs`` provisioning is not implemented on the
      local build — raise ``CaseLifecycleError(PER_CASE_QGS_UNAVAILABLE)``.

    Args:
        persistence: live ``Persistence`` instance. Required.
        case_id: the Case identifier (ULID, matches ``projects._id``).

    Returns:
        The case-scoped ``.qgs`` URI to mutate.

    Raises:
        CaseLifecycleError: on any failure path. Callers (chiefly
            ``server._invoke_tool_via_emitter`` when invoking ``publish_layer``
            in-Case) should let this propagate so the pipeline strip shows
            the typed error rather than retrying — that call site currently
            catches it and falls back to the single-tenant default ``.qgs``.
    """
    if persistence is None:
        raise CaseLifecycleError(
            "PERSISTENCE_UNBOUND",
            "ensure_case_qgs requires a Persistence instance; got None. "
            "Production startup binds the singleton; tests inject a mock.",
        )

    case = await persistence.get_case(case_id)
    if case is None:
        raise CaseLifecycleError(
            "CASE_NOT_FOUND",
            f"Case {case_id!r} not found in persistence; cannot resolve .qgs",
        )

    # Already initialized — short-circuit. The ``publish_layer`` tool will
    # route its mutation to this URI.
    if case.qgs_project_uri:
        logger.debug(
            "case_lifecycle: case=%s qgs_project_uri already set; using it",
            case_id,
        )
        return case.qgs_project_uri

    raise CaseLifecycleError(
        "PER_CASE_QGS_UNAVAILABLE",
        f"Case {case_id!r} has no qgs_project_uri and per-Case .qgs "
        "provisioning is not implemented on the local build; falls back "
        "to the single-tenant default .qgs.",
    )


__all__ = [
    "CaseLifecycleError",
    "ensure_case_qgs",
]
