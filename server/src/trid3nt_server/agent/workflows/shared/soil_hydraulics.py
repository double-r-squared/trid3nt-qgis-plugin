"""Pedotransfer seam: soil texture -> saturated hydraulic conductivity.

A SHARED, engine-agnostic seam. It converts near-surface soil texture (sand /
clay / organic-matter fractions, e.g. from ``fetch_soilgrids`` or
``fetch_statsgo_soils``) into a saturated hydraulic conductivity via the
published Saxton & Rawls (2006) pedotransfer functions, and returns the value
WITH a loud provenance label so no caller can mistake it for measured aquifer K.

Provenance honesty (input-review norm):
    The result is a DERIVED, near-surface texture PROXY - NOT a measured aquifer
    hydraulic conductivity. Saxton-Rawls estimates Ksat of the SOIL matrix from
    texture at a shallow depth; a confined/semi-confined aquifer's K can differ
    by orders of magnitude (fractures, gravel lenses, cementation). Every result
    carries ``basis="pedotransfer_saxton_rawls_2006"`` and a ``limitation``
    string the composer must narrate. Use it as a labeled screening default when
    the user supplies no K and no aquifer-test value exists - never as a silent
    substitute for measured hydrogeology (data-source-fallback norm: a
    cross-domain proxy must be LOUD).

Citation:
    Saxton, K.E. and Rawls, W.J. (2006). "Soil Water Characteristic Estimates by
    Texture and Organic Matter for Hydrologic Solutions." Soil Science Society of
    America Journal 70(5):1569-1578. doi:10.2136/sssaj2005.0117
    (Ksat regression, their Eq. 16 with the moisture regressions Eq. 1-5.)

Shared use:
    Imported by the MODFLOW capture-zone / wellhead-protection composer (a labeled
    DERIVED ``aquifer_k_ms`` default) and available to the Landlab groundwater
    templates (``groundwater_water_table`` / ``groundwater_storm_recession``) for
    the same texture->K step. One implementation, one provenance vocabulary.

Pure + offline: numpy-free, no I/O, no network. Deterministic. NEVER raises on a
physically-shaped input; raises ``SoilHydraulicsInputError`` only on out-of-range
fractions so a bad upstream texture read is a typed stop, not a garbage K.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SoilHydraulicsInputError",
    "PedotransferK",
    "saxton_rawls_ksat",
    "ksat_from_texture",
    "MM_PER_HR_TO_M_PER_S",
]

#: mm/hr -> m/s (Saxton-Rawls reports Ksat in mm/hr).
MM_PER_HR_TO_M_PER_S: float = 1.0 / (1000.0 * 3600.0)

#: Plausibility clamp on the returned K (m/s). Saxton-Rawls can extrapolate to
#: unphysical extremes for texture blends outside its calibration cloud; the
#: clamp keeps a screening default inside the span of natural granular media
#: (coarse gravelly sand ~ 1e-2 m/s .. dense clay ~ 1e-9 m/s). A clamp HIT is
#: recorded in provenance so the narration can flag the extrapolation.
K_FLOOR_M_S: float = 1.0e-9
K_CEIL_M_S: float = 1.0e-2


class SoilHydraulicsInputError(ValueError):
    """Texture fractions were missing or out of the [0, 1] simplex (typed stop)."""


@dataclass(frozen=True)
class PedotransferK:
    """A derived saturated hydraulic conductivity + its full provenance.

    Every field the composer needs to narrate the basis honestly: the value, the
    named function, the exact texture inputs it was computed from, whether the
    plausibility clamp fired, and the standing limitation string.
    """

    k_m_s: float
    k_mm_hr: float
    basis: str
    sand_fraction: float
    clay_fraction: float
    organic_matter_pct: float
    depth_label: str
    clamped: bool
    porosity: float
    limitation: str
    intermediates: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly provenance dict for the narration summary."""
        return {
            "k_m_s": self.k_m_s,
            "k_mm_hr": self.k_mm_hr,
            "basis": self.basis,
            "sand_fraction": self.sand_fraction,
            "clay_fraction": self.clay_fraction,
            "organic_matter_pct": self.organic_matter_pct,
            "depth_label": self.depth_label,
            "clamped": self.clamped,
            "porosity": self.porosity,
            "limitation": self.limitation,
        }


