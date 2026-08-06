"""DL_calculation CLI harness: drive pelicun's ``DL_calculation.run_pelicun`` in an
isolated tempdir with the process-global working directory serialized + restored.

pelicun's DL_calculation entrypoint is cwd-sensitive: it resolves the config, the
demand file, and the auto-population script relative to the current working
directory and writes ~20 output files into the config's directory. This harness
copies an AIM config + demand CSV into a fresh tempdir, injects a fixed ``Seed``
(so the Monte-Carlo run is reproducible), chdir's into that dir under a
module-level lock (``os.chdir`` is process-global -- two concurrent DL runs would
otherwise corrupt each other's cwd), runs the pipeline, reads the outputs back
into memory, restores cwd, snapshots-and-restores pelicun's global
``LoggerRegistry`` (whose file-backed loggers would otherwise dangle at the
deleted tempdir and raise from ``sys.excepthook`` later), and deletes the tempdir.

The whole run is synchronous and MUST be invoked via ``asyncio.to_thread`` (never
on the event loop). A single ``threading.Lock`` serializes the cwd mutation across
worker threads; combined with the per-call unique tempdir this keeps concurrent
callers correct in the one-daemon-one-user monolith.

This is the shared machinery under every HAZUS DL_calculation-driven template
(seismic building runs today; wind / wind+surge next). It brings a new hazard's
bundled fragility/consequence tables in through one auto-populated invocation.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.pelicun._dl_calculation"
)

__all__ = [
    "DLCalculationError",
    "DLCalculationResult",
    "run_dl_calculation",
]

#: Serializes the process-global ``os.chdir`` across DL_calculation worker threads.
_CWD_LOCK = threading.Lock()


class DLCalculationError(RuntimeError):
    """A pelicun DL_calculation harness run failed.

    ``error_code`` maps to the WebSocket error frame; ``retryable`` guides the
    agent retry loop. Raised for an invalid config/demand, a missing pelicun
    install, or a run that produced no summary.
    """

    error_code: str = "PELICUN_DL_CALCULATION_ERROR"
    retryable: bool = False


@dataclass(frozen=True)
class DLCalculationResult:
    """Typed result of one DL_calculation run.

    Fields:
        output_files: sorted basenames of every file the run wrote (the DL output
            manifest -- compared against a checked-in reference set by callers).
        dl_summary: the per-realization ``DL_summary.csv`` (repair_cost,
            repair_time, collapse, irreparable).
        dl_summary_stats: the ``DL_summary_stats.csv`` (mean / std / percentiles).
        auto_populated_config: the ``<stem>_ap.json`` config the auto-population
            produced (the resolved Asset / Damage / Loss blocks).
        component_assignment: the auto-assigned HAZUS component IDs (from
            ``CMP_QNT.csv``) -- the building type the AIM attributes mapped to.
        demand_sample: the parsed ``DEM_sample.json`` (the realized EDP sample;
            with ``coupled_edp`` it reproduces the input demand).
        seed: the injected Monte-Carlo seed.
        realizations: the requested sample size.
    """

    output_files: list[str]
    dl_summary: Any
    dl_summary_stats: Any
    auto_populated_config: dict[str, Any]
    component_assignment: list[str]
    demand_sample: dict[str, Any]
    seed: int
    realizations: int


def _read_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def run_dl_calculation(
    *,
    aim_config: dict[str, Any],
    demand_csv_path: str,
    realizations: int,
    seed: int,
    coupled_edp: bool = True,
) -> DLCalculationResult:
    """Run pelicun DL_calculation on ``aim_config`` + ``demand_csv_path``, isolated.

    The AIM config drives auto-population (its ``Applications/DL/ApplicationData/
    DL_Method`` selects the bundled fragility/consequence dataset and the
    building-type assignment from the ``GeneralInformation`` attributes). A fixed
    ``seed`` is injected so the run is reproducible. Runs synchronously; call via
    ``asyncio.to_thread``.

    Raises ``DLCalculationError`` (loud, typed) on an invalid input, a missing
    pelicun install, or a run that yields no ``DL_summary.csv``.
    """
    if int(realizations) <= 0:
        raise DLCalculationError("realizations must be a positive integer.")
    if not os.path.isfile(demand_csv_path):
        raise DLCalculationError(f"demand CSV not found: {demand_csv_path!r}")
    try:
        import pelicun  # noqa: F401
        from pelicun.base import LoggerRegistry
        from pelicun.tools.DL_calculation import run_pelicun
    except ImportError as exc:
        raise DLCalculationError(
            "pelicun is not installed; cannot run DL_calculation."
        ) from exc

    cfg = copy.deepcopy(aim_config)
    app_data = (
        cfg.setdefault("Applications", {})
        .setdefault("DL", {})
        .setdefault("ApplicationData", {})
    )
    options = app_data.setdefault("Options", {})
    options["Seed"] = int(seed)
    options.setdefault("Sampling", {})["SampleSize"] = int(realizations)

    stem = "AIM"
    cfg_name = f"{stem}.json"
    tmp = tempfile.mkdtemp(prefix="trid3nt_pelicun_dl_")
    try:
        with open(os.path.join(tmp, cfg_name), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        shutil.copy(demand_csv_path, os.path.join(tmp, "response.csv"))

        with _CWD_LOCK:
            initial_cwd = os.getcwd()
            logger_snapshot = list(LoggerRegistry._loggers)
            try:
                os.chdir(tmp)
                run_pelicun(
                    demand_file="response.csv",
                    config_path=cfg_name,
                    output_path=None,
                    coupled_edp=bool(coupled_edp),
                    realizations=int(realizations),
                    auto_script_path="",
                    detailed_results=False,
                    output_format=None,
                    custom_model_dir=None,
                )
            finally:
                os.chdir(initial_cwd)
                # Drop any file-backed loggers this run registered so they do not
                # dangle at the (about-to-be-deleted) tempdir log path.
                LoggerRegistry._loggers[:] = logger_snapshot

        import pandas as pd

        summary_path = os.path.join(tmp, "DL_summary.csv")
        if not os.path.isfile(summary_path):
            raise DLCalculationError(
                "DL_calculation produced no DL_summary.csv (run did not complete)."
            )
        dl_summary = pd.read_csv(summary_path)
        dl_summary_stats = pd.read_csv(
            os.path.join(tmp, "DL_summary_stats.csv"), index_col=0
        )
        auto_populated_config = _read_json(os.path.join(tmp, f"{stem}_ap.json"))
        cmp = pd.read_csv(os.path.join(tmp, "CMP_QNT.csv"))
        component_assignment = [str(x) for x in cmp[cmp.columns[0]].tolist()]
        demand_sample: dict[str, Any] = {}
        dem_path = os.path.join(tmp, "DEM_sample.json")
        if os.path.isfile(dem_path):
            demand_sample = _read_json(dem_path)
        output_files = sorted(
            name for name in os.listdir(tmp)
            if os.path.isfile(os.path.join(tmp, name))
        )
        logger.info(
            "run_dl_calculation: %d output files, component=%s, seed=%d, N=%d",
            len(output_files), component_assignment, int(seed), int(realizations),
        )
        return DLCalculationResult(
            output_files=output_files,
            dl_summary=dl_summary,
            dl_summary_stats=dl_summary_stats,
            auto_populated_config=auto_populated_config,
            component_assignment=component_assignment,
            demand_sample=demand_sample,
            seed=int(seed),
            realizations=int(realizations),
        )
    except DLCalculationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DLCalculationError(f"DL_calculation run failed: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
