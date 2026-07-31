"""Entry point for the ``trid3nt-server`` console script.

Run the WebSocket server. Optionally run an MCP smoke pre-flight (gated by
``TRID3NT_AGENT_SKIP_MCP_SMOKE=1`` to skip).

Startup-time tool-registry wiring:

Importing ``trid3nt_server.agent.tools`` populates the module-level ``TOOL_REGISTRY``
via the import-time ``@register_tool`` decorators in the package's
submodules (``passthroughs``, ``fetchers``, etc.). The
``--startup-only`` flag below verifies the registry is
populated without binding the WebSocket port; ``make run-agent`` continues
to start the server normally.

FR-CE-8 fail-fast: any tool whose ``AtomicToolMetadata`` is misconfigured
(e.g. ``cacheable=True`` with ``ttl_class="live-no-cache"``) raises a
``pydantic.ValidationError`` at import time and prevents the agent service
from starting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# FR-FR-3: agent-side max-turns cap -- cheap insurance.
#
# ``MAX_TURNS_PER_SESSION`` is the maximum number of user-message / tool-call
# turns allowed before the agent refuses further dispatch and emits a
# ``session-state`` envelope with ``status="max_turns_reached"``.
#
# Override via the ``TRID3NT_MAX_TURNS_PER_SESSION`` environment variable for
# ops flexibility (e.g. set to 0 to disable -- sentinel value; or raise for
# long sessions during demos). TENTATIVE default 25 per OQ-FR-1.
# ---------------------------------------------------------------------------
MAX_TURNS_PER_SESSION: int = int(os.environ.get("TRID3NT_MAX_TURNS_PER_SESSION", "25"))


def _import_tools_registry() -> int:
    """Import ``trid3nt_server.agent.tools`` to populate ``TOOL_REGISTRY``.

    Returns the number of registered tools. Surfaced at startup so an empty
    registry (typically a packaging mistake) is visible in the logs rather
    than silent.

    eagerly imports ``data_fetch`` (the 4 fetcher atomic tools) so
    their ``@register_tool`` decorators fire alongside the eager
    ``passthroughs`` import in ``tools/__init__.py``. ``tools/__init__.py``
    is FROZEN per file ownership, so the fetcher import is
    co-located here instead.

    similarly imports ``qgis_discovery`` so the 2 QGIS-algorithm
    discovery atomic tools (``list_qgis_algorithms`` +
    ``describe_qgis_algorithm``) register at startup. Together with
    ``passthroughs.qgis_process`` they complete the FR-AS-9 Level 1a
    capability-discovery loop.

    imports ``solver`` so the 2 solver-dispatch atomic tools
    (``run_solver`` + ``wait_for_completion``) register at startup. These
    are FR-DC-6 uncacheable (``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="solver_dispatch"``) -- they drive solver
    executions of the SFINCS substrate.

    imports ``workflows.sfincs.flood.flood`` so the M5 capstone
    workflow's thin atomic-tool wrapper (the ``sfincs_flood`` engine template) is
    registered alongside the atomic tools it composes. The workflow itself
    is deterministic Python (FR-TA-1, Decision G); the wrapper exists so the
    LLM sees a single invocable tool that triggers the whole chain.
    """
    from .agent import tools  # noqa: F401 -- side-effect: registers atomic tools
    # register the 4 data-fetch atomic tools (FROZEN __init__.py).
    from .agent.tools.fetchers.climate.lookup_precip_return_period import lookup_precip_return_period  # noqa: F401
    from .agent.tools.fetchers.hydrology.fetch_river_geometry import fetch_river_geometry  # noqa: F401
    from .agent.tools.fetchers.socioeconomic.fetch_buildings import fetch_buildings  # noqa: F401
    from .agent.tools.fetchers.socioeconomic.fetch_population import fetch_population  # noqa: F401
    from .agent.tools.fetchers.socioeconomic.geocode_location import geocode_location  # noqa: F401
    from .agent.tools.fetchers.terrain.fetch_dem import fetch_dem  # noqa: F401
    from .agent.tools.fetchers.terrain.fetch_landcover import fetch_landcover  # noqa: F401
    # register the 2 QGIS discovery atomic tools.
    from .agent.tools.search.qgis_discovery import qgis_discovery  # noqa: F401
    # register run_solver + wait_for_completion (M5 substrate).
    from .agent.tools.simulation.solver import solver  # noqa: F401
    # register sfincs_flood (M5 capstone workflow wrapper; engine template).
    from .agent.workflows.sfincs.flood.flood import sfincs_flood  # noqa: F401
    # register search_data_catalog + fetch_from_catalog (Mode 1 substrate).
    from .agent.tools.search.fetch_from_catalog import fetch_from_catalog  # noqa: F401
    from .agent.tools.search.search_data_catalog import search_data_catalog  # noqa: F401
    # register publish_layer (COG → QGIS Server WMS bridge; side-effect tool).
    from .agent.tools.publish_layer import publish_layer  # noqa: F401
    # register compute_colored_relief (gdaldem color-relief; 4 ramp presets).
    from .agent.tools.processing.compute_colored_relief import compute_colored_relief  # noqa: F401
    # register compute_slope (gdaldem slope; degrees + percent units; Horn + ZevenbergenThorne).
    from .agent.tools.processing.compute_slope import compute_slope  # noqa: F401
    # register compute_aspect (gdaldem aspect; Horn + ZevenbergenThorne; zero_for_flat flag).
    from .agent.tools.processing.compute_aspect import compute_aspect  # noqa: F401
    # register clip_raster_to_polygon (rasterio.mask; polygon OR bbox clip; folded
    # in clip_raster_to_bbox). compute_zonal_statistics demoted to the code_exec
    # playground (docs/playbooks/zonal-statistics-recipe.md).
    from .agent.tools.processing.clip_raster_to_polygon import clip_raster_to_polygon  # noqa: F401
    # register fetch_administrative_boundaries (TIGER/Line 2024; state/county/place/zcta).
    from .agent.tools.fetchers.socioeconomic.fetch_administrative_boundaries import fetch_administrative_boundaries  # noqa: F401
    # register compute_hillshade (gdaldem hillshade; 5 style presets; swiss_double multiply-blend).
    from .agent.tools.processing.compute_hillshade import compute_hillshade  # noqa: F401
    # register fetch_wdpa_protected_areas (WDPA ArcGIS REST; designation_filter; FlatGeobuf).
    from .agent.tools.fetchers.biodiversity.fetch_wdpa_protected_areas import fetch_wdpa_protected_areas  # noqa: F401
    # register web_fetch (generic web-page ingest with 4 extraction modes).
    from .agent.tools.search.web_fetch import web_fetch  # noqa: F401
    # register fetch_inaturalist_observations (iNat API v1; vetted citizen-science points).
    from .agent.tools.fetchers.biodiversity.fetch_inaturalist_observations import fetch_inaturalist_observations  # noqa: F401
    # register fetch_gbif_occurrences (GBIF Tier-1 species occurrence point fetcher).
    from .agent.tools.fetchers.biodiversity.fetch_gbif_occurrences import fetch_gbif_occurrences  # noqa: F401
    # register fetch_storm_events_db (NOAA Storm Events DB Tier-1 fetcher).
    from .agent.tools.fetchers.weather.fetch_storm_events_db import fetch_storm_events_db  # noqa: F401
    # fetch_nws_event: data-router fold tier-3 hooks (ADR 0061) -- twin DELETED, now
    # spec-driven (source.yaml + nws_event hooks), registered via register_specs_from_tree.
    # register fetch_nws_alerts_conus (CONUS-wide companion to fetch_nws_event).
    from .agent.tools.fetchers.weather.fetch_nws_alerts_conus import fetch_nws_alerts_conus  # noqa: F401
    # aggregate_claims_across_sources DEMOTED to an importable library (no longer
    # an LLM-facing tool); model_groundwater imports its private extractors. News
    # ingest re-homes onto web_fetch / fetch_nws_event / fetch_storm_events_db.
    # register compute_impervious_surface (NLCD impervious-fraction raster).
    from .agent.tools.processing.compute_impervious_surface import compute_impervious_surface  # noqa: F401
    # register extract_landcover_class (NLCD binary-mask extractor for zone_input).
    from .agent.tools.processing.extract_landcover_class import extract_landcover_class  # noqa: F401
    # register compute_building_density (MS Global ML Building Footprints density raster).
    from .agent.tools.processing.compute_building_density import compute_building_density  # noqa: F401
    # register fetch_roads_osm (OSM Overpass road LineStrings; major+arterial default).
    from .agent.tools.fetchers.socioeconomic.fetch_roads_osm import fetch_roads_osm  # noqa: F401
    # -> engine-door refactor (PELICUN slice): the pelicun_damage_assessment
    # TEMPLATE (was run_pelicun_damage_assessment) now lives under
    # workflows/pelicun/damage_assessment/; import it so it registers at daemon startup.
    from .agent.workflows.pelicun.damage_assessment.damage_assessment import pelicun_damage_assessment  # noqa: F401
    # register fetch_nexrad_reflectivity (Iowa Mesonet NEXRAD WMS passthrough).
    from .agent.tools.fetchers.weather.fetch_nexrad_reflectivity import fetch_nexrad_reflectivity  # noqa: F401
    # register fetch_goes_satellite (GOES-16/17/18/19 satellite imagery via NOAA Big-Data S3).
    from .agent.tools.fetchers.imagery.fetch_goes_satellite import fetch_goes_satellite  # noqa: F401
    # register fetch_mrms_qpe (NOAA MRMS gauge-corrected QPE precipitation; SFINCS Harvey reference).
    from .agent.tools.fetchers.weather.fetch_mrms_qpe import fetch_mrms_qpe  # noqa: F401
    # fetch_hrsl_population: data-router fold phase-2 wave-9 (ADR 0055) -- twin
    # DELETED, now spec-driven (source.yaml + multi_url VRT fan-out), registered
    # by register_specs_from_tree() via the agent.tools import above.
    # register fetch_firms_active_fire (NASA FIRMS VIIRS/MODIS active-fire detections; Wave 1.5).
    from .agent.tools.fetchers.hazard.fetch_firms_active_fire import fetch_firms_active_fire  # noqa: F401
    # fetch_landfire_fuels: data-router fold phase-2 wave-7 (ADR 0053) -- twin
    # DELETED, now spec-driven (source.yaml + imageserver_export), auto-registered.
    # fetch_gcn250_curve_numbers: data-router fold phase-2 wave-8 (ADR 0054) --
    # twin DELETED, now spec-driven (source.yaml + direct_window skip-HEAD),
    # registered by register_specs_from_tree() via the agent.tools import above.
    # fetch_mtbs_burn_severity + fetch_nifc_fire_perimeters: data-router fold
    # phase-2 wave-2 -- twins DELETED, now spec-driven (source.yaml + router),
    # registered by register_specs_from_tree() via agent.tools import (no eager
    # module import here).
    # register fetch_ebird_observations (Cornell Lab eBird Tier-2 recent sightings; per-Case secret_ref).
    from .agent.tools.fetchers.biodiversity.fetch_ebird_observations import fetch_ebird_observations  # noqa: F401
    # register fetch_iucn_red_list_range (IUCN Red List Tier-2 species range info fetcher; per-Case secret_ref).
    from .agent.tools.fetchers.biodiversity.fetch_iucn_red_list_range import fetch_iucn_red_list_range  # noqa: F401
    # register fetch_movebank_tracks (Movebank Tier-2 animal-tracking trajectories; per-Case secret_ref).
    from .agent.tools.fetchers.biodiversity.fetch_movebank_tracks import fetch_movebank_tracks  # noqa: F401
    # register fetch_era5_reanalysis (Copernicus ERA5 reanalysis Tier-2 fetcher; compound-flood global substrate).
    from .agent.tools.fetchers.climate.fetch_era5_reanalysis import fetch_era5_reanalysis  # noqa: F401
    # register fetch_gtsm_tide_surge (GTSM v3.0 Tier-2 coastal water-level via CDS; compound-flood coastal boundary).
    from .agent.tools.fetchers.ocean.fetch_gtsm_tide_surge import fetch_gtsm_tide_surge  # noqa: F401
    # register fetch_cama_flood_discharge (CaMa-Flood global river discharge Tier-2 fetcher; compound-flood fluvial forcing).
    from .agent.tools.fetchers.hydrology.fetch_cama_flood_discharge import fetch_cama_flood_discharge  # noqa: F401

    return len(tools.TOOL_REGISTRY)


def _default_qgis_process_submitter():
    """Return the default ``qgis_process`` submitter used by ``set_worker_submitter``.

    DI seam for the ``passthroughs.set_worker_submitter`` hook. The
    submitter is a callable
    matching the signature ``(args: list[str], timeout_s: int) -> dict``
    where the returned dict carries at least ``stdout`` (str), ``returncode``
    (int), and ``duration_s`` (float). Both ``qgis_discovery`` discovery
    tools and the ``qgis_process`` pass-through call this seam.

    The default submitter runs ``qgis_process`` as a local subprocess --
    suitable for the local environment and the M4 discovery loop.

    Override via ``TRID3NT_QGIS_PROCESS_BIN`` env var; defaults to
    ``qgis_process`` discovered on PATH.

    Returns:
        A zero-argument-less callable bound to the chosen ``qgis_process``
        binary; the agent service calls ``set_worker_submitter(callable)``
        during startup.
    """
    import os
    import shutil
    import subprocess
    import time

    # Prefer a docker-backed qgis_process submitter when an image is configured
    # (TRID3NT_QGIS_DOCKER_IMAGE) OR when no local qgis_process binary exists but
    # docker + the image are available (some hosts ship QGIS only inside the
    # trid3nt-qgis container). Same (args, timeout_s) -> dict contract;
    # list/describe pass file-free args so a plain `docker run` suffices.
    # (qgis_process RUN with data I/O uses the separate stage-then-mount path.)
    _image = os.environ.get("TRID3NT_QGIS_DOCKER_IMAGE")
    _local_bin = os.environ.get("TRID3NT_QGIS_PROCESS_BIN") or shutil.which("qgis_process")
    if _image or (_local_bin is None and shutil.which("docker")):
        _image = _image or "trid3nt-qgis:ltr"

        def _submit_docker(args: list[str], timeout_s: int) -> dict[str, object]:
            cmd = [
                "docker", "run", "--rm", "-e", "QT_QPA_PLATFORM=offscreen",
                _image, "qgis_process", *args,
            ]
            start = time.monotonic()
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout_s, check=False
            )
            return {
                "stdout": proc.stdout.decode("utf-8", errors="replace"),
                "stderr": proc.stderr.decode("utf-8", errors="replace"),
                "returncode": proc.returncode,
                "duration_s": time.monotonic() - start,
                "qgis_bin": f"docker:{_image}",
            }

        return _submit_docker

    qgis_bin = _local_bin
    if qgis_bin is None:
        # Last-resort hint for the user's conda env on this Debian box (per
        # PROJECT_STATE env-facts). Production agent image will bake the
        # binary in (or route through the Cloud Run Job submitter).
        candidate = os.path.expanduser("~/miniforge3/envs/grace2/bin/qgis_process")
        if os.path.exists(candidate):
            qgis_bin = candidate
    if qgis_bin is None:
        raise RuntimeError(
            "qgis_process binary not found on PATH; "
            "set TRID3NT_QGIS_PROCESS_BIN or install the grace2 conda env."
        )

    def _submit(args: list[str], timeout_s: int) -> dict[str, object]:
        # QT_QPA_PLATFORM=offscreen mirrors the worker container env (the
        # worker Dockerfile) so QGIS' Qt machinery doesn't try to attach to a display.
        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        cmd = [qgis_bin, *args]
        start = time.monotonic()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            env=env,
        )
        duration_s = time.monotonic() - start
        return {
            "stdout": proc.stdout.decode("utf-8", errors="replace"),
            "stderr": proc.stderr.decode("utf-8", errors="replace"),
            "returncode": proc.returncode,
            "duration_s": duration_s,
            "qgis_bin": qgis_bin,
        }

    return _submit


def _maybe_bind_dev_persistence() -> None:
    """Bind file-backed dev Persistence.

    Local-dev fallback for when MongoDB MCP is not provisioned (the typical
    fresh-clone case). Engages a JSON-on-disk substrate so the Case lifecycle
    (create / select / archive / delete) and chat persistence work without
    any Atlas / MCP setup.

    Precedence:
    - ``TRID3NT_DEV_PERSISTENCE=0`` → never engage (escape hatch for CI that
      wants the M1 None-Persistence path even on a dev box);
    - ``TRID3NT_MONGO_MCP_STDIO=1`` OR ``TRID3NT_MONGO_MCP_URL`` set → defer to
      the real MCP path; ``server.init_persistence_from_env`` constructs the
      MCP-backed singleton at server startup and we leave this no-op;
    - otherwise (default on a fresh local clone) → bind a ``FilePersistence``
      singleton pointing at ``~/.trid3nt/dev_persistence/`` (override via
      ``TRID3NT_DEV_PERSISTENCE_DIR``).

    Production agent containers always set ``TRID3NT_MONGO_MCP_STDIO=1`` so
    this path is bypassed at deploy.
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
            "TRID3NT_DEV_PERSISTENCE=0 to disable, "
            "TRID3NT_MONGO_MCP_STDIO=1 for live MCP.",
            backend,
            _default_dev_persistence_dir(),
        )
    except Exception as exc:  # noqa: BLE001 -- startup must not abort on dev-fallback
        log.warning("dev Persistence bind failed: %s", exc)


