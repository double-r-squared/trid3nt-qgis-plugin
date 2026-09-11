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

# ---------------------------------------------------------------------------
# The maximum number of user-message / tool-call turns allowed before the agent
# refuses further dispatch and emits a ``session-state`` envelope with
# ``status="max_turns_reached"``. ``TRID3NT_MAX_TURNS_PER_SESSION`` overrides
# the default 25; 0 is the sentinel that disables the cap.
# ---------------------------------------------------------------------------
MAX_TURNS_PER_SESSION: int = int(os.environ.get("TRID3NT_MAX_TURNS_PER_SESSION", "25"))


def _import_tools_registry() -> int:
    """Import ``trid3nt_server.tools`` to populate ``TOOL_REGISTRY`` and return
    the number of registered tools; an empty registry is a packaging fault.
    """
    from . import tools  # noqa: F401 -- side-effect: registers atomic tools
    # Coded tools register only when their module is imported; the spec-driven
    # fetchers are promoted by the router's tree walk and need no import here.
    from .tools.fetchers.climate.lookup_precip_return_period import lookup_precip_return_period  # noqa: F401
    from .tools.fetchers.socioeconomic.geocode_location import geocode_location  # noqa: F401
    from .workflows.solver import solver  # noqa: F401
    from .tools.derive.compute_colored_relief import compute_colored_relief  # noqa: F401
    from .tools.derive.compute_slope import compute_slope  # noqa: F401
    from .tools.derive.compute_aspect import compute_aspect  # noqa: F401
    from .tools.derive.clip_raster_to_polygon import clip_raster_to_polygon  # noqa: F401
    from .tools.derive.compute_hillshade import compute_hillshade  # noqa: F401
    from .tools.search.web_fetch import web_fetch  # noqa: F401
    from .tools.derive.compute_impervious_surface import compute_impervious_surface  # noqa: F401
    from .tools.derive.extract_landcover_class import extract_landcover_class  # noqa: F401
    from .tools.derive.compute_building_density import compute_building_density  # noqa: F401
    from .tools.display.show_nexrad_radar.show_nexrad_radar import show_nexrad_radar  # noqa: F401

    return len(tools.TOOL_REGISTRY)


def _maybe_bind_dev_persistence() -> None:
    """Bind the file-backed Persistence singleton at ``TRID3NT_DEV_PERSISTENCE_DIR``
    or ``~/.trid3nt/dev_persistence/``; ``TRID3NT_DEV_PERSISTENCE=0`` refuses the bind.
    """
    from .persistence import (
        is_dev_persistence_enabled,
        make_persistence_for_backend,
        resolve_persistence_backend,
        _default_dev_persistence_dir,
    )
    from .server import get_persistence, set_persistence

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


#: Size-capped rotation for the Python-owned agent log file (~10MB active +
#: 3 rotated backups = ~40MB ceiling regardless of session length). Overridable
#: via ``TRID3NT_AGENT_LOG_MAX_BYTES`` / ``TRID3NT_AGENT_LOG_BACKUPS`` for ops.
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


# ONE PROCESS, ONE USER. The daemon carries the socket, the turn loop, tool
# dispatch, the gates and persistence in a single process on purpose: every
# connection is the SAME user, so the session registries are in-memory
# single-user state rather than a store, and there is no per-user isolation to
# split a service along. Remote access does not change that - a second machine
# dialing in is the same user at another address. The trigger for revisiting it
# is more than one CONCURRENT user, which is when isolation becomes a
# correctness problem instead of a shape preference.
def run(argv: list[str] | None = None) -> int:
    """Console-script entry point; ``--startup-only`` verifies the tool
    registry and exits 0 without binding the WebSocket port.
    """
    _configure_logging()
    logger = logging.getLogger("trid3nt_server.main")

    args = sys.argv[1:] if argv is None else argv
    startup_only = "--startup-only" in args

    # Populate TOOL_REGISTRY by importing the tools package. Any import-time
    # registration error (duplicate name, bad metadata) surfaces here.
    n_tools = _import_tools_registry()
    from . import tools

    tool_names = sorted(tools.TOOL_REGISTRY.keys())
    logger.info("tool registry loaded: %d tool(s): %s", n_tools, tool_names)

    # Retention: prune tool-call telemetry segments beyond the last
    # TRID3NT_TELEMETRY_KEEP (default 3). Best-effort; never blocks boot.
    try:
        from . import telemetry as _telemetry

        _removed = _telemetry.cleanup_telemetry_segments()
        if _removed:
            logger.info(
                "telemetry retention: removed %d stale segment(s): %s",
                len(_removed),
                _removed,
            )
    except Exception:  # noqa: BLE001 -- retention must never block boot
        logger.warning("telemetry retention cleanup failed", exc_info=True)

    # Bind the file-backed Persistence singleton (the default backend).
    # ``server.init_persistence_from_env`` (called inside ``run_server``)
    # preserves a pre-bound singleton, so this binding survives startup.
    _maybe_bind_dev_persistence()

    if startup_only:
        logger.info("--startup-only: tool registry verified; exiting without serving")
        return 0

    from .server import run_server

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        print("trid3nt-server: interrupted, shutting down.", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
