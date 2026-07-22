"""Per-Case ``.qgs`` lazy-init helpers (job-0121, FR-MP-6 + OQ-62-QGS-MUTATION-CONFLICT).

This module is the single home for the lazy-init policy that resolves
OQ-62-QGS-MUTATION-CONFLICT: rather than every Case mutating a single
shared canonical ``.qgs`` (a hidden global), each Case gets its own
``{case_id}.qgs`` in GCS, copied from the template on first publish.

Why this lives in its own module (not in ``server.py``):

- The lifecycle is purely-functional state on ``CaseSummary.qgs_project_uri``
  + a GCS copy operation, with no WebSocket coupling. Extracting it makes
  the per-Case .qgs policy testable without a live WebSocket.
- The server's role is to *call into* this module when ``publish_layer`` is
  invoked inside an active Case context — see ``server._invoke_tool_via_emitter``
  for the call site.

Lazy-init contract:

1. On first publish inside a Case context (``active_case_id`` set):

   - Read the persisted ``CaseSummary`` via ``Persistence.get_case``.
   - If ``case.qgs_project_uri is None``: compute the case-scoped path
     ``gs://<qgs-bucket>/<case_id>.qgs``, copy the template ``.qgs`` to
     that path via GCS, and persist the URI by calling ``Persistence.upsert_case``
     with the updated summary.
   - Return the case-scoped ``.qgs`` URI (whether freshly-initialized or
     pre-existing) so the publish tool routes its mutation there.

2. Subsequent publishes inside the same Case: the persisted
   ``qgs_project_uri`` is non-None — we return it directly without
   re-copying.

3. Out-of-case publishes (no ``active_case_id``): this module is NOT called;
   ``publish_layer`` falls through to its existing single-tenant default.
   This preserves the M1 demo path verbatim — existing tests still pass.

Bucket policy (TENTATIVE — see OQ-0121-QGS-CASE-BUCKET):

The kickoff names ``gs://grace-2-qgis-projects/{case_id}.qgs`` but the
bucket provisioned by the (now-decommissioned) GCP infra no longer
exists; the resolved default follows the local MinIO naming convention
(``trid3nt-qgs``) and tests assert against the constant, not a literal; the env
override ``GRACE2_CASE_QGS_BUCKET`` lets a future infra job split the
buckets (one for the template, one for case-scoped copies) without code
change. The TEMPLATE ``.qgs`` defaults to ``DEFAULT_PROJECT_QGS_URI`` from
``tools.publish_layer`` so the seed-from-template flow stays consistent
with the existing single-tenant demo.

GCS copy seam:

The actual ``storage.Client.copy_blob`` call is wrapped behind ``_GCS_COPY``
so tests can swap in an in-memory copier without ADC. The production
binding (``main.py``) calls ``set_gcs_copy(real_storage_copy)`` at startup;
tests pass a mock.

Invariants:

- **9. No cost theater.** No cost / quota / quote field anywhere on this seam.
- **3. Engine registration, not modification.** This module knows ``publish_layer``
  needs a ``.qgs`` URI per Case; it does NOT special-case any hazard or
  engine. The seam is engine-agnostic — a future ``publish_vector_layer``
  uses the same resolver.
- **MongoDB MCP canonical persistence (job-0115).** All Case reads/writes
  here go through ``Persistence`` — no custom Mongo wrapper, no direct driver.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable, Protocol

from grace2_contracts import now_utc

from .persistence import Persistence
from .tools.publish_layer import DEFAULT_PROJECT_QGS_URI

logger = logging.getLogger("grace2_agent.case_lifecycle")


# --------------------------------------------------------------------------- #
# Constants & DI seams
# --------------------------------------------------------------------------- #

#: Default bucket holding per-Case ``.qgs`` files. The GCP bucket this seam
#: was built against no longer exists; locally the copy seam is DORMANT
#: (nothing binds ``set_gcs_copy``, so ``ensure_case_qgs`` fail-fasts with
#: ``GCS_COPY_UNBOUND`` before any copy happens). The default follows the
#: local MinIO bucket naming convention; override via
#: ``GRACE2_CASE_QGS_BUCKET``.
DEFAULT_CASE_QGS_BUCKET: str = "trid3nt-qgs"


# Pluggable GCS copy callable. Signature mirrors a minimal subset of
# ``google.cloud.storage.Client.copy_blob`` arguments. Returns the
# destination ``gs://`` URI on success or raises on failure.
GcsCopyCallable = Callable[[str, str], Awaitable[str]] | Callable[[str, str], str]


_GCS_COPY: GcsCopyCallable | None = None


def set_gcs_copy(fn: GcsCopyCallable | None) -> None:
    """Bind the GCS copy callable used by ``ensure_case_qgs``.

    Production binding (``main.py``) injects a function that wraps
    ``google.cloud.storage.Client`` and copies the template blob to the
    case-scoped path. Tests inject an in-memory mock (see
    ``test_case_lifecycle.py``).

    Passing ``None`` clears the binding; calls to ``ensure_case_qgs`` then
    raise ``CaseLifecycleError`` so the lazy-init never silently succeeds
    against a non-existent backend.
    """
    global _GCS_COPY
    _GCS_COPY = fn


def get_gcs_copy() -> GcsCopyCallable | None:
    """Return the bound GCS copy callable, or ``None`` if unbound."""
    return _GCS_COPY


def _get_qgs_bucket() -> str:
    """Resolve the per-Case ``.qgs`` bucket name from env or default.

    Order: ``GRACE2_CASE_QGS_BUCKET`` env var > ``DEFAULT_CASE_QGS_BUCKET``.
    The bucket name is NOT prefixed with ``gs://`` — callers concatenate.
    """
    return os.environ.get("GRACE2_CASE_QGS_BUCKET", DEFAULT_CASE_QGS_BUCKET)


def _get_template_qgs_uri() -> str:
    """Resolve the template ``.qgs`` URI to seed-from on first publish.

    Order: ``GRACE2_CASE_QGS_TEMPLATE`` env var > ``DEFAULT_PROJECT_QGS_URI``
    (the canonical single-tenant project from ``tools.publish_layer``).
    """
    return os.environ.get("GRACE2_CASE_QGS_TEMPLATE", DEFAULT_PROJECT_QGS_URI)


def case_qgs_uri(case_id: str) -> str:
    """Compute the case-scoped ``.qgs`` URI for a Case.

    Pure function — no IO. Useful so tests can assert the URI shape without
    exercising the GCS copy seam.
    """
    bucket = _get_qgs_bucket()
    return f"gs://{bucket}/{case_id}.qgs"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class CaseLifecycleError(RuntimeError):
    """Raised when per-Case ``.qgs`` lazy-init cannot complete.

    ``error_code`` carries a SCREAMING_SNAKE_CASE code so the pipeline strip
    can surface a useful failure narration:

    - ``CASE_NOT_FOUND`` — the requested Case does not exist in Persistence.
    - ``GCS_COPY_UNBOUND`` — ``set_gcs_copy`` was never called; production
      startup should have bound a real copier (FR-CE-8 fail-fast at use).
    - ``GCS_COPY_FAILED`` — the underlying ``copy_blob`` call raised.
    - ``PERSISTENCE_UNBOUND`` — caller passed ``None`` for the Persistence
      instance; the lazy-init seam requires a real persistence layer.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# Lazy-init entry point
