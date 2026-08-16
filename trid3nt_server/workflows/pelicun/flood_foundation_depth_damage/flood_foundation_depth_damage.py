"""Engine template ``pelicun_flood_foundation_depth_damage_sweep`` - how sensitive
is the HAZUS flood repair-cost depth-damage curve to the building's foundation
type?

The bundled HAZUS v6.1 flood dataset (the same ``loss_repair.csv`` the
FloodRulesets auto-population selects from) ships a depth-damage loss function per
occupancy class and foundation configuration (number of floors, basement,
elevation, flood zone). For a chosen occupancy class this template extracts the
loss-ratio-vs-inundation-depth curve for each requested foundation variant and
compares them - the sensitivity of the assigned flood building class, and hence
the repair-cost loss function, to the foundation assumption.

Data: pelicun's bundled DamageAndLossModelLibrary flood ``loss_repair.csv`` (no
fetch, no Monte-Carlo). The output is a family-of-curves CHART (one depth-damage
curve per foundation variant), not a map. Every plotted point is a bundled HAZUS
loss-function breakpoint - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import numpy as np

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.workflows.pelicun._template_card import TemplateCard
from trid3nt_server.workflows.pelicun._validation_common import (
    PelicunValidationError,
    emit_chart_if_live,
    multi_series_line_spec,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.pelicun.flood_foundation_depth_damage."
    "flood_foundation_depth_damage"
)

__all__ = [
    "pelicun_flood_foundation_depth_damage_sweep",
    "extract_foundation_curves",
    "build_depth_damage_chart_spec",
    "TEMPLATE_CARD",
]

#: Default RES1 foundation configurations (matched as ID substrings).
_DEFAULT_VARIANTS = (
    "one_floor.no_basement",
    "one_floor.with_basement",
    "two_floors.no_basement",
    "split_level.no_basement",
)


TEMPLATE_CARD = TemplateCard(
    question=(
        "how sensitive the HAZUS flood repair-cost depth-damage curve is to the "
        "building foundation type -- compare loss-ratio-vs-inundation-depth curves "
        "across foundation configurations for one occupancy class"
    ),
    required_inputs=[],
    knobs="occupancy_class, foundation_variants",
)

_METADATA = AtomicToolMetadata(
    name="pelicun_flood_foundation_depth_damage_sweep",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="pelicun",
    tier="template",
)


def _flood_loss_repair_csv_path() -> str:
    """Absolute path to pelicun's bundled HAZUS v6.1 flood loss_repair.csv."""
    try:
        import pelicun  # noqa: F401
    except ImportError as exc:
        raise PelicunValidationError(
            "pelicun is not installed; cannot load HAZUS flood curves."
        ) from exc
    path = os.path.join(
        os.path.dirname(pelicun.__file__), "resources", "DamageAndLossModelLibrary",
        "flood", "building", "portfolio", "Hazus v6.1", "loss_repair.csv",
    )
    if not os.path.isfile(path):
        raise PelicunValidationError(
            f"HAZUS v6.1 flood loss_repair.csv not found at {path!r}."
        )
    return path


def _parse_curve(field: str) -> tuple[list[float], list[float]]:
    """Parse a ``lrs|depths`` HAZUS flood LossFunction-Theta_0 field -> (depths, lrs)."""
    if "|" not in field:
        raise PelicunValidationError(
            f"flood loss function has no '|' separator: {field[:60]!r}")
    lrs_part, depths_part = field.split("|", 1)
    lrs = [float(x) for x in lrs_part.split(",")]
    depths = [float(x) for x in depths_part.split(",")]
    if len(lrs) != len(depths):
        raise PelicunValidationError("flood loss function length mismatch")
    order = np.argsort(depths)
    return [depths[i] for i in order], [lrs[i] for i in order]


def extract_foundation_curves(
    occupancy_class: str, foundation_variants: list[str]
) -> list[dict[str, Any]]:
    """Extract one structural depth-damage curve per foundation variant.

    Reads the bundled HAZUS v6.1 flood ``loss_repair.csv`` and, for each variant
    substring, returns the first ``structural.*`` cost curve matching the
    occupancy class and that substring: ``{"variant", "curve_id", "depths_ft",
    "loss_ratios"}``. Raises ``PelicunValidationError`` when no curve matches a
    requested variant (a loud typed failure, never a silent empty family).
    """
    import pandas as pd

    df = pd.read_csv(_flood_loss_repair_csv_path())
    if "ID" not in df.columns or "LossFunction-Theta_0" not in df.columns:
        raise PelicunValidationError("flood loss_repair.csv missing required columns")
    ids = df["ID"].astype(str)
    occ = f".{occupancy_class}."
    out: list[dict[str, Any]] = []
    missing: list[str] = []
    for variant in foundation_variants:
        matches = df[
            ids.str.startswith("structural.")
            & ids.str.contains(occ, regex=False)
            & ids.str.contains(variant, regex=False)
            & ids.str.endswith("-Cost")
        ]
        if matches.empty:
            missing.append(variant)
            continue
        row = matches.iloc[0]
        depths, lrs = _parse_curve(str(row["LossFunction-Theta_0"]))
        out.append({
            "variant": variant, "curve_id": str(row["ID"]),
            "depths_ft": depths, "loss_ratios": lrs,
        })
    if missing:
        raise PelicunValidationError(
            f"no HAZUS flood curve for occupancy {occupancy_class!r} + foundation "
            f"variant(s) {missing}. Try foundation substrings like "
            "'one_floor.no_basement', 'with_basement', 'two_floors', 'split_level'.")
    return out


