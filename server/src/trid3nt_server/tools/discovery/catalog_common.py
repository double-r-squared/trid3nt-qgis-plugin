"""Shared core of the public-data-source catalog tools (split from the
original two-tool ``catalog`` module): the YAML catalog loader + module-level
cache, ``CatalogNotFoundError`` and the test-only cache reset.

This module registers nothing; ``search_data_catalog`` / ``fetch_from_catalog`` are
siblings that share the loaded catalog through this module.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from trid3nt_contracts.catalog import CatalogEntry
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.discovery.ogc_adapter import OGCAdapterError, fetch_ogc_layer

__all__ = [
    "CatalogNotFoundError",
    "CATALOG_YAML_PATH",
    "load_catalog",
    "user_catalog_path",
    "append_user_catalog_entry",
    "reset_catalog_cache",
]

logger = logging.getLogger("trid3nt_server.tools.discovery.catalog_common")


class CatalogNotFoundError(RuntimeError):
    """The requested catalog entry id was not found in the v0.1 YAML catalog.

    Carries an ``error_code="CATALOG_ENTRY_NOT_FOUND"`` for the FR-AS-11 typed-
    error surface. Not retryable — a missing entry id is a configuration error
    rather than a transient failure.
    """

    error_code: str = "CATALOG_ENTRY_NOT_FOUND"
    retryable: bool = False

# Repo-root location of the catalog YAML. Override via env for tests / non-prod.
def _default_catalog_yaml_path() -> Path:
    """Resolve the default ``public_data_source_catalog.yaml`` path.

    The file lives at the repo root for v0.1 (curator-edited under git).
    Walk up from this module's directory to find the repo root; fall back to
    an explicit env override.
    """
    env_path = os.environ.get("TRID3NT_CATALOG_YAML")
    if env_path:
        return Path(env_path).expanduser().resolve()
    # server/src/trid3nt_server/tools/discovery/catalog_common.py -> repo
    # root is 5 levels up (the walk below normally finds it first).
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "public_data_source_catalog.yaml"
        if candidate.exists():
            return candidate
    return here.parents[5] / "public_data_source_catalog.yaml"

CATALOG_YAML_PATH = _default_catalog_yaml_path()


def user_catalog_path() -> Path:
    """Resolve the USER-OVERLAY catalog path (§F.1.2 Mode 2 offer-to-add).

    The overlay is where Mode 2 user-accepted entries are appended -- it is
    SEPARATE from the vendored ``public_data_source_catalog.yaml`` (which is
    never mutated). ``load_catalog`` merges the overlay on top of the vendored
    catalog (overlay wins on id collision). Resolved at call time (not frozen
    at import) so tests can point it at a temp file via the env override.
    Default: ``<repo-root>/data/persistence/user_catalog.yaml``.
    """
    env_path = os.environ.get("TRID3NT_USER_CATALOG_YAML")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return CATALOG_YAML_PATH.parent / "data" / "persistence" / "user_catalog.yaml"


# In-memory catalog cache (lazy-loaded, refreshed at process restart). v0.1
# only — when D.11 ``catalog_entries`` is populated, this becomes a Mongo
# read at the FR-DC-2 ``semi-static-7d`` cadence.
_CATALOG_CACHE: list[CatalogEntry] | None = None

def _parse_last_verified(raw: Any) -> str:
    """Coerce a YAML ``last_verified`` field into a UTC datetime ISO-Z string.

    The seed catalog stores ``last_verified`` as a YAML date (parsed as
    ``datetime.date``). The CatalogEntry pydantic shape demands a
    ``UTCDatetime`` — we widen the date to midnight UTC.
    """
    from datetime import datetime, time, timezone

    if hasattr(raw, "isoformat"):
        # date or datetime — coerce to UTC midnight if just a date.
        if hasattr(raw, "hour"):
            dt = raw
        else:
            dt = datetime.combine(raw, time.min)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if isinstance(raw, str):
        # Tolerate a bare date string like "2026-06-07".
        if "T" not in raw:
            return f"{raw}T00:00:00+00:00"
        return raw
    raise ValueError(f"unsupported last_verified shape: {type(raw).__name__}")

def _parse_catalog_rows(raw: Any, source: str) -> list[CatalogEntry]:
    """Parse + validate the ``entries`` rows of a loaded YAML mapping.

    Shared by the vendored-catalog load and the user-overlay merge so both go
    through the SAME validation (a malformed row is a typed skip -- logged and
    dropped -- never a crash of the whole load).
    """
    entries: list[CatalogEntry] = []
    for row in (raw or {}).get("entries", []) or []:
        if not isinstance(row, dict):
            logger.warning("skipping non-dict catalog row in %s", source)
            continue
        row = dict(row)  # don't mutate the loaded YAML
        try:
            row["last_verified"] = _parse_last_verified(row.get("last_verified"))
            entries.append(CatalogEntry.model_validate(row))
        except Exception as exc:  # noqa: BLE001 — surface + skip the bad row
            logger.warning(
                "skipping catalog row id=%r in %s — validation failed: %s",
                row.get("id"),
                source,
                exc,
            )
            continue
    return entries


def _merge_user_overlay(base: list[CatalogEntry]) -> list[CatalogEntry]:
    """Merge the USER-OVERLAY catalog on top of ``base`` (overlay wins on id).

    §F.1.2 Mode 2 offer-to-add: user-accepted entries live in a separate
    overlay file (``user_catalog_path()``) so the vendored catalog is never
    mutated. A missing / malformed overlay is a no-op (the vendored catalog is
    authoritative); malformed rows inside a readable overlay are typed-skipped
    by ``_parse_catalog_rows``. Emits exactly ONE log line when the overlay
    contributes entries.
    """
    path = user_catalog_path()
    if not path.exists():
        return base
    try:
        with path.open() as fh:
            raw = yaml.safe_load(fh)
    except Exception as exc:  # noqa: BLE001 — unreadable overlay -> vendored only
        logger.warning("user-overlay: unreadable %s — skipped: %s", path, exc)
        return base
    if not isinstance(raw, dict):
        logger.warning(
            "user-overlay: malformed top-level in %s (expected mapping) — skipped",
            path,
        )
        return base

    overlay = _parse_catalog_rows(raw, f"user-overlay {path.name}")
    if not overlay:
        return base

    by_id: dict[str, CatalogEntry] = {e.id: e for e in base}
    overridden = sum(1 for e in overlay if e.id in by_id)
    for e in overlay:
        by_id[e.id] = e  # overlay wins on id collision
    merged = list(by_id.values())
    logger.info(
        "user-overlay: merged %d entries (%d overrode vendored) from %s",
        len(overlay),
        overridden,
        path,
    )
    return merged


def load_catalog(yaml_path: Path | str | None = None) -> list[CatalogEntry]:
    """Load + parse + validate the YAML catalog into a list of CatalogEntry.

    Cached in-memory after the first call. On the DEFAULT load path the
    user-overlay catalog (§F.1.2 Mode 2) is merged on top of the vendored
    catalog (overlay wins on id collision). Pass ``yaml_path=...`` to force a
    reload from a specific vendored file WITHOUT the overlay merge (test
    scaffolding for the vendored file itself).
    """
    global _CATALOG_CACHE
    if yaml_path is None and _CATALOG_CACHE is not None:
        return _CATALOG_CACHE

    path = Path(yaml_path) if yaml_path is not None else CATALOG_YAML_PATH
    if not path.exists():
        raise CatalogNotFoundError(
            f"catalog YAML not found at {path}; set TRID3NT_CATALOG_YAML env var "
            "or place the file at the repo root."
        )

    with path.open() as fh:
        raw = yaml.safe_load(fh)

    entries = _parse_catalog_rows(raw, str(path))

    if yaml_path is None:
        # DEFAULT load path only: fold in the user-overlay (Mode 2 offer-to-add).
        entries = _merge_user_overlay(entries)
        _CATALOG_CACHE = entries
    logger.info("loaded %d catalog entries from %s", len(entries), path)
    return entries


def reset_catalog_cache() -> None:
    """Clear the in-memory catalog cache so the next ``load_catalog`` rebuilds.

    Called after ``append_user_catalog_entry`` so a freshly-added Mode 2 entry
    is visible to ``search_data_catalog`` on the very next call.
    """
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


def _reset_catalog_cache_for_tests() -> None:
    """Back-compat alias: tests force-reload the YAML by clearing the cache."""
    reset_catalog_cache()


def append_user_catalog_entry(entry: CatalogEntry) -> None:
    """Append (or replace-by-id) a Mode 2 entry in the USER-OVERLAY catalog.

    Writes to ``user_catalog_path()`` (NOT the vendored catalog), overlay-wins
    dedup by id (a re-add of the same id replaces the prior overlay row), then
    resets the catalog cache so ``search_data_catalog`` finds the entry on the
    next load. Atomic (tmp + replace) so a crash mid-write can't corrupt the
    overlay. Synchronous file I/O -- callers on the asyncio loop MUST wrap this
    in ``asyncio.to_thread`` (server.py does).
    """
    path = user_catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    if path.exists():
        try:
            with path.open() as fh:
                raw = yaml.safe_load(fh)
            if isinstance(raw, dict):
                rows = [
                    r
                    for r in (raw.get("entries") or [])
                    if isinstance(r, dict)
                ]
        except Exception as exc:  # noqa: BLE001 — corrupt overlay -> start fresh
            logger.warning(
                "user-overlay: could not read %s before append — starting "
                "fresh: %s",
                path,
                exc,
            )
            rows = []

    # Overlay-wins dedup by id: drop any prior row with this id, then append.
    rows = [r for r in rows if r.get("id") != entry.id]
    rows.append(json.loads(entry.model_dump_json()))

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as fh:
        yaml.safe_dump({"entries": rows}, fh, sort_keys=False, default_flow_style=False)
    tmp.replace(path)

    reset_catalog_cache()
    logger.info(
        "user-overlay: appended catalog entry id=%s (overlay now %d entries) at %s",
        entry.id,
        len(rows),
        path,
    )
