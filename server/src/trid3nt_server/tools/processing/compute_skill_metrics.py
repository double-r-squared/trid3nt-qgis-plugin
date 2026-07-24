"""``compute_skill_metrics`` atomic tool -- paired obs-vs-model skill metrics.

Computes the standard hydrologic/hydraulic model-skill metrics over an
ALREADY-PAIRED observed/simulated series: NSE, KGE, PBIAS, RSR, RMSE, R2,
plus two simple derived quantities (peak error percent, peak-timing error).
Every metric that ``spotpy.objectivefunctions`` provides is computed via
spotpy directly -- NO bespoke reimplementation of NSE/KGE/PBIAS/RSR/RMSE/R2
math lives in this file (per the V&V build contract, section 3.2/5.4).

Input is EITHER:

- ``paired_table_uri`` -- a lane-C ``extract_model_at_observations`` paired
  table (FlatGeobuf, EPSG:4326, one feature per sample, columns ``obs_id`` /
  ``observed`` / ``simulated`` / ``time`` per the build contract section 3.3
  storage format). Read with ``geopandas.read_file``; this tool consumes the
  ``observed`` / ``simulated`` (and ``time`` for peak-timing) columns and
  does NO sampling/pairing of its own -- that math lives entirely upstream in
  ``extract_model_at_observations`` (this tool never duplicates it).
- OR explicit ``observed`` + ``simulated`` arrays (+ optional ``time``) --
  the direct-array path for a caller that already has aligned series (e.g.
  from ``fetch_usgs_nwis_gauges`` + a model timeseries extracted elsewhere).

``variable="head"`` adds SRMS (RMSE / observed head range) -- the fold of the
former standalone ``compute_head_calibration_stats`` primitive (Anderson and
Woessner convention, MODFLOW research brief).

Honesty floor: any metric spotpy cannot compute (zero-variance denominator,
mismatched lengths, all-NaN pair) comes back ``null``, never a fabricated
number; ``verdict_is_heuristic`` is ALWAYS ``true`` -- the Moriasi/published
bands are a decision-support heuristic, not a pass/fail gate. A small paired
sample (``n < 5``) still returns full metric values but downgrades
``suggested_verdict`` to ``"indeterminate"`` with a caveat rather than a
graded verdict that would overstate confidence.

``cacheable=False`` (``live-no-cache``): a comparison compute over
live/caller-supplied inputs, mirroring ``compute_model_residuals`` /
``compute_flood_extent_skill``. Returns a plain JSON-serializable dict (the
build-contract section 3.2 envelope), not a ``LayerURI`` -- this tool never
produces a map layer.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime
from typing import Any

import numpy as np

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "compute_skill_metrics",
    "SkillMetricsError",
    "SkillMetricsInputError",
    "SkillMetricsNoDataError",
    "SkillMetricsUpstreamError",
    "SkillMetricsDependencyMissingError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_skill_metrics")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class SkillMetricsError(RuntimeError):
    """Base class for compute_skill_metrics failures."""

    error_code: str = "SKILL_METRICS_ERROR"
    retryable: bool = True


class SkillMetricsInputError(SkillMetricsError):
    """Bad inputs -- no selector, mismatched lengths, unresolvable column."""

    error_code = "SKILL_METRICS_INPUT_INVALID"
    retryable = False


class SkillMetricsNoDataError(SkillMetricsError):
    """Zero usable paired samples after dropping non-finite entries."""

    error_code = "SKILL_METRICS_NO_DATA"
    retryable = False


class SkillMetricsUpstreamError(SkillMetricsError):
    """Staging (S3 download) or the paired-table read failed."""

    error_code = "SKILL_METRICS_UPSTREAM_ERROR"
    retryable = True


class SkillMetricsDependencyMissingError(SkillMetricsError):
    """``spotpy`` is not importable in this environment (section 5.4)."""

    error_code = "SKILL_METRICS_DEPENDENCY_MISSING"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Below this many paired samples, NSE/KGE/RSR estimates are unstable enough
#: that a graded verdict would overstate confidence -- values are still
#: computed and returned, but suggested_verdict downgrades to
#: "indeterminate" with a caveat.
_MIN_N_FOR_VERDICT = 5

#: Moriasi 2007 / 2015 published acceptance bands (streamflow calibration).
#: Applied as a general-reference heuristic regardless of ``variable``
#: (research.md section 2.2); a caveat notes the streamflow provenance when
#: ``variable`` is not "streamflow".
_MORIASI_2007 = "Moriasi et al. 2007 (https://swat.tamu.edu/media/90109/moriasimodeleval.pdf)"
_KNOBEN_2019 = "Knoben et al. 2019 HESS (https://hess.copernicus.org/preprints/hess-2019-327/hess-2019-327.pdf)"
_ANDERSON_WOESSNER = (
    "Anderson and Woessner convention (MODFLOW calibration research brief; "
    "heuristic, not a hard rule)"
)

#: Per-metric acceptance bands (contract 3.2 shape: satisfactory/good/very_good
#: ONLY -- no per-band ``source`` key). Citations are consolidated into the
#: single top-level ``bands_source`` string (see ``_BANDS_SOURCE``).
_BANDS: dict[str, dict[str, str | None] | None] = {
    "NSE": {"satisfactory": ">0.50", "good": "0.65-0.75", "very_good": ">0.75"},
    "PBIAS": {"satisfactory": "<=25", "good": "<=15", "very_good": "<=10"},
    "RSR": {"satisfactory": "<=0.70", "good": "<=0.60", "very_good": "<=0.50"},
    "KGE": None,
    "RMSE": None,
    "R2": None,
    "peak_error": None,
    "peak_timing_error": None,
    "SRMS": None,
}

#: Single top-level citation string for every band (FIX 4a -- replaces the
#: per-band ``source`` keys that were not in the pinned envelope).
_BANDS_SOURCE = (
    f"NSE/PBIAS/RSR bands: {_MORIASI_2007}. KGE has no graded acceptance band "
    f"({_KNOBEN_2019}). SRMS band (variable='head' only): {_ANDERSON_WOESSNER}."
)

_METADATA = AtomicToolMetadata(
    name="compute_skill_metrics",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


# ---------------------------------------------------------------------------
# Dependency seam.
# ---------------------------------------------------------------------------


def _import_spotpy_objectivefunctions() -> Any:
    """Import ``spotpy.objectivefunctions`` behind a typed honest error."""
    try:
        import spotpy.objectivefunctions as sof
    except ImportError as exc:
        raise SkillMetricsDependencyMissingError(
            f"spotpy is not importable in this environment "
            f"({type(exc).__name__}: {exc}); compute_skill_metrics requires "
            "spotpy.objectivefunctions for NSE/KGE/PBIAS/RSR/RMSE/R2 -- see "
            "server/pyproject.toml."
        ) from exc
    return sof


# ---------------------------------------------------------------------------
# Staging + loading (mirrors compute_model_residuals._stage_uri_local).
# ---------------------------------------------------------------------------


def _stage_uri_local(uri: str, tmpdir: str, label: str) -> str:
    """Return a local file path for ``uri`` (s3:// download or local path)."""
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3

        name = uri.rstrip("/").rsplit("/", 1)[-1] or f"{label}.bin"
        local = os.path.join(tmpdir, f"{label}_{name}")
        try:
            data = read_object_bytes_s3(uri)
        except Exception as exc:  # noqa: BLE001
            raise SkillMetricsUpstreamError(
                f"S3 download failed for {label} uri {uri!r}: {exc}"
            ) from exc
        with open(local, "wb") as f:
            f.write(data)
        return local
    if uri.startswith(("gs://", "http://", "https://")):
        raise SkillMetricsInputError(
            f"{label} uri scheme not supported: {uri!r} (use s3:// or a local path)"
        )
    if not os.path.exists(uri):
        raise SkillMetricsInputError(
            f"{label} uri points at a missing local file: {uri!r}"
        )
    return uri


def _to_float_array(values: Any) -> np.ndarray:
    """Best-effort elementwise float coercion; unparsable entries -> NaN."""
    out = np.empty(len(values), dtype=np.float64)
    for i, v in enumerate(values):
        try:
            fv = float(v)
            out[i] = fv if math.isfinite(fv) else np.nan
        except (TypeError, ValueError):
            out[i] = np.nan
    return out


def _parse_iso_times(values: Any) -> list[datetime | None]:
    """Best-effort ISO8601 parse; unparsable/missing entries -> None."""
    out: list[datetime | None] = []
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out.append(None)
            continue
        s = str(v).strip()
        if not s:
            out.append(None)
            continue
        try:
            out.append(datetime.fromisoformat(s.replace("Z", "+00:00")))
        except ValueError:
            out.append(None)
    return out


def _load_paired_table(
    paired_table_uri: str,
    observed_field: str,
    simulated_field: str,
    time_field: str,
    notes: list[str],
) -> tuple[np.ndarray, np.ndarray, list[datetime | None] | None, int]:
    """Load a lane-C paired table; return (observed, simulated, times, n_id_groups)."""
    import geopandas as gpd

    with tempfile.TemporaryDirectory(prefix="trid3nt_skill_metrics_") as tmpdir:
        local = _stage_uri_local(paired_table_uri, tmpdir, "paired_table")
        try:
            gdf = gpd.read_file(local)
        except Exception as exc:  # noqa: BLE001
            raise SkillMetricsUpstreamError(
                f"could not open paired_table_uri {paired_table_uri!r}: {exc}"
            ) from exc

    if len(gdf) == 0:
        raise SkillMetricsNoDataError(
            f"paired_table_uri {paired_table_uri!r} contains zero rows -- "
            "nothing to score."
        )
    columns = [c for c in gdf.columns if c != "geometry"]
    if observed_field not in gdf.columns:
        raise SkillMetricsInputError(
            f"observed_field={observed_field!r} not found on the paired "
            f"table; available columns: {sorted(columns)}"
        )
    if simulated_field not in gdf.columns:
        raise SkillMetricsInputError(
            f"simulated_field={simulated_field!r} not found on the paired "
            f"table; available columns: {sorted(columns)}"
        )

    observed = _to_float_array(gdf[observed_field].tolist())
    simulated = _to_float_array(gdf[simulated_field].tolist())
    times: list[datetime | None] | None = None
    if time_field in gdf.columns:
        times = _parse_iso_times(gdf[time_field].tolist())
        if all(t is None for t in times):
            times = None
            notes.append(
                f"paired table carried a {time_field!r} column but no entry "
                "parsed as ISO8601 -- peak_timing_error is null."
            )

    n_id_groups = 1
    if "obs_id" in gdf.columns:
        n_id_groups = int(gdf["obs_id"].astype(str).nunique())

    notes.append(
        f"Observed/simulated pairs from paired_table_uri "
        f"({paired_table_uri}); {len(gdf)} row(s), fields "
        f"observed={observed_field!r} simulated={simulated_field!r}."
    )
    return observed, simulated, times, n_id_groups


# ---------------------------------------------------------------------------
# Metric computation.
# ---------------------------------------------------------------------------


def _clean(value: Any) -> float | None:
    """NaN/inf -> None (never fabricate a metric value); else round(6)."""
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fv):
        return None
    return round(fv, 6)


