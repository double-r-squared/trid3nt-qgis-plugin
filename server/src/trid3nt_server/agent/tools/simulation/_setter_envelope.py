"""Shared machinery for the group-D parameter setters (LANE D EXCLUSIVE).

``set_sfincs_parameters`` / ``set_swmm_parameters`` / ``set_modflow_parameters``
(the V&V-wave calibration primitives, docs/validation/build-contract.md section
3.4) all share the same shape: copy-on-write a parent model directory/file into
a fresh child, apply named-parameter changes via the engine package's own API,
read the written values back, and return one ``SetterEnvelope`` dict. This
module is the common seam so that shape is not hand-rolled three times.

Bounds policy (conformed to build-contract.md section 3.4):
``plausibility[].in_range=false`` is a WARNING carried honestly in the
envelope, NOT a hard reject -- a user may intentionally set an atypical
out-of-plausible-range value (a very high Manning's n, an implausibly large K)
and the setter proceeds, recording the value with ``in_range=false`` and a note
explaining it is atypical. A HARD typed ``BoundsViolation`` is reserved ONLY
for a mathematically / physically MEANINGLESS value -- a negative Manning's n,
hydraulic conductivity ``<= 0``, percent-impervious outside 0-100, a negative
infiltration rate -- because such a value cannot be written into a runnable
deck at all. Each ``PhysicalBound`` therefore carries BOTH a soft plausible
band (``lo``/``hi`` -> warning) and an optional hard floor/ceiling
(``hard_min``/``hard_max`` -> ``BoundsViolation``).

Copy-on-write + publish: the parent model is only ever READ (via
``trid3nt_server.agent.tools.simulation.solver``'s scheme-dispatched
``_read_object_bytes`` / ``_download_object`` / ``_get_s3_client`` seams --
reused, not reimplemented); every write lands under a fresh child directory.
``publish_child`` best-effort mirrors the child to
``s3://<runs_bucket>/param_setter/<engine>/<child_id>/...`` when the parent was
itself an ``s3://`` handle (production shape); otherwise (and in every offline
test) it stays a plain local directory addressed by a ``file://`` URI -- ZERO
network, matching the offline-first hard rule. Which happened is always stated
in the envelope's ``notes[]`` (never a silent partial success).

ASCII hyphens only.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid

__all__ = [
    "SetterError",
    "SetterInputError",
    "BoundsViolation",
    "SetterUpstreamError",
    "PhysicalBound",
    "check_bounds",
    "new_child_id",
    "stage_parent",
    "publish_child",
    "build_setter_envelope",
    "utc_now_iso",
]

logger = logging.getLogger("trid3nt_server.agent.tools.simulation.param_setters")


# --------------------------------------------------------------------------- #
# Typed errors (FR-AS-11 convention: error_code + retryable class attrs).
# --------------------------------------------------------------------------- #


class SetterError(RuntimeError):
    """Base class for group-D parameter-setter failures."""

    error_code: str = "PARAM_SETTER_ERROR"
    retryable: bool = True


class SetterInputError(SetterError):
    """Malformed request: unknown parameter name, bad op, missing value/factor,
    unreadable parent model. Never retryable -- the caller must fix the args."""

    error_code = "PARAM_SETTER_INPUT_INVALID"
    retryable = False


class BoundsViolation(SetterError):
    """A requested value (or a scale factor applied to the current value) is
    mathematically / physically MEANINGLESS -- it violates the named engine
    parameter's HARD floor/ceiling (negative Manning's n, K <= 0,
    percent-impervious outside 0-100, negative infiltration).

    This is a hard error, NOT the soft out-of-plausible-range warning
    (``plausibility[].in_range=false``, which proceeds). Carries the offending
    engine/param/value/bound/reason so the agent surface can render a precise,
    honest message rather than a bare ValueError. See the module docstring.
    """

    error_code = "PARAM_BOUNDS_VIOLATION"
    retryable = False

    def __init__(
        self,
        *,
        engine: str,
        param: str,
        value: float,
        bound: "PhysicalBound",
        reason: str | None = None,
    ) -> None:
        self.engine = engine
        self.param = param
        self.value = value
        self.bound = bound
        self.reason = reason
        detail = f" -- {reason}" if reason else ""
        message = (
            f"{engine} parameter {param!r}={value!r} {bound.unit} is physically "
            f"meaningless{detail}; this is a hard validation error, not an "
            f"out-of-plausible-range warning ({bound.source})"
        )
        super().__init__(message)


class SetterUpstreamError(SetterError):
    """Parent-model read, package-API call, or child write/publish failed for
    a reason that is not a bad request (I/O error, missing dependency,
    unsupported deck shape). Retryable -- a transient staging hiccup may clear."""

    error_code = "PARAM_SETTER_UPSTREAM_ERROR"
    retryable = True


# --------------------------------------------------------------------------- #
# Physical bounds table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PhysicalBound:
    """One named parameter's plausible band + optional hard floor/ceiling.

    ``lo``/``hi`` = the soft PLAUSIBLE band (outside -> ``in_range=false``
    WARNING, proceeds). ``hard_min``/``hard_max`` = the hard PHYSICAL floor /
    ceiling; a value below ``hard_min`` (``<=`` when ``hard_min_exclusive``) or
    above ``hard_max`` is mathematically / physically meaningless and raises
    ``BoundsViolation``. ``None`` hard bounds mean "no hard limit on that side".
    """

    lo: float
    hi: float
    unit: str
    source: str
    hard_min: float | None = None
    hard_min_exclusive: bool = False
    hard_max: float | None = None


def _hard_bound_reason(bound: PhysicalBound, v: float) -> str | None:
    """Return a reason string when ``v`` is physically meaningless, else None."""
    if bound.hard_min is not None:
        if bound.hard_min_exclusive and v <= bound.hard_min:
            return f"must be strictly greater than {bound.hard_min:g} {bound.unit}"
        if not bound.hard_min_exclusive and v < bound.hard_min:
            return (
                f"must be >= {bound.hard_min:g} {bound.unit} "
                "(a negative value is non-physical)"
            )
    if bound.hard_max is not None and v > bound.hard_max:
        return f"must be <= {bound.hard_max:g} {bound.unit}"
    return None


def check_bounds(
    *, engine: str, param: str, value: float, table: dict[str, PhysicalBound]
) -> dict[str, Any]:
    """Validate ``value`` against ``table[param]``; return a plausibility entry.

    Raises ``SetterInputError`` when ``param`` has no bounds entry (an unknown
    parameter name is a malformed request, not a physical-plausibility
    question). Raises ``BoundsViolation`` (HARD) ONLY when the value is
    physically meaningless (below/above the hard floor/ceiling -- negative
    Manning's n, K <= 0, percent-impervious outside 0-100, negative
    infiltration). An out-of-PLAUSIBLE-band-but-still-physical value is NOT a
    hard error: it returns a section-3.4 plausibility dict with
    ``in_range=false`` and an explanatory note, and the caller proceeds
    (build-contract 3.4 -- a user may intentionally set an atypical value).
    """
    bound = table.get(param)
    if bound is None:
        raise SetterInputError(
            f"{engine} parameter {param!r} has no bounds-table entry; known "
            f"parameters: {sorted(table)}"
        )
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise SetterInputError(
            f"{engine} parameter {param!r} value must be numeric; got {value!r}"
        ) from exc

    reason = _hard_bound_reason(bound, v)
    if reason is not None:
        raise BoundsViolation(
            engine=engine, param=param, value=v, bound=bound, reason=reason
        )

    in_range = bound.lo <= v <= bound.hi
    note = bound.source
    if not in_range:
        note = (
            f"{bound.source}; value {v:g} is OUTSIDE the plausible band "
            f"[{bound.lo:g}, {bound.hi:g}] {bound.unit} -- carried as a WARNING "
            "(an intentional out-of-range value proceeds; it is atypical, not "
            "physically meaningless)"
        )
    return {
        "param": param,
        "value": round(v, 8),
        "in_range": in_range,
        "range": [bound.lo, bound.hi],
        "note": note,
    }


# --------------------------------------------------------------------------- #
# Handle IO: copy-on-write staging + best-effort publish.
# --------------------------------------------------------------------------- #


def new_child_id() -> str:
    return new_ulid()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_path_from_uri(uri: str) -> Path:
    if uri.startswith("file://"):
        return Path(uri[len("file://"):])
    return Path(uri)


def _iter_s3_keys(s3: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []) or []:
            keys.append(obj["Key"])
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return keys


def stage_parent(
    parent_model_uri: str,
    dest_dir: Path,
    *,
    is_dir: bool,
) -> Path:
    """Copy the parent model (read-only) into ``dest_dir`` (copy-on-write).

    ``is_dir=True``: ``parent_model_uri`` addresses a whole model directory
    (SFINCS deck root, MODFLOW ``sim_ws``) -- every object/file under it is
    copied, preserving relative structure, and ``dest_dir`` is returned.
    ``is_dir=False``: ``parent_model_uri`` addresses ONE file (a SWMM
    ``.inp``) -- that file is copied into ``dest_dir`` and its new path is
    returned. The parent is never opened for writing.

    Resolution is scheme-dispatched exactly like ``solver.py``'s own staging
    (``s3://`` via boto3, ``file://`` / a bare path via the filesystem) --
    reuses ``solver._get_s3_client`` / ``solver._download_object`` rather
    than reimplementing S3 IO. Zero network for ``file://`` / bare-path
    parents, which is what every offline unit test uses.
    """
    if not isinstance(parent_model_uri, str) or not parent_model_uri.strip():
        raise SetterInputError(
            f"parent_model_uri must be a non-empty string; got {parent_model_uri!r}"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)

    if parent_model_uri.startswith("s3://"):
        from .solver.solver import SolverDispatchError, _download_object, _get_s3_client

        bucket, _, key = parent_model_uri[len("s3://"):].partition("/")
        if not bucket or not key:
            raise SetterInputError(f"malformed s3:// parent_model_uri: {parent_model_uri!r}")
        try:
            s3 = _get_s3_client()
            if is_dir:
                prefix = key if key.endswith("/") else key + "/"
                keys = _iter_s3_keys(s3, bucket, prefix)
                if not keys:
                    raise SetterInputError(
                        f"no objects found under s3://{bucket}/{prefix} "
                        "(parent_model_uri does not resolve to a model directory)"
                    )
                for obj_key in keys:
                    rel = obj_key[len(prefix):]
                    if not rel:  # the "directory marker" object itself
                        continue
                    dest = dest_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _download_object(f"s3://{bucket}/{obj_key}", dest)
                return dest_dir
            dest = dest_dir / Path(key).name
            _download_object(parent_model_uri, dest)
            return dest
        except SetterInputError:
            raise
        except SolverDispatchError as exc:
            raise SetterUpstreamError(f"parent model staging failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise SetterUpstreamError(
                f"parent model staging failed for {parent_model_uri}: {exc}"
            ) from exc

    local = _local_path_from_uri(parent_model_uri)
    if not local.exists():
        raise SetterInputError(f"parent_model_uri path not found: {local}")
    if is_dir:
        if not local.is_dir():
            raise SetterInputError(
                f"parent_model_uri must be a directory for this engine; got a file: {local}"
            )
        shutil.copytree(local, dest_dir, dirs_exist_ok=True)
        return dest_dir
    if local.is_dir():
        raise SetterInputError(
            f"parent_model_uri must be a single file for this engine; got a directory: {local}"
        )
    dest = dest_dir / local.name
    shutil.copy2(local, dest)
    return dest


def publish_child(
    child_local_root: Path,
    *,
    engine: str,
    child_id: str,
    manifest: dict[str, Any],
    prefer_s3: bool,
) -> dict[str, str]:
    """Write ``manifest.json`` next to the child model and best-effort mirror
    the whole child tree to S3 when ``prefer_s3`` (the parent came from
    ``s3://``). Returns ``{"child_setup_uri", "model_root_uri", "storage"}``
    -- ``storage`` is ``"s3"`` or ``"local"`` so the caller can state plainly
    in ``notes[]`` which one actually happened (never silently claim S3 when
    the upload failed).
    """
    manifest_path = child_local_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    local_model_root_uri = f"file://{child_local_root / 'model'}"
    local_manifest_uri = f"file://{manifest_path}"

    if not prefer_s3:
        return {
            "child_setup_uri": local_manifest_uri,
            "model_root_uri": local_model_root_uri,
            "storage": "local",
        }

    try:
        from .solver.solver import _get_runs_bucket, _get_s3_client, _upload_file_s3

        s3 = _get_s3_client()
        bucket = _get_runs_bucket()
        base_key = f"param_setter/{engine}/{child_id}/"
        for path in child_local_root.rglob("*"):
            if path.is_file():
                rel = path.relative_to(child_local_root).as_posix()
                _upload_file_s3(s3, path, bucket, base_key + rel)
        return {
            "child_setup_uri": f"s3://{bucket}/{base_key}manifest.json",
            "model_root_uri": f"s3://{bucket}/{base_key}model/",
            "storage": "s3",
        }
    except Exception as exc:  # noqa: BLE001 -- best-effort mirror only
        logger.warning(
            "param_setter S3 publish failed for engine=%s child_id=%s (%s); "
            "keeping the child local-only",
            engine,
            child_id,
            exc,
        )
        return {
            "child_setup_uri": local_manifest_uri,
            "model_root_uri": local_model_root_uri,
            "storage": "local",
        }


def build_setter_envelope(
    *,
    engine: str,
    child_setup_uri: str,
    parent_model: str,
    changes_applied: list[dict[str, Any]],
    plausibility: list[dict[str, Any]],
    notes: list[str],
) -> dict[str, Any]:
    """Assemble the section-3.4 SetterEnvelope plain dict."""
    return {
        "engine": engine,
        "child_setup_uri": child_setup_uri,
        "parent_model": parent_model,
        "changes_applied": changes_applied,
        "plausibility": plausibility,
        "notes": notes,
    }


def make_work_dir(_work_dir: str | None) -> Path:
    """Scratch root for a setter call: the private ``_work_dir`` test seam
    when given (hermetic, tmp_path-backed offline tests), else a fresh
    ``tempfile.mkdtemp`` (ephemeral production scratch -- mirrors the
    "local rundir is scratch, may be reaped" convention; the durable copy is
    whatever ``publish_child`` mirrors to S3)."""
    if _work_dir:
        p = Path(_work_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return Path(tempfile.mkdtemp(prefix="param_setter_"))
