"""The seam's quantity -> style-preset registry (emit-on-solve).

The ``outputs.json`` entry carries a physical ``quantity`` and NO ``style_preset``
(NATE ruling: flat, role-free entries). The seam resolves styling HERE: a central
``quantity -> style_preset`` map, seeded from the style presets already in use in
``publish_layer._QGIS_STYLE_REGISTRY``. This is the "keeping only its idea as a
proper quantity->style registry" that survives the ``output_quantities`` scaffold
reconciliation -- the quantity is the lookup key, the preset stays the single
source of truth for rescale + colormap.

An unrecognized quantity resolves to the honest NEUTRAL RAMP (a fixed single-hue
viridis rescale) plus a ``logger.warning`` and a process-lifetime counter -- never
a silent physically-wrong colormap. Section 5.2 of the schema: the seam does the
one lazy COG touch for an unregistered quantity so the neutral ramp reads the
field's own dynamic range; that read happens at publish time in the consumer, not
here (this module owns only the preset decision).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("trid3nt_server.emission.quantity_styles")

__all__ = [
    "NEUTRAL_FALLBACK_PRESET",
    "MESH_PRESETS",
    "QUANTITY_STYLE_PRESETS",
    "resolve_style_preset",
    "unknown_quantity_fallback_count",
    "reset_unknown_quantity_fallback_count",
]

#: Presets that are NOT raster colormaps: a ``layer_type="mesh"`` layer renders
#: through the plugin's MDAL path (``QgsMeshLayer``), never the raster titiler
#: rescale registry (``publish_layer._QGIS_STYLE_REGISTRY``), so its preset is a
#: routing style, not a band rescale. A seeded quantity mapping to one of these is
#: valid even though the preset is absent from the raster registry (ADR 0283).
MESH_PRESETS: frozenset[str] = frozenset({"mesh_grid"})

#: The honest neutral ramp for an unregistered quantity: a single-hue viridis
#: rescale. The consumer computes the rescale from the COG's own band stats at
#: publish time; this preset selects the colormap family only. It is DELIBERATELY
#: NOT a physical band (an unknown quantity has no known physical range).
NEUTRAL_FALLBACK_PRESET: str = "neutral_ramp"

#: The per-instance family separator (ADR 0284). A producer that emits N sibling
#: temporal groups sharing ONE physical quantity but which MUST NOT collide on
#: the seam's ``(quantity, t)`` grouping (e.g. a multi_species MODFLOW run: N
#: concentration stacks on ONE time discretization -> identical ``t`` per step)
#: mints a per-instance quantity ``<family>__<slug>`` (e.g.
#: ``plume_concentration__tce``). ``resolve_style_preset`` falls back to the
#: family's registered preset when the full per-instance key is not itself
#: registered, so every sibling styles as its physical family with ONE registry
#: row. The double-underscore never appears in a base quantity key (they use
#: single underscores between words), so the split is unambiguous.
QUANTITY_FAMILY_SEP: str = "__"

#: quantity (outputs.json entry key) -> style_preset (the key into
#: publish_layer._QGIS_STYLE_REGISTRY). Seeded from the quantities engines emit
#: today. A NEW quantity registers ONE row here; an unregistered one degrades to
#: NEUTRAL_FALLBACK_PRESET (never a silent physically-wrong map).
QUANTITY_STYLE_PRESETS: dict[str, str] = {
    # Native-mesh temporal animation (TELEMAC SELAFIN sibling, ADR 0283): a
    # kind="mesh" entry MDAL animates directly. The preset is the generic
    # mesh-wireframe style (bbox=None, role=context) the mesh-preview seam uses --
    # the plugin's _add_mesh drives the dataset-group/CRS, so this is a routing
    # style, not a raster colormap.
    "model_results": "mesh_grid",
    # Hydrology (the flood proving case + plume).
    "flood_depth": "continuous_flood_depth",
    "wave_height": "continuous_wave_height",
    "dye_concentration": "continuous_plume_concentration",
    "plume_concentration": "continuous_plume_concentration",
    # MODFLOW head / archetype products.
    "water_table": "continuous_head_m",
    "head": "continuous_head_m",
    "drawdown": "continuous_drawdown_m",
    "mounding": "continuous_mounding_m",
    "dewatering_rate": "continuous_dewatering_rate",
    "hydroperiod": "continuous_hydroperiod_m",
    "subsidence": "continuous_subsidence_cm",
    "river_seepage": "diverging_river_seepage",
    "temperature": "continuous_temperature_c",
    # Landlab.
    "landslide_susceptibility": "continuous_landslide_susceptibility",
    "drainage_area": "continuous_drainage_area",
    "slope": "continuous_slope",
    "relative_wetness": "continuous_relative_wetness",
    "discharge": "continuous_discharge_m3s",
    "factor_of_safety": "continuous_factor_of_safety",
    # SWMM node/link outputs.
    "flooding_losses": "continuous_flooding_losses",
    "ponded_volume": "continuous_ponded_volume",
    "conduit_flow": "diverging_conduit_flow",
    "conduit_velocity": "continuous_conduit_velocity",
    # Seismic / geohazard.
    "seismic_pga": "continuous_seismic_pga",
    "liquefaction_probability": "continuous_liquefaction_probability",
    # Sediment / coastal deformation.
    "bed_evolution": "diverging_bed_evolution",
    "seafloor_deformation": "diverging_seafloor_deformation",
    # Fire (ELMFIRE).
    "fire_arrival": "continuous_fire_arrival_hr",
    "flame_length": "continuous_flame_length_m",
    "fire_spread_rate": "continuous_fire_spread_rate",
}

#: Process-lifetime count of unknown-quantity fallbacks (telemetry). Read via
#: ``unknown_quantity_fallback_count``; the seam's own JSONL telemetry can fold
#: it in. Not persisted -- a boot-scoped signal, like the retrieval-shadow counts.
_UNKNOWN_FALLBACK_COUNT = 0


def resolve_style_preset(quantity: str) -> tuple[str, bool]:
    """Resolve ``quantity`` to ``(style_preset, is_fallback)``.

    A registered quantity returns its physical preset and ``is_fallback=False``.
    An unregistered quantity returns ``(NEUTRAL_FALLBACK_PRESET, True)``, logs a
    WARNING, and bumps the process fallback counter -- the honest neutral ramp,
    never a silent physically-wrong colormap.
    """
    global _UNKNOWN_FALLBACK_COUNT
    key = (quantity or "").strip().lower()
    preset = QUANTITY_STYLE_PRESETS.get(key)
    if preset is not None:
        return preset, False
    # Per-instance family fallback (ADR 0284): "plume_concentration__tce" ->
    # the "plume_concentration" family preset. ONE registry row styles every
    # sibling stack; the siblings differ only to keep their seam temporal groups
    # (and layer ids) distinct, never their physical colormap.
    if QUANTITY_FAMILY_SEP in key:
        family = key.split(QUANTITY_FAMILY_SEP, 1)[0]
        family_preset = QUANTITY_STYLE_PRESETS.get(family)
        if family_preset is not None:
            return family_preset, False
    _UNKNOWN_FALLBACK_COUNT += 1
    logger.warning(
        "quantity_styles: unknown quantity %r -> neutral ramp (%s); register a "
        "quantity->preset row to give it a physical colormap (fallback #%d)",
        quantity,
        NEUTRAL_FALLBACK_PRESET,
        _UNKNOWN_FALLBACK_COUNT,
    )
    return NEUTRAL_FALLBACK_PRESET, True


def unknown_quantity_fallback_count() -> int:
    """Process-lifetime count of unknown-quantity neutral-ramp fallbacks."""
    return _UNKNOWN_FALLBACK_COUNT


def reset_unknown_quantity_fallback_count() -> None:
    """Reset the fallback counter (test hook only)."""
    global _UNKNOWN_FALLBACK_COUNT
    _UNKNOWN_FALLBACK_COUNT = 0
