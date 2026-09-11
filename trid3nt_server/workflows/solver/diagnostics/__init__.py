"""``read_run_diagnostics`` - ONE engine-diagnostics dispatcher.

The engine is recovered from ``completion.json``; a value the engine does not
report is ``null``, and a missing artifact is a typed exception.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

from ._common import (
    DiagnosticsArtifactMissing,
    DiagnosticsEngineUnknown,
    DiagnosticsError,
    DiagnosticsParseError,
    DiagnosticsRunNotFound,
    EngineDiagnostics,
    RunArtifacts,
    RunHandleUnresolved,
)
from .telemac import parse_telemac

__all__ = [
    "read_run_diagnostics",
    "DiagnosticsError",
    "RunHandleUnresolved",
    "DiagnosticsRunNotFound",
    "DiagnosticsEngineUnknown",
    "DiagnosticsArtifactMissing",
    "DiagnosticsParseError",
]

logger = logging.getLogger(
    "trid3nt_server.workflows.solver.diagnostics.read_run_diagnostics"
)

#: Crockford base32 ULID, 26 chars (matches ``trid3nt_contracts.new_ulid``).
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

#: engine -> internal parser. Keys are the canonical normalized engine names.
_PARSERS: dict[str, Callable[[RunArtifacts, str], EngineDiagnostics]] = {
    "telemac": parse_telemac,
}

_METADATA = AtomicToolMetadata(
    name="read_run_diagnostics",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)




def _resolve_run_handle(run_handle: str) -> tuple[str | None, str]:
    """Resolve ``run_handle`` -> ``(runs_bucket_or_None, run_id)``.
    A bare ULID, or the first ULID path segment of an ``s3://`` uri; anything else
    fails typed. ``runs_bucket`` is ``None`` unless the handle named one."""
    if not run_handle or not str(run_handle).strip():
        raise RunHandleUnresolved(
            "read_run_diagnostics requires a run_handle (a run-id ULID or an "
            "s3:// run uri); got an empty handle."
        )
    s = str(run_handle).strip()
    if _ULID_RE.match(s):
        return None, s
    if s.startswith("s3://"):
        without = s[len("s3://"):]
        parts = [p for p in without.split("/") if p]
        bucket = parts[0] if parts else None
        for seg in parts[1:]:
            if _ULID_RE.match(seg):
                return bucket, seg
        raise RunHandleUnresolved(
            f"no run-id ULID path segment found under s3 handle {run_handle!r}."
        )
    # Any other string: recover the first ULID-looking segment.
    for seg in re.split(r"[/\\]", s):
        if _ULID_RE.match(seg):
            return None, seg
    raise RunHandleUnresolved(
        f"could not recover a run-id ULID from run_handle {run_handle!r}."
    )


def _normalize_engine(raw: Any) -> str | None:
    """Map a solver-spec name / stdout-field stem to a canonical engine.
    Every name returned is a key of ``_PARSERS``; anything else answers ``None``, so
    the caller raises typed rather than reaching a parser that does not exist."""
    if raw is None:
        return None
    r = str(raw).strip().lower()
    if not r:
        return None
    if "telemac" in r:
        return "telemac"
    return None


def _code_staleness_warnings(completion: dict[str, Any]) -> list[str]:
    """The run-vs-code warning as a ``warnings[]`` line, or no lines at all.
    Never raises: a provenance answer must not be able to fail a health read."""
    try:
        from trid3nt_server.workflows.solver.code_provenance import staleness

        warning = staleness(
            code_sha=completion.get("code_sha"),
            engine=str(completion.get("engine") or ""),
            code_dirty=completion.get("code_dirty"),
        )
    except Exception:  # noqa: BLE001 -- provenance never fails a health read
        logger.warning("read_run_diagnostics: code-staleness check failed",
                       exc_info=True)
        return []
    return [warning["message"]] if warning else []


def _recover_engine(completion: dict[str, Any]) -> str:
    """Engine identity: the ``engine`` field (fix), else the stdout-field stem."""
    raw = completion.get("engine")
    if raw is None:
        for key in completion:
            if key.endswith("_stdout_uri"):
                raw = key[: -len("_stdout_uri")]
                break
    engine = _normalize_engine(raw)
    if engine is None:
        raise DiagnosticsEngineUnknown(
            "could not recover the engine identity from completion.json "
            f"(engine={completion.get('engine')!r}, stdout fields="
            f"{[k for k in completion if k.endswith('_stdout_uri')]}); no "
            "diagnostics parser applies."
        )
    return engine




def _load_completion(
    run_handle: str, run_dir: str | None
) -> tuple[dict[str, Any], str, str, str | None]:
    """Load completion.json -> ``(completion, run_id, source, runs_bucket_or_None)``.
    With ``run_dir`` set the file is read from disk and S3 is skipped entirely;
    otherwise the handle resolves and the object is polled through the solver seam."""
    if run_dir is not None:
        path = os.path.join(run_dir, "completion.json")
        if not os.path.exists(path):
            raise DiagnosticsRunNotFound(
                f"no completion.json in run_dir {run_dir!r}."
            )
        try:
            with open(path, "rb") as fh:
                completion = json.loads(fh.read())
        except Exception as exc:  # noqa: BLE001
            raise DiagnosticsParseError(
                "unknown", "unknown", path, f"completion.json unreadable: {exc}"
            ) from exc
        # A handle is still parsed when supplied, but the fixture's own run_id
        # wins offline.
        parsed_id = None
        try:
            _bucket, parsed_id = _resolve_run_handle(run_handle)
        except RunHandleUnresolved:
            parsed_id = None
        run_id = str(completion.get("run_id") or parsed_id or "unknown")
        return completion, run_id, path, None

    runs_bucket, run_id = _resolve_run_handle(run_handle)
    from trid3nt_server.workflows.solver import solver

    bucket = runs_bucket or solver._get_runs_bucket()
    completion = solver._try_get_completion_s3(bucket, run_id)
    if completion is None:
        raise DiagnosticsRunNotFound(
            f"no completion.json at s3://{bucket}/{run_id}/ -- run not found "
            "(or not finished)."
        )
    resolved_id = str(completion.get("run_id") or run_id)
    source = f"s3://{bucket}/{resolved_id}/completion.json"
    return completion, resolved_id, source, bucket




@register_tool(_METADATA)
def read_run_diagnostics(
    run_handle: str,
    *,
    _run_dir: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Read a finished simulation run's engine diagnostics (mass balance, stability).

    **What it does:** returns ONE normalized health envelope for whichever engine
    produced the run - the engine comes from its completion record, never from you.

    **When to use:** right after any solve - did it converge and conserve mass;
    before trusting a result downstream or in calibration.

    **When NOT to use:** comparing against observations
    (``compute_skill_metrics``); fetching layers or frames (``list_run_frames``).

    **Parameters:** ``run_handle`` - a run id ULID or any ``s3://`` uri beneath it.

    **Returns:** one health envelope - ``healthy`` (coarse, ``null`` when
    indeterminate), ``mass_balance_pct`` with its source, ``instability``,
    ``nonconverged_pct``, ``dry_cells``, ``warnings``, ``engine_specific`` and the
    artifact ``sources``. An unresolvable handle, an unknown engine, a missing
    artifact or an unparseable one raises typed rather than reporting health.
    """
    completion, run_id, completion_source, runs_bucket = _load_completion(
        run_handle, _run_dir
    )
    engine = _recover_engine(completion)
    status = str(completion.get("status") or "unknown")

    if _run_dir is not None:
        art = RunArtifacts(
            completion, engine=engine, run_id=run_id, run_dir=_run_dir
        )
    else:
        from trid3nt_server.workflows.solver import solver

        art = RunArtifacts(
            completion,
            engine=engine,
            run_id=run_id,
            reader=solver._read_object_bytes,
        )

    parser = _PARSERS[engine]
    diag = parser(art, status)

    envelope: dict[str, Any] = {
        "engine": engine,
        "run_id": run_id,
        "status": status,
        "healthy": diag.healthy,
        "mass_balance_pct": diag.mass_balance_pct,
        "mass_balance_source": diag.mass_balance_source,
        "instability": diag.instability,
        "nonconverged_pct": diag.nonconverged_pct,
        "dry_cells": diag.dry_cells,
        "warnings": list(diag.warnings) + _code_staleness_warnings(completion),
        "engine_specific": dict(diag.engine_specific),
        "sources": {
            "completion_json": completion_source,
            "diagnostics_files": list(diag.diagnostics_files),
        },
        "notes": list(diag.notes),
    }
    logger.info(
        "read_run_diagnostics: engine=%s run_id=%s status=%s healthy=%s "
        "mass_balance_pct=%s (%s)",
        engine,
        run_id,
        status,
        diag.healthy,
        diag.mass_balance_pct,
        diag.mass_balance_source,
    )
    return envelope