def build_depth_damage_chart_spec(curves: list[dict[str, Any]]) -> dict[str, Any]:
    """Family of depth-damage curves, one line per foundation variant. Pure."""
    rows: list[dict[str, Any]] = []
    for c in curves:
        for d, lr in zip(c["depths_ft"], c["loss_ratios"]):
            rows.append({"depth_ft": float(d), "loss_ratio": float(lr),
                         "foundation": c["variant"]})
    return multi_series_line_spec(
        rows, x_field="depth_ft", y_field="loss_ratio", color_field="foundation",
        x_title="inundation depth (ft)", y_title="repair-cost loss ratio",
        title="HAZUS flood depth-damage sensitivity to foundation type",
    )


@register_tool(
    _METADATA,
    read_only_hint=True,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def pelicun_flood_foundation_depth_damage_sweep(
    occupancy_class: str = "RES1",
    foundation_variants: list[str] | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Compare HAZUS flood depth-damage curves across foundation types.

    Fidelity: reads pelicun's bundled HAZUS v6.1 flood loss functions directly
    (the dataset the FloodRulesets auto-population selects from) - no fetch, no
    Monte-Carlo. Answers "how much does the foundation assumption change the flood
    repair-cost depth-damage curve" -- a data-driven sensitivity comparison, not a
    site loss estimate. Off-scope: real per-asset flood damage over a depth raster
    -> pelicun_damage_assessment.

    Use this when: the user asks how foundation type (basement, number of floors,
    elevation) affects HAZUS flood loss, how the flood building class / depth-damage
    curve is assigned, or wants to compare foundation depth-damage curves.

    Params:
        occupancy_class: HAZUS occupancy code (default "RES1"). Others include
            RES3A, COM1, IND1 - any occupancy with structural flood curves.
        foundation_variants: foundation-configuration ID substrings to compare
            (default single-family set: one_floor/two_floors/split_level x
            no_basement/with_basement). Examples: "with_basement",
            "elevated_open+4ft", "two_floors".

    Returns:
        On success: ``{"status": "ok", "occupancy_class", "curves": [{"variant",
        "curve_id", "depths_ft", "loss_ratios"}...], "loss_at_4ft": {variant ->
        loss ratio at 4 ft}, "chart_emitted"}``. A depth-damage family chart is
        emitted when a live emitter is bound.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
    """
    variants = list(foundation_variants) if foundation_variants else list(_DEFAULT_VARIANTS)
    if not variants:
        return {"status": "error", "error_code": "PELICUN_VALIDATION_INVALID",
                "error_message": "foundation_variants must be a non-empty list or None."}
    try:
        curves = await asyncio.to_thread(
            extract_foundation_curves, str(occupancy_class), variants)
        spec = build_depth_damage_chart_spec(curves)
        emitted = await emit_chart_if_live(
            spec, title="HAZUS flood depth-damage sensitivity to foundation type",
            caption="Repair-cost loss ratio vs inundation depth, one HAZUS v6.1 "
            "curve per foundation configuration.")
        loss_at_4ft = {
            c["variant"]: float(np.interp(4.0, c["depths_ft"], c["loss_ratios"]))
            for c in curves
        }
        logger.info(
            "pelicun_flood_foundation_depth_damage_sweep occ=%s variants=%d loss_at_4ft=%s",
            occupancy_class, len(curves), loss_at_4ft)
        return {
            "status": "ok", "occupancy_class": str(occupancy_class),
            "curves": curves, "loss_at_4ft": loss_at_4ft, "chart_emitted": emitted,
        }
    except asyncio.CancelledError:
        raise
    except PelicunValidationError as exc:
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("pelicun_flood_foundation_depth_damage_sweep failed: %s", exc)
        return {"status": "error", "error_code": "PELICUN_VALIDATION_ERROR",
                "error_message": f"flood depth-damage extraction failed: {exc}"}
