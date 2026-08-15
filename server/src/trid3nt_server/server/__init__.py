"""``trid3nt_server.server`` package.

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

# Sibling extraction modules (waves 1-3). ``_core`` re-imports every moved name,
# so READS resolve through ``_core``; but a moved function reads a moved
# sibling-scope name as its OWN module global (e.g. ``_register_pending_catalog_offer``
# in :mod:`.interactions` reading ``_CATALOG_OFFER_MAX``). A monkeypatch write
# must therefore land on the module that OWNS the binding, not just ``_core`` --
# else the sibling's internal reference never observes the patch. The facade
# propagates writes to every extraction module that defines the name.
from . import config as _config
from . import dispatch as _dispatch
from . import errors as _errors
from . import interactions as _interactions
from . import protocol as _protocol
from . import reuse as _reuse
from . import session as _session
from . import spatial as _spatial
from . import styles as _styles
from . import turn as _turn

_EXTRACTION_MODULES = (
    _errors,
    _config,
    _interactions,
    _spatial,
    _styles,
    _reuse,
    _dispatch,
    _protocol,
    _session,
    _turn,
)


class _ServerFacade(_ModuleType):
    """Transparent proxy over :mod:`._core` and the sibling extraction modules.

    Preserves the monolith's single-namespace semantics across the package
    split: a bare ``trid3nt_server.server.X`` read resolves to ``_core.X``, and
    a monkeypatch ``setattr(trid3nt_server.server, "X", ...)`` rebinds ``_core.X``
    AND the binding in any sibling extraction module that defines ``X`` -- so a
    moved function reading ``X`` as its own module global observes the patch,
    exactly as when the whole file was one module.
    """

    def __getattr__(self, name: str):
        return getattr(_core, name)

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("__") and name.endswith("__"):
            object.__setattr__(self, name, value)
            return
        setattr(_core, name, value)
        for _mod in _EXTRACTION_MODULES:
            if name in _mod.__dict__:
                setattr(_mod, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(_core, name)
        for _mod in _EXTRACTION_MODULES:
            if name in _mod.__dict__:
                delattr(_mod, name)


_sys.modules[__name__].__class__ = _ServerFacade
