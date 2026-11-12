"""The study's charts: phi per iteration, the residual at each observation, each parameter's ensemble range.

Each is a chart payload the dock renders, built from what the output table read; nothing here reads a file.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from trid3nt_server.render.charts import build_chart_payload

__all__ = ["parameter_chart", "phi_chart", "residual_chart"]


def _title(caption: str) -> str:
    return f"{caption[:1].upper()}{caption[1:]}"


def phi_chart(phi: Sequence[Mapping[str, Any]], *, caption: str) -> dict[str, Any]:
    """Phi per iteration: the ensemble's mean, its range, and the base realization's where the study kept one."""
    rows = [{"iteration": row["iteration"], "phi": row[key], "series": label}
            for row in phi for key, label in (("mean", "mean"), ("min", "lowest"), ("max", "highest"),
                                              ("base", "base realization")) if key in row]
    first, last = phi[0], phi[-1]
    return build_chart_payload(
        vega_lite_spec={"mark": {"type": "line", "point": True}, "data": {"values": rows},
                        "encoding": {"x": {"field": "iteration", "type": "ordinal", "title": "Iteration"},
                                     "y": {"field": "phi", "type": "quantitative", "scale": {"type": "log"},
                                           "title": "Phi (sum of weighted squared residuals)"},
                                     "color": {"field": "series", "type": "nominal", "title": None}}},
        title=_title(caption),
        caption=(f"The {caption}: the ensemble's mean phi falls from {first['mean']:.4g} at iteration "
                 f"{first['iteration']} to {last['mean']:.4g} at iteration {last['iteration']}."))


def residual_chart(residuals: Sequence[Mapping[str, Any]], *, caption: str) -> dict[str, Any]:
    """Simulated minus observed at each observation of the base realization, in the observation's own unit."""
    rows = [{"observation": row["name"], "simulated_minus_observed": row["modelled"] - row["measured"],
             "observed": row["measured"], "simulated": row["modelled"]} for row in residuals]
    worst = max(rows, key=lambda row: abs(row["simulated_minus_observed"])) if rows else None
    return build_chart_payload(
        vega_lite_spec={"mark": "bar", "data": {"values": rows},
                        "encoding": {"x": {"field": "observation", "type": "nominal", "title": "Observation"},
                                     "y": {"field": "simulated_minus_observed", "type": "quantitative",
                                           "title": "Simulated minus observed"}}},
        title=_title(caption),
        caption=(f"The {caption} at {len(rows)} observations"
                 + (f"; the largest is {worst['simulated_minus_observed']:.3g} at {worst['observation']}."
                    if worst else ".")))


def parameter_chart(ensemble: Mapping[str, Mapping[str, float]], parameters: Sequence[Any], *,
                    caption: str) -> dict[str, Any]:
    """Each parameter's multiplier across the last ensemble - its range and the base realization's value -
    against the bounds it was tuned inside."""
    rows = []
    for parameter in parameters:
        values = [float(real[parameter.name]) for real in ensemble.values()]
        rows.append({"parameter": parameter.name, "lowest": min(values), "highest": max(values),
                     "base": float(ensemble["base"][parameter.name]) if "base" in ensemble else None,
                     "lower_bound": parameter.lower, "upper_bound": parameter.upper})
    return build_chart_payload(
        vega_lite_spec={"data": {"values": rows}, "layer": [
            {"mark": "rule", "encoding": {"x": {"field": "parameter", "type": "nominal", "title": "Parameter"},
                                          "y": {"field": "lowest", "type": "quantitative", "scale": {"type": "log"},
                                                "title": "Multiplier on the run's own array"},
                                          "y2": {"field": "highest"}}},
            {"mark": {"type": "point", "filled": True},
             "encoding": {"x": {"field": "parameter", "type": "nominal"},
                          "y": {"field": "base", "type": "quantitative"}}}]},
        title=_title(caption),
        caption=f"The {caption}: " + "; ".join(
            f"{row['parameter']} {row['lowest']:.3g} to {row['highest']:.3g} (base {row['base']:.3g}) inside "
            f"{row['lower_bound']:g} to {row['upper_bound']:g}" for row in rows) + ".")
