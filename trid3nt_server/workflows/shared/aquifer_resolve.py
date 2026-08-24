"""Shared soil-hydraulics + aquifer-property resolution (law 9).

Several engines need a soil / aquifer material property to solve - MODFLOW an
aquifer hydraulic conductivity + porosity, the Landlab groundwater + Green-Ampt
chains the same K + porosity, the SWMM two-zone aquifer a full moisture column
(porosity, wilting point, field capacity, conductivity). Historically each fell
back to a demo constant (e.g. MODFLOW's ``DEFAULT_AQUIFER_K_MS = 1e-4`` /
``DEFAULT_POROSITY = 0.3``) - a physics value invented out of nothing, labeled but
run on regardless. Law 9 forbids that: a physics-consequential parameter with no
real data source must REFUSE, not run on an invention.

This module is the single resolution seam those engines share (it lives in
``workflows/shared`` so landlab / swmm / modflow import one derivation, not three).
Near-surface soil texture (SoilGrids sand + clay, AOI-window mean) drives the
Saxton-Rawls (2006) pedotransfer functions - a real derived basis at the AOI:

- ``derive_soil_k`` / ``resolve_aquifer_properties`` -> saturated K + porosity
  (the MODFLOW / Landlab-groundwater path).
- ``derive_soil_column`` -> the two-zone moisture column porosity / wilting /
  field-capacity / conductivity (the SWMM aquifer-baseflow path), surfaced from
  the SAME texture fit (theta_s / theta_1500 / theta_33 / Ksat).

When the caller supplies a value it is used (``user``); when SoilGrids can serve,
the value is DERIVED (``derived``, source named, screening caveat stated); when
SoilGrids cannot serve (fetch fails, AOI off the soil surface / outside coverage)
the value is UNRESOLVED and its ``SyntheticInput`` carries ``basis="default_demo",
consequence="physics"`` so the input-review gate REFUSES in auto mode. The demo
constants are gone - there is no invented value to fall back to. Each engine
narrates its own entry (its own param names); the derivation is shared.

The screening caveat is stated truthfully in the entry note: pedotransfer from
shallow soil texture is a NEAR-SURFACE proxy, NOT a measured aquifer conductivity
(true aquifer K can differ by orders of magnitude). Derived-from-real-data is
acceptable under law 9; invented is not.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from trid3nt_contracts.common import SyntheticInput

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.shared.soil_hydraulics import (
    MM_PER_HR_TO_M_PER_S,
    SoilHydraulicsInputError,
    ksat_from_texture,
)

logger = logging.getLogger("trid3nt_server.workflows.shared.aquifer_resolve")

__all__ = [
    "sample_raster_at_points",
    "mean_valid_raster",
    "derive_soil_k",
    "SoilColumn",
    "derive_soil_column",
    "usda_texture_class",
    "SoilDerivation",
    "derive_soil_scalars",
    "soil_derived_entry",
    "literature_offer_entry",
    "AquiferResolution",
    "resolve_aquifer_properties",
    "provenance_summary",
    "SOIL_TEXTURE_HALF_DEG",
    "SOIL_TEXTURE_DEPTH",
    "PARTICLE_DENSITY_KG_M3",
]

#: Mineral soil particle (grain) density (kg/m^3) - the Saxton-Rawls (2006)
#: convention for the bulk-density closure rho_b = (1 - theta_s) * particle_density.
PARTICLE_DENSITY_KG_M3: float = 2650.0


def provenance_summary(resolution: "AquiferResolution") -> str:
    """Join the resolution's entry notes into one summary caveat line."""
    return " ".join(e.note for e in resolution.entries if e.note)

#: Half-width (deg) of the SoilGrids window fetched around the AOI for texture.
#: A tight box (~0.02 deg ~= 2 km) is ample - the pedotransfer K is driven by the
#: mean of the valid cells over this AOI window (robust to a nodata centroid).
SOIL_TEXTURE_HALF_DEG: float = 0.02

#: SoilGrids depth read for texture. The 5-15 cm horizon is a stable near-surface
#: texture (below the tilled/organic surface skin) - still a NEAR-SURFACE proxy,
#: NOT aquifer-depth material (the standing pedotransfer limitation).
SOIL_TEXTURE_DEPTH: str = "5-15cm"


