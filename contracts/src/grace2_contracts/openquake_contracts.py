"""OpenQuake Engine probabilistic-seismic-hazard (PSHA) contracts (sprint-17).

The OpenQuake analogue of ``modflow_contracts.py`` / ``swmm_contracts.py``. Two
shapes back the seismic-hazard demo path (an AOI -> a classical PSHA over a site
grid -> a hazard map COG that pairs DIRECTLY with the existing Pelicun impact
path: the OpenQuake hazard becomes Pelicun's ground-motion intensity input).

- ``OpenQuakeRunArgs`` — the hazard-calculation parameters the agent confirms
  with the user before submitting an OpenQuake run. Consumed by the engine
  composer / worker (``services/workers/openquake/...``) that maps these onto a
  ``job.ini`` + a source-model / GMPE logic tree for a CLASSICAL PSHA over the
  AOI site grid.
- ``SeismicHazardLayerURI`` — the postprocess output layer. Extends ``LayerURI``
  field-for-field (so it still maps onto ``map-command load-layer`` with no
  translation, like every other layer) and adds the two hazard scalars the agent
  narrates: the peak ground-motion value and the hazard footprint.

Design notes
------------
- ``bbox`` is the project ``BBox`` convention: ``(min_lon, min_lat, max_lon,
  max_lat)`` in EPSG:4326 (lon-first), range-validated by the shared ``BBox``
  type. A PSHA site grid covers an *area*, not a point (contrast with MODFLOW's
  ``spill_location_latlon`` point), so it is a bbox.
- ``imt`` (Intensity Measure Type) is the seismic-hazard analogue of a units
  selector. Default ``"PGA"`` (Peak Ground Acceleration, fraction of g) is the
  canonical input to Pelicun's fragility curves. ``"SA(0.3)"`` /
  ``"SA(1.0)"`` (Spectral Acceleration at a period) are the other common forms.
  Open ``str`` (OpenQuake accepts a wide IMT vocabulary), validated to a small
  PGA/PGV/SA(...) family so a typo fails honestly rather than at solve time.
- ``poe`` (Probability of Exceedance) + ``investigation_time_years`` pick the
  return period of the hazard map. The canonical engineering map is "10% PoE in
  50 years" (a 475-year return period — the design basis for most building
  codes), so the defaults are ``poe=0.10`` + ``investigation_time_years=50``.
- ``OpenQuakeRunArgs`` carries the hazard-curve mesh spacing + the maximum
  source-to-site distance, both adaptive-budget levers (the OpenQuake engine is
  RAM-hungry, ~2 GB/thread; a fine ``site_grid_spacing_km`` over a wide AOI
  blows the cell budget, so the composer/worker may COARSEN it — this is the
  requested spacing, not a guarantee, mirroring the SFINCS/SWMM adaptive grid).
- ``SeismicHazardLayerURI`` is a structured numeric carrier (invariant 1 /
  Decision N / FR-AS-7): the agent narrates ``max_hazard_value`` and
  ``hazard_area_km2`` from these typed fields rather than inventing them.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator

from .common import BBox, EngineRunArgsMixin, GraceModel
from .execution import LayerURI

__all__ = [
    "DEFAULT_IMT",
    "DEFAULT_POE",
    "DEFAULT_INVESTIGATION_TIME_YEARS",
    "DEFAULT_SITE_GRID_SPACING_KM",
    "DEFAULT_MAX_DISTANCE_KM",
    "DEFAULT_GMPE",
    "OpenQuakeRunArgs",
    "SeismicHazardLayerURI",
]


# TENTATIVE seismic-demo defaults (sprint-17; narrated as demo values, not
# site-calibrated seismic-source parameters, by the composer).
#: Canonical engineering IMT — fraction of g; the Pelicun fragility input.
DEFAULT_IMT: str = "PGA"
#: Probability of exceedance for the hazard map (10% PoE in 50 yr == 475-yr RP).
DEFAULT_POE: float = 0.10
#: Investigation time, years (the PoE window).
DEFAULT_INVESTIGATION_TIME_YEARS: float = 50.0
#: Requested PSHA site-grid spacing, km (subject to the adaptive cell budget).
DEFAULT_SITE_GRID_SPACING_KM: float = 5.0
#: Maximum source-to-site distance, km (integration distance for the ruptures).
DEFAULT_MAX_DISTANCE_KM: float = 300.0
#: A single demo GMPE (ground-motion prediction equation). A 1-branch trivial
#: logic tree is the v0.1 default; OpenQuake names this class verbatim in the
#: GMPE logic-tree XML. BooreAtkinson2008 is a widely-used active-crust GMPE.
DEFAULT_GMPE: str = "BooreAtkinson2008"


#: Accepted Intensity Measure Types. PGA / PGV plus spectral-acceleration at a
#: period ``SA(<float>)``. Validated structurally (not an exhaustive Literal —
#: OpenQuake's IMT vocabulary is large) so a typo fails on the FIRST attempt.
_IMT_RE = re.compile(r"^(PGA|PGV|SA\(\d+(\.\d+)?\))$")


class OpenQuakeRunArgs(EngineRunArgsMixin):
    """Parameters for a classical probabilistic-seismic-hazard (PSHA) run.

    Adopts ``EngineRunArgsMixin`` (levers STEP 3): ``advanced_physics`` keys are
    validated against ``physics_registry.PHYSICS_REGISTRY["openquake"]``
    (truncation_level / rupture_mesh_spacing_km / width_of_mfd_bin /
    area_source_discretization_km) and threaded into the ``job.ini`` deck;
    ``None`` => byte-identical classical-PSHA deck. ``temporal_mode`` /
    ``output_frames`` are inert for OpenQuake (no animation).

    Returned/assembled by the seismic composer after agent-confirmed parameter
    extraction; consumed by the OpenQuake worker (``services/workers/openquake``)
    that templates a ``job.ini`` + source-model / GMPE logic tree and runs
    ``oq engine --run job.ini`` headless. The agent confirms these with the user
    before submission (confirmation-before-consequence, invariant 9).

    Use this when:
        Building the input to a probabilistic seismic-hazard calculation over an
        AOI (a hazard map / hazard curves at a return period, e.g. the ground
        motion with a 10% chance of exceedance in 50 years). The resulting
        ground-motion hazard is the canonical input to the Pelicun building
        damage/impact path.

    Do NOT use this for:
        Surface-water flooding (SFINCS ``ModelSetup`` / SWMM ``SWMMRunArgs``),
        groundwater contamination (``MODFLOWRunArgs``), nor for carrying solver
        output (that is ``SeismicHazardLayerURI``).

    Fields:
        schema_version: contract version pin (additive growth only).
        bbox: AOI as ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. The
            engine lays a regular site grid over it (``site_grid_spacing_km``)
            and computes the hazard at every site.
        imt: Intensity Measure Type. ``"PGA"`` (DEFAULT, fraction of g — the
            Pelicun fragility input), ``"PGV"``, or ``"SA(<period>)"`` such as
            ``"SA(0.3)"`` / ``"SA(1.0)"``.
        poe: Probability of Exceedance for the hazard map, in (0, 1). Default
            0.10. With ``investigation_time_years=50`` this is the standard "10%
            in 50 years" (475-year return period) engineering hazard map.
        investigation_time_years: the PoE window, years (> 0). Default 50.
        site_grid_spacing_km: requested PSHA site-grid spacing, km (> 0). Default
            5. Subject to the adaptive cell budget for a large AOI (OpenQuake is
            RAM-hungry ~2 GB/thread, so a fine spacing over a wide AOI is
            COARSENED — this is the requested spacing, not a guarantee).
        max_distance_km: maximum source-to-site integration distance, km (> 0).
            Default 300. Ruptures farther than this are ignored.
        gmpe: the ground-motion prediction equation class name OpenQuake uses in
            the GMPE logic tree (a single-branch trivial logic tree for v0.1).
            Default ``"BooreAtkinson2008"``. Open ``str`` (OpenQuake names many
            GMPE classes); non-empty validated.
        a_value / b_value: Gutenberg-Richter recurrence parameters for the demo
            area source (seismicity rate + magnitude-frequency slope). TENTATIVE
            demo values; narrate as demo defaults, not a site-specific seismic
            source model.
        min_magnitude / max_magnitude: the magnitude range of the demo source.
    """

    schema_version: Literal["v1"] = "v1"

    bbox: BBox

    imt: str = DEFAULT_IMT

    poe: float = Field(default=DEFAULT_POE, gt=0.0, lt=1.0)
    investigation_time_years: float = Field(
        default=DEFAULT_INVESTIGATION_TIME_YEARS, gt=0.0
    )

    site_grid_spacing_km: float = Field(default=DEFAULT_SITE_GRID_SPACING_KM, gt=0.0)
    max_distance_km: float = Field(default=DEFAULT_MAX_DISTANCE_KM, gt=0.0)

    gmpe: str = Field(default=DEFAULT_GMPE, min_length=1)

    # Gutenberg-Richter recurrence + magnitude range of the demo area source.
    # TENTATIVE demo seismicity (narrated as demo values by the composer).
    a_value: float = Field(default=4.0)
    b_value: float = Field(default=1.0, gt=0.0)
    min_magnitude: float = Field(default=5.0, ge=0.0)
    max_magnitude: float = Field(default=7.5, gt=0.0)

    @field_validator("imt", mode="before")
    @classmethod
    def _normalize_imt(cls, value: Any) -> Any:
        """Uppercase + strip so ``pga`` / ``sa(1.0)`` normalize on the FIRST
        attempt (no self-correcting retry loop). Spectral-acceleration keeps its
        parenthesized period. An unknown string passes through so the structural
        validator below still raises an honest error."""
        if not isinstance(value, str):
            return value
        return value.strip().upper()

    @field_validator("imt")
    @classmethod
    def _validate_imt(cls, value: str) -> str:
        """Structurally validate the IMT against the PGA/PGV/SA(...) family."""
        if not _IMT_RE.match(value):
            raise ValueError(
                f"imt must be one of PGA, PGV, or SA(<period>) "
                f"(e.g. 'SA(0.3)'), got {value!r}"
            )
        return value

    @field_validator("max_magnitude")
    @classmethod
    def _validate_magnitude_range(cls, value: float, info: Any) -> float:
        """``max_magnitude`` must exceed ``min_magnitude`` (a non-empty G-R bin)."""
        min_mag = info.data.get("min_magnitude")
        if min_mag is not None and value <= float(min_mag):
            raise ValueError(
                f"max_magnitude ({value}) must be greater than min_magnitude "
                f"({min_mag})"
            )
        return value


class SeismicHazardLayerURI(LayerURI):
    """A ``LayerURI`` for an OpenQuake seismic-hazard-map layer, plus narration
    scalars.

    Extends ``LayerURI`` field-for-field so it still maps onto
    ``map-command load-layer`` with no translation (same as every other layer).
    Adds the structured numbers the agent narrates about the hazard so the LLM
    cites typed fields, never invents them (invariant 1, FR-AS-7):

        imt: the Intensity Measure Type the map represents (e.g. ``"PGA"``).
        poe: the probability of exceedance the map is computed at (e.g. 0.10).
        investigation_time_years: the PoE window, years (e.g. 50).
        return_period_years: the implied return period, years
            (-investigation_time / ln(1 - poe)); a convenience scalar the agent
            narrates ("the 475-year hazard map").
        max_hazard_value: peak ground-motion value across the AOI, in the IMT's
            units (g for PGA/SA, cm/s for PGV) (>= 0).
        hazard_area_km2: areal footprint above the hazard floor, km^2 (>= 0).
        n_sites: number of PSHA site-grid points the hazard was computed at
            (>= 0).
        source_model_kind: which seismic-source model produced this hazard map --
            ``"real-fault"`` (GEM Global Active Faults ``simpleFaultSource``
            traces, so the hazard PEAKS ON the actual faults) or
            ``"synthetic-area"`` (the synthetic uniform-rate AOI area source, the
            honest fallback when no mapped active fault intersects the AOI).
            HONESTY FLOOR: this is set to the path the run ACTUALLY took -- it
            NEVER reads ``"real-fault"`` when the run fell back to the synthetic
            source, so the agent can never claim real faults it did not use.
        source_model_note: a human-readable one-line narration of the source-model
            decision (e.g. "Hazard built from 6 real GEM active-fault sources ..."
            or "No mapped active fault intersects this AOI; used the synthetic
            area source.") that the agent surfaces verbatim.

    ``layer_type`` for a hazard map is ``"raster"`` (a hazard-value COG); the
    base contract's vocabulary is inherited unchanged.
    """

    imt: str = DEFAULT_IMT
    poe: float = Field(default=DEFAULT_POE, gt=0.0, lt=1.0)
    investigation_time_years: float = Field(
        default=DEFAULT_INVESTIGATION_TIME_YEARS, gt=0.0
    )
    return_period_years: float = Field(ge=0.0)

    max_hazard_value: float = Field(ge=0.0)
    hazard_area_km2: float = Field(ge=0.0)
    n_sites: int = Field(default=0, ge=0)

    # task #199 real-fault wiring: which source model drove the hazard + a
    # narration line. Default "synthetic-area" so EVERY existing construction
    # (and the synthetic fallback path) stays valid without change; the composer
    # flips it to "real-fault" only when it actually built fault sources.
    source_model_kind: Literal["real-fault", "synthetic-area"] = "synthetic-area"
    source_model_note: str = ""