def _bind_worker_submitter() -> None:
    """Bind the default ``qgis_process`` submitter into ``passthroughs``.

    Called from ``run`` at agent service startup. After this binds, the
    ``qgis_process`` pass-through body no longer raises ``RuntimeError`` and
    the two QGIS-discovery tools can invoke the substrate.

    Gated by env var ``TRID3NT_SKIP_WORKER_SUBMITTER`` for test contexts that
    don't want the binary resolved (CI without QGIS installed). When the env
    var is set, the binding stays None and tools raise the documented
    "submitter not bound" RuntimeError on call.
    """
    import os

    if os.environ.get("TRID3NT_SKIP_WORKER_SUBMITTER"):
        return
    try:
        submitter = _default_qgis_process_submitter()
    except RuntimeError as exc:
        # A missing qgis_process is informational, not fatal -- we let the
        # agent service start so the other tools (data_fetch, passthroughs)
        # keep working, and any actual QGIS discovery call surfaces the
        # RuntimeError.
        logging.getLogger("trid3nt_server.main").warning(
            "worker submitter not bound (qgis_process unavailable): %s", exc
        )
        return
    from .agent.tools.meta.passthroughs.passthroughs import set_worker_submitter

    set_worker_submitter(submitter)

    # Best-effort readiness probe. The submitter binding above is
    # silent-on-success: a mis-set env flip (e.g. a TRID3NT_QGIS_DOCKER_IMAGE
    # pointing at a tag that isn't pulled, or a qgis_process binary that's on
    # PATH but broken) would only surface on the FIRST discovery call, deep
    # in a user session. Probe ``qgis_process --version`` once at boot so a
    # broken substrate is visible in the startup logs.
    #
    # Cold-start guarantee: the probe must never delay the WS port bind on
    # the live box (no QGIS infra configured).
    #   - QGIS infra configured (TRID3NT_QGIS_DOCKER_IMAGE set) -> run the
    #     probe synchronously so the boot diagnostic is in the startup logs
    #     the operator is watching.
    #   - QGIS infra NOT configured (the live box default) -> run the probe
    #     in a daemon thread so it NEVER delays the WS port bind. Zero added
    #     cold-start latency; the diagnostic still lands in the logs shortly
    #     after boot if anything is wrong.
    # Either way the probe is best-effort + non-fatal: any failure (timeout,
    # non-zero exit, exception) logs a warning and the agent keeps serving;
    # the real call still raises its own typed error if the substrate is
    # down.
    if os.environ.get("TRID3NT_QGIS_DOCKER_IMAGE"):
        _run_readiness_probe(submitter)
    else:
        import threading

        threading.Thread(
            target=_run_readiness_probe,
            args=(submitter,),
            name="qgis-readiness-probe",
            daemon=True,
        ).start()