def sample_raster_at_points(
    dem_uri: str, lons: list[float], lats: list[float]
) -> list[float | None]:
    """Sample a raster's band-1 value at each ``(lon, lat)``; None off-grid.

    The rasterio ``MemoryFile`` is held open across the whole sample loop (an
    orphaned MemoryFile GC-corrupts a lazy read). Reprojects the query points to
    the dataset CRS when it is not already EPSG:4326. NEVER raises.
    """
    try:
        import rasterio
        from pyproj import Transformer

        from trid3nt_server.tools.processing._gdal_runner import read_raster_bytes

        read_uri = dem_uri[len("file://"):] if dem_uri.startswith("file://") else dem_uri
        dem_bytes = read_raster_bytes(read_uri, on_error=lambda msg: RuntimeError(msg))
        out: list[float | None] = []
        with rasterio.MemoryFile(dem_bytes) as mf:
            with mf.open() as src:
                src_crs = src.crs
                if src_crs is not None and src_crs.to_epsg() != 4326:
                    to_ds = Transformer.from_crs("EPSG:4326", src_crs, always_xy=True)
                    xs, ys = to_ds.transform(list(lons), list(lats))
                else:
                    xs, ys = list(lons), list(lats)
                nodata = src.nodata
                for val in src.sample(list(zip(xs, ys)), indexes=1):
                    v = float(val[0])
                    if (nodata is not None and v == float(nodata)) or not math.isfinite(v):
                        out.append(None)
                    else:
                        out.append(v)
        return out
    except Exception as exc:  # noqa: BLE001 -- raster sampling is best-effort
        logger.warning("modflow: raster point-sampling failed (non-fatal): %s", exc)
        return [None] * len(lons)


def mean_valid_raster(uri: str) -> float | None:
    """Mean of the VALID (non-nodata, finite) band-1 cells over a raster window.

    The SoilGrids texture read fetches a tight (~2 km) AOI box, not a single cell.
    Sampling only the exact AOI centroid is brittle: an urban / open-water /
    river-corridor centroid can land on a nodata pixel while the surrounding AOI
    carries real soil texture, which would wrongly force a law-9 refusal when a
    genuine screening value exists. Averaging the valid cells over the window is
    the representative AOI texture for a screening pedotransfer estimate. Returns
    None only when EVERY cell is nodata / non-finite (genuinely no soil surface).
    NEVER raises.
    """
    try:
        import numpy as np
        import rasterio

        from trid3nt_server.tools.processing._gdal_runner import read_raster_bytes

        read_uri = uri[len("file://"):] if uri.startswith("file://") else uri
        data = read_raster_bytes(read_uri, on_error=lambda msg: RuntimeError(msg))
        with rasterio.MemoryFile(data) as mf:
            with mf.open() as src:
                arr = src.read(1, masked=True).astype("float64")
                nodata = src.nodata
        masked = np.ma.masked_invalid(arr)
        if nodata is not None:
            masked = np.ma.masked_values(masked, float(nodata))
        if masked.count() == 0:
            return None
        return float(masked.mean())
    except Exception as exc:  # noqa: BLE001 -- raster read is best-effort
        logger.warning("modflow: raster window-mean failed (non-fatal): %s", exc)
        return None


