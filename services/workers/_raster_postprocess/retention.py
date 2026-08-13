"""Post-success run-scratch reaper (docs/decisions/0233-runs-retention.md).

A solver's RAW SCRATCH (GeoClaw's ``fort.q*`` AMR frames, etc.) is uploaded to
the runs prefix alongside the publishable artifacts (COGs/charts/json) because
the worker entrypoints upload everything the manifest ``outputs`` glob matches.
Nothing downstream ever re-reads that raw scratch once postprocess has written
its COGs -- and for GeoClaw it is the proven ``XMinioStorageFull`` offender
(~7.7 GB/run, never reaped). This module is the ONE shared reap primitive every
engine's worker calls, on postprocess SUCCESS ONLY (a failed run keeps its
scratch for debugging -- see the entrypoint call sites).

Worker-local: no agent import (mirrors ``upload.py`` in this package).
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Callable, Iterable

LOG = logging.getLogger("trid3nt.worker.raster_postprocess.retention")


def match_scratch_keys(
    relative_keys: Iterable[str],
    patterns: Iterable[str],
    *,
    keep_patterns: Iterable[str] = (),
) -> list[str]:
    """Relative keys matching ANY ``patterns`` glob, minus any matching a
    ``keep_patterns`` glob (keep always wins over a reap pattern).

    Order-preserving over ``relative_keys``, de-duplicated. Pure/no I/O so the
    matching logic is independently testable from the delete side-effect.
    """
    pats = list(patterns)
    keeps = list(keep_patterns)
    out: list[str] = []
    seen: set[str] = set()
    for key in relative_keys:
        if key in seen:
            continue
        if any(fnmatch.fnmatch(key, kp) for kp in keeps):
            continue
        if any(fnmatch.fnmatch(key, p) for p in pats):
            out.append(key)
            seen.add(key)
    return out


def reap_run_scratch(
    delete_fn: Callable[[str], None],
    run_prefix: str,
    relative_keys: Iterable[str],
    patterns: Iterable[str],
    *,
    keep_patterns: Iterable[str] = (),
    logger: logging.Logger | None = None,
) -> dict[str, list[str]]:
    """Delete a run's raw solver scratch by relative key -- best-effort.

    Args:
        delete_fn: ``(relative_key) -> None``, deletes ONE object. The caller
            owns scheme/bucket/prefix resolution (e.g.
            ``lambda rel: s3.delete_object(Bucket=b, Key=f"{run_id}/{rel}")``)
            so this module stays store-agnostic and trivially mockable.
        run_prefix: the run id / prefix -- used only for logging (the actual
            key composition lives in ``delete_fn``).
        relative_keys: the run's ALREADY-UPLOADED relative keys (the entrypoint
            already has this list from its upload sweep -- no S3 LIST call
            needed, so reap can never touch an object this run didn't itself
            just write).
        patterns: fnmatch globs (relative to the run prefix) identifying raw
            scratch, e.g. ``"_output/fort.q*"``.
        keep_patterns: fnmatch globs that are NEVER reaped even if they also
            match a ``patterns`` glob (e.g. gauge time series consumed later).

    Never raises: a delete failure is logged and recorded in ``errors``; the
    reap continues with the remaining keys. A failed/absent postprocess must
    call this ZERO times -- callers gate on postprocess success, not this
    function.

    Returns ``{"deleted": [...], "errors": [...]}`` (both relative-key lists,
    reap-pattern order).
    """
    log = logger or LOG
    to_delete = match_scratch_keys(relative_keys, patterns, keep_patterns=keep_patterns)
    deleted: list[str] = []
    errors: list[str] = []
    for rel in to_delete:
        try:
            delete_fn(rel)
            deleted.append(rel)
        except Exception as exc:  # noqa: BLE001 -- reap failures are never fatal
            log.warning(
                "reap_run_scratch: delete failed run_prefix=%s key=%s: %s",
                run_prefix, rel, exc,
            )
            errors.append(rel)
    if deleted or errors:
        log.info(
            "reap_run_scratch run_prefix=%s deleted=%d errors=%d",
            run_prefix, len(deleted), len(errors),
        )
    return {"deleted": deleted, "errors": errors}
