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


def _auto_populate_with_options(original: Any) -> Any:
    """Wrap pelicun's ``auto_populate`` so the returned ``DL`` config always has an
    ``Options`` dict.

    pelicun merges the harness-injected assessment ``Options`` (Seed, SampleSize)
    into ``config_ap['DL']['Options']`` by direct key access; an auto-pop script
    that returns a ``DL`` block without that key crashes the merge. The bundled
    water and power lifeline scripts do exactly that, so this shim adds an empty
    ``Options`` dict when the auto-pop leaves one out.
    """

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        config_ap, comp = original(*args, **kwargs)
        dl = config_ap.get("DL") if isinstance(config_ap, dict) else None
        if isinstance(dl, dict):
            dl.setdefault("Options", {})
        return config_ap, comp

    return _wrapped


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
        damage_state_probs: per-component damage-state probabilities parsed from the
            damage sample -- ``{component: {ds_index: probability}}`` where ds 0 is
            undamaged. Empty unless the run was requested with ``detailed_results``
            (lifeline network assets, whose loss summary carries no repair figures).
        expected_component_quantity: per-component expected damaged quantity summed
            over damage states and locations (Hazus pipe repair counts: leaks +
            breaks). Empty unless ``detailed_results`` was requested.
        seed: the injected Monte-Carlo seed.
        realizations: the requested sample size.
    """

    output_files: list[str]
    dl_summary: Any
    dl_summary_stats: Any
    auto_populated_config: dict[str, Any]
    component_assignment: list[str]
    demand_sample: dict[str, Any]
    damage_state_probs: dict[str, dict[int, float]]
    expected_component_quantity: dict[str, float]
    seed: int
    realizations: int


def _read_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _read_zip_csv(zip_path: str) -> Any:
    """Read the single CSV member of a pelicun output zip (``DMG_sample.zip``)."""
    import io
    import zipfile

    import pandas as pd

    with zipfile.ZipFile(zip_path) as zf:
        member = next(n for n in zf.namelist() if n.endswith(".csv"))
        return pd.read_csv(io.BytesIO(zf.read(member)), index_col=0)


def _summarize_damage_sample(
    tmp_dir: str,
) -> tuple[dict[str, dict[int, float]], dict[str, float]]:
    """Parse ``DMG_sample.zip`` into per-component damage-state probabilities.

    The damage sample columns are ``<component>-<loc>-<dir>-<ds>`` with a 0/1
    quantity per realization (damage states are mutually exclusive per component
    block, so exactly one ds column is 1). Returns ``(damage_state_probs,
    expected_component_quantity)``: the first maps each component to
    ``{ds_index: P(component in ds)}`` averaged over its locations/directions; the
    second maps each component to its expected damaged quantity summed over damage
    states >= 1 and over locations (the Hazus pipe repair count -- leaks + breaks).
    """
    import numpy as np
    import pandas as pd

    zip_path = os.path.join(tmp_dir, "DMG_sample.zip")
    if not os.path.isfile(zip_path):
        return {}, {}
    df = _read_zip_csv(zip_path)
    # per (component, ds) accumulate mean probability and expected quantity
    ds_means: dict[str, dict[int, list[float]]] = {}
    exp_qty: dict[str, float] = {}
    for col in df.columns:
        parts = str(col).rsplit("-", 3)
        if len(parts) != 4:
            continue
        cmp_id, _loc, _dir, ds_str = parts
        try:
            ds = int(ds_str)
        except ValueError:
            continue
        series = pd.to_numeric(df[col], errors="coerce").dropna()
        if series.empty:
            continue
        mean_qty = float(series.mean())
        ds_means.setdefault(cmp_id, {}).setdefault(ds, []).append(mean_qty)
        if ds >= 1:
            exp_qty[cmp_id] = exp_qty.get(cmp_id, 0.0) + mean_qty
    damage_state_probs: dict[str, dict[int, float]] = {
        cmp_id: {ds: float(np.mean(vals)) for ds, vals in sorted(per_ds.items())}
        for cmp_id, per_ds in ds_means.items()
    }
    return damage_state_probs, exp_qty


def run_dl_calculation(
    *,
    aim_config: dict[str, Any],
    demand_csv_path: str,
    realizations: int,
    seed: int,
    coupled_edp: bool = True,
    detailed_results: bool = False,
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
        from pelicun.tools import DL_calculation as _dl_module
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
            original_auto_populate = _dl_module.auto_populate
            try:
                os.chdir(tmp)
                # The injected assessment ``Options`` (Seed + SampleSize) are merged
                # by DL_calculation into ``config_ap['DL']['Options']`` via direct
                # key access -- but the bundled water/power lifeline auto-pop scripts
                # return a ``DL`` block without an ``Options`` key (only buildings and
                # transportation include one), so that merge raises ``KeyError:
                # 'Options'``. Guarantee the key exists post-auto-pop so the Seed lands
                # (reproducibility holds for every asset class). Safe under the lock:
                # the patch is process-global but every DL run is serialized here.
                _dl_module.auto_populate = _auto_populate_with_options(
                    original_auto_populate)
                run_pelicun(
                    demand_file="response.csv",
                    config_path=cfg_name,
                    output_path=None,
                    coupled_edp=bool(coupled_edp),
                    realizations=int(realizations),
                    auto_script_path="",
                    detailed_results=bool(detailed_results),
                    output_format="csv" if detailed_results else None,
                    custom_model_dir=None,
                )
            finally:
                _dl_module.auto_populate = original_auto_populate
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
        damage_state_probs: dict[str, dict[int, float]] = {}
        expected_component_quantity: dict[str, float] = {}
        if detailed_results:
            damage_state_probs, expected_component_quantity = (
                _summarize_damage_sample(tmp)
            )
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
            damage_state_probs=damage_state_probs,
            expected_component_quantity=expected_component_quantity,
            seed=int(seed),
            realizations=int(realizations),
        )
    except DLCalculationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DLCalculationError(f"DL_calculation run failed: {exc}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
