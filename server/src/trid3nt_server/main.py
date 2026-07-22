"""Entry point for the ``trid3nt-server`` console script.

Run the WebSocket server. Optionally run an MCP smoke pre-flight (gated by
``TRID3NT_AGENT_SKIP_MCP_SMOKE=1`` to skip).

Startup-time tool-registry wiring (job-0032, M4 substrate):

Importing ``trid3nt_server.tools`` populates the module-level ``TOOL_REGISTRY``
via the import-time ``@register_tool`` decorators in the package's
submodules (``passthroughs`` for M4 job-0032; ``fetchers`` etc. for
job-0033+). The ``--startup-only`` flag below verifies the registry is
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

# ---------------------------------------------------------------------------
# FR-FR-3 (job-0048): agent-side max-turns cap — cheap insurance.
#
# ``MAX_TURNS_PER_SESSION`` is the maximum number of user-message / tool-call
# turns allowed before the agent refuses further dispatch and emits a
# ``session-state`` envelope with ``status="max_turns_reached"``.
#
# Override via the ``TRID3NT_MAX_TURNS_PER_SESSION`` environment variable for
# ops flexibility (e.g. set to 0 to disable — sentinel value; or raise for
# long sessions during demos). TENTATIVE default 25 per OQ-FR-1.
# ---------------------------------------------------------------------------
MAX_TURNS_PER_SESSION: int = int(os.environ.get("TRID3NT_MAX_TURNS_PER_SESSION", "25"))


def _import_tools_registry() -> int:
    """Import ``trid3nt_server.tools`` to populate ``TOOL_REGISTRY``.

    Returns the number of registered tools. Surfaced at startup so an empty
    registry (typically a packaging mistake) is visible in the logs rather
    than silent.

    job-0033: eagerly imports ``data_fetch`` (the 4 fetcher atomic tools) so
    their ``@register_tool`` decorators fire alongside the eager
    ``passthroughs`` import in ``tools/__init__.py``. ``tools/__init__.py``
    is FROZEN per job-0033 file ownership, so the fetcher import is
    co-located here instead.

    job-0034: similarly imports ``qgis_discovery`` so the 2 QGIS-algorithm
    discovery atomic tools (``list_qgis_algorithms`` +
    ``describe_qgis_algorithm``) register at startup. Together with
    ``passthroughs.qgis_process`` they complete the FR-AS-9 Level 1a
    capability-discovery loop.

    job-0041: imports ``solver`` so the 2 solver-dispatch atomic tools
    (``run_solver`` + ``wait_for_completion``) register at startup. These
    are FR-DC-6 uncacheable (``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="solver_dispatch"``) — they drive Cloud Workflows
    executions of the M5 SFINCS substrate landed by job-0040.

    job-0042: imports ``workflows.model_flood_scenario`` so the M5 capstone
    workflow's thin atomic-tool wrapper ``run_model_flood_scenario`` is
    registered alongside the atomic tools it composes. The workflow itself
    is deterministic Python (FR-TA-1, Decision G); the wrapper exists so the
    LLM sees a single invocable tool that triggers the whole chain.
    """
    from . import tools  # noqa: F401 — side-effect: registers atomic tools
    # job-0033: register the 4 data-fetch atomic tools (FROZEN __init__.py).
    from .tools.fetchers.climate import lookup_precip_return_period  # noqa: F401
    from .tools.fetchers.hydrology import fetch_river_geometry  # noqa: F401
    from .tools.fetchers.socioeconomic import fetch_buildings  # noqa: F401
    from .tools.fetchers.socioeconomic import fetch_population  # noqa: F401
    from .tools.fetchers.socioeconomic import geocode_location  # noqa: F401
    from .tools.fetchers.terrain import fetch_dem  # noqa: F401
    from .tools.fetchers.terrain import fetch_landcover  # noqa: F401
    # job-0034: register the 2 QGIS discovery atomic tools.
    from .tools.discovery import qgis_discovery  # noqa: F401
    # job-0041: register run_solver + wait_for_completion (M5 substrate).
    from .tools.simulation import solver  # noqa: F401
    # job-0042: register run_model_flood_scenario (M5 capstone workflow wrapper).
    from .workflows import model_flood_scenario  # noqa: F401
    # job-0047: register catalog_search + catalog_fetch (Mode 1 substrate).
    from .tools.discovery import catalog_fetch  # noqa: F401
    from .tools.discovery import catalog_search  # noqa: F401
    # job-0062: register publish_layer (COG → QGIS Server WMS bridge; side-effect tool).
    from .tools import publish_layer  # noqa: F401
    # job-0080: register compute_colored_relief (gdaldem color-relief; 4 ramp presets).
    from .tools.processing import compute_colored_relief  # noqa: F401
    # job-0081: register compute_slope (gdaldem slope; degrees + percent units; Horn + ZevenbergenThorne).
    from .tools.processing import compute_slope  # noqa: F401
    # job-0082: register compute_aspect (gdaldem aspect; Horn + ZevenbergenThorne; zero_for_flat flag).
    from .tools.processing import compute_aspect  # noqa: F401
    # job-0083: register compute_zonal_statistics (hazard-analysis primitive; raster + vector zone).
    from .tools.processing import compute_zonal_statistics  # noqa: F401
    # job-0085: register clip_raster_to_bbox (gdal_translate / gdalwarp bbox clip; gs:// or local).
    from .tools.processing import clip_raster_to_bbox  # noqa: F401
    # job-0106: register clip_raster_to_polygon (rasterio.mask; arbitrary polygon clip; gs:// or local).
    from .tools.processing import clip_raster_to_polygon  # noqa: F401
    # job-0084: register fetch_administrative_boundaries (TIGER/Line 2024; state/county/place/zcta).
    from .tools.fetchers.socioeconomic import fetch_administrative_boundaries  # noqa: F401
    # job-0079: register compute_hillshade (gdaldem hillshade; 5 style presets; swiss_double multiply-blend).
    from .tools.processing import compute_hillshade  # noqa: F401
    # job-0089: register fetch_wdpa_protected_areas (WDPA ArcGIS REST; designation_filter; FlatGeobuf).
    from .tools.fetchers.biodiversity import fetch_wdpa_protected_areas  # noqa: F401
    # job-0092: register web_fetch (generic web-page ingest with 4 extraction modes).
    from .tools.meta import web_fetch  # noqa: F401
    # job-0088: register fetch_inaturalist_observations (iNat API v1; vetted citizen-science points).
    from .tools.fetchers.biodiversity import fetch_inaturalist_observations  # noqa: F401
    # job-0087: register fetch_gbif_occurrences (GBIF Tier-1 species occurrence point fetcher).
    from .tools.fetchers.biodiversity import fetch_gbif_occurrences  # noqa: F401
    # job-0091: register fetch_storm_events_db (NOAA Storm Events DB Tier-1 fetcher).
    from .tools.fetchers.weather import fetch_storm_events_db  # noqa: F401
    # job-0090: register fetch_nws_event (NWS active alerts/events; dynamic-1h Tier-1 fetcher).
    from .tools.fetchers.weather import fetch_nws_event  # noqa: F401
    # job-0105: register fetch_nws_alerts_conus (CONUS-wide companion to fetch_nws_event).
    from .tools.fetchers.weather import fetch_nws_alerts_conus  # noqa: F401
    # job-0093: register aggregate_claims_across_sources (cross-source FR-HEP claim aggregator).
    from .tools.processing import aggregate_claims_across_sources  # noqa: F401
    # job-0095: register compute_impervious_surface (NLCD impervious-fraction raster).
    from .tools.processing import compute_impervious_surface  # noqa: F401
    # job-0094: register extract_landcover_class (NLCD binary-mask extractor for zone_input).
    from .tools.processing import extract_landcover_class  # noqa: F401
    # job-0096: register compute_building_density (MS Global ML Building Footprints density raster).
    from .tools.processing import compute_building_density  # noqa: F401
    # job-0097: register fetch_roads_osm (OSM Overpass road LineStrings; major+arterial default).
    from .tools.fetchers.socioeconomic import fetch_roads_osm  # noqa: F401
    # job-0098: register run_pelicun_damage_assessment (Wave 1 stub; Wave 2 composer is job-0106).
    from .tools.simulation import run_pelicun_damage_assessment  # noqa: F401
    # job-0102: register fetch_nexrad_reflectivity (Iowa Mesonet NEXRAD WMS passthrough).
    from .tools.fetchers.weather import fetch_nexrad_reflectivity  # noqa: F401
    # job-0107: register clip_vector_to_polygon (vector clip-to-polygon utility).
    from .tools.processing import clip_vector_to_polygon  # noqa: F401
    # job-0104: register fetch_goes_satellite (GOES-16/17/18/19 satellite imagery via NOAA Big-Data S3).
    from .tools.fetchers.imagery import fetch_goes_satellite  # noqa: F401
    # job-0103: register fetch_mrms_qpe (NOAA MRMS gauge-corrected QPE precipitation; SFINCS Harvey reference).
    from .tools.fetchers.weather import fetch_mrms_qpe  # noqa: F401
    # job-0112: register fetch_hrsl_population (Meta + CIESIN HRSL persons/cell via global VRT; Wave 1.5).
    from .tools.fetchers.socioeconomic import fetch_hrsl_population  # noqa: F401
    # job-0108: register fetch_firms_active_fire (NASA FIRMS VIIRS/MODIS active-fire detections; Wave 1.5).
    from .tools.fetchers.hazard import fetch_firms_active_fire  # noqa: F401
    # job-0111: register fetch_landfire_fuels (LANDFIRE LF2022 fuels & canopy rasters; Wave 1.5).
    from .tools.fetchers.hazard import fetch_landfire_fuels  # noqa: F401
    # job-0113: register fetch_gcn250_curve_numbers (GCN250 global SCS curve numbers; Wave 1.5).
    from .tools.fetchers.soil import fetch_gcn250_curve_numbers  # noqa: F401
    # job-0109: register fetch_mtbs_burn_severity (MTBS historic burn-severity polygons; CONUS+AK+HI 1984-).
    from .tools.fetchers.hazard import fetch_mtbs_burn_severity  # noqa: F401
    # job-0110: register fetch_nifc_fire_perimeters (NIFC current wildfire perimeters; Wave 1.5).
    from .tools.fetchers.hazard import fetch_nifc_fire_perimeters  # noqa: F401
    # job-0128: register fetch_ebird_observations (Cornell Lab eBird Tier-2 recent sightings; per-Case secret_ref).
    from .tools.fetchers.biodiversity import fetch_ebird_observations  # noqa: F401
    # job-0129: register fetch_iucn_red_list_range (IUCN Red List Tier-2 species range info fetcher; per-Case secret_ref).
    from .tools.fetchers.biodiversity import fetch_iucn_red_list_range  # noqa: F401
    # job-0130: register fetch_movebank_tracks (Movebank Tier-2 animal-tracking trajectories; per-Case secret_ref).
    from .tools.fetchers.biodiversity import fetch_movebank_tracks  # noqa: F401
    # job-0131: register fetch_era5_reanalysis (Copernicus ERA5 reanalysis Tier-2 fetcher; compound-flood global substrate).
    from .tools.fetchers.climate import fetch_era5_reanalysis  # noqa: F401
    # job-0132: register fetch_gtsm_tide_surge (GTSM v3.0 Tier-2 coastal water-level via CDS; compound-flood coastal boundary).
    from .tools.fetchers.ocean import fetch_gtsm_tide_surge  # noqa: F401
    # job-0133: register fetch_cama_flood_discharge (CaMa-Flood global river discharge Tier-2 fetcher; compound-flood fluvial forcing).
    from .tools.fetchers.hydrology import fetch_cama_flood_discharge  # noqa: F401

    return len(tools.TOOL_REGISTRY)


def _default_qgis_process_submitter():
    """Return the default ``qgis_process`` submitter used by ``set_worker_submitter``.

    job-0034 DI seam: completes the wire-up promised by job-0032's
    ``passthroughs.set_worker_submitter`` hook. The submitter is a callable
    matching the signature ``(args: list[str], timeout_s: int) -> dict``
    where the returned dict carries at least ``stdout`` (str), ``returncode``
    (int), and ``duration_s`` (float). Both ``qgis_discovery`` discovery
    tools and the ``qgis_process`` pass-through call this seam.

    The default submitter runs ``qgis_process`` as a local subprocess —
    suitable for the local environment and the M4 discovery loop.

    Override via ``TRID3NT_QGIS_PROCESS_BIN`` env var; defaults to
    ``qgis_process`` discovered on PATH (the ``grace2`` conda env per
    PROJECT_STATE / job-0022 has this).

    Returns:
        A zero-argument-less callable bound to the chosen ``qgis_process``
        binary; the agent service calls ``set_worker_submitter(callable)``
        during startup.
    """
    import os
    import shutil
    import subprocess
    import time

    # job-0308 (sprint-16, Decision Q): on the AWS EC2 box QGIS lives ONLY
    # inside the grace2-qgis container (no qgis_process on PATH). Prefer a
    # docker-backed submitter when an image is configured (TRID3NT_QGIS_DOCKER_
    # IMAGE) OR when no local qgis_process exists but docker + the image are
    # available. Same (args, timeout_s) -> dict contract; list/describe pass
    # file-free args so a plain `docker run` suffices. (qgis_process RUN with
    # data I/O uses the separate stage-then-mount path — job-0308 follow-up.)
    _image = os.environ.get("TRID3NT_QGIS_DOCKER_IMAGE")
    _local_bin = os.environ.get("TRID3NT_QGIS_PROCESS_BIN") or shutil.which("qgis_process")
    if _image or (_local_bin is None and shutil.which("docker")):
        _image = _image or "grace2-qgis:ltr"

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
        # QT_QPA_PLATFORM=offscreen mirrors the worker container env (job-0021
        # Dockerfile) so QGIS' Qt machinery doesn't try to attach to a display.
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
    """job-0161 (sprint-12-mega Wave 4.6): bind file-backed dev Persistence.

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
        # Already bound (test harness or a prior init pass) — don't trample.
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
    except Exception as exc:  # noqa: BLE001 — startup must not abort on dev-fallback
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
        # A missing qgis_process is informational, not fatal — we let the
        # agent service start so the other tools (data_fetch, passthroughs)
        # keep working, and any actual QGIS discovery call surfaces the
        # RuntimeError.
        logging.getLogger("trid3nt_server.main").warning(
            "worker submitter not bound (qgis_process unavailable): %s", exc
        )
        return
    from .tools.meta.passthroughs import set_worker_submitter

    set_worker_submitter(submitter)

    # job-0308 (Q-discovery lane): best-effort readiness probe. The submitter
    # binding above is silent-on-success: a mis-set env flip (e.g. a
    # TRID3NT_QGIS_DOCKER_IMAGE pointing at a tag that isn't pulled, or a
    # qgis_process binary that's on PATH but broken) would only surface on the
    # FIRST discovery call, deep in a user session. Probe ``qgis_process
    # --version`` once at boot so a broken substrate is visible in the startup
    # logs.
    #
    # COLD-START GUARANTEE (P0 review): on the LIVE box there is no QGIS infra
    # (TRID3NT_QGIS_DOCKER_IMAGE unset), yet ``submitter(["--version"], 30)`` ran
    # SYNCHRONOUSLY here at every boot BEFORE ``run_server`` binds the WS port,
    # so a slow/hung ``qgis_process`` could add up to ~30 s of cold-start. Fix:
    #   - QGIS infra configured (TRID3NT_QGIS_DOCKER_IMAGE set) -> run the probe
    #     synchronously so the boot diagnostic is in the startup logs the
    #     operator is watching.
    #   - QGIS infra NOT configured (the live box default) -> run the probe in a
    #     daemon thread so it NEVER delays the WS port bind. Zero added
    #     cold-start latency on the live box; the diagnostic still lands in the
    #     logs shortly after boot if anything is wrong.
    # Either way the probe is best-effort + non-fatal: any failure (timeout,
    # non-zero exit, exception) logs a warning and the agent keeps serving; the
    # real call still raises its own typed error if the substrate is down.
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


def run(argv: list[str] | None = None) -> int:
    """Console-script entry point. ``make run-agent`` calls this.

    Supports a ``--startup-only`` flag that imports the tool registry, logs
    the registered tools, and exits 0 without binding the WebSocket port.
    Used by job-0032 acceptance and by container healthchecks.
    """
    logging.basicConfig(
        level=os.environ.get("TRID3NT_AGENT_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("trid3nt_server.main")

    args = sys.argv[1:] if argv is None else argv
    startup_only = "--startup-only" in args

    # Populate TOOL_REGISTRY by importing the tools package. Any import-time
    # registration error (duplicate name, bad metadata) surfaces here.
    n_tools = _import_tools_registry()
    from . import tools

    tool_names = sorted(tools.TOOL_REGISTRY.keys())
    logger.info("tool registry loaded: %d tool(s): %s", n_tools, tool_names)

    # job-0034: bind the qgis_process submitter so the discovery tools and the
    # qgis_process pass-through can reach the substrate. Best-effort: failure
    # to resolve a local qgis_process is informational, not fatal.
    _bind_worker_submitter()

    # job-0161: pre-bind the file-backed dev Persistence when MongoDB MCP is
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
