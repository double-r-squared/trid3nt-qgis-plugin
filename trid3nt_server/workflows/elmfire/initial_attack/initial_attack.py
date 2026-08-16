"""Engine template ``elmfire_initial_attack_containment_probability`` - the
Hirsch initial-attack Probability Of Containment (POC) closed form.

CLOSED-FORM, no ELMFIRE engine run: given a fire's HEAD-FIRE INTENSITY (kW/m)
and its size when the first crew arrives, what is the probability initial attack
CONTAINS it - and how does that probability collapse as the ATTACK DELAY (the
get-away time between detection and line-building) grows? A slower response lets
the fire grow (elliptical spread from the head-fire rate of spread), and a
bigger, more intense fire is harder to hold.

Two coupled pieces of published fire science:

1. Byram (1959) fireline-intensity identity ``I = H * w * ROS`` inverted for the
   head-fire rate of spread ``ROS = I / (H * w)`` (H = 18 000 kJ/kg low heat of
   combustion, w = fuel consumed per unit area, kg/m2). A higher head-fire
   intensity means a faster-spreading head.
2. The Hirsch et al. (1998) POC model form - a logistic in fire SIZE, fireline
   INTENSITY, and their INTERACTION (the three predictors the paper found
   significant) - evaluated on the fire size at the moment of attack.

The fire grows during the attack delay via a standard elliptical point-source
model (length = (ROS_head + ROS_back) * t, breadth = length / length-to-breadth
ratio, area = pi/4 * L * B), so POC(delay) folds the growth INTO the containment
probability. The deliverable is a CHART (POC vs attack delay, one curve per head-
fire intensity) + the critical-delay scalars (delay at which POC crosses 0.5),
NOT a map - this is a closed-form validation-class tool, so it emits no raster.

Citation (published closed-form with EXACT coefficients):
  ELMFIRE suppression model, https://elmfire.io/user_guide/suppression.html
  (the Hirsch initial-attack POC formula ELMFIRE implements), after
  Hirsch, K.G., Corey, P.N., Martell, D.L. 1998. "Using expert judgment to model
  initial attack fire crew effectiveness." Forest Science 44(4):539-549.
The published POC formula, used VERBATIM here (``_HIRSCH_POC_COEFFS``):

    POC = E / (1 + E),  ln(E) = 4.6835 - 0.7043*A - 0.00041*I - 0.000052*A*I

with A = fire size (HECTARES) and I = head-fire intensity (kW/m) -- i.e. POC is
the logistic (``E/(1+E) = 1/(1+e^-lnE)``) of ln(E), which is linear in A, in I,
and in the A*I interaction (fire size + intensity + their interaction, the three
significant predictors of the Hirsch model). These are the EXACT published
coefficients (not a reconstruction).

Determinism boundary (Invariant 1): every POC number narrated comes from the
typed summary this tool returns - never free-generated.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.workflows.elmfire._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.workflows.elmfire.initial_attack.initial_attack"
)

__all__ = [
    "elmfire_initial_attack_containment_probability",
    "hirsch_poc",
    "byram_head_ros_m_per_min",
    "fire_size_at_attack_ha",
    "poc_vs_delay",
    "TEMPLATE_CARD",
]


# --------------------------------------------------------------------------- #
# Published constants + the Hirsch POC coefficient block (see module docstring)
# --------------------------------------------------------------------------- #
#: Byram low heat of combustion (kJ/kg). Byram 1959.
_BYRAM_H_KJ_PER_KG = 18000.0

#: Published Hirsch POC coefficients (elmfire.io suppression model), for
#:   ln(E) = b0 + b_A*A + b_I*I + b_AI*A*I ,  POC = E/(1+E)
#: A = fire size at attack (HECTARES), I = head-fire intensity (kW/m). EXACT
#: published values (module docstring) - NOT a reconstruction.
_HIRSCH_POC_COEFFS = (4.6835, -0.7043, -0.00041, -0.000052)  # (b0, b_A, b_I, b_AI)


TEMPLATE_CARD = TemplateCard(
    question=(
        "what is the PROBABILITY that initial attack CONTAINS a wildfire given "
        "its head-fire intensity + size at attack, and how fast does that "
        "probability collapse as the ATTACK DELAY (get-away time) grows "
        "(Hirsch 1998 POC closed form; no engine run)"
    ),
    required_inputs=[],
    knobs=(
        "head_fire_intensity_kw_m, detection_size_ha, attack_delay_min, "
        "fuel_consumption_kg_m2, length_to_breadth, head_to_back_ratio, "
        "intensity_levels_kw_m, max_delay_min"
    ),
)

_METADATA = AtomicToolMetadata(
    name="elmfire_initial_attack_containment_probability",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="elmfire",
    tier="template",
)


# --------------------------------------------------------------------------- #
# Closed-form physics (pure, unit-tested offline)
# --------------------------------------------------------------------------- #
def hirsch_poc(size_ha: float, intensity_kw_m: float) -> float:
    """Hirsch probability of containment (elmfire.io suppression formula) for a
    fire of ``size_ha`` HECTARES at head-fire ``intensity_kw_m`` kW/m:
    ``ln(E) = 4.6835 - 0.7043*A - 0.00041*I - 0.000052*A*I``, ``POC = E/(1+E)``
    (the logistic of ln(E) - linear in A, I, and their interaction)."""
    a = max(float(size_ha), 0.0)
    i = max(float(intensity_kw_m), 0.0)
    b0, b_a, b_i, b_ai = _HIRSCH_POC_COEFFS
    ln_e = b0 + b_a * a + b_i * i + b_ai * a * i
    ln_e = max(min(ln_e, 60.0), -60.0)  # overflow guard
    e = math.exp(ln_e)
    return e / (1.0 + e)


def byram_head_ros_m_per_min(intensity_kw_m: float, fuel_consumption_kg_m2: float) -> float:
    """Head-fire rate of spread (m/min) from Byram: ROS = I / (H * w).

    I in kW/m (= kJ/s/m), H = 18 000 kJ/kg, w = fuel consumed (kg/m2)."""
    w = max(float(fuel_consumption_kg_m2), 1e-3)
    ros_m_s = float(intensity_kw_m) / (_BYRAM_H_KJ_PER_KG * w)
    return ros_m_s * 60.0


def fire_size_at_attack_ha(
    detection_size_ha: float,
    head_ros_m_per_min: float,
    attack_delay_min: float,
    length_to_breadth: float,
    head_to_back_ratio: float,
) -> float:
    """Fire size (ha) when the crew begins line-building, = detection size plus
    elliptical point-source growth over the attack delay.

    Elliptical model: length L = (ROS_head + ROS_back) * t, breadth B = L / LB,
    area = pi/4 * L * B. ROS_back = ROS_head / HB."""
    lb = max(float(length_to_breadth), 1.0)
    hb = max(float(head_to_back_ratio), 1.0)
    ros_back = head_ros_m_per_min / hb
    length_m = (head_ros_m_per_min + ros_back) * max(float(attack_delay_min), 0.0)
    breadth_m = length_m / lb
    grown_ha = (math.pi / 4.0) * length_m * breadth_m / 10000.0
    return max(float(detection_size_ha), 0.0) + grown_ha


def poc_vs_delay(
    intensity_kw_m: float,
    detection_size_ha: float,
    fuel_consumption_kg_m2: float,
    length_to_breadth: float,
    head_to_back_ratio: float,
    delays_min: list[float],
) -> list[dict[str, float]]:
    """POC over an attack-delay ladder for one head-fire intensity. Pure."""
    ros = byram_head_ros_m_per_min(intensity_kw_m, fuel_consumption_kg_m2)
    out: list[dict[str, float]] = []
    for d in delays_min:
        size = fire_size_at_attack_ha(
            detection_size_ha, ros, d, length_to_breadth, head_to_back_ratio)
        out.append({"delay_min": float(d), "size_ha": size,
                    "poc": hirsch_poc(size, intensity_kw_m)})
    return out


def _critical_delay_min(curve: list[dict[str, float]], poc_threshold: float = 0.5) -> float | None:
    """Linear-interpolated delay at which POC first drops below ``poc_threshold``.
    None if POC never crosses (stays above or starts below)."""
    for i in range(1, len(curve)):
        p0, p1 = curve[i - 1]["poc"], curve[i]["poc"]
        if p0 >= poc_threshold > p1:
            d0, d1 = curve[i - 1]["delay_min"], curve[i]["delay_min"]
            frac = (p0 - poc_threshold) / (p0 - p1) if p0 != p1 else 0.0
            return d0 + frac * (d1 - d0)
    return None


# --------------------------------------------------------------------------- #
# Chart spec (Vega-Lite; no source layer -- closed-form class)
# --------------------------------------------------------------------------- #
def build_poc_chart_spec(curves: dict[float, list[dict[str, float]]]) -> dict[str, Any]:
    """Multi-line POC-vs-delay spec, one line per head-fire intensity, with a
    0.5 containment-threshold rule. Pure."""
    rows: list[dict[str, Any]] = []
    for intensity, curve in curves.items():
        label = f"{intensity:.0f} kW/m"
        for pt in curve:
            rows.append({"delay_min": pt["delay_min"], "poc": round(pt["poc"], 4),
                         "intensity": label})
    return {
        "title": "Initial-attack probability of containment vs attack delay (Hirsch 1998)",
        "layer": [
            {
                "data": {"values": rows},
                "mark": {"type": "line", "point": False},
                "encoding": {
                    "x": {"field": "delay_min", "type": "quantitative",
                          "title": "attack delay (min)"},
                    "y": {"field": "poc", "type": "quantitative",
                          "title": "probability of containment",
                          "scale": {"domain": [0, 1]}},
                    "color": {"field": "intensity", "type": "nominal",
                              "title": "head-fire intensity"},
                },
            },
            {
                "data": {"values": [{"t": 0.5}]},
                "mark": {"type": "rule", "color": "#888", "strokeDash": [4, 4]},
                "encoding": {"y": {"field": "t", "type": "quantitative"}},
            },
        ],
    }


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def elmfire_initial_attack_containment_probability(
    head_fire_intensity_kw_m: float = 2500.0,
    detection_size_ha: float = 0.1,
    attack_delay_min: float = 30.0,
    fuel_consumption_kg_m2: float = 1.5,
    length_to_breadth: float = 2.5,
    head_to_back_ratio: float = 4.0,
    intensity_levels_kw_m: list[float] | None = None,
    max_delay_min: float = 120.0,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Hirsch (1998) initial-attack probability of containment (POC), closed form.

    Computes the probability initial attack contains a fire of a given head-fire
    intensity as a function of the ATTACK DELAY (get-away time), plus a
    sensitivity sweep over several head-fire intensities. No ELMFIRE engine run -
    this is the closed-form containment model (module docstring cites Hirsch et
    al. 1998 Forest Science 44(4):539-549 + Byram 1959). Emits a POC-vs-delay
    chart (one line per intensity, 0.5 containment-threshold rule) and returns
    the typed scalars.

    Parameters:
      head_fire_intensity_kw_m: the nominal head-fire (Byram) intensity, kW/m.
        Default 2500 (near the direct hand-tool containment limit).
      detection_size_ha: fire size when detected, ha. Default 0.1.
      attack_delay_min: nominal get-away time (detection -> line-building), min.
        Default 30. The reported POC uses this delay.
      fuel_consumption_kg_m2: fuel consumed per unit area (Byram w), kg/m2.
        Default 1.5. Governs the head-fire ROS for a given intensity.
      length_to_breadth: fire ellipse length:breadth ratio. Default 2.5.
      head_to_back_ratio: head:back ROS ratio. Default 4.0.
      intensity_levels_kw_m: the head-fire intensities to sweep for the chart.
        Default [1000, 2500, 4000, 6000].
      max_delay_min: the attack-delay ladder upper bound, min. Default 120.

    Returns:
      A dict of scalars: ``poc_at_nominal_delay`` (POC at the nominal intensity +
      delay), ``fire_size_at_attack_ha``, ``head_ros_m_per_min``,
      ``critical_delay_min`` per intensity (delay where POC crosses 0.5),
      ``curves`` (the full POC-vs-delay ladders), and ``chart_emitted``.
    """
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    try:
        i_nom = float(head_fire_intensity_kw_m)
        det = max(float(detection_size_ha), 0.0)
        delay_nom = max(float(attack_delay_min), 0.0)
        w = max(float(fuel_consumption_kg_m2), 1e-3)
        lb = max(float(length_to_breadth), 1.0)
        hb = max(float(head_to_back_ratio), 1.0)
        max_delay = max(float(max_delay_min), 1.0)
        levels = intensity_levels_kw_m or [1000.0, 2500.0, 4000.0, 6000.0]
        levels = [float(x) for x in levels if float(x) > 0.0]
        if i_nom not in levels:
            levels = sorted(set(levels + [i_nom]))
    except (TypeError, ValueError) as exc:
        return {"status": "error", "error_code": "ELMFIRE_IA_POC_INVALID",
                "error_message": f"bad numeric input: {exc}"}

    # attack-delay ladder (0 .. max_delay) at ~2.5-min resolution
    n = max(int(max_delay / 2.5), 8)
    delays = [round(max_delay * k / n, 3) for k in range(n + 1)]

    curves: dict[float, list[dict[str, float]]] = {
        lvl: poc_vs_delay(lvl, det, w, lb, hb, delays) for lvl in levels
    }
    ros_nom = byram_head_ros_m_per_min(i_nom, w)
    size_nom = fire_size_at_attack_ha(det, ros_nom, delay_nom, lb, hb)
    poc_nom = hirsch_poc(size_nom, i_nom)
    critical = {f"{lvl:.0f}": _critical_delay_min(curves[lvl]) for lvl in levels}

    logger.info(
        "elmfire IA POC: I=%.0f kW/m ROS=%.1f m/min size@%.0fmin=%.3f ha POC=%.3f",
        i_nom, ros_nom, delay_nom, size_nom, poc_nom,
    )

    emitter = current_emitter()
    chart_emitted = False
    if emitter is not None and hasattr(emitter, "emit_chart"):
        try:
            from trid3nt_server.data.processing.charts_common import build_chart_payload
            spec = build_poc_chart_spec(curves)
            payload = build_chart_payload(
                vega_lite_spec=spec,
                title="Initial-attack probability of containment vs attack delay",
                caption=(
                    f"Hirsch (1998) POC closed form. Nominal {i_nom:.0f} kW/m head-fire "
                    f"intensity: POC {poc_nom:.2f} at a {delay_nom:.0f}-min get-away time "
                    f"(fire grown to {size_nom:.2f} ha). Higher intensity spreads faster "
                    f"(Byram ROS) and holds worse, so the curves fall off sooner. Dashed "
                    f"line = 0.5 containment threshold. Published elmfire.io Hirsch "
                    f"coefficients (exact)."
                ),
            )
            await emitter.emit_chart(payload)
            chart_emitted = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("elmfire IA POC chart emit failed: %s", exc)

    return {
        "status": "ok",
        "model": "hirsch_poc_elmfire",
        "citation": ("ELMFIRE suppression Hirsch POC, elmfire.io/user_guide/"
                     "suppression.html (exact published coefficients)"),
        "head_fire_intensity_kw_m": i_nom,
        "attack_delay_min": delay_nom,
        "detection_size_ha": det,
        "head_ros_m_per_min": round(ros_nom, 3),
        "fire_size_at_attack_ha": round(size_nom, 4),
        "poc_at_nominal_delay": round(poc_nom, 4),
        "critical_delay_min": {k: (round(v, 2) if v is not None else None)
                               for k, v in critical.items()},
        "intensity_levels_kw_m": levels,
        "curves": {f"{lvl:.0f}": curves[lvl] for lvl in levels},
        "chart_emitted": chart_emitted,
    }