def _fetch_texture(lat: float, lon: float) -> tuple[float | None, float | None, dict[str, Any]]:
    """AOI-window mean sand+clay percent from SoilGrids at ``SOIL_TEXTURE_DEPTH``.

    The single texture read every soil-hydraulics derivation shares (K, porosity,
    the two-zone soil column). Fetches sand + clay percent in a tight box around
    the AOI and averages the valid cells over the window (robust to a nodata
    centroid pixel). Returns ``(sand_pct_or_None, clay_pct_or_None, meta)``;
    NEVER raises - any failure returns ``(None, None, meta_with_reason)`` so the
    caller REFUSES (law 9 - there is no demo default to fall back to).
    """
    meta: dict[str, Any] = {}
    try:
        soil_entry = TOOL_REGISTRY.get("fetch_soilgrids")
        if soil_entry is None:
            meta["reason"] = "fetch_soilgrids not registered"
            return None, None, meta
        d = SOIL_TEXTURE_HALF_DEG
        soil_bbox = [lon - d, lat - d, lon + d, lat + d]

        def _fetch(prop: str) -> str | None:
            layer = soil_entry.fn(
                bbox=soil_bbox, soil_property=prop, depth=SOIL_TEXTURE_DEPTH
            )
            return (
                layer.get("uri") if isinstance(layer, dict)
                else getattr(layer, "uri", None)
            )

        sand_uri = _fetch("sand")
        clay_uri = _fetch("clay")
        if not sand_uri or not clay_uri:
            meta["reason"] = "soilgrids returned no raster (ocean / off soil surface)"
            return None, None, meta
        sand_pct = mean_valid_raster(sand_uri)
        clay_pct = mean_valid_raster(clay_uri)
        if sand_pct is None or clay_pct is None:
            meta["reason"] = "no valid soil texture over the AOI window (all nodata)"
            return None, None, meta
        return float(sand_pct), float(clay_pct), meta
    except Exception as exc:  # noqa: BLE001 -- texture read is best-effort; refuse on failure
        meta["reason"] = f"soil-texture step error: {exc}"
        logger.warning(
            "soil-texture step failed (non-fatal, will REFUSE - no demo default): %s",
            exc,
        )
        return None, None, meta


def derive_soil_k(lat: float, lon: float) -> tuple[Any, dict[str, Any]]:
    """Derive a labeled pedotransfer K at the AOI from SoilGrids texture.

    Fetches AOI-window SoilGrids sand + clay percent and runs the shared
    Saxton-Rawls seam. Returns ``(PedotransferK_or_None, meta)`` - a loud
    provenance dict for narration. NEVER raises: any fetch / sample / pedotransfer
    failure returns ``(None, meta_with_reason)`` so the caller REFUSES (law 9 -
    there is no demo default to fall back to).
    """
    sand_pct, clay_pct, meta = _fetch_texture(lat, lon)
    if sand_pct is None or clay_pct is None:
        return None, meta
    try:
        pk = ksat_from_texture(
            sand_pct / 100.0, clay_pct / 100.0, depth_label=SOIL_TEXTURE_DEPTH,
        )
    except SoilHydraulicsInputError as exc:
        meta["reason"] = f"pedotransfer input invalid: {exc}"
        return None, meta
    meta.update(
        {"sand_pct": round(sand_pct, 1), "clay_pct": round(clay_pct, 1),
         "k_m_s": pk.k_m_s, "porosity": pk.porosity, "basis": pk.basis,
         "depth": SOIL_TEXTURE_DEPTH, "clamped": pk.clamped}
    )
    return pk, meta


@dataclass(frozen=True)
class SoilColumn:
    """A derived two-zone soil-moisture column + its provenance.

    The SWMM ``[AQUIFERS]`` two-zone balance (and any moisture-column model) needs
    porosity, wilting point, field capacity, and saturated conductivity. All four
    come from the SAME Saxton-Rawls (2006) texture fit that serves the aquifer K:
    theta_s (saturation) is porosity, theta_1500 is wilting point, theta_33 is
    field capacity, and the Ksat closure is the conductivity. A NEAR-SURFACE soil
    proxy, NOT a measured column - the standing pedotransfer limitation applies.
    """

    porosity: float
    wilting_point: float
    field_capacity: float
    conductivity_m_s: float
    conductivity_in_hr: float
    sand_pct: float
    clay_pct: float
    clamped: bool


