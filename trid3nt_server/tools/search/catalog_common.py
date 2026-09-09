"""The YAML catalog loader, its process-lifetime cache and the typed not-found
error. This module registers NOTHING: the two catalog tools are siblings that share
the loaded catalog through it."""

from __future__ import annotations

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
from trid3nt_server.tools.search.ogc_adapter import OGCAdapterError, fetch_ogc_layer

__all__ = [
    "CatalogNotFoundError",
    "CATALOG_YAML_PATH",
    "load_catalog",
    "user_catalog_path",
    "reset_catalog_cache",
]

logger = logging.getLogger("trid3nt_server.tools.search.catalog_common")


class CatalogNotFoundError(RuntimeError):
    """The requested catalog entry id is not in the YAML catalog. Not retryable:
    a missing id is a configuration error, not a transient failure."""

    error_code: str = "CATALOG_ENTRY_NOT_FOUND"
    retryable: bool = False

def _default_catalog_yaml_path() -> Path:
    """The vendored ``public_data_source_catalog.yaml``, found by walking up from
    this module. ``TRID3NT_CATALOG_YAML`` overrides it outright."""
    env_path = os.environ.get("TRID3NT_CATALOG_YAML")
    if env_path:
        return Path(env_path).expanduser().resolve()
    # The catalog is curator-edited under git at the repo root; the walk normally
    # finds it, and the trailing parents[3] is the fallback if it does not.
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "public_data_source_catalog.yaml"
        if candidate.exists():
            return candidate
    return here.parents[3] / "public_data_source_catalog.yaml"

CATALOG_YAML_PATH = _default_catalog_yaml_path()


def user_catalog_path() -> Path:
    """Where user-accepted catalog entries are appended, kept SEPARATE from the
    vendored catalog, which is never mutated. Resolved at CALL time, not frozen at
    import, so an env override can point it at a temp file."""
    env_path = os.environ.get("TRID3NT_USER_CATALOG_YAML")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return CATALOG_YAML_PATH.parent / "data" / "persistence" / "user_catalog.yaml"


# In-memory catalog cache: lazy-loaded, and refreshed only at process restart.
_CATALOG_CACHE: list[CatalogEntry] | None = None

def _parse_last_verified(raw: Any) -> str:
    """Coerce a YAML ``last_verified`` field into a UTC ISO-Z string. The catalog
    stores it as a bare date while ``CatalogEntry`` demands a datetime, so a date
    widens to midnight UTC."""
    from datetime import datetime, time, timezone

    if hasattr(raw, "isoformat"):
        # date or datetime - coerce to UTC midnight if just a date.
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
    """Parse and validate the ``entries`` rows of a loaded YAML mapping. A
    malformed row is logged and DROPPED, never a crash of the whole load - the
    vendored catalog and the user overlay both come through here."""
    entries: list[CatalogEntry] = []
    for row in (raw or {}).get("entries", []) or []:
        if not isinstance(row, dict):
            logger.warning("skipping non-dict catalog row in %s", source)
            continue
        row = dict(row)  # don't mutate the loaded YAML
        try:
            row["last_verified"] = _parse_last_verified(row.get("last_verified"))
            entries.append(CatalogEntry.model_validate(row))
        except Exception as exc:  # noqa: BLE001 - surface and skip the bad row
            logger.warning(
                "skipping catalog row id=%r in %s — validation failed: %s",
                row.get("id"),
                source,
                exc,
            )
            continue
    return entries


def _merge_user_overlay(base: list[CatalogEntry]) -> list[CatalogEntry]:
    """Merge the user overlay on top of ``base``; the overlay WINS on an id
    collision. A missing or malformed overlay is a no-op - the vendored catalog
    stays authoritative - and one log line fires when the overlay contributes."""
    path = user_catalog_path()
    if not path.exists():
        return base
    try:
        with path.open() as fh:
            raw = yaml.safe_load(fh)
    except Exception as exc:  # noqa: BLE001 - unreadable overlay -> vendored only
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
    """The validated catalog, cached in memory after the first call. The DEFAULT
    path merges the user overlay on top; passing ``yaml_path`` forces a reload of
    that file alone, WITHOUT the overlay and without touching the cache."""
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
        # DEFAULT load path only: fold in the user overlay.
        entries = _merge_user_overlay(entries)
        _CATALOG_CACHE = entries
    logger.info("loaded %d catalog entries from %s", len(entries), path)
    return entries


def reset_catalog_cache() -> None:
    """Clear the in-memory cache so the next ``load_catalog`` rebuilds, picking up
    a freshly written user overlay."""
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


def _reset_catalog_cache_for_tests() -> None:
    """Back-compat alias: tests force-reload the YAML by clearing the cache."""
    reset_catalog_cache()
