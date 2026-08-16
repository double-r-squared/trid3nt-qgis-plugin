"""Shared machinery for the pelicun Assessment-API validation/sensitivity templates.

Each template here drives pelicun's real ``assessment.Assessment`` pipeline on
SYNTHETIC, domain-free inputs (idealized fragility/loss/demand models built in
memory, no fetched hazard raster or asset inventory) and returns distribution /
curve-shaped diagnostics. The primary output is a CHART (Vega-Lite spec), not a
map: these answer "does pelicun's Monte-Carlo match the analytic closed form",
"how does correlation change the loss spread", "where does the irreparable
override switch on", and "how sensitive is the flood depth-damage curve to
foundation type".

This module holds what those templates share:

  - :func:`build_damage_state_db`, :func:`build_loss_function_df`,
    :func:`build_consequence_df` -- construct the pelicun input DataFrames in the
    exact column schema ``file_io.load_data`` produces from the bundled CSVs.
  - :func:`ds_probability_check` -- Monte-Carlo damage-state probabilities for a
    2-damage-state sequential component vs the analytic lognormal closed form.
  - :func:`loss_function_identity_check` -- a 1:1 loss function reproduces the
    input EDP distribution (median + log-dispersion).
  - :func:`mixed_assessment_losses` -- one fragility-driven + one loss-function
    component aggregated together under a chosen EDP correlation structure.
  - :func:`replacement_threshold_point` -- fraction of realizations pushed to
    full replacement by a RID-triggered irreparable override at one threshold,
    with the residual drift either inferred from PID or fixed.
  - :func:`emit_chart_if_live` -- side-emit a Vega-Lite chart through the live
    pipeline emitter when one is bound (a no-op offline).

Determinism boundary: every returned scalar is a pelicun output or an analytic
closed-form value -- never free-generated.
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(
    "trid3nt_server.workflows.pelicun._validation_common"
)

__all__ = [
    "PelicunValidationError",
    "ds_probability_check",
    "loss_function_identity_check",
    "mixed_assessment_losses",
    "replacement_threshold_point",
    "emit_chart_if_live",
    "grouped_bar_spec",
    "multi_series_line_spec",
    "VEGA_LITE_V5_SCHEMA",
]

VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def grouped_bar_spec(
    rows: list[dict[str, Any]],
    *,
    x_field: str,
    y_field: str,
    color_field: str,
    x_title: str,
    y_title: str,
    title: str,
) -> dict[str, Any]:
    """A grouped (color-split) bar chart Vega-Lite spec. Pure."""
    return {
        "$schema": VEGA_LITE_V5_SCHEMA,
        "title": title,
        "data": {"values": list(rows)},
        "mark": {"type": "bar"},
        "encoding": {
            "x": {"field": x_field, "type": "nominal", "title": x_title},
            "xOffset": {"field": color_field, "type": "nominal"},
            "y": {"field": y_field, "type": "quantitative", "title": y_title},
            "color": {"field": color_field, "type": "nominal"},
            "tooltip": [
                {"field": x_field, "type": "nominal"},
                {"field": color_field, "type": "nominal"},
                {"field": y_field, "type": "quantitative", "format": ".4g"},
            ],
        },
    }


def multi_series_line_spec(
    rows: list[dict[str, Any]],
    *,
    x_field: str,
    y_field: str,
    color_field: str,
    x_title: str,
    y_title: str,
    title: str,
    x_scale: str | None = None,
) -> dict[str, Any]:
    """A multi-series (color-grouped) line+point chart Vega-Lite spec. Pure."""
    x_enc: dict[str, Any] = {"field": x_field, "type": "quantitative", "title": x_title}
    if x_scale:
        x_enc["scale"] = {"type": x_scale}
    return {
        "$schema": VEGA_LITE_V5_SCHEMA,
        "title": title,
        "data": {"values": list(rows)},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": x_enc,
            "y": {"field": y_field, "type": "quantitative", "title": y_title},
            "color": {"field": color_field, "type": "nominal"},
            "tooltip": [
                {"field": color_field, "type": "nominal"},
                {"field": x_field, "type": "quantitative", "format": ".4g"},
                {"field": y_field, "type": "quantitative", "format": ".4g"},
            ],
        },
    }


class PelicunValidationError(RuntimeError):
    """A pelicun validation/sensitivity template failed to run.

    ``error_code`` maps to the WebSocket error frame; ``retryable`` guides the
    agent retry loop. Raised for invalid knobs or a pelicun runtime failure.
    """

    error_code: str = "PELICUN_VALIDATION_ERROR"
    retryable: bool = False


# ---------------------------------------------------------------------------
# pelicun input-DataFrame builders. pelicun's models expect the column schema
# that ``file_io.load_data`` yields from the bundled CSVs (a MultiIndex on the
# LS*/Demand-* columns). Round-tripping a small in-memory frame through a temp
# CSV + ``file_io.load_data`` reproduces that schema exactly.
# ---------------------------------------------------------------------------


def _normalize_via_file_io(df: pd.DataFrame, index_name: str, ucf: Any) -> pd.DataFrame:
    """Round-trip ``df`` through ``file_io.load_data`` to get pelicun's schema."""
    from pelicun import file_io

    fd, path = tempfile.mkstemp(suffix=".csv", prefix="trid3nt_pelicun_")
    os.close(fd)
    try:
        out = df.copy()
        out.index.name = index_name
        out.to_csv(path)
        loaded = file_io.load_data(path, reindex=False, unit_conversion_factors=ucf)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    assert isinstance(loaded, pd.DataFrame)
    return loaded