def derive_soil_column(lat: float, lon: float) -> tuple[SoilColumn | None, dict[str, Any]]:
    """Derive a labeled two-zone soil-moisture column at the AOI from SoilGrids.

    Reuses the shared texture read + Saxton-Rawls seam: porosity = theta_s,
    wilting point = theta_1500, field capacity = theta_33, conductivity = the
    Ksat closure (m/s and in/hr). Returns ``(SoilColumn_or_None, meta)``. NEVER
    raises: any fetch / pedotransfer failure returns ``(None, meta_with_reason)``
    so the caller REFUSES (law 9 - no invented default). A NEAR-SURFACE proxy.
    """
    sand_pct, clay_pct, meta = _fetch_texture(lat, lon)
    if sand_pct is None or clay_pct is None:
        return None, meta
    try:
        pk = ksat_from_texture(
            sand_pct / 100.0, clay_pct / 100.0, depth_label=SOIL_TEXTURE_DEPTH,
        )
    except SoilHydraulicsInputError as exc:
        meta["reason"] = f"pedotransfer input invalid: {exc}"
        return None, meta
    inter = pk.intermediates
    column = SoilColumn(
        porosity=round(float(inter["theta_s"]), 4),
        wilting_point=round(float(inter["theta_1500"]), 4),
        field_capacity=round(float(inter["theta_33"]), 4),
        conductivity_m_s=pk.k_m_s,
        conductivity_in_hr=round(pk.k_m_s / MM_PER_HR_TO_M_PER_S / 25.4, 4),
        sand_pct=round(sand_pct, 1),
        clay_pct=round(clay_pct, 1),
        clamped=pk.clamped,
    )
    meta.update(
        {"sand_pct": column.sand_pct, "clay_pct": column.clay_pct,
         "porosity": column.porosity, "wilting_point": column.wilting_point,
         "field_capacity": column.field_capacity,
         "conductivity_in_hr": column.conductivity_in_hr,
         "basis": pk.basis, "depth": SOIL_TEXTURE_DEPTH, "clamped": pk.clamped}
    )
    return column, meta


#: USDA soil-texture classes Landlab's SoilInfiltrationGreenAmpt tabulates
#: (capillary-head + porosity per class). The classifier below returns one of
#: these so the Green-Ampt suction is a texture-DERIVED selection, not a demo pick.
_USDA_TEXTURE_CLASSES: frozenset[str] = frozenset({
    "sand", "loamy sand", "sandy loam", "loam", "silt loam", "silt",
    "sandy clay loam", "clay loam", "silty clay loam", "sandy clay",
    "silty clay", "clay",
})


def usda_texture_class(sand_pct: float, clay_pct: float) -> str:
    """Classify a (sand%, clay%) point into its USDA soil-texture-triangle class.

    The standard USDA textural-triangle boundaries (silt = 100 - sand - clay).
    Returns one of ``_USDA_TEXTURE_CLASSES`` - the label Landlab's Green-Ampt
    table keys its capillary-head + porosity on, so the texture read SELECTS the
    Green-Ampt parameters (a derived choice, not an invented default). Pure.
    """
    s = max(0.0, min(100.0, float(sand_pct)))
    c = max(0.0, min(100.0, float(clay_pct)))
    silt = max(0.0, 100.0 - s - c)
    if c >= 40.0:
        if silt >= 40.0:
            return "silty clay"
        if s <= 45.0:
            return "clay"
        return "sandy clay"
    if c >= 27.0:
        if s <= 20.0:
            return "silty clay loam"
        if s <= 45.0:
            return "clay loam"
        return "sandy clay loam"
    if c >= 20.0:
        if s > 45.0 and silt < 28.0:
            return "sandy clay loam"
        if silt < 50.0:
            return "loam"
        return "silt loam"
    # clay < 20
    if silt >= 80.0 and c < 12.0:
        return "silt"
    if silt >= 50.0:
        if c >= 12.0 or s <= 52.0:
            return "silt loam"
    if c >= 7.0 and silt >= 28.0 and s <= 52.0:
        return "loam"
    # sandy end of the triangle
    if s >= 85.0 and (silt + 1.5 * c) < 15.0:
        return "sand"
    if s >= 70.0 and (silt + 2.0 * c) < 30.0:
        return "loamy sand"
    return "sandy loam"


