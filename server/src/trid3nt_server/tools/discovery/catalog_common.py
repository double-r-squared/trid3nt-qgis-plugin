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

def load_catalog(yaml_path: Path | str | None = None) -> list[CatalogEntry]:
    """Load + parse + validate the YAML catalog into a list of CatalogEntry.

    Cached in-memory after the first call. Pass ``yaml_path=...`` to force a
    reload from a different file (test scaffolding).
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

    entries: list[CatalogEntry] = []
    for row in raw.get("entries", []) or []:
        row = dict(row)  # don't mutate the loaded YAML
        row["last_verified"] = _parse_last_verified(row.get("last_verified"))
        try:
            entries.append(CatalogEntry.model_validate(row))
        except Exception as exc:  # noqa: BLE001 — surface the bad row
            logger.warning(
                "skipping catalog row id=%r — validation failed: %s",
                row.get("id"),
                exc,
            )
            continue

    if yaml_path is None:
        _CATALOG_CACHE = entries
    logger.info("loaded %d catalog entries from %s", len(entries), path)
    return entries

def _reset_catalog_cache_for_tests() -> None:
    """Tests force-reload the YAML by clearing the in-memory cache."""
    global _CATALOG_CACHE
    _CATALOG_CACHE = None
