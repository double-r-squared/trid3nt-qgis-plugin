"""Entry point for the ``trid3nt-server`` console script.

Importing ``trid3nt_server.tools`` populates ``TOOL_REGISTRY`` through the
import-time ``@register_tool`` decorators; a tool whose ``AtomicToolMetadata``
is misconfigured raises there and the service refuses to start."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Turn cap before dispatch refuses and emits ``session-state`` ``status="max_turns_reached"``;
# ``TRID3NT_MAX_TURNS_PER_SESSION`` overrides 25, 0 disables.
MAX_TURNS_PER_SESSION: int = int(os.environ.get("TRID3NT_MAX_TURNS_PER_SESSION", "25"))


def _import_tools_registry() -> int:
    """Import ``trid3nt_server.tools`` to populate ``TOOL_REGISTRY`` and return
    the number of registered tools; an empty registry is a packaging fault.
    """
    from . import tools  # noqa: F401 -- side-effect: registers atomic tools
    # Coded tools register only when imported; row-driven fetchers need no import.
    from .tools.fetchers.climate.lookup_precip_return_period import lookup_precip_return_period  # noqa: F401
    from .tools.fetchers.socioeconomic.geocode_location import geocode_location  # noqa: F401
    from .workflows.solver import solver  # noqa: F401

    return len(tools.TOOL_REGISTRY)


def _maybe_bind_dev_persistence() -> None:
    """Bind the file-backed Persistence singleton at ``TRID3NT_DEV_PERSISTENCE_DIR``
    or ``~/.trid3nt/dev_persistence/``; ``TRID3NT_DEV_PERSISTENCE=0`` refuses the bind.
    """
    from .store.cases import (
        is_dev_persistence_enabled,
        make_persistence_for_backend,
        resolve_persistence_backend,
        _default_dev_persistence_dir,
    )
    from .server.session.persistence_ref import get_persistence, set_persistence

    log = logging.getLogger("trid3nt_server.main")
    if not is_dev_persistence_enabled():
        return
    if get_persistence() is not None:
        # Already bound (test harness or a prior init pass) -- don't trample.
        log.info("dev Persistence: singleton already bound; skipping")
        return
    try:
        p = make_persistence_for_backend()
        set_persistence(p)
        backend = resolve_persistence_backend()
        log.info(
            "dev Persistence bound (backend=%s; %s). "
            "TRID3NT_DEV_PERSISTENCE=0 to disable.",
            backend,
            _default_dev_persistence_dir(),
        )
    except Exception as exc:  # noqa: BLE001 -- startup must not abort on dev-fallback
        log.warning("dev Persistence bind failed: %s", exc)


#: Size-capped rotation (~10MB active + 3 backups); ``TRID3NT_AGENT_LOG_MAX_BYTES`` / ``TRID3NT_AGENT_LOG_BACKUPS`` override.
_DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_LOG_BACKUPS = 3


def _resolve_agent_log_file() -> str | None:
    """Resolve the rotating handler's target path, or ``None`` for console-only:
    ``TRID3NT_AGENT_LOG_FILE`` wins, otherwise ``<repo>/logs/agent.log``.
    """
    raw = os.environ.get("TRID3NT_AGENT_LOG_FILE")
    if raw:
        return raw
    try:
        repo_root = Path(__file__).resolve().parents[1]
        return str(repo_root / "logs" / "agent.log")
    except (IndexError, OSError):
        return None


def _configure_logging() -> None:
    """Console (stdout) plus a size-capped rotating file handler; no
    ``force=True``, so this is a no-op once the root logger has a handler.
    """
    level = os.environ.get("TRID3NT_AGENT_LOG", "INFO")
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    # Explicit stdout, not the logging default of stderr: start_agent.sh captures
    # ONLY stderr into the boot-crash file, so routine output must not land there.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    log_file = _resolve_agent_log_file()
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            max_bytes = int(
                os.environ.get("TRID3NT_AGENT_LOG_MAX_BYTES", _DEFAULT_LOG_MAX_BYTES)
            )
            backups = int(
                os.environ.get("TRID3NT_AGENT_LOG_BACKUPS", _DEFAULT_LOG_BACKUPS)
            )
            handlers.append(
                RotatingFileHandler(
                    log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
                )
            )
        except (OSError, ValueError) as exc:
            logging.getLogger("trid3nt_server.main").warning(
                "log rotation file handler unavailable path=%s: %s -- "
                "continuing console-only",
                log_file,
                exc,
            )

    logging.basicConfig(level=level, format=fmt, handlers=handlers)


# One process, one user: every connection is the same user, so session registries are in-memory
# single-user state. Revisit on more than one concurrent user.
def run(argv: list[str] | None = None) -> int:
    """Console-script entry point; ``--startup-only`` verifies the tool
    registry and exits 0 without binding the WebSocket port.
    """
    _configure_logging()
    logger = logging.getLogger("trid3nt_server.main")

    args = sys.argv[1:] if argv is None else argv
    startup_only = "--startup-only" in args

    n_tools = _import_tools_registry()
    from . import tools

    tool_names = sorted(tools.TOOL_REGISTRY.keys())
    logger.info("tool registry loaded: %d tool(s): %s", n_tools, tool_names)

    # Built once the registry is complete: the routing prompt names the tools this registry holds.
    from .model.adapters.adapter import system_prompt

    logger.info("routing prompt built: %d chars", len(system_prompt()))

    # ``init_persistence_from_env`` preserves a pre-bound singleton, so this binding survives startup.
    _maybe_bind_dev_persistence()

    # Must exist before the socket does; minted on a first start and printed once.
    from .model.credentials.auth_handshake import ensure_access_token

    ensure_access_token()

    if startup_only:
        logger.info("--startup-only: tool registry verified; exiting without serving")
        return 0

    from .server.protocol.loop import run_server

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        print("trid3nt-server: interrupted, shutting down.", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