# --------------------------------------------------------------------------- #


async def ensure_case_qgs(
    persistence: Persistence | None,
    case_id: str,
    *,
    template_qgs_uri: str | None = None,
) -> str:
    """Lazy-init the per-Case ``.qgs`` and return its URI.

    First call for a Case (when ``CaseSummary.qgs_project_uri is None``):

    1. Copy the template ``.qgs`` to ``gs://<bucket>/<case_id>.qgs``.
    2. Persist the new URI via ``Persistence.upsert_case``.
    3. Return the case-scoped URI.

    Subsequent calls (when ``qgs_project_uri`` is already set): return the
    persisted URI directly. NO re-copy.

    Args:
        persistence: live ``Persistence`` instance. Required.
        case_id: the Case identifier (ULID, matches ``projects._id``).
        template_qgs_uri: optional override for the template path. Defaults
            to ``GRACE2_CASE_QGS_TEMPLATE`` env > ``DEFAULT_PROJECT_QGS_URI``.

    Returns:
        The case-scoped ``.qgs`` URI to mutate.

    Raises:
        CaseLifecycleError: on any failure path. Callers (chiefly
            ``server._invoke_tool_via_emitter`` when invoking ``publish_layer``
            in-Case) should let this propagate so the pipeline strip shows
            the typed error rather than retrying.
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
            f"Case {case_id!r} not found in persistence; cannot lazy-init .qgs",
        )

    # Already initialized — short-circuit. The ``publish_layer`` tool will
    # route its mutation to this URI on the second and subsequent publishes.
    if case.qgs_project_uri:
        logger.debug(
            "case_lifecycle: case=%s qgs_project_uri already set; no copy",
            case_id,
        )
        return case.qgs_project_uri

    # Lazy-init path: copy template -> case-scoped path, persist URI.
    target_uri = case_qgs_uri(case_id)
    template_uri = template_qgs_uri or _get_template_qgs_uri()

    copier = _GCS_COPY
    if copier is None:
        raise CaseLifecycleError(
            "GCS_COPY_UNBOUND",
            "set_gcs_copy(...) was never called; the agent service startup "
            "path must bind a real GCS copier before first publish.",
        )

    logger.info(
        "case_lifecycle lazy-init: case=%s template=%s -> target=%s",
        case_id,
        template_uri,
        target_uri,
    )

    try:
        result = copier(template_uri, target_uri)
        # Allow both sync and async copy callables — the production binding
        # is sync (the storage client's copy_blob is sync); tests use sync
        # mocks too, but async is supported for forward-compat.
        if hasattr(result, "__await__"):
            resolved_uri = await result  # type: ignore[misc]
        else:
            resolved_uri = result  # type: ignore[assignment]
    except CaseLifecycleError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CaseLifecycleError(
            "GCS_COPY_FAILED",
            f"GCS copy {template_uri} -> {target_uri} failed: {exc}",
        ) from exc

    # ``resolved_uri`` MAY be the same as ``target_uri``; we trust the
    # returned value so a future copier can rewrite (e.g. add a generation
    # suffix). Persist whatever the copier reports as the durable URI.
    if not isinstance(resolved_uri, str) or not resolved_uri.startswith("gs://"):
        # Defensive: fall back to the computed target. Better to persist
        # something deterministic than to break the FR-MP-6 flow.
        logger.warning(
            "case_lifecycle: GCS copier returned %r; falling back to %s",
            resolved_uri,
            target_uri,
        )
        resolved_uri = target_uri

    updated = case.model_copy(
        update={
            "qgs_project_uri": resolved_uri,
            "updated_at": now_utc(),
        }
    )
    await persistence.upsert_case(updated)
    logger.info(
        "case_lifecycle: persisted qgs_project_uri=%s for case=%s",
        resolved_uri,
        case_id,
    )
    return resolved_uri


# --------------------------------------------------------------------------- #
# Production object-store copy binding (S3; GCP decommissioned)
# --------------------------------------------------------------------------- #


def default_gcs_copy(template_uri: str, target_uri: str) -> str:
    """Production-default object-store copy implementation (boto3 / S3).

    GCP is decommissioned: the per-Case ``.qgs`` template copy is a
    server-side S3 copy via ``boto3``. The name ``default_gcs_copy`` is kept
    for the historical ``set_gcs_copy`` injection seam; tests inject an
    in-memory mock instead.

    Args:
        template_uri: source object URI (``s3://<bucket>/<key>`` or legacy
            ``gs://<bucket>/<key>`` — the scheme is stripped either way).
        target_uri: destination object URI (same accepted shapes).

    Returns:
        The destination URI on success.

    Raises:
        Whatever the underlying storage client raises — wrapped by
        ``ensure_case_qgs`` into ``CaseLifecycleError(GCS_COPY_FAILED)``.
    """
    import boto3

    def _parse(uri: str) -> tuple[str, str]:
        rest = uri.split("://", 1)[-1]
        slash = rest.find("/")
        return rest[:slash], rest[slash + 1:]

    src_bucket, src_key = _parse(template_uri)
    dst_bucket, dst_key = _parse(target_uri)

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
    s3.copy_object(
        Bucket=dst_bucket,
        Key=dst_key,
        CopySource={"Bucket": src_bucket, "Key": src_key},
    )
    return target_uri


__all__ = [
    "CaseLifecycleError",
    "DEFAULT_CASE_QGS_BUCKET",
    "case_qgs_uri",
    "default_gcs_copy",
    "ensure_case_qgs",
    "get_gcs_copy",
    "set_gcs_copy",
]