def _validate_fraction(name: str, value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise SoilHydraulicsInputError(f"{name} must be a number; got {value!r}") from exc
    if not math.isfinite(v):
        raise SoilHydraulicsInputError(f"{name} must be finite; got {value!r}")
    if v < 0.0 or v > 1.0:
        raise SoilHydraulicsInputError(
            f"{name} must be a fraction in [0, 1]; got {v}. Pass sand/clay as "
            "fractions (SoilGrids percent / 100), not percentages."
        )
    return v


def saxton_rawls_ksat(
    sand_fraction: float,
    clay_fraction: float,
    organic_matter_pct: float = 1.0,
) -> tuple[float, dict[str, float]]:
    """Saturated hydraulic conductivity (mm/hr) from texture, Saxton-Rawls (2006).

    Implements the 2006 moisture regressions (their Eq. 1-5) and the Ksat closure
    (their Eq. 16): Ksat = 1930 * (theta_s - theta_33) ** (3 - lambda), with
    lambda = 1 / B and B the slope of the logarithmic tension-moisture curve
    between the 33 kPa (field capacity) and 1500 kPa (wilting) points.

    Args:
        sand_fraction: sand mass fraction, 0..1.
        clay_fraction: clay mass fraction, 0..1.
        organic_matter_pct: organic matter, percent by weight (default 1.0; OM =
            soil-organic-carbon * 1.724 when only SOC is available).

    Returns:
        ``(ksat_mm_hr, intermediates)`` - the conductivity plus the fitted
        moisture points (theta_1500, theta_33, theta_s, lambda) for provenance.

    Raises:
        SoilHydraulicsInputError: a fraction outside [0, 1], or a degenerate
            texture that drives a non-finite moisture curve.
    """
    S = _validate_fraction("sand_fraction", sand_fraction)
    C = _validate_fraction("clay_fraction", clay_fraction)
    try:
        OM = float(organic_matter_pct)
    except (TypeError, ValueError) as exc:
        raise SoilHydraulicsInputError(
            f"organic_matter_pct must be a number; got {organic_matter_pct!r}"
        ) from exc
    if not math.isfinite(OM) or OM < 0.0:
        OM = 0.0

    # 1500 kPa (permanent wilting point), Saxton-Rawls Eq. 1.
    t1500t = (
        -0.024 * S + 0.487 * C + 0.006 * OM
        + 0.005 * (S * OM) - 0.013 * (C * OM) + 0.068 * (S * C) + 0.031
    )
    theta_1500 = t1500t + (0.14 * t1500t - 0.02)

    # 33 kPa (field capacity), Eq. 2.
    t33t = (
        -0.251 * S + 0.195 * C + 0.011 * OM
        + 0.006 * (S * OM) - 0.027 * (C * OM) + 0.452 * (S * C) + 0.299
    )
    theta_33 = t33t + (1.283 * t33t * t33t - 0.374 * t33t - 0.015)

    # SAT-33 kPa (the air-entry to saturation increment), Eq. 3.
    ts33t = (
        0.278 * S + 0.034 * C + 0.022 * OM
        - 0.018 * (S * OM) - 0.027 * (C * OM) - 0.584 * (S * C) + 0.078
    )
    theta_s33 = ts33t + (0.636 * ts33t - 0.107)

    # Saturation (total porosity), Eq. 5.
    theta_s = theta_33 + theta_s33 - 0.097 * S + 0.043

    if not (0.0 < theta_1500 < theta_33 < theta_s < 1.0):
        raise SoilHydraulicsInputError(
            "degenerate Saxton-Rawls moisture curve for texture "
            f"(sand={S}, clay={C}, om={OM}): theta_1500={theta_1500:.4f}, "
            f"theta_33={theta_33:.4f}, theta_s={theta_s:.4f}. The texture is "
            "outside the pedotransfer calibration cloud."
        )

    # Tension-moisture slope B and pore-size index lambda, Eq. 15 / 18.
    B = (math.log(1500.0) - math.log(33.0)) / (
        math.log(theta_33) - math.log(theta_1500)
    )
    lam = 1.0 / B

    # Saturated conductivity, Eq. 16 (mm/hr).
    ksat_mm_hr = 1930.0 * (theta_s - theta_33) ** (3.0 - lam)

    intermediates = {
        "theta_1500": theta_1500,
        "theta_33": theta_33,
        "theta_s": theta_s,
        "lambda": lam,
    }
    return ksat_mm_hr, intermediates


def ksat_from_texture(
    sand_fraction: float,
    clay_fraction: float,
    organic_matter_pct: float = 1.0,
    *,
    depth_label: str = "0-5cm",
) -> PedotransferK:
    """Derive a labeled ``PedotransferK`` (m/s) from soil texture (Saxton-Rawls).

    The composer-facing entry point. Computes Ksat, clamps it to the
    natural-media plausibility span (recording a clamp hit), estimates a matching
    effective porosity from the saturation moisture, and attaches the standing
    limitation string. The value is a DERIVED near-surface proxy - the caller
    MUST narrate ``limitation``.

    Args:
        sand_fraction / clay_fraction: mass fractions 0..1.
        organic_matter_pct: OM percent (default 1.0).
        depth_label: the SoilGrids/STATSGO depth the texture was read at (for
            provenance; a shallow depth is exactly why this is a near-surface
            proxy).

    Returns:
        PedotransferK with ``k_m_s``, the named ``basis``, the texture inputs,
        the ``clamped`` flag, an estimated ``porosity``, and ``limitation``.
    """
    ksat_mm_hr, inter = saxton_rawls_ksat(
        sand_fraction, clay_fraction, organic_matter_pct
    )
    k_raw = ksat_mm_hr * MM_PER_HR_TO_M_PER_S
    k_clamped = min(max(k_raw, K_FLOOR_M_S), K_CEIL_M_S)
    clamped = not math.isclose(k_raw, k_clamped, rel_tol=1e-9, abs_tol=0.0)
    # Effective porosity: total porosity (theta_s) less field-capacity retention
    # approximates drainable/effective porosity better than theta_s alone for a
    # PRT travel-time default; floor to a physically sane lower bound.
    porosity = max(0.02, min(0.6, float(inter["theta_s"] - inter["theta_33"] + 0.05)))
    limitation = (
        "DERIVED near-surface soil-texture proxy (Saxton-Rawls 2006 pedotransfer, "
        f"depth {depth_label}), NOT a measured aquifer hydraulic conductivity. "
        "Aquifer K can differ by orders of magnitude from the shallow soil matrix "
        "(fractures, gravel lenses, cementation). Screening default only - narrate "
        "loudly and prefer a site aquifer test when one exists."
    )
    return PedotransferK(
        k_m_s=float(k_clamped),
        k_mm_hr=float(ksat_mm_hr),
        basis="pedotransfer_saxton_rawls_2006",
        sand_fraction=float(sand_fraction),
        clay_fraction=float(clay_fraction),
        organic_matter_pct=float(organic_matter_pct),
        depth_label=str(depth_label),
        clamped=bool(clamped),
        porosity=float(porosity),
        limitation=limitation,
        intermediates={k: float(v) for k, v in inter.items()},
    )
