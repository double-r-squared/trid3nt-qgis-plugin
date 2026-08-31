"""Entry point for the ``trid3nt-server`` console script.

Run the WebSocket server.

Startup-time tool-registry wiring:

Importing ``trid3nt_server.tools`` populates the module-level ``TOOL_REGISTRY``
via the import-time ``@register_tool`` decorators in the package's
submodules (``fetchers``, ``search``, ``meta``, etc.). The
``--startup-only`` flag below verifies the registry is
populated without binding the WebSocket port; ``make run-agent`` continues
to start the server normally.

A tool whose ``AtomicToolMetadata`` is misconfigured (e.g. ``cacheable=True``
with ``ttl_class="live-no-cache"``) raises a ``pydantic.ValidationError`` at
import time and prevents the agent service from starting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Agent-side max-turns cap -- cheap insurance.
#
# ``MAX_TURNS_PER_SESSION`` is the maximum number of user-message / tool-call
# turns allowed before the agent refuses further dispatch and emits a
# ``session-state`` envelope with ``status="max_turns_reached"``.
#
# Override via the ``TRID3NT_MAX_TURNS_PER_SESSION`` environment variable for
# ops flexibility (e.g. set to 0 to disable -- sentinel value; or raise for
# long sessions during demos). Default 25.
# ---------------------------------------------------------------------------
MAX_TURNS_PER_SESSION: int = int(os.environ.get("TRID3NT_MAX_TURNS_PER_SESSION", "25"))


def _import_tools_registry() -> int:
    """Import ``trid3nt_server.tools`` to populate ``TOOL_REGISTRY``.

    Returns the number of registered tools. Surfaced at startup so an empty
    registry (typically a packaging mistake) is visible in the logs rather
    than silent.

    eagerly imports ``data_fetch`` (the 4 fetcher atomic tools) so
    their ``@register_tool`` decorators fire at ``tools/__init__.py``
    import. ``tools/__init__.py`` is FROZEN per file ownership, so the
    fetcher import is co-located here instead.

    imports ``solver`` so the 2 solver-dispatch atomic tools
    (``run_solver`` + ``wait_for_completion``) register at startup. These
    are uncacheable (``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="solver_dispatch"``) -- they drive the engine's solver
    executions.
    """
    from . import tools  # noqa: F401 -- side-effect: registers atomic tools
    # register the 4 data-fetch atomic tools (FROZEN __init__.py).
    from .tools.fetchers.climate.lookup_precip_return_period import lookup_precip_return_period  # noqa: F401
    # fetch_river_geometry, fetch_buildings, fetch_population: spec-driven,
    # auto-registered by the router spec tree walk; no eager twin import.
    from .tools.fetchers.socioeconomic.geocode_location import geocode_location  # noqa: F401
    # fetch_dem, fetch_landcover: spec-driven, promoted by
    # register_specs_from_tree at agent.tools import; no eager twin import.
    # register run_solver + wait_for_completion (solver-dispatch substrate).
    from .workflows.solver import solver  # noqa: F401
    # register search_data_catalog + fetch_from_catalog (catalog search substrate).
    from .tools.search.fetch_from_catalog import fetch_from_catalog  # noqa: F401
    from .tools.search.search_data_catalog import search_data_catalog  # noqa: F401
    # register compute_colored_relief (gdaldem color-relief; 4 ramp presets).
    from .tools.processing.compute_colored_relief import compute_colored_relief  # noqa: F401
    # register compute_slope (gdaldem slope; degrees + percent units; Horn + ZevenbergenThorne).
    from .tools.processing.compute_slope import compute_slope  # noqa: F401
    # register compute_aspect (gdaldem aspect; Horn + ZevenbergenThorne; zero_for_flat flag).
    from .tools.processing.compute_aspect import compute_aspect  # noqa: F401
    # register clip_raster_to_polygon (rasterio.mask; polygon OR bbox clip; folds
    # in clip_raster_to_bbox). compute_zonal_statistics demoted to the code_exec
    # playground (docs/playbooks/zonal-statistics-recipe.md).
    from .tools.processing.clip_raster_to_polygon import clip_raster_to_polygon  # noqa: F401
    # fetch_administrative_boundaries: spec-driven (zip_vector extract executor +
    # FIPS planner), registered via register_specs_from_tree (agent.tools import above).
    # register compute_hillshade (gdaldem hillshade; 5 style presets; swiss_double multiply-blend).
    from .tools.processing.compute_hillshade import compute_hillshade  # noqa: F401
    # register web_fetch (generic web-page ingest with 4 extraction modes).
    from .tools.search.web_fetch import web_fetch  # noqa: F401
    # fetch_inaturalist_observations + fetch_gbif_occurrences: spec-driven
    # (resolve-then-fetch hooks), registered via register_specs_from_tree.
    # fetch_storm_events_db: spec-driven (directory-index resolve -> bulk
    # gzip-CSV point decode), registered via register_specs_from_tree.
    # fetch_nws_event: spec-driven (source.yaml + nws_event hooks),
    # registered via register_specs_from_tree.
    # fetch_nws_alerts_conus: spec-driven (single /alerts/active GET +
    # zone-polygon enrichment), registered via register_specs_from_tree.
    # News ingest rides web_fetch / fetch_nws_event / fetch_storm_events_db.
    # register compute_impervious_surface (NLCD impervious-fraction raster).
    from .tools.processing.compute_impervious_surface import compute_impervious_surface  # noqa: F401
    # register extract_landcover_class (NLCD binary-mask extractor for zone_input).
    from .tools.processing.extract_landcover_class import extract_landcover_class  # noqa: F401
    # register compute_building_density (MS Global ML Building Footprints density raster).
    from .tools.processing.compute_building_density import compute_building_density  # noqa: F401
    # fetch_roads_osm + fetch_overpass_pois: spec-driven (source.yaml +
    # overpass hooks), auto-registered by the router spec tree walk; no eager
    # twin import here.
    # register show_nexrad_radar (display tool: composes an Iowa Mesonet NEXRAD WMS URL).
    from .tools.display.show_nexrad_radar.show_nexrad_radar import show_nexrad_radar  # noqa: F401
    # fetch_goes_satellite: spec-driven, auto-registered by
    # register_specs_from_tree (goes_satellite library-delegate raster hooks); no eager import.
    # fetch_mrms_qpe: spec-driven (S3-listed key resolve -> grib_object
    # whole-object COG), via register_specs_from_tree.
    # fetch_hrsl_population: spec-driven (source.yaml + multi_url VRT
    # fan-out), registered by register_specs_from_tree() via the agent.tools import above.
    # fetch_firms_active_fire: spec-driven (source.yaml + firms_active_fire
    # hooks), auto-registered via register_specs_from_tree().
    # fetch_landfire_fuels: spec-driven (source.yaml + imageserver_export), auto-registered.
    # fetch_gcn250_curve_numbers: spec-driven (source.yaml + direct_window
    # skip-HEAD), registered by register_specs_from_tree() via the agent.tools import above.
    # fetch_mtbs_burn_severity + fetch_nifc_fire_perimeters: spec-driven
    # (source.yaml + router), registered by register_specs_from_tree() via
    # agent.tools import (no eager module import here).
    # register fetch_ebird_observations (Cornell Lab eBird Tier-2 recent sightings; per-Case secret_ref).
    # register fetch_iucn_red_list_range (IUCN Red List Tier-2 species range info fetcher; per-Case secret_ref).
    # fetch_movebank_tracks: spec-driven (source.yaml + movebank_tracks
    # hooks), auto-registered via register_specs_from_tree().
    # fetch_era5_reanalysis + fetch_gtsm_tide_surge: spec-driven (source.yaml
    # + cds hooks), auto-registered via register_specs_from_tree().

    return len(tools.TOOL_REGISTRY)


def _maybe_bind_dev_persistence() -> None:
    """Bind the file-backed Persistence singleton (the default backend).

    Engages a JSON-on-disk substrate so the Case lifecycle (create / select /
    archive / delete) and chat persistence work with zero config on a fresh
    clone.

    Precedence (see ``persistence.is_dev_persistence_enabled``):
    - ``TRID3NT_DEV_PERSISTENCE=0`` → never engage (escape hatch for CI that
      wants the in-memory, no-persistence path);
    - otherwise (the default) → bind a ``FilePersistence`` singleton pointing at
      ``~/.trid3nt/dev_persistence/`` (override via
      ``TRID3NT_DEV_PERSISTENCE_DIR``).
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
    """Resolve the rotating file-handler's target path, or ``None`` (console-only).

    ``TRID3NT_AGENT_LOG_FILE`` (set by ``scripts/start_agent.sh``) is
    authoritative. Unset -> ``<repo>/logs/agent.log`` inferred from this
    module's install path (``trid3nt_server/main.py`` -> ``src`` -> ``server``
    -> repo root), matching the path ``start_agent.sh`` has always used.
    Falls back to ``None`` (console-only logging) if that layout assumption
    doesn't hold (e.g. an unexpected install) -- logging setup must never
    abort agent startup.
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
    """Console + size-capped rotating file handler.

    Python owns log rotation at the logging layer -- ``RotatingFileHandler``
    (~10MB x 3 backups by default) -- so the on-disk log is size-bounded
    regardless of how the process is started (``scripts/start_agent.sh``'s
    shell redirection no longer needs to, and no longer does, pipe routine
    output into the rotated file; see the script for the boot-crash-only
    redirect it keeps instead). Uses plain ``logging.basicConfig`` (no
    ``force=True``): this is a NO-OP when the root logger already has a
    handler (e.g. pytest's ``caplog`` fixture pre-installs one for every
    test), matching the prior behavior exactly and keeping test runs from
    ever touching a real rotating file on disk.
    """
    level = os.environ.get("TRID3NT_AGENT_LOG", "INFO")
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    # Explicit stdout (not the logging module's stderr default): start_agent.sh
    # discards stdout and captures ONLY stderr into the boot-crash file, so
    # routine INFO+ logging never accumulates there -- the RotatingFileHandler
    # below is the sole durable, size-capped sink for routine output.
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


def run(argv: list[str] | None = None) -> int:
    """Console-script entry point. ``make run-agent`` calls this.

    Supports a ``--startup-only`` flag that imports the tool registry, logs
    the registered tools, and exits 0 without binding the WebSocket port.
    Used by acceptance and by container healthchecks.
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

    # Deliberate telemetry retention (daemon-boot cleanup pass): prune
    # tool-call telemetry segments beyond the last TRID3NT_TELEMETRY_KEEP
    # (default 3). Ephemerality is policy, enforced here, not a platform
    # accident. Best-effort -- retention must never block boot.
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
