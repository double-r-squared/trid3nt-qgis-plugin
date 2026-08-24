"""Source-spec loader: schema validation + co-located corpus pickup + tree walk.

Mirrors ``search_tools._compose_corpus_from_tree`` (which rglobs
``corpus.yaml``): ``_compose_specs_from_tree`` rglobs ``fetchers/**/source.yaml``,
validates each into a :class:`SourceSpec`, and returns ``{name: spec}``. Clean-
as-you-go (contract sec 1): when a twin dies in phase 2 its ``fetch_X.py`` is
deleted and ``source.yaml`` remains as the sole surface.

Co-located corpus pickup (contract sec 3.3 point 3): the spec MAY carry its
retrieval ``corpus`` phrasings inline; when it does not, the loader lifts them
verbatim from the sibling ``corpus.yaml`` under the twin's name, so the phrasings
route to the virtual tool with zero index change.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from trid3nt_contracts.source_spec import SourceSpec

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.spec"
)

__all__ = [
    "SpecLoadError",
    "load_spec",
    "load_spec_from_path",
    "compose_specs_from_tree",
]


class SpecLoadError(ValueError):
    """A ``source.yaml`` failed to parse or validate against ``SourceSpec``."""


def _fetchers_root() -> Path:
    """Return the ``fetchers/`` package root this module lives under."""
    # _router/spec.py -> _router -> fetchers
    return Path(__file__).resolve().parents[1]


def _read_sibling_corpus(source_yaml: Path, name: str) -> list[str]:
    """Lift retrieval phrasings for ``name`` from the sibling ``corpus.yaml``.

    Returns ``[]`` when the sibling file is absent / malformed / lacks the key.
    """
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


def load_spec(data: dict, *, source_hint: str = "<dict>") -> SourceSpec:
    """Validate an already-parsed spec mapping into a :class:`SourceSpec`.

    Raises :class:`SpecLoadError` (wrapping the pydantic ``ValidationError``) so
    callers get one exception type for both parse and validation failures.
    """
    if not isinstance(data, dict):
        raise SpecLoadError(f"{source_hint}: spec must be a mapping, got {type(data).__name__}")
    try:
        return SourceSpec.model_validate(data)
    except ValidationError as exc:
        raise SpecLoadError(f"{source_hint}: invalid SourceSpec: {exc}") from exc


def load_spec_from_path(path: Path) -> SourceSpec:
    """Load + validate one ``source.yaml``, filling corpus from the sibling file.

    If the spec omits ``corpus`` (or leaves it empty), the loader lifts the
    phrasings from the sibling ``corpus.yaml`` under the spec's ``name``.
    """
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

    A single malformed spec does NOT abort the whole compose -- it is logged and
    skipped (a broken co-located file never takes down startup), mirroring the
    best-effort corpus tree walk. Duplicate ``name`` keys: last-wins with a
    warning (the loader is deterministic via ``sorted`` iteration).
    """
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
