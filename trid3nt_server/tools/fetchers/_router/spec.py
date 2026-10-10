"""Source-spec loader: schema validation, co-located corpus pickup, tree walk.

``fetchers/**/source.yaml`` validates into a :class:`SourceSpec` keyed by name; a row without
inline ``corpus`` phrasings has them lifted from the sibling ``corpus.yaml``."""

from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from trid3nt_contracts.coverage import PER_RECORD
from trid3nt_contracts.source_spec import SourceSpec

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.spec"
)

__all__ = [
    "SpecLoadError",
    "load_spec",
    "load_spec_from_path",
    "compose_specs_from_tree",
    "record_shape",
    "served_frames",
]

_OFFSET_FETCH = "fetch_vertical_datum_offset"


class SpecLoadError(ValueError):
    """A ``source.yaml`` failed to parse or validate against ``SourceSpec``."""


def _fetchers_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_sibling_corpus(source_yaml: Path, name: str) -> list[str]:
    corpus_path = source_yaml.parent / "corpus.yaml"
    if not corpus_path.exists():
        return []
    try:
        with corpus_path.open() as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001 -- best-effort corpus read (matches search_tools)
        logger.warning("router.spec: failed to parse sibling corpus %s", corpus_path)
        return []
    if not isinstance(data, dict):
        return []
    phrasings = data.get(name) or []
    return [str(q) for q in phrasings if isinstance(q, str)]


@lru_cache(maxsize=1)
def served_frames() -> frozenset[str]:
    """The vertical frames the offset fetch converts between, lower-cased, read off that row's own params."""
    for path in sorted(_fetchers_root().rglob("source.yaml")):
        with path.open() as fh:
            raw = yaml.safe_load(fh) or {}
        if isinstance(raw, dict) and raw.get("name") == _OFFSET_FETCH:
            stated = ((raw.get("params") or {}).get("from_frame") or {})
            return frozenset(str(v).lower() for v in (stated.get("values") or ()))
    return frozenset()


@lru_cache(maxsize=None)
def _module_bytes(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def _hook_sources(spec: SourceSpec) -> list[str]:
    from .hooks import HOOK_REGISTRY

    named = spec.hooks.model_dump(exclude_none=True).values() if spec.hooks else ()
    code = (getattr(HOOK_REGISTRY.get(str(name)), "__code__", None) for name in named)
    return sorted({c.co_filename for c in code if c is not None})


def record_shape(spec: SourceSpec) -> str:
    """The digest of what shapes a record this source lands (rows, ``ingest``, normalization and hook bytes).
    It salts the cache key so a correction here is not hidden by already-cached AOIs until the TTL turns over."""
    stated = {
        "coverage": [row.model_dump(mode="json") for row in spec.coverage],
        "ingest": spec.ingest,
        "normalize": spec.normalize.model_dump(mode="json"),
        "vertical_datum": spec.vertical_datum,
    }
    digest = hashlib.sha256(json.dumps(
        stated, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
    for path in _hook_sources(spec):
        digest.update(_module_bytes(path))
    return digest.hexdigest()[:16]


def _validate_row_datums(spec: SourceSpec, source_hint: str) -> None:
    """Every row states its zero as a frame, per record, or not at all; prose is a zero nothing can convert."""
    frames = served_frames()
    for row in spec.coverage:
        stated = (row.datum or "").strip()
        if not stated or stated == PER_RECORD or stated.lower() in frames:
            continue
        raise SpecLoadError(
            f"{source_hint}: {spec.name}'s {row.data_class} row states its "
            f"datum as {stated!r}, which names no frame the offset service "
            f"converts ({', '.join(sorted(frames))}). State the frame the "
            f"dataset's documentation counts from, {PER_RECORD!r} where each "
            "record carries its own zero, or nothing at all - prose names a "
            "surface nothing can bring another onto.")


#: Longest extent note: the match quotes it whole behind a prefix inside a 300-char wire field;
#: what the sentence leaves out belongs in the caveats.
_NOTE_LIMIT = 200


def _validate_row_notes(spec: SourceSpec, source_hint: str) -> None:
    for row in spec.coverage:
        note = row.extent.note or ""
        if len(note) <= _NOTE_LIMIT:
            continue
        raise SpecLoadError(
            f"{source_hint}: {spec.name}'s {row.data_class} row states an "
            f"extent note of {len(note)} characters, over the {_NOTE_LIMIT} a "
            "note may be. The match quotes it whole behind its own prefix in a "
            "wire field of 300, so a longer note leaves the excluded row "
            "unsendable. State the outline in one sentence and move the rest to "
            "the caveats.")


def load_spec(data: dict, *, source_hint: str = "<dict>") -> SourceSpec:
    """Validate a parsed row mapping into a :class:`SourceSpec`, raising :class:`SpecLoadError`;
    a source carrying a row is never ``internal_only``."""
    if not isinstance(data, dict):
        raise SpecLoadError(f"{source_hint}: row must be a mapping, got {type(data).__name__}")
    try:
        spec = SourceSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecLoadError(f"{source_hint}: invalid SourceSpec: {exc}") from exc
    if spec.coverage and spec.internal_only:
        raise SpecLoadError(
            f"{source_hint}: {spec.name} states {len(spec.coverage)} coverage row(s) "
            "AND internal_only: a ROWED source is one the match may pick and the "
            "gate hands the model on that pick, so it can never be tier=internal. "
            "The internal tier is for an absorbed in-process seam that states NO "
            "row: drop the rows or drop internal_only.")
    _validate_row_datums(spec, source_hint)
    _validate_row_notes(spec, source_hint)
    return spec


def load_spec_from_path(path: Path) -> SourceSpec:
    """Load and validate one ``source.yaml``; an omitted or empty ``corpus`` is lifted from the sibling ``corpus.yaml``."""
    path = Path(path)
    try:
        with path.open() as fh:
            raw = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise SpecLoadError(f"{path}: not found") from exc
    except yaml.YAMLError as exc:
        raise SpecLoadError(f"{path}: YAML parse error: {exc}") from exc

    if not isinstance(raw, dict):
        raise SpecLoadError(f"{path}: top-level YAML must be a mapping")

    spec = load_spec(raw, source_hint=str(path))
    if not spec.corpus:
        sibling = _read_sibling_corpus(path, spec.name)
        if sibling:
            spec = spec.model_copy(update={"corpus": sibling})
    return spec


def compose_specs_from_tree(root: Path | None = None) -> dict[str, SourceSpec]:
    """Walk ``fetchers/**/source.yaml`` and return ``{name: SourceSpec}``; a malformed row is logged and skipped, a duplicate ``name`` is last-wins."""
    base = Path(root) if root is not None else _fetchers_root()
    composed: dict[str, SourceSpec] = {}
    for spath in sorted(base.rglob("source.yaml")):
        try:
            spec = load_spec_from_path(spath)
        except SpecLoadError:
            logger.warning("router.spec: skipping invalid source.yaml at %s", spath, exc_info=True)
            continue
        if spec.name in composed:
            logger.warning(
                "router.spec: duplicate row name %r (%s overrides earlier)",
                spec.name,
                spath,
            )
        composed[spec.name] = spec
    return composed