def _compute_core_metrics(
    observed: np.ndarray, simulated: np.ndarray, sof: Any, caveats: list[str]
) -> dict[str, float | None]:
    """NSE/KGE/PBIAS/RSR/RMSE/R2 via spotpy.objectivefunctions; NaN -> None."""
    raw: dict[str, Any]
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = {
            "NSE": sof.nashsutcliffe(observed, simulated),
            "KGE": sof.kge(observed, simulated),
            "PBIAS": sof.pbias(observed, simulated),
            "RSR": sof.rsr(observed, simulated),
            "RMSE": sof.rmse(observed, simulated),
            "R2": sof.rsquared(observed, simulated),
        }
    cleaned = {k: _clean(v) for k, v in raw.items()}
    if cleaned["NSE"] is None:
        caveats.append(
            "NSE is null: undefined (observed series has zero variance, or "
            "spotpy returned NaN)."
        )
    if cleaned["KGE"] is None:
        caveats.append(
            "KGE is null: undefined (zero-variance observed or simulated "
            "series)."
        )
    if cleaned["RSR"] is None:
        caveats.append("RSR is null: undefined (observed series has zero variance).")
    if cleaned["PBIAS"] is None:
        caveats.append("PBIAS is null: undefined (sum of observed values is zero).")
    if cleaned["R2"] is None:
        caveats.append(
            "R2 is null: undefined (zero-variance observed or simulated series)."
        )
    if cleaned["RMSE"] is None:
        caveats.append("RMSE is null: spotpy returned a non-finite value.")
    return cleaned