def _new_assessment(seed: int) -> Any:
    from pelicun import assessment

    return assessment.Assessment({"PrintLog": False, "Seed": int(seed)})


# ---------------------------------------------------------------------------
# Check 1: Monte-Carlo damage-state probabilities vs analytic closed form.
# A single component with two sequential lognormal capacity limit states under a
# lognormal drift demand -- the pelicun CI validation-v1 configuration.
# ---------------------------------------------------------------------------


def ds_probability_check(
    *,
    demand_median: float,
    demand_beta: float,
    capacity_1_median: float,
    capacity_2_median: float,
    capacity_beta: float,
    sample_size: int,
    seed: int = 42,
) -> dict[str, Any]:
    """Monte-Carlo DS probabilities vs the analytic lognormal closed form.

    Returns the per-DS analytic probability (``p_analytic`` = [P(DS0), P(DS1),
    P(DS2)]), the pelicun Monte-Carlo probability (``p_montecarlo``), and the
    ``max_abs_delta`` between them. The closed form for a demand ``D ~
    LN(demand_median, demand_beta)`` and capacities ``C_i ~ LN(capacity_i_median,
    capacity_beta)`` uses ``P(D > C_i) = 1 - Phi( (ln m_D - ln m_Ci) /
    sqrt(beta_D^2 + beta_C^2) )``.
    """
    from scipy.stats import norm

    a = _new_assessment(seed)
    demands = pd.DataFrame(
        {
            "Theta_0": [demand_median],
            "Theta_1": [demand_beta],
            "Family": ["lognormal"],
            "Units": ["rad"],
        },
        index=pd.MultiIndex.from_tuples([("PID", "1", "1")]),
    )
    a.demand.load_model({"marginals": demands})
    a.demand.generate_sample({"SampleSize": int(sample_size)})
    a.stories = 1
    cmp = pd.DataFrame(
        {"Units": ["ea"], "Location": [1], "Direction": [1], "Theta_0": [1], "Blocks": [1]},
        index=["cmp.A"],
    )
    a.asset.load_cmp_model({"marginals": cmp})
    a.asset.generate_cmp_sample(int(sample_size))
    damage_db = _normalize_via_file_io(
        pd.DataFrame(
            {
                "Demand-Directional": [1], "Demand-Offset": [0],
                "Demand-Type": ["Story Drift Ratio"], "Demand-Unit": ["rad"],
                "LS1-Family": ["lognormal"], "LS1-Theta_0": [capacity_1_median],
                "LS1-Theta_1": [capacity_beta],
                "LS2-Family": ["lognormal"], "LS2-Theta_0": [capacity_2_median],
                "LS2-Theta_1": [capacity_beta],
            },
            index=["cmp.A"],
        ),
        "ID",
        a.unit_conversion_factors,
    )
    a.damage.load_model_parameters([damage_db], set(a.asset.list_unique_component_ids()))
    a.damage.calculate()
    probs = a.damage.ds_model.probabilities()
    mc = [float(probs.iloc[0, i]) for i in range(3)]

    denom = float(np.sqrt(demand_beta**2 + capacity_beta**2))
    z1 = (np.log(demand_median) - np.log(capacity_1_median)) / denom
    z2 = (np.log(demand_median) - np.log(capacity_2_median)) / denom
    p0 = 1.0 - float(norm.cdf(z1))
    p1 = float(norm.cdf(z1)) - float(norm.cdf(z2))
    p2 = float(norm.cdf(z2))
    analytic = [p0, p1, p2]
    return {
        "p_analytic": analytic,
        "p_montecarlo": mc,
        "max_abs_delta": max(abs(mc[i] - analytic[i]) for i in range(3)),
    }


