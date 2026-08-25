"""``trid3nt_server.server`` -- the daemon core.

The turn engine, WS connection loop, tool dispatch, gate waits, and session
state live in the ``session/`` ``turn/`` ``dispatch/`` ``protocol/`` subpackages
(plus the evicted gate engine in ``trid3nt_server.gates.confirm``). This package
presents ONE ``trid3nt_server.server.<name>`` namespace: the facade below proxies
attribute reads across the leaf modules and propagates monkeypatch writes to the
leaf that owns the binding, so importers and tests see a single flat surface.
"""

from __future__ import annotations

import sys as _sys
from types import ModuleType as _ModuleType

from .session import case_state as _session_case_state
from .session import persistence_ref as _session_persistence_ref
from .session import state as _session_state
from .turn import cases as _turn_cases
from .turn import engine as _turn_engine
from .turn import live_turn as _turn_live_turn
from .turn import stream as _turn_stream
from .turn import wire as _turn_wire
from .dispatch import aoi as _dispatch_aoi
from .dispatch import emitter as _dispatch_emitter
from .dispatch import helpers as _dispatch_helpers
from .dispatch import persist as _dispatch_persist
from .dispatch import results as _dispatch_results
from .dispatch import reuse as _dispatch_reuse
from .protocol import auth as _protocol_auth
from .protocol import connections as _protocol_connections
from .protocol import handlers as _protocol_handlers
from .protocol import loop as _protocol_loop
from . import config as _config
from . import errors as _errors
from . import interactions as _interactions
from . import spatial as _spatial
from ..gates import confirm as _gates_confirm
from ..gates import pending as _gates_pending
from ..gates.cards import solver_confirm as _gates_cards_confirm

# Facade read order; monkeypatch writes propagate to EVERY leaf already binding
# the name (so a leaf reading it as its own global sees the patch).
_LEAF_MODULES = (
    _session_state,
    _session_persistence_ref,
    _session_case_state,
    _turn_wire,
    _turn_live_turn,
    _turn_cases,
    _turn_engine,
    _turn_stream,
    _dispatch_helpers,
    _dispatch_reuse,
    _dispatch_persist,
    _dispatch_aoi,
    _dispatch_emitter,
    _dispatch_results,
    _protocol_connections,
    _protocol_auth,
    _protocol_handlers,
    _protocol_loop,
    _errors,
    _config,
    _interactions,
    _spatial,
    _gates_confirm,
    _gates_pending,
    _gates_cards_confirm,
)

__all__ = [
    "run_server",
    "SessionState",
    "_invoke_tool_via_emitter",
    "_maybe_gate_on_payload_warning",
    "_parse_invoke_directive",
    "get_persistence",
    "set_persistence",
    "init_persistence_from_env",
    "inflight_turn_count",
    "_emit_case_list",
    "_emit_case_open",
    "_handle_case_command",
    "_handle_dev_tool_invoke",
    "_persist_chat_turn",
    "_drain_bg_tasks",
    "_turn_case_id",
    "_dispatch_tool_and_persist",
    "_dispatch_model_turn_and_persist",
    "_auto_create_case_from_root",
    "_emit_auto_case_open",
    "_prepare_user_turn",
    "_handle_secret_add",
    "_inject_secret_ref",
    "_maybe_handle_credential_error",
    "_emit_credential_request_and_wait",
    "_resolve_pending_credential",
    "_send_loop_exhausted",
    "CircuitBreakerError",
    "ToolCircuitBreaker",
]


class _ServerFacade(_ModuleType):
    """One flat ``trid3nt_server.server.X`` namespace over the leaf modules.

    A read resolves to the first leaf that binds ``X``; a monkeypatch write
    rebinds ``X`` in every leaf that already defines it, and a novel write lands
    on the facade. ``SOLVER_CONFIRM_TOOLS`` / ``FETCH_CONFIRM_TOOLS`` synthesize
    from the registry via the gate engine.
    """

    def __getattr__(self, name: str):
        if name == "SOLVER_CONFIRM_TOOLS":
            return _gates_confirm._confirm_tools_by_kind("solver")
        if name == "FETCH_CONFIRM_TOOLS":
            return _gates_confirm._confirm_tools_by_kind("fetch")
        for _mod in _LEAF_MODULES:
            if name in _mod.__dict__:
                return _mod.__dict__[name]
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("__") and name.endswith("__"):
            object.__setattr__(self, name, value)
            return
        found = False
        for _mod in _LEAF_MODULES:
            if name in _mod.__dict__:
                setattr(_mod, name, value)
                found = True
        # Always mirror onto the facade instance: mock.patch restores a
        # facade-resolved attribute via setattr only when the name is locally
        # present; without the mirror its exit path delattr's the name out of
        # the owning leaf permanently.
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        # mock.patch restores a facade-resolved name with delattr when it
        # judged the attribute non-local at enter. Deleting the name out of
        # the owning leaf would break every later reader of that module's
        # globals - so a facade delete RESTORES each leaf to its import-time
        # binding instead (and drops any facade mirror).
        if name in self.__dict__:
            object.__delattr__(self, name)
        for _mod in _LEAF_MODULES:
            if name in _mod.__dict__:
                _orig = _IMPORT_ORIGINALS.get((id(_mod), name), _MISSING)
                if _orig is _MISSING:
                    delattr(_mod, name)
                else:
                    _mod.__dict__[name] = _orig


#: Import-time bindings per leaf so a facade delete restores, never destroys.
_MISSING = object()
_IMPORT_ORIGINALS: dict[tuple[int, str], object] = {
    (id(_mod), _name): _val
    for _mod in _LEAF_MODULES
    for _name, _val in _mod.__dict__.items()
}

_sys.modules[__name__].__class__ = _ServerFacade