def _peak_metrics(
    observed: np.ndarray,
    simulated: np.ndarray,
    times: list[datetime | None] | None,
    caveats: list[str],
) -> tuple[float | None, float | None]:
    """Peak-magnitude error (percent) + peak-timing error (seconds).

    Peak error compares each series' OWN maximum (index-independent, the
    standard peak-flow-error convention); peak-timing error compares WHEN
    each series' maximum occurred, only when a parseable time column/array
    was supplied for both peak indices.
    """
    idx_obs = int(np.argmax(observed))
    idx_sim = int(np.argmax(simulated))
    obs_peak = float(observed[idx_obs])
    sim_peak = float(simulated[idx_sim])

    if obs_peak == 0.0:
        peak_error = None
        caveats.append("peak_error is null: observed peak value is exactly zero.")
    else:
        peak_error = round(100.0 * (sim_peak - obs_peak) / abs(obs_peak), 6)

    peak_timing_error: float | None = None
    if times is not None:
        t_obs = times[idx_obs]
        t_sim = times[idx_sim]
        if t_obs is not None and t_sim is not None:
            peak_timing_error = round((t_sim - t_obs).total_seconds(), 3)
    return peak_error, peak_timing_error


def _suggested_verdict(
    n: int,
    nse: float | None,
    pbias: float | None,
    rsr: float | None,
    caveats: list[str],
) -> str:
    """Combined Moriasi 2007 grading (NSE + |PBIAS| + RSR, most conservative tier)."""
    if n < _MIN_N_FOR_VERDICT:
        caveats.append(
            f"n={n} < {_MIN_N_FOR_VERDICT}: suggested_verdict downgraded to "
            "'indeterminate' -- too few paired samples for a stable graded "
            "verdict (metric values above are still real, just noisy)."
        )
        return "indeterminate"
    if nse is None or pbias is None or rsr is None:
        caveats.append(
            "suggested_verdict is 'indeterminate': one or more of "
            "NSE/PBIAS/RSR is null (see caveats above)."
        )
        return "indeterminate"
    # abs(PBIAS): the reported PBIAS sign convention (spotpy: positive = model
    # over-predicts) is opposite Moriasi's, so grade off the magnitude only.
    abs_pbias = abs(pbias)
    if nse > 0.75 and rsr <= 0.50 and abs_pbias <= 10.0:
        return "very_good"
    if nse > 0.65 and rsr <= 0.60 and abs_pbias <= 15.0:
        return "good"
    if nse > 0.50 and rsr <= 0.70 and abs_pbias <= 25.0:
        return "satisfactory"
    return "unsatisfactory"


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(_METADATA)
def compute_skill_metrics(
    paired_table_uri: str | None = None,
    observed: list[float] | None = None,
    simulated: list[float] | None = None,
    time: list[str] | None = None,
    variable: str = "generic",
    observed_field: str = "observed",
    simulated_field: str = "simulated",
    time_field: str = "time",
    units: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Score a paired observed-vs-model series: NSE, KGE, PBIAS, RSR, RMSE, R2.

    Use this to grade how well a model result matches real measurements once
    they are already paired point-for-point -- e.g. after
    ``extract_model_at_observations`` produces a paired table from a model
    raster/timeseries + observation points, or with any two aligned series
    you already have (a gauge timeseries + a model timeseries). Wraps
    ``spotpy.objectivefunctions`` for every metric it provides -- no bespoke
    metric math.

    **When to use:**
    - "How well does this SWMM/SFINCS timeseries match the USGS gauge?" or
      any observed-vs-simulated skill scoring once the pairs are aligned.
    - Groundwater head calibration: pass ``variable="head"`` to also get
      SRMS (RMSE / observed head range, the MODFLOW convention).
    - After a calibration parameter change, to check whether the fit
      improved (compare ``suggested_verdict`` / metric values run to run).

    **When NOT to use:**
    - You do not yet have paired values -- call
      ``extract_model_at_observations`` first to sample a model raster/
      timeseries AT observation points/times.
    - Comparing a flood WET/DRY extent raster against a benchmark extent --
      use ``compute_flood_extent_skill`` (categorical, not continuous).
    - Point-by-point spatial residual mapping -- use
      ``compute_model_residuals`` (returns a per-point residual map layer;
      this tool returns summary skill metrics only, no layer).

    **Parameters:**
    - ``paired_table_uri``: OPTIONAL. A lane-C paired-table handle/URI
      (FlatGeobuf with ``obs_id``/``observed``/``simulated``/``time``
      columns -- the ``extract_model_at_observations`` output shape). When
      given, ``observed``/``simulated``/``time`` args are ignored.
    - ``observed`` / ``simulated``: OPTIONAL. Explicit aligned numeric
      arrays (same length) -- used when ``paired_table_uri`` is omitted.
      Exactly one of ``paired_table_uri`` or ``(observed, simulated)`` must
      be given.
    - ``time``: OPTIONAL. ISO8601 timestamp strings aligned with
      ``observed``/``simulated`` (direct-array path only) -- enables
      ``peak_timing_error``.
    - ``variable``: ``"streamflow"`` / ``"stage"`` / ``"head"`` / any label.
      ``"head"`` adds ``SRMS`` to the metrics. Purely descriptive otherwise
      (echoed back, used in ``units``/notes context).
    - ``observed_field`` / ``simulated_field`` / ``time_field``: column
      names on ``paired_table_uri`` (defaults match the lane-C storage
      format: ``"observed"`` / ``"simulated"`` / ``"time"``).
    - ``units``: OPTIONAL physical units of the paired quantity (e.g.
      ``"m3/s"``, ``"ft"``) -- echoed back verbatim; ``null`` if not given
      and not auto-detectable from the table.

    **Returns:** a plain dict -- ``variable``, ``n`` (paired sample count
    used), ``metrics`` (``NSE``/``KGE``/``PBIAS``/``RSR``/``RMSE``/``R2``/
    ``peak_error`` [percent]/``peak_timing_error`` [seconds, null unless a
    time column is present]/``SRMS`` [null unless ``variable=="head"``]),
    ``bands`` (Moriasi 2007 acceptance bands per metric, ``null`` where no
    codified band exists), ``suggested_verdict``
    (``very_good``/``good``/``satisfactory``/``unsatisfactory``/
    ``indeterminate``), ``verdict_is_heuristic`` (always ``true``),
    ``bands_source`` (single citation string for every band), ``caveats``
    (list, always populated when any metric is null or n is small), ``units``,
    ``notes`` (provenance).

    **PBIAS sign convention (IMPORTANT):** this tool reports spotpy's PBIAS =
    ``100*sum(sim-obs)/sum(obs)``, so a POSITIVE PBIAS means the model
    OVER-predicts (simulated > observed). That is the OPPOSITE sign of the
    Moriasi 2007 tables (where positive PBIAS = model under-estimation). The
    graded-verdict band lookup keys off ``abs(PBIAS)``, so the convention
    difference never misbands a result; only the reported sign differs.

    **SRMS** (``variable=="head"`` only) is a PLAIN RATIO
    ``RMSE / (max(obs)-min(obs))`` (not a percent); the Anderson-Woessner
    ``<0.10`` band means RMSE within 10% of the observed head range.

    **Errors (FR-AS-11):** ``SkillMetricsInputError`` (no selector given,
    mismatched array lengths, unresolvable column); ``SkillMetricsNoDataError``
    (zero usable paired samples after dropping non-finite entries);
    ``SkillMetricsUpstreamError`` (S3 download / table read failed);
    ``SkillMetricsDependencyMissingError`` (spotpy not importable).

    Cross-tool dependencies:
        Upstream (consumes):
        - ``extract_model_at_observations`` -- produces ``paired_table_uri``.
        Downstream (feeds):
        - Agent narration reads ``suggested_verdict`` / ``caveats`` for the
          headline calibration answer; NEVER treats the verdict as a hard
          pass/fail gate (``verdict_is_heuristic`` is always true).
    """
    has_table = isinstance(paired_table_uri, str) and paired_table_uri.strip()
    has_arrays = observed is not None and simulated is not None
    if not has_table and not has_arrays:
        raise SkillMetricsInputError(
            "compute_skill_metrics requires either paired_table_uri or both "
            "observed and simulated arrays."
        )

    notes: list[str] = []
    caveats: list[str] = []
    n_id_groups = 1
    times_all: list[datetime | None] | None = None

    if has_table:
        if has_arrays:
            notes.append(
                "Both paired_table_uri and explicit observed/simulated arrays "
                "were supplied; paired_table_uri took priority (arrays ignored)."
            )
        observed_arr, simulated_arr, times_all, n_id_groups = _load_paired_table(
            paired_table_uri,  # type: ignore[arg-type]
            observed_field,
            simulated_field,
            time_field,
            notes,
        )
    else:
        if len(observed) != len(simulated):  # type: ignore[arg-type]
            raise SkillMetricsInputError(
                f"observed (len={len(observed)}) and simulated "  # type: ignore[arg-type]
                f"(len={len(simulated)}) must be the same length."  # type: ignore[arg-type]
            )
        if len(observed) == 0:  # type: ignore[arg-type]
            raise SkillMetricsInputError(
                "observed/simulated arrays are empty -- nothing to score."
            )
        observed_arr = _to_float_array(observed)
        simulated_arr = _to_float_array(simulated)
        if time is not None:
            if len(time) != len(observed_arr):
                raise SkillMetricsInputError(
                    f"time (len={len(time)}) must match observed/simulated "
                    f"length ({len(observed_arr)})."
                )
            times_all = _parse_iso_times(time)
            if all(t is None for t in times_all):
                times_all = None
                notes.append(
                    "time array supplied but no entry parsed as ISO8601 -- "
                    "peak_timing_error is null."
                )
        notes.append(
            f"Observed/simulated pairs from explicit arrays ({len(observed_arr)} "
            "sample(s))."
        )

    n_total = len(observed_arr)
    valid = np.isfinite(observed_arr) & np.isfinite(simulated_arr)
    n = int(valid.sum())
    if n == 0:
        raise SkillMetricsNoDataError(
            f"all {n_total} paired sample(s) had a non-finite observed or "
            "simulated value -- no usable pairs to score."
        )
    n_dropped = n_total - n
    if n_dropped:
        notes.append(
            f"{n_dropped} of {n_total} paired sample(s) excluded: "
            "non-finite observed or simulated value."
        )

    obs = observed_arr[valid]
    sim = simulated_arr[valid]
    times: list[datetime | None] | None = None
    if times_all is not None:
        times = [t for t, keep in zip(times_all, valid) if keep]

    if n_id_groups > 1:
        caveats.append(
            f"paired_table_uri carried {n_id_groups} distinct obs_id groups "
            "(multiple stations); NSE/KGE/PBIAS/RSR/RMSE/R2 and the peak "
            "metrics are computed on the POOLED sample across all stations, "
            "not per-station."
        )

    variable_norm = str(variable or "generic").strip().lower() or "generic"

    sof = _import_spotpy_objectivefunctions()
    metrics = _compute_core_metrics(obs, sim, sof, caveats)

    peak_error, peak_timing_error = _peak_metrics(obs, sim, times, caveats)
    metrics["peak_error"] = peak_error
    metrics["peak_timing_error"] = peak_timing_error

    srms: float | None = None
    if variable_norm == "head":
        rmse_val = metrics["RMSE"]
        obs_range = float(np.max(obs) - np.min(obs))
        if rmse_val is not None and obs_range > 0.0:
            # SRMS = RMSE / (max(obs) - min(obs)) -- a PLAIN RATIO (contract
            # 3.2); do NOT multiply by 100.
            srms = round(rmse_val / obs_range, 6)
        else:
            caveats.append(
                "SRMS is null: observed head range is zero (or RMSE is null)."
            )
    metrics["SRMS"] = srms

    bands = dict(_BANDS)
    if variable_norm != "streamflow":
        caveats.append(
            "NSE/PBIAS/RSR bands are published for MONTHLY STREAMFLOW "
            "calibration (Moriasi 2007) and are applied here as a general "
            f"reference heuristic for variable={variable_norm!r}, not a "
            "domain-specific rule."
        )
    if variable_norm == "head" and srms is not None:
        # SRMS is a plain ratio, so the Anderson-Woessner threshold is 0.10
        # (i.e. RMSE within 10% of the observed head range), not "<10%".
        bands["SRMS"] = {"satisfactory": "<0.10", "good": None, "very_good": None}
    caveats.append(f"KGE has no graded acceptance band; diagnostic only ({_KNOBEN_2019}).")

    suggested_verdict = _suggested_verdict(
        n, metrics["NSE"], metrics["PBIAS"], metrics["RSR"], caveats
    )

    resolved_units = units
    logger.info(
        "compute_skill_metrics: variable=%s n=%d NSE=%s KGE=%s PBIAS=%s "
        "verdict=%s",
        variable_norm,
        n,
        metrics["NSE"],
        metrics["KGE"],
        metrics["PBIAS"],
        suggested_verdict,
    )

    notes.append(
        "Metrics via spotpy.objectivefunctions (nashsutcliffe/kge/pbias/rsr/"
        "rmse/rsquared); peak_error is a percent of the observed peak "
        "magnitude (positive = model over-predicts the peak); "
        "peak_timing_error is in seconds (positive = simulated peak occurs "
        "AFTER the observed peak)."
    )
    notes.append(
        "PBIAS sign convention: spotpy computes 100*sum(sim-obs)/sum(obs), so "
        "POSITIVE PBIAS = model OVER-predicts (simulated > observed). This is "
        "the OPPOSITE of the Moriasi 2007 tables (where positive PBIAS = model "
        "UNDER-estimation); the graded band lookup keys off abs(PBIAS), so the "
        "convention difference cannot misband the verdict."
    )

    return {
        "variable": variable_norm,
        "n": n,
        "metrics": metrics,
        "bands": bands,
        "bands_source": _BANDS_SOURCE,
        "suggested_verdict": suggested_verdict,
        "verdict_is_heuristic": True,
        "caveats": caveats,
        "units": resolved_units,
        "notes": notes,
    }