# ---------------------------------------------------------------------------
# Check 2: a 1:1 loss function reproduces the input EDP distribution.
# ---------------------------------------------------------------------------


def loss_function_identity_check(
    *,
    edp_median: float,
    edp_beta: float,
    sample_size: int,
    seed: int = 42,
) -> dict[str, Any]:
    """A 1:1 loss function reproduces the input EDP distribution.

    A single loss-function component maps the peak-floor-acceleration EDP to loss
    with the identity curve ``loss = EDP`` (breakpoints ``0..1000 -> 0..1000``).
    The demand and the loss-function domain share the ``g`` unit (no conversion),
    so the loss distribution must reproduce the input lognormal EDP exactly:
    returns the realized ``loss_median`` (target ``edp_median``) and
    ``loss_log_std`` (target ``edp_beta``), plus the sample summary for plotting.
    """
    from pelicun import file_io

    a = _new_assessment(seed)
    demands = pd.DataFrame(
        {
            "Theta_0": [edp_median], "Theta_1": [edp_beta],
            "Family": ["lognormal"], "Units": ["g"],
        },
        index=pd.MultiIndex.from_tuples([("PFA", "0", "1")]),
    )
    a.demand.load_model({"marginals": demands})
    a.demand.generate_sample({"SampleSize": int(sample_size)})
    a.stories = 1
    cmp = pd.DataFrame(
        {"Units": ["ea"], "Location": [1], "Direction": [1], "Theta_0": [1], "Blocks": [1]},
        index=["cmp.A"],
    )
    a.asset.load_cmp_model({"marginals": cmp})
    a.asset.generate_cmp_sample(int(sample_size))
    a.loss.decision_variables = ("Cost",)
    a.loss.add_loss_map(pd.DataFrame(["cmp.A"], columns=["Repair"], index=["cmp.A"]))
    loss_functions = _normalize_via_file_io(
        pd.DataFrame(
            {
                "DV-Unit": ["loss_ratio"], "Demand-Directional": [1], "Demand-Offset": [0],
                "Demand-Type": ["Peak Floor Acceleration"], "Demand-Unit": ["g"],
                "LossFunction-Theta_0": ["0.00,1000.00|0.00,1000.00"],
            },
            index=["cmp.A-Cost"],
        ),
        "-",
        a.unit_conversion_factors,
    )
    a.loss.load_model_parameters([loss_functions])
    a.loss.calculate()
    loss, _ = a.loss.aggregate_losses(future=True)
    vals = loss["repair_cost"].to_numpy()
    vals = vals[np.isfinite(vals) & (vals > 0)]
    return {
        "loss_median": float(np.median(vals)),
        "loss_log_std": float(np.log(vals).std()),
        "edp_median": float(edp_median),
        "edp_beta": float(edp_beta),
        "samples": vals,
    }


# ---------------------------------------------------------------------------
# Check 3: one fragility-driven + one loss-function component in ONE assessment,
# under a chosen EDP correlation structure.
# ---------------------------------------------------------------------------


