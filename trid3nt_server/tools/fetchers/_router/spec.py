"""Source-spec loader: schema validation, co-located corpus pickup, tree walk.

``fetchers/**/source.yaml`` validates into a :class:`SourceSpec` keyed by name. A
spec MAY carry its retrieval ``corpus`` phrasings inline; when it does not, the
loader lifts them verbatim from the sibling ``corpus.yaml`` under the spec name."""

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

#: The fetch whose frames ARE the vocabulary a coverage row states its zero in.
_OFFSET_FETCH = "fetch_vertical_datum_offset"


class SpecLoadError(ValueError):
    """A ``source.yaml`` failed to parse or validate against ``SourceSpec``."""


def _fetchers_root() -> Path:
    """Return the ``fetchers/`` package root this module lives under."""
    # _router/spec.py -> _router -> fetchers
    return Path(__file__).resolve().parents[1]


def _read_sibling_corpus(source_yaml: Path, name: str) -> list[str]:
    """Lift retrieval phrasings for ``name`` from the sibling ``corpus.yaml``,
    returning ``[]`` when that file is absent, malformed, or lacks the key."""
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
    """The vertical frames the offset fetch converts between, lower-cased.

    Read off that spec's own params, because a second list of frame names here
    would drift from the one service that has to answer for them."""
    for path in sorted(_fetchers_root().rglob("source.yaml")):
        with path.open() as fh:
            raw = yaml.safe_load(fh) or {}
        if isinstance(raw, dict) and raw.get("name") == _OFFSET_FETCH:
            stated = ((raw.get("params") or {}).get("from_frame") or {})
            return frozenset(str(v).lower() for v in (stated.get("values") or ()))
    return frozenset()


@lru_cache(maxsize=None)
def _module_bytes(path: str) -> bytes:
    """One hook module's source, or empty for a module with no readable file."""
    try:
        return Path(path).read_bytes()
    except OSError:
        return b""


def _hook_sources(spec: SourceSpec) -> list[str]:
    """The source files of the hooks this spec names, deduped and in one order."""
    from .hooks import HOOK_REGISTRY

    named = spec.hooks.model_dump(exclude_none=True).values() if spec.hooks else ()
    code = (getattr(HOOK_REGISTRY.get(str(name)), "__code__", None) for name in named)
    return sorted({c.co_filename for c in code if c is not None})


def record_shape(spec: SourceSpec) -> str:
    """The digest of what shapes a record this source lands: its coverage rows -
    the columns, the vocabulary they are asked by, their units and the zero they
    are counted from - its normalization, and the bytes of every hook that
    decodes the body.

    It salts the cache key, because the cached artifact was shaped by the
    statements standing at the fetch: a correction landed here would otherwise be
    invisible to every AOI already cached until the TTL bucket turned over, which
    is a fixed source still answering with the wrong record."""
    stated = {
        "coverage": [row.model_dump(mode="json") for row in spec.coverage],
        "normalize": spec.normalize.model_dump(mode="json"),
        "vertical_datum": spec.vertical_datum,
    }
    digest = hashlib.sha256(json.dumps(
        stated, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8"))
    for path in _hook_sources(spec):
        digest.update(_module_bytes(path))
    return digest.hexdigest()[:16]


def _validate_row_datums(spec: SourceSpec, source_hint: str) -> None:
    """Every coverage row states its zero as a frame, per record, or not at all.

    A row's datum is what the match reads and what the offset row is asked in, so
    prose here is a zero nothing can convert: the source is unplaceable against
    every other surface and nothing downstream can say why."""
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


def load_spec(data: dict, *, source_hint: str = "<dict>") -> SourceSpec:
    """Validate an already-parsed spec mapping into a :class:`SourceSpec`.
    Raises :class:`SpecLoadError` wrapping the pydantic ``ValidationError``, so one
    exception type covers both parse and validation failures - and on the one
    invariant pydantic cannot state alone: a source carrying a coverage row is
    never ``internal_only``."""
    if not isinstance(data, dict):
        raise SpecLoadError(f"{source_hint}: spec must be a mapping, got {type(data).__name__}")
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
    return spec


def load_spec_from_path(path: Path) -> SourceSpec:
    """Load and validate one ``source.yaml``, filling corpus from the sibling file:
    an omitted or empty ``corpus`` is lifted from ``corpus.yaml`` under the spec name."""
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
    """Walk ``fetchers/**/source.yaml`` and return ``{name: SourceSpec}``.
    A malformed spec is logged and skipped, never aborting the walk; a duplicate
    ``name`` is last-wins with a warning, deterministic by sorted iteration."""
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
                "router.spec: duplicate source spec name %r (%s overrides earlier)",
                spec.name,
                spath,
            )
        composed[spec.name] = spec
    return composed
