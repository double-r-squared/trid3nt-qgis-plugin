"""Shared core of the ESRI Living Atlas tools: the harvested-catalog entry model,
the two-stratum YAML loader, and a module-level cache.

NATE's two-pool curation rule is STRUCTURAL here: the harvest writes TWO catalog
files -- ``living_atlas_authoritative.yaml`` (ESRI ``contentStatus`` authoritative
badge) and ``living_atlas_community.yaml`` (everything else). The authoritative
stratum is the default data-source surface; the community stratum has ZERO default
quota and surfaces only on explicit opt-in or as a labelled last resort. This
module registers nothing; ``search_living_atlas`` (ranks entries) and
``fetch_living_atlas_layer`` (fetches one entry's bytes) share the loaded catalogs
through it, mirroring how ``catalog_common`` serves the public-catalog tools.

The two YAML files are harvested DATA (produced by ``scripts/harvest_living_atlas.py``),
not code -- excluded from the coded-tool / coded-LOC metric.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

__all__ = [
    "LivingAtlasEntry",
    "CurationClass",
    "SERVICE_TYPES",
    "living_atlas_dir",
    "catalog_path",
    "load_living_atlas",
    "get_entry",
    "reset_living_atlas_cache",
]

logger = logging.getLogger("trid3nt_server.tools.search.living_atlas_common")

CurationClass = Literal["authoritative", "community"]

#: The consumable ArcGIS service types the harvest keeps (web maps/apps/scenes are
#: skipped -- they are not fetchable layers).
SERVICE_TYPES: tuple[str, ...] = ("Image Service", "Feature Service", "Map Service")


class LivingAtlasEntry(BaseModel):
    """One harvested ESRI Living Atlas item, normalized to a fetchable entry.

    Curation is NATE's two-pool discriminant: ``authoritative`` iff ESRI's item
    ``contentStatus`` carries the authoritative badge (``public_authoritative`` /
    ``org_authoritative``); everything else is ``community``.
    """

    schema_version: Literal["v1"] = "v1"
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    snippet: str = ""
    service_url: str = Field(min_length=1)
    service_type: Literal["Image Service", "Feature Service", "Map Service"]
    owner: str = ""
    #: (min_lon, min_lat, max_lon, max_lat) in EPSG:4326, or None when the item
    #: declared no usable extent (supports_global_query stays False regardless).
    extent: tuple[float, float, float, float] | None = None
    authoritative: bool = False
    curation: CurationClass = "community"
    #: True when the item is ESRI premium/subscription content (typeKeywords
    #: ``Requires Subscription`` / ``Requires Credits``). Authoritative gate at
    #: fetch time is the token-required probe -- this is the harvest-time signal.
    premium: bool = False
    tags: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Path resolution (env-overridable; mirrors catalog_common).
# --------------------------------------------------------------------------- #


def _repo_data_dir() -> Path:
    """Walk up from this module to the repo root's ``data/living_atlas`` dir."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "data" / "living_atlas"
        if candidate.exists():
            return candidate
    # Fall back to the conventional location even if not yet created.
    return here.parents[3] / "data" / "living_atlas"


def living_atlas_dir() -> Path:
    """The directory holding the two harvested catalogs (env-overridable)."""
    env_dir = os.environ.get("TRID3NT_LIVING_ATLAS_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return _repo_data_dir()


def catalog_path(curation: CurationClass) -> Path:
    """The YAML path for one curation stratum (per-file env override wins)."""
    env_key = (
        "TRID3NT_LIVING_ATLAS_AUTHORITATIVE_YAML"
        if curation == "authoritative"
        else "TRID3NT_LIVING_ATLAS_COMMUNITY_YAML"
    )
    env_path = os.environ.get(env_key)
    if env_path:
        return Path(env_path).expanduser().resolve()
    return living_atlas_dir() / f"living_atlas_{curation}.yaml"


# --------------------------------------------------------------------------- #
# Load + cache.
# --------------------------------------------------------------------------- #

#: Per-stratum in-memory cache (lazy; refreshed at process restart or reset).
_CACHE: dict[CurationClass, list[LivingAtlasEntry]] = {}


def _parse_rows(raw: Any, source: str) -> list[LivingAtlasEntry]:
    """Validate the ``entries`` rows of a loaded catalog mapping (typed-skip)."""
    if not isinstance(raw, dict):
        logger.warning("living_atlas: %s is not a mapping; treating as empty", source)
        return []
    rows = raw.get("entries") or []
    out: list[LivingAtlasEntry] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        try:
            out.append(LivingAtlasEntry.model_validate(row))
        except ValidationError:
            logger.warning("living_atlas: skipping invalid entry %d in %s", i, source, exc_info=True)
    return out


def load_living_atlas(curation: CurationClass) -> list[LivingAtlasEntry]:
    """Load one curation stratum's entries (cached). Missing file -> ``[]``.

    A missing catalog is honest-empty (the harvest has not run / this stratum has
    no members), NOT an error -- ``search_living_atlas`` degrades to the other
    stratum or an empty result, never a fabricated entry.
    """
    if curation in _CACHE:
        return _CACHE[curation]
    path = catalog_path(curation)
    if not path.exists():
        logger.info("living_atlas: no %s catalog at %s (honest-empty)", curation, path)
        _CACHE[curation] = []
        return _CACHE[curation]
    try:
        with path.open() as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        logger.warning("living_atlas: failed to read %s", path, exc_info=True)
        _CACHE[curation] = []
        return _CACHE[curation]
    entries = _parse_rows(data, str(path))
    _CACHE[curation] = entries
    logger.info("living_atlas: loaded %d %s entries from %s", len(entries), curation, path)
    return entries


def get_entry(item_id_or_url: str) -> tuple[LivingAtlasEntry, CurationClass] | None:
    """Resolve one entry by item id OR exact service_url, across both strata.

    Authoritative is searched first so an id that (impossibly) lived in both
    resolves to the authoritative copy. Returns ``None`` when unknown.
    """
    needle = (item_id_or_url or "").strip()
    if not needle:
        return None
    for curation in ("authoritative", "community"):
        for entry in load_living_atlas(curation):  # type: ignore[arg-type]
            if entry.id == needle or entry.service_url.rstrip("/") == needle.rstrip("/"):
                return entry, curation  # type: ignore[return-value]
    return None


def reset_living_atlas_cache() -> None:
    """Clear the in-memory cache (test seam; also after a re-harvest)."""
    _CACHE.clear()