def mixed_assessment_losses(
    *,
    correlation: str,
    sample_size: int,
    across_floors: bool,
    across_damage_states: bool,
    seed: int = 415,
) -> np.ndarray:
    """Aggregate repair-cost realizations from a mixed fragility + loss-function run.

    A drift-driven fragility component (with a discrete DS consequence) and an
    acceleration-driven loss-function component are assessed together. ``PID`` and
    ``PFA`` demands are sampled with either ``"perfect"`` (all-ones copula) or
    ``"independent"`` correlation. The ``across_floors`` / ``across_damage_states``
    economy-of-scale aggregation options are set on the assessment. Returns the
    finite aggregate ``repair_cost`` realizations (loss ratio).
    """
    if correlation not in ("perfect", "independent"):
        raise PelicunValidationError(
            f"correlation must be 'perfect' or 'independent'; got {correlation!r}"
        )
    a = _new_assessment(seed)
    a.options.eco_scale["AcrossFloors"] = bool(across_floors)
    a.options.eco_scale["AcrossDamageStates"] = bool(across_damage_states)
    idx = pd.MultiIndex.from_tuples([("PID", "1", "1"), ("PFA", "0", "1")])
    dem = pd.DataFrame(
        {
            "Family": ["lognormal", "lognormal"],
            "Theta_0": [0.02, 0.50], "Theta_1": [0.40, 0.40],
            "Units": ["rad", "g"],
        },
        index=idx,
    )
    if correlation == "perfect":
        corr = pd.DataFrame(np.ones((2, 2)), columns=idx, index=idx)
        a.demand.load_model({"marginals": dem, "correlation": corr})
    else:
        a.demand.load_model({"marginals": dem})
    a.demand.generate_sample({"SampleSize": int(sample_size)})
    a.stories = 1
    cmp = pd.DataFrame(
        {
            "Units": ["ea", "ea"], "Location": ["1", "1"], "Direction": ["1", "1"],
            "Theta_0": [1, 1], "Blocks": [1, 1],
        },
        index=["frag.drift", "lossfn.accel"],
    )
    a.asset.load_cmp_model({"marginals": cmp})
    a.asset.generate_cmp_sample(int(sample_size))
    ucf = a.unit_conversion_factors
    dmg = _normalize_via_file_io(
        pd.DataFrame(
            {
                "Demand-Directional": [1.0], "Demand-Offset": [0.0],
                "Demand-Type": ["Story Drift Ratio"], "Demand-Unit": ["rad"],
                "LS1-Family": ["lognormal"], "LS1-Theta_0": [0.02], "LS1-Theta_1": [0.5],
            },
            index=["frag.drift"],
        ),
        "ID",
        ucf,
    )
    # Loss-function demand domain is generous (0..10 g) so no sampled PFA tail
    # falls outside the interpolation range (pelicun stores accel in mps2, 9.81x).
    lossfn = _normalize_via_file_io(
        pd.DataFrame(
            {
                "DV-Unit": ["loss_ratio"], "Demand-Directional": [1], "Demand-Offset": [0],
                "Demand-Type": ["Peak Floor Acceleration"], "Demand-Unit": ["g"],
                "LossFunction-Theta_0": ["0.00,0.40|0.00,10.00"],
            },
            index=["lossfn.accel-Cost"],
        ),
        "-",
        ucf,
    )
    conseq = _normalize_via_file_io(
        pd.DataFrame(
            {
                "Incomplete-": [0], "Quantity-Unit": ["1 EA"],
                "DV-Unit": ["loss_ratio"], "DS1-Theta_0": [0.25],
            },
            index=["frag.drift-Cost"],
        ),
        "-",
        ucf,
    )
    a.damage.load_model_parameters([dmg], set(a.asset.list_unique_component_ids()))
    a.damage.calculate()
    a.loss.decision_variables = ("Cost",)
    a.loss.add_loss_map(
        pd.DataFrame(
            ["frag.drift", "lossfn.accel"], columns=["Repair"],
            index=["frag.drift", "lossfn.accel"],
        ),
        loss_map_policy="fill",
    )
    a.loss.load_model_parameters([conseq, lossfn])
    a.loss.calculate()
    agg, _ = a.loss.aggregate_losses(future=True)
    v = agg["repair_cost"].to_numpy()
    return v[np.isfinite(v)]


# ---------------------------------------------------------------------------
# Check 4: RID-triggered irreparable override -> full replacement, at one
# threshold, with residual drift inferred from PID or fixed.
# ---------------------------------------------------------------------------


