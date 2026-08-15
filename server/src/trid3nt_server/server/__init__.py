"""``trid3nt_server.server`` package (server-refactor wave 1, ADR 0261).

The monolith body now lives in :mod:`._core` and shrinks wave by wave as
regions extract into sibling modules (:mod:`.errors`, :mod:`.config`, ... more
to come). This package presents the SAME surface the single ``server.py``
module did: the facade below proxies attribute READS *and* monkeypatch-style
WRITES on ``trid3nt_server.server.<name>`` straight through to ``_core`` (the
module of record), so no importer -- ``main``, ``telemetry``,
``tool_catalog_http``, ``cases.ingest_user_layer`` -- and no test changes
behavior across the split. Symbols the monolith exposed at
``trid3nt_server.server.X`` still resolve at ``trid3nt_server.server.X``.
"""

from __future__ import annotations

import sys as _sys
from types import ModuleType as _ModuleType

from . import _core


class _ServerFacade(_ModuleType):
    """Transparent proxy over :mod:`._core`.

    Preserves the monolith's single-namespace semantics across the package
    split: a bare ``trid3nt_server.server.X`` read resolves to ``_core.X``, and
    a monkeypatch ``setattr(trid3nt_server.server, "X", ...)`` rebinds
    ``_core.X`` so ``_core``'s own internal references observe the patch --
    exactly as when the whole file was one module.
    """

    def __getattr__(self, name: str):
        return getattr(_core, name)

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("__") and name.endswith("__"):
            object.__setattr__(self, name, value)
        else:
            setattr(_core, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(_core, name)


_sys.modules[__name__].__class__ = _ServerFacade