@dataclass(frozen=True)
class SoilDerivation:
    """The full set of soil scalars a texture read serves + its provenance flags.

    ONE SoilGrids texture read (sand + clay, AOI-window mean) drives the whole
    Saxton-Rawls (2006) fit, so every scalar several Landlab chains need comes from
    the same derivation, not N separate reads:

    - ``k_m_s`` - saturated hydraulic conductivity (the Ksat closure).
    - ``drainable_porosity`` - effective/drainable porosity.
    - ``bulk_density_kg_m3`` - dry bulk density rho_b = (1 - theta_s) *
      particle_density (Saxton-Rawls Eq. 6): the ONE soil-strength scalar texture
      honestly serves (cohesion + internal friction are NOT texture-derivable ->
      literature-range user-gated offers; soil mantle thickness is depth-to-bedrock,
      not a texture output -> refuse).
    - ``texture_class`` - the USDA class (selects the Green-Ampt suction table).

    A NEAR-SURFACE soil proxy, NOT a measured column (the standing pedotransfer
    limitation).
    """

    k_m_s: float
    drainable_porosity: float
    bulk_density_kg_m3: float
    texture_class: str
    sand_pct: float
    clay_pct: float
    clamped: bool


def derive_soil_scalars(lat: float, lon: float) -> tuple[SoilDerivation | None, dict[str, Any]]:
    """Derive the shared soil scalars at the AOI from SoilGrids texture.

    Reuses the single texture read + Saxton-Rawls seam that serves the aquifer K
    and the two-zone column: Ksat, drainable porosity, dry bulk density (from
    theta_s), and the USDA texture class. Returns ``(SoilDerivation_or_None,
    meta)``. NEVER raises: any fetch / pedotransfer failure returns ``(None,
    meta_with_reason)`` so the caller REFUSES (law 9 - no invented default).
    A NEAR-SURFACE proxy.
    """
    sand_pct, clay_pct, meta = _fetch_texture(lat, lon)
    if sand_pct is None or clay_pct is None:
        return None, meta
    try:
        pk = ksat_from_texture(
            sand_pct / 100.0, clay_pct / 100.0, depth_label=SOIL_TEXTURE_DEPTH,
        )
    except SoilHydraulicsInputError as exc:
        meta["reason"] = f"pedotransfer input invalid: {exc}"
        return None, meta
    theta_s = float(pk.intermediates["theta_s"])
    bulk_density = round((1.0 - theta_s) * PARTICLE_DENSITY_KG_M3, 1)
    derivation = SoilDerivation(
        k_m_s=pk.k_m_s,
        drainable_porosity=round(float(pk.porosity), 4),
        bulk_density_kg_m3=bulk_density,
        texture_class=usda_texture_class(sand_pct, clay_pct),
        sand_pct=round(sand_pct, 1),
        clay_pct=round(clay_pct, 1),
        clamped=pk.clamped,
    )
    meta.update(
        {"sand_pct": derivation.sand_pct, "clay_pct": derivation.clay_pct,
         "k_m_s": derivation.k_m_s, "drainable_porosity": derivation.drainable_porosity,
         "bulk_density_kg_m3": derivation.bulk_density_kg_m3,
         "texture_class": derivation.texture_class,
         "basis": pk.basis, "depth": SOIL_TEXTURE_DEPTH, "clamped": pk.clamped}
    )
    return derivation, meta


def soil_derived_entry(
    *,
    param: str,
    units: str | None,
    user_value: float | None,
    derived_value: float | None,
    meta: dict[str, Any],
    need: str,
    derived_note: str,
) -> tuple[float | None, SyntheticInput]:
    """The user -> SoilGrids-derived -> REFUSE ladder for one texture-derived scalar.

    Returns ``(effective_value_or_None, entry)``. ``user_value`` is used verbatim
    (``basis="user"``); else ``derived_value`` from the SoilGrids texture fit
    (``basis="derived"``, source named); else the value stays None and the entry
    carries ``basis="default_demo", consequence="physics"`` so the input-review
    gate REFUSES in auto (law 9 - no invented default). Shared by the Landlab
    groundwater / Green-Ampt / susceptibility rows so one derivation serves all.
    """
    if user_value is not None:
        return float(user_value), SyntheticInput(
            param=param, value=user_value, units=units,
            basis="user", consequence="physics", real_source_if_any=None,
            note=f"caller-supplied {param}.",
        )
    if derived_value is not None:
        clamp = " (clamped to the plausible-media span)" if meta.get("clamped") else ""
        return float(derived_value), SyntheticInput(
            param=param, value=derived_value, units=units,
            basis="derived", consequence="physics",
            real_source_if_any="fetch_soilgrids (Saxton-Rawls 2006 pedotransfer)",
            note=(
                f"{derived_note} DERIVED from SoilGrids texture at the AOI "
                f"(sand={meta.get('sand_pct')}%, clay={meta.get('clay_pct')}%, "
                f"{meta.get('depth')}){clamp}. SCREENING near-surface proxy, NOT a "
                "measured value; supply a site value when one exists."
            ),
        )
    return None, SyntheticInput(
        param=param, value=None, units=units,
        basis="default_demo", consequence="physics", real_source_if_any=None,
        note=(
            f"{need} could not be resolved from SoilGrids at this AOI "
            f"({meta.get('reason', 'unavailable')}). No invented default (law 9): "
            f"supply {param} or run where SoilGrids has coverage."
        ),
    )


