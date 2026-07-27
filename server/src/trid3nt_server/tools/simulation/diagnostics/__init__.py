"""``read_run_diagnostics`` -- ONE engine-diagnostics dispatcher (V&V wave, ADR 0021).

Reads a completed simulation run's retained diagnostics files and returns ONE
normalized health envelope, regardless of engine. The LLM never picks an
engine-specific reader: this tool resolves the run handle, recovers the engine
identity from ``completion.json`` (the ``solver`` field the run supervisor now
records; a stdout-field-name fallback for legacy runs), dispatches to the
internal per-engine parser (``sfincs`` / ``swmm`` / ``modflow`` / ``geoclaw`` /
``telemac``), and folds the result into the build-contract diagnostics envelope.

Honesty floor: a value the engine does not report is ``null`` (never invented);
a derived value carries ``mass_balance_source="derived"``; a missing/unparseable
artifact is a typed exception carrying engine + run_id + the offending file,
never a silent ``None`` or a fabricated ``healthy=true``.

ASCII only. No emojis, no typographic dashes.
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
from .geoclaw import parse_geoclaw
from .modflow import parse_modflow
from .sfincs import parse_sfincs
from .swmm import parse_swmm
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
    "trid3nt_server.tools.simulation.diagnostics.read_run_diagnostics"
)

#: Crockford base32 ULID, 26 chars (matches ``trid3nt_contracts.new_ulid``).
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

#: engine -> internal parser. Keys are the canonical normalized engine names.
_PARSERS: dict[str, Callable[[RunArtifacts, str], EngineDiagnostics]] = {
    "sfincs": parse_sfincs,
    "swmm": parse_swmm,
    "modflow": parse_modflow,
    "geoclaw": parse_geoclaw,
    "telemac": parse_telemac,
}

_METADATA = AtomicToolMetadata(
    name="read_run_diagnostics",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)


# --------------------------------------------------------------------------- #
# Handle resolution + engine identity.
# --------------------------------------------------------------------------- #


def _resolve_run_handle(run_handle: str) -> tuple[str | None, str]:
    """Resolve ``run_handle`` -> ``(runs_bucket_or_None, run_id)``.

    Accepts (priority order): a bare ULID; any ``s3://`` uri under the runs
    prefix (the run prefix OR an object beneath it -- extract the FIRST ULID
    path segment); else fail typed. ``runs_bucket`` is the bucket parsed from
    an ``s3://`` handle, else ``None`` (caller falls back to the solver default).
    """
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

    Handles ``solver`` values that are not bare engine names
    (``telemac_river_dye`` -> ``telemac``) AND the legacy stdout-field stems
    (``mf6`` -> ``modflow``). Returns ``None`` for an unrecognized engine (a
    SWAN / Landlab run has no diagnostics parser here).
    """
    if raw is None:
        return None
    r = str(raw).strip().lower()
    if not r:
        return None
    if "sfincs" in r:
        return "sfincs"
    if "swmm" in r:
        return "swmm"
    if "modflow" in r or r == "mf6":
        return "modflow"
    if "geoclaw" in r:
        return "geoclaw"
    if "telemac" in r:
        return "telemac"
    return None


def _recover_engine(completion: dict[str, Any]) -> str:
    """Engine identity: the ``solver`` field (fix), else the stdout-field stem."""
    raw = completion.get("solver")
    if raw is None:
        for key in completion:
            if key.endswith("_stdout_uri"):
                raw = key[: -len("_stdout_uri")]
                break
    engine = _normalize_engine(raw)
    if engine is None:
        raise DiagnosticsEngineUnknown(
            "could not recover the engine identity from completion.json "
            f"(solver={completion.get('solver')!r}, stdout fields="
            f"{[k for k in completion if k.endswith('_stdout_uri')]}); no "
            "diagnostics parser applies."
        )
    return engine


# --------------------------------------------------------------------------- #
# Completion loading (offline fixture dir OR production S3 seam).
# --------------------------------------------------------------------------- #


def _load_completion(
    run_handle: str, run_dir: str | None
) -> tuple[dict[str, Any], str, str, str | None]:
    """Load completion.json + resolve run identity.

    Returns ``(completion, run_id, completion_source, runs_bucket_or_None)``.
    OFFLINE (``run_dir`` set): read ``<run_dir>/completion.json`` and skip S3.
    PRODUCTION: resolve the handle to ``(runs_bucket, run_id)`` and poll S3 via
    the solver seam.
    """
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
    from trid3nt_server.tools.simulation.solver import solver

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


# --------------------------------------------------------------------------- #
# The registered tool.
# --------------------------------------------------------------------------- #


@register_tool(_METADATA)
def read_run_diagnostics(
    run_handle: str,
    *,
    _run_dir: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Read a finished simulation run's engine diagnostics (mass balance, stability).

    **What it does:** Resolves a run handle to its retained diagnostics files and
    returns ONE normalized health envelope for whichever engine produced it
    (SFINCS, SWMM, MODFLOW, GeoClaw, TELEMAC). You do NOT pick an engine-specific
    reader -- this tool recovers the engine from the run's completion record and
    dispatches internally.

    **When to use:**
    - Right after any ``run_solver`` / ``run_model_*`` solve, to check "did the
      run actually converge / conserve mass, or is it garbage?"
    - "Is this flood/groundwater/tsunami run healthy? What's its mass-balance or
      continuity error?"
    - Before trusting a model result for downstream analysis or calibration.

    **When NOT to use:**
    - To compare the model against real observations -> use
      ``compute_skill_metrics`` / ``compute_model_residuals``.
    - To fetch the run's output layers/frames -> use ``list_run_frames``.

    **Parameters:**
    - ``run_handle``: the run id ULID, OR any ``s3://`` run uri (the run prefix
      or a published-output uri beneath it). The tool extracts the run id.

    **Returns:** a dict envelope: ``engine``, ``run_id``, ``status``, ``healthy``
    (coarse heuristic; ``null`` when indeterminate), ``mass_balance_pct`` +
    ``mass_balance_source`` (``reported``/``derived``/``null``), ``instability``,
    ``nonconverged_pct``, ``dry_cells``, ``warnings[]``, ``engine_specific{}``,
    ``sources``, ``notes[]``. Raises a typed error (``RUN_HANDLE_UNRESOLVED``,
    ``DIAGNOSTICS_RUN_NOT_FOUND``, ``DIAGNOSTICS_ENGINE_UNKNOWN``,
    ``DIAGNOSTICS_ARTIFACT_MISSING``, ``DIAGNOSTICS_PARSE_ERROR``) rather than a
    fabricated healthy result.

    Cross-tool dependencies: consumes the run produced by any ``run_solver`` /
    ``run_model_*`` engine tool.
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
        from trid3nt_server.tools.simulation.solver import solver

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
        "warnings": list(diag.warnings),
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