def _run_readiness_probe(submitter) -> None:
    """Probe ``qgis_process --version`` and log readiness. Never raises.

    Factored out of ``_bind_worker_submitter`` so it can run either inline
    (QGIS infra configured) or on a daemon thread (no QGIS infra) without
    duplicating the logging. Best-effort: every failure path logs a warning
    and returns; nothing here aborts agent startup.
    """
    log = logging.getLogger("trid3nt_server.main")
    try:
        probe = submitter(["--version"], 30)
        rc = probe.get("returncode")
        ver = (probe.get("stdout") or "").strip().splitlines()[:1]
        ver_line = ver[0] if ver else "<no version output>"
        if rc == 0:
            log.info(
                "qgis_process readiness probe OK (bin=%s): %s",
                probe.get("qgis_bin", "?"),
                ver_line,
            )
        else:
            log.warning(
                "qgis_process readiness probe NOT-READY (bin=%s returncode=%s): %s",
                probe.get("qgis_bin", "?"),
                rc,
                (probe.get("stderr") or ver_line).strip()[:200],
            )
    except Exception as exc:  # noqa: BLE001 - probe must never abort startup
        log.warning(
            "qgis_process readiness probe NOT-READY (probe raised): %s", exc
        )


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
        repo_root = Path(__file__).resolve().parents[3]
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
    from .agent import tools

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

    # bind the qgis_process submitter so the discovery tools and the
    # qgis_process pass-through can reach the substrate. Best-effort: failure
    # to resolve a local qgis_process is informational, not fatal.
    _bind_worker_submitter()

    # pre-bind the file-backed dev Persistence when MongoDB MCP is
    # not provisioned. ``server.init_persistence_from_env`` (called inside
    # ``run_server``) preserves a pre-bound singleton, so the dev fallback
    # survives the regular MCP-not-provisioned branch. Production agents set
    # ``TRID3NT_MONGO_MCP_STDIO=1`` and this is a no-op.
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