def literature_offer_entry(
    *,
    param: str,
    units: str | None,
    user_value: float | None,
    need: str,
    offer: str,
) -> tuple[float | None, SyntheticInput]:
    """The user -> REFUSE-with-literature-offer ladder for an UN-derivable physics
    scalar (no fetchable real-world value: geotechnical strength, calibration
    coefficients).

    Returns ``(effective_value_or_None, entry)``. A caller value is used
    (``basis="user"``); otherwise the value stays None and the entry carries
    ``basis="default_demo", consequence="physics"`` so the gate REFUSES in auto,
    with the literature-range ``offer`` in the note a ``user_gated`` session can
    approve. Law 9: never solve on an invented strength/calibration value.
    """
    if user_value is not None:
        return float(user_value), SyntheticInput(
            param=param, value=user_value, units=units,
            basis="user", consequence="physics", real_source_if_any=None,
            note=f"caller-supplied {param}.",
        )
    return None, SyntheticInput(
        param=param, value=None, units=units,
        basis="default_demo", consequence="physics", real_source_if_any=None,
        note=(
            f"{need} has no fetchable real-world value and is not derivable from "
            f"soil texture. No invented default (law 9): supply {param}, or re-run "
            f"in user_gated mode to approve a literature-range value ({offer})."
        ),
    )


@dataclass
class AquiferResolution:
    """The resolved aquifer K + porosity and their machine-readable provenance.

    ``k_ms`` / ``porosity`` are None only when UNRESOLVED (SoilGrids could not
    serve and the caller supplied nothing) - in that case the corresponding
    ``SyntheticInput`` in ``entries`` carries ``basis="default_demo",
    consequence="physics"`` so the input-review gate refuses in auto mode. When
    the resolution proceeds (user or derived), both values are real.
    """

    k_ms: float | None
    porosity: float | None
    k_source: str  # "user_supplied" | "soil_pedotransfer" | "unresolved"
    porosity_source: str  # "user_supplied" | "soil_pedotransfer" | "unresolved"
    soil_meta: dict[str, Any]
    entries: list[SyntheticInput] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        """True when both K and porosity are real (never an invented default)."""
        return self.k_ms is not None and self.porosity is not None

    @property
    def k_source_token(self) -> str:
        """Legacy ``k_source`` token for the existing caveat narration helpers."""
        return {
            "user_supplied": "user_supplied",
            "soil_pedotransfer": "soil_pedotransfer",
            "unresolved": "unresolved",
        }[self.k_source]


def _screening_caveat(meta: dict[str, Any]) -> str:
    clamp = " (clamped to the plausible-media span)" if meta.get("clamped") else ""
    return (
        f"DERIVED from SoilGrids texture at the AOI (sand={meta.get('sand_pct')}%, "
        f"clay={meta.get('clay_pct')}%, {meta.get('depth')}) via the Saxton-Rawls "
        f"(2006) pedotransfer function{clamp}. SCREENING estimate - a NEAR-SURFACE "
        "soil proxy, NOT a measured aquifer conductivity (true aquifer K can differ "
        "by orders of magnitude). Supply a site aquifer-test value when one exists."
    )