def replacement_threshold_point(
    *,
    rid_threshold: float,
    sample_size: int,
    rid_source: str,
    fixed_rid: float = 0.006,
    seed: int = 415,
) -> dict[str, Any]:
    """Fraction pushed to full replacement by the irreparable override at one threshold.

    A drift demand ``PID`` drives a residual-drift ``RID`` (either inferred from
    ``PID`` via ``estimate_RID`` -- the FEMA P-58 PID->RID model, ``rid_source=
    "inferred"`` -- or a ``fixed`` constant). An ``excessiveRID`` fragility with
    median capacity ``rid_threshold`` triggers -- through the damage process -- an
    ``irreparable`` state that the loss map redirects to full ``replacement``
    (loss ratio 1.0). A ``collapse`` branch (Sa fragility) also maps to
    replacement. Returns ``frac_replaced`` (mean aggregate loss, since a replaced
    realization is loss ratio 1.0 and a repairable one is 0) and the finite loss
    realizations.
    """
    if rid_source not in ("inferred", "fixed"):
        raise PelicunValidationError(
            f"rid_source must be 'inferred' or 'fixed'; got {rid_source!r}"
        )
    a = _new_assessment(seed)
    a.options.eco_scale["AcrossFloors"] = True
    a.options.eco_scale["AcrossDamageStates"] = True
    idx = pd.MultiIndex.from_tuples([("PID", "1", "1")])
    dem = pd.DataFrame(
        {"Family": ["lognormal"], "Theta_0": [0.02], "Theta_1": [0.5], "Units": ["rad"]},
        index=idx,
    )
    a.demand.load_model({"marginals": dem})
    a.demand.generate_sample({"SampleSize": int(sample_size)})
    ds = a.demand.save_sample()
    if rid_source == "inferred":
        rid = a.demand.estimate_RID(ds["PID"], {"yield_drift": 0.01})
    else:
        rid = pd.concat(
            [pd.DataFrame(
                np.full(ds["PID"].shape, float(fixed_rid)),
                index=ds["PID"].index, columns=ds["PID"].columns)],
            axis=1, keys=["RID"],
        )
    ext = pd.concat([ds, rid], axis=1)
    ext["SA_1.13", 0, 1] = 1.20
    units = pd.DataFrame("", index=["Units"], columns=ext.columns, dtype=object)
    units.loc["Units", ["SA_1.13"]] = "g"
    units.loc["Units", ["PID", "RID"]] = "rad"
    a.demand.load_sample(pd.concat([ext, units]))
    a.stories = 1
    cmp = pd.DataFrame(
        {
            "Units": ["ea", "ea", "ea"], "Location": ["0", "1", "0"],
            "Direction": ["1", "1", "1"], "Theta_0": [1, 1, 1], "Blocks": [1, 1, 1],
        },
        index=["collapse", "excessiveRID", "irreparable"],
    )
    a.asset.load_cmp_model({"marginals": cmp})
    a.asset.generate_cmp_sample(int(sample_size))
    ucf = a.unit_conversion_factors
    dmg = _normalize_via_file_io(
        pd.DataFrame(
            {
                "Demand-Directional": [1.0, 1.0, 1.0], "Demand-Offset": [0.0, 0.0, 0.0],
                "Demand-Type": [
                    "Residual Interstory Drift Ratio",
                    "Peak Spectral Acceleration|1.13",
                    "Peak Spectral Acceleration|1.13",
                ],
                "Demand-Unit": ["rad", "g", "g"],
                "LS1-Family": ["lognormal", "", "lognormal"],
                "LS1-Theta_0": [float(rid_threshold), 1e10, 1.50],
                "LS1-Theta_1": [0.3, "", 0.5],
            },
            index=["excessiveRID", "irreparable", "collapse"],
        ),
        "ID",
        ucf,
    )
    a.damage.load_model_parameters([dmg], set(a.asset.list_unique_component_ids()))
    a.damage.calculate(
        dmg_process={
            "1_collapse": {"DS1": "ALL_NA"},
            "2_excessiveRID": {"DS1": "irreparable_DS1"},
        }
    )
    a.loss.decision_variables = ("Cost",)
    a.loss.add_loss_map(
        pd.DataFrame(
            ["replacement", "replacement"], columns=["Repair"],
            index=["collapse", "irreparable"],
        ),
        loss_map_policy="fill",
    )
    conseq = _normalize_via_file_io(
        pd.DataFrame(
            {
                "Incomplete-": [0], "Quantity-Unit": ["1 EA"],
                "DV-Unit": ["loss_ratio"], "DS1-Theta_0": [1.0],
            },
            index=["replacement-Cost"],
        ),
        "-",
        ucf,
    )
    a.loss.load_model_parameters([conseq])
    a.loss.calculate()
    agg, _ = a.loss.aggregate_losses(future=True)
    v = agg["repair_cost"].to_numpy()
    v = v[np.isfinite(v)]
    return {"frac_replaced": float(np.mean(v > 0.5)), "mean_loss": float(v.mean()), "samples": v}


# ---------------------------------------------------------------------------
# Live chart side-emission (a no-op offline / on direct-call drivers).
# ---------------------------------------------------------------------------


async def emit_chart_if_live(
    vega_lite_spec: dict[str, Any], *, title: str, caption: str
) -> bool:
    """Side-emit ``vega_lite_spec`` through the live pipeline emitter, if bound.

    Returns True when a chart was emitted. Offline (no bound emitter) it is a
    no-op returning False -- the numeric summary the template returns is the
    determinism-boundary source of truth regardless.
    """
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    emitter = current_emitter()
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return False
    from trid3nt_server.data.processing.charts_common import build_chart_payload

    try:
        payload = build_chart_payload(
            vega_lite_spec=vega_lite_spec, title=title, caption=caption
        )
        await emitter.emit_chart(payload)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("pelicun validation chart emit failed: %s", exc)
        return False