async def resolve_aquifer_properties(
    lat: float,
    lon: float,
    aquifer_k_ms: float | None,
    porosity: float | None,
    *,
    allow_soil_derive: bool = True,
) -> AquiferResolution:
    """Resolve aquifer K + porosity at the AOI: user -> SoilGrids-derived -> REFUSE.

    - Caller-supplied values are used verbatim (``basis="user"``).
    - Any missing value is DERIVED from SoilGrids texture at the AOI via the shared
      Saxton-Rawls pedotransfer seam (``basis="derived"``, source named, screening
      caveat in the note).
    - When SoilGrids cannot serve a still-missing physics value, that value is
      UNRESOLVED: its ``SyntheticInput`` carries ``basis="default_demo",
      consequence="physics"`` (value None) so the input-review gate REFUSES in auto
      mode (law 9 - no invented default). The SoilGrids fetch is offloaded to a
      thread (never blocks the event loop).
    """
    need_k = aquifer_k_ms is None
    need_por = porosity is None

    pk = None
    meta: dict[str, Any] = {}
    if (need_k or need_por) and allow_soil_derive:
        pk, meta = await asyncio.to_thread(derive_soil_k, lat, lon)
    elif need_k or need_por:
        meta = {"reason": "soil-derivation disabled by caller (use_soil_k=False)"}

    entries: list[SyntheticInput] = []

    # --- aquifer_k_ms -----------------------------------------------------------
    if not need_k:
        k_ms = float(aquifer_k_ms)
        k_source = "user_supplied"
        entries.append(SyntheticInput(
            param="aquifer_k_ms", value=round(k_ms, 8), units="m/s",
            basis="user", consequence="physics", real_source_if_any=None,
            note="caller-supplied aquifer hydraulic conductivity.",
        ))
    elif pk is not None:
        k_ms = float(pk.k_m_s)
        k_source = "soil_pedotransfer"
        entries.append(SyntheticInput(
            param="aquifer_k_ms", value=round(k_ms, 8), units="m/s",
            basis="derived", consequence="physics",
            real_source_if_any="fetch_soilgrids (Saxton-Rawls 2006 pedotransfer)",
            note="Aquifer K " + _screening_caveat(meta),
        ))
    else:
        k_ms = None
        k_source = "unresolved"
        entries.append(SyntheticInput(
            param="aquifer_k_ms", value=None, units="m/s",
            basis="default_demo", consequence="physics", real_source_if_any=None,
            note=(
                "aquifer hydraulic conductivity (m/s) is required and could not be "
                f"resolved from SoilGrids at this AOI ({meta.get('reason', 'unavailable')}). "
                "No invented default (law 9): supply aquifer_k_ms or run where "
                "SoilGrids has coverage."
            ),
        ))

    # --- porosity ---------------------------------------------------------------
    if not need_por:
        por = float(porosity)
        por_source = "user_supplied"
        entries.append(SyntheticInput(
            param="porosity", value=round(por, 6), units="dimensionless",
            basis="user", consequence="physics", real_source_if_any=None,
            note="caller-supplied effective porosity.",
        ))
    elif pk is not None:
        por = float(pk.porosity)
        por_source = "soil_pedotransfer"
        entries.append(SyntheticInput(
            param="porosity", value=round(por, 6), units="dimensionless",
            basis="derived", consequence="physics",
            real_source_if_any="fetch_soilgrids (Saxton-Rawls 2006 pedotransfer)",
            note="Effective porosity " + _screening_caveat(meta),
        ))
    else:
        por = None
        por_source = "unresolved"
        entries.append(SyntheticInput(
            param="porosity", value=None, units="dimensionless",
            basis="default_demo", consequence="physics", real_source_if_any=None,
            note=(
                "effective porosity is required and could not be resolved from "
                f"SoilGrids at this AOI ({meta.get('reason', 'unavailable')}). No "
                "invented default (law 9): supply porosity or run where SoilGrids "
                "has coverage."
            ),
        ))

    return AquiferResolution(
        k_ms=k_ms, porosity=por, k_source=k_source, porosity_source=por_source,
        soil_meta=meta, entries=entries,
    )
