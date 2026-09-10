"""The TELEMAC result layers - one ``LayerURI`` subclass per product.

Each adds the scalars a narration CITES rather than invents, so a number that
reaches a reader came off the field. Every one is a raster anchor: the animation
rides the engine's NATIVE time-stepped mesh beside it, which a mesh reader opens
directly, so no per-frame raster set is published.
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import Field

from .execution import LayerURI

__all__ = [
    "TELEMAC_DYE_STYLE",
    "TELEMAC_SEDIMENT_CONCENTRATION_STYLE",
    "TELEMAC_SUBSTANCE_PRODUCTS",
    "SubstanceProduct",
    "TELEMAC_BED_EVOLUTION_STYLE",
    "TELEMAC_WSE_STYLE",
    "TELEMAC_DO_STYLE",
    "TELEMAC_WAVE_STYLE",
    "TELEMAC_AGITATION_STYLE",
    "TELEMAC3D_STRATIFICATION_STYLE",
    "TELEMAC_COASTAL_DEPTH_STYLE",
    "TELEMAC_MAX_DEPTH_STYLE",
    "TELEMAC_RAIN_ON_GRID_MESH_GROUP",
    "TELEMAC3D_SIGNED_STYLE",
    "TelemacDyeLayerURI",
    "TelemacSedimentLayerURI",
    "TelemacWseLayerURI",
    "TelemacDoLayerURI",
    "TelemacWaveLayerURI",
    "ArtemisAgitationLayerURI",
    "Telemac3dLayerURI",
    "TelemacCoastalLayerURI",
    "TelemacRainOnGridLayerURI",
]

# --------------------------------------------------------------------------- #
# The product contract's own style rows.
#
# Each row says which of the four preset shapes draws the product and what the
# quantity contributes to it - its ramp, its units, its legend title. Nothing
# here is a preset NAME, so nothing here can drift from one: the dye and the
# suspended-sediment fields differ because their PARAMETERS differ, which is
# what a reader sees on the canvas.
# --------------------------------------------------------------------------- #

#: The coastal tidal/surge PEAK-INUNDATION-DEPTH raster. The rising-tide
#: animation rides the result SELAFIN as a ``layer_type="mesh"`` layer.
TELEMAC_COASTAL_DEPTH_STYLE: dict = {
    "kind": "continuous", "ramp": "ylgnbu", "units": "m",
    "label": "Peak inundation depth"}

#: The TELEMAC-3D surface (or bottom) field, where the field is strictly
#: positive - temperature C, salinity psu - and reads on a sequential ramp. The
#: COG variable differs by mode, so the caller titles this row with the variable
#: it actually rasterized rather than minting a row per mode.
TELEMAC3D_STRATIFICATION_STYLE: dict = {"kind": "continuous", "ramp": "viridis"}

#: The same TELEMAC-3D field when it is SIGNED - a velocity component, where the
#: sign IS the direction - so the ramp diverges about zero and a reader tells
#: upstream from downstream by colour rather than by magnitude.
TELEMAC3D_SIGNED_STYLE: dict = {"kind": "continuous", "ramp": "rdbu"}

#: The ARTEMIS agitation coefficient Kd = Hs/H0 - a dimensionless amplification
#: ratio, not a wave height.
TELEMAC_AGITATION_STYLE: dict = {
    "kind": "continuous", "units": "Kd", "label": "Agitation coefficient (Kd)"}

#: The TOMAWAC significant wave height Hs.
TELEMAC_WAVE_STYLE: dict = {
    "kind": "continuous", "ramp": "gnbu", "units": "m",
    "label": "Significant wave height"}

#: The dye-concentration raster.
TELEMAC_DYE_STYLE: dict = {
    "kind": "continuous", "ramp": "reds", "units": "mg/L",
    "label": "Dye concentration"}

#: The GAIA SUSPENDED-SEDIMENT concentration raster - a grain load, on its own
#: ramp so it never reads as the dissolved dye field a sediment run publishes
#: beside it.
TELEMAC_SEDIMENT_CONCENTRATION_STYLE: dict = {
    "kind": "continuous", "ramp": "oranges", "units": "mg/L",
    "label": "Suspended sediment concentration"}

#: The GAIA bed-evolution raster: deposition positive, erosion negative, so the
#: ramp diverges about zero.
TELEMAC_BED_EVOLUTION_STYLE: dict = {
    "kind": "continuous", "ramp": "rdbu", "units": "mm", "label": "Bed evolution"}

#: The MAX FREE-SURFACE ELEVATION raster. A water SURFACE is not a depth: it is
#: referenced to a vertical datum and it is signed, so it is titled and ramped
#: apart from the inundation depth a coastal run publishes beside it.
TELEMAC_WSE_STYLE: dict = {
    "kind": "continuous", "ramp": "cividis", "units": "m",
    "label": "Max water-surface elevation"}

#: The MAX WATER DEPTH raster the same postprocess publishes when the run's
#: answer is depth above ground rather than an elevation: no datum, always
#: positive, and on the wet-blue ramp an inundation field is read on.
TELEMAC_MAX_DEPTH_STYLE: dict = {
    "kind": "continuous", "ramp": "ylgnbu", "units": "m",
    "label": "Max water depth"}

#: The DISSOLVED-OXYGEN field from a WAQTEL O2 sag run. rdylbu, NOT reversed:
#: low DO reads red and high DO reads blue, which is the direction a sag curve
#: is read in.
TELEMAC_DO_STYLE: dict = {
    "kind": "continuous", "ramp": "rdylbu", "units": "mg/L",
    "label": "Dissolved oxygen"}


class SubstanceProduct(NamedTuple):
    """What ONE substance class publishes as its transported-field raster.
    A product NAME must not assert more than the field carries, so each class
    names its own file and declares its own quantity, style row and noun.
    """

    #: The basename the raster is uploaded under, per run.
    cog: str
    #: The physical field the raster carries.
    quantity: str
    #: The declared style row that draws it.
    style: dict
    #: What the layer name and the legend call the field.
    noun: str
    #: The mesh variable this class's tracer lands in - the group the results
    #: mesh's preset paints.
    mesh_group: str


#: substance class -> its transported-field product. A class absent here takes
#: the dye row: a tracer whose class declared no chemistry of its own IS dye.
TELEMAC_SUBSTANCE_PRODUCTS: dict[str, SubstanceProduct] = {
    "tracer": SubstanceProduct("telemac_dye_peak.tif", "dye_concentration",
                               TELEMAC_DYE_STYLE, "dye", "DYE"),
    "decay": SubstanceProduct("telemac_dye_peak.tif", "dye_concentration",
                              TELEMAC_DYE_STYLE, "dye", "DYE"),
    "oil": SubstanceProduct("telemac_oil_tracer_peak.tif",
                            "oil_tracer_concentration",
                            TELEMAC_DYE_STYLE, "oil tracer", "DYE"),
    "sediment": SubstanceProduct("telemac_sediment_peak.tif",
                                 "suspended_sediment_concentration",
                                 TELEMAC_SEDIMENT_CONCENTRATION_STYLE,
                                 "suspended sediment", "NCOH SEDIMENT1"),
}

#: The SELAFIN group a rain-on-grid results mesh paints. A mesh preset binds ONE
#: group and the reader binds it BY NAME, so the answer field is named here in
#: the solver's own spelling rather than derived from a quantity token.
TELEMAC_RAIN_ON_GRID_MESH_GROUP: str = "WATER DEPTH"

class TelemacWseLayerURI(LayerURI):
    """The peak (max-over-time) FREE-SURFACE elevation raster.
    An ELEVATION above a datum, not a depth above ground, masked to cells that
    were ever wet so dry terrain is never read as a water surface."""

    #: The crest: peak free-surface elevation anywhere, any time. UNBOUNDED
    #: below, because an elevation is negative for a below-datum bed.
    wse_max_m: float
    wse_peak_time_s: float | None = Field(default=None, ge=0.0)
    #: How many output frames the peak was taken over. A COARSE cadence can
    #: UNDER-estimate a transient crest, so this is published: without it a
    #: low-cadence run reads as a full peak envelope.
    n_frames: int | None = Field(default=None, ge=0)
    #: Also stamped as a raster TAG, so a comparison resolves the model quantity
    #: from the file and pairs it against a matching observation.
    quantity: str = "water_surface_elevation"
    #: The datum the elevations are counted from. Carried EXPLICITLY because
    #: nothing derives it: a pairing tool without it silently assumes a match.
    vertical_datum: str | None = Field(default=None)
    #: The EPSG the raster is written in. For a local-frame validation mesh this
    #: is a PLACEHOLDER the coordinates are stamped with, so the raster and its
    #: observations share one CRS and pairing is an exact identity with no
    #: reprojection; the caveat is recorded in ``fallback_note``.
    mesh_epsg: int | None = Field(default=None, gt=0)


class TelemacDyeLayerURI(LayerURI):
    """The peak dye-concentration raster of a tracer run, plus its scalars.
    The animation plays from the mesh sibling, not from per-frame rasters.
    """

    #: Peak concentration anywhere, any time - the strength of the signal.
    dye_cmax_mgl: float = Field(ge=0.0)
    dye_peak_time_s: float | None = Field(default=None, ge=0.0)
    #: How far the plume centroid travelled downstream from the release point.
    plume_reach_m: float | None = Field(default=None, ge=0.0)
    #: In how many output frames the plume was present in-reach - how long the
    #: tracer lingered before it passed.
    active_frames: int | None = Field(default=None, ge=0)
    #: EQUAL-LENGTH arrays: at each output time, the highest concentration
    #: anywhere in the reach. The two scalars above are this curve's maximum and
    #: the time it occurs at, so the chart and the narration are one measurement.
    dye_curve_time_s: list[float] | None = Field(default=None)
    dye_curve_cmax_mgl: list[float] | None = Field(default=None)
    #: The GRANULARITY the solve actually used, its size estimate, and how the
    #: resolution was chosen. Published so mesh resolution stays a visible,
    #: narratable lever rather than a hidden one.
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_node_estimate: int | None = Field(default=None, ge=0)
    mesh_resolution_label: str | None = Field(default=None)
    # Deposition scalars, populated only for a sediment run, so the returned
    # concentration layer ALSO carries the numbers that run is narrated on.
    # ``None`` for every other substance class.
    deposited_mass_kg: float | None = Field(default=None, ge=0.0)
    deposit_fraction: float | None = Field(default=None, ge=0.0)
    max_deposition_mm: float | None = Field(default=None, ge=0.0)
    # The deepest bed SCOUR magnitude. ``None`` on a supply-limited run, where
    # nothing erodes, and on every non-sediment run.
    max_scour_mm: float | None = Field(default=None, ge=0.0)
    # The spread of SURFACE mean grain size once the bed sorts: it armors in
    # scour zones and fines in deposits. Populated only for a multi-class run -
    # a single class is uniform by construction, so a nonzero range IS the
    # sorting signature, read off the field rather than asserted.
    sediment_n_classes: int | None = Field(default=None, ge=2)
    sediment_surface_d50_min_um: float | None = Field(default=None, ge=0.0)
    sediment_surface_d50_max_um: float | None = Field(default=None, ge=0.0)
    sediment_surface_d50_range_um: float | None = Field(default=None, ge=0.0)


class TelemacDoLayerURI(LayerURI):
    """The steady-state DISSOLVED-OXYGEN raster of a sag run, plus its scalars.
    Below a discharge, decay consumes oxygen and reaeration recovers it; the
    along-reach curve rides here so a chart plots the run's own numbers.
    """

    #: The SAG minimum - how low oxygen bottoms out - and how far downstream of
    #: the discharge that critical point sits.
    do_min_mgl: float = Field(ge=0.0)
    do_min_distance_m: float | None = Field(default=None, ge=0.0)
    #: The pre-sag reference: oxygen carried in at the top of the reach.
    do_upstream_mgl: float | None = Field(default=None, ge=0.0)
    #: The temperature-dependent saturation the deficit is measured against -
    #: the ceiling recovery approaches.
    do_saturation_mgl: float | None = Field(default=None, ge=0.0)
    #: The standard the sag is JUDGED against, and whether it is breached. The
    #: second is the permit answer; the first is a chart reference only.
    do_standard_mgl: float | None = Field(default=None, ge=0.0)
    do_violates_standard: bool | None = Field(default=None)
    #: The MODELED peak ultimate load along the reach, once mixed into the
    #: carrier flow - the driver of the sag.
    bod_mixed_mgl: float | None = Field(default=None, ge=0.0)
    #: The solved mean along-reach velocity: what converts downstream distance
    #: into the travel time the sag develops over.
    mean_velocity_mps: float | None = Field(default=None)
    #: EQUAL-LENGTH arrays of the solved centerline curve, plotted against the
    #: standard line.
    sag_curve_distance_m: list[float] | None = Field(default=None)
    sag_curve_do_mgl: list[float] | None = Field(default=None)
    sag_curve_bod_mgl: list[float] | None = Field(default=None)
    #: The analytical CLOSED FORM over the same bins - the deterministic overlay
    #: drawn beside the solved profile.
    sp_curve_distance_m: list[float] | None = Field(default=None)
    sp_curve_do_mgl: list[float] | None = Field(default=None)
    #: How far the solve sits from that closed form: whole-profile RMS, and the
    #: solved sag minimum minus the analytical one. Negative means the solve
    #: sags deeper.
    sp_rms_mgl: float | None = Field(default=None, ge=0.0)
    sp_sag_deviation_mgl: float | None = Field(default=None)
    #: Why the overlay reads as it does, or why there is none.
    sp_note: str | None = Field(default=None)
    #: The granularity the solve used - the visible resolution lever.
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_node_estimate: int | None = Field(default=None, ge=0)
    mesh_resolution_label: str | None = Field(default=None)
    #: The solver run this layer came out of - i.e. its object-store prefix. The
    #: run's own chart spec + metrics are written there, so a reader can pull the
    #: product's chart instead of rebuilding one from the scalars above.
    run_id: str | None = Field(default=None)


class TelemacSedimentLayerURI(LayerURI):
    """The BED-EVOLUTION raster: deposition positive, erosion negative.
    The SECOND raster a sediment run emits, beside its suspended-concentration
    ribbon, drawn on a ramp that diverges about zero.
    """

    #: NET mass left on the bed over the run, from the solver's own mass balance
    #: and clamped at zero. The SAME net quantity the final-frame map and the
    #: fraction below integrate - NEVER the gross deposition, which cancels
    #: against re-suspension and would contradict an empty map.
    deposited_mass_kg: float | None = Field(default=None, ge=0.0)
    #: The fraction of injected mass that settled: net bed mass over injected
    #: mass. ``None`` when the injected mass is unknown.
    deposit_fraction: float | None = Field(default=None, ge=0.0)
    #: The thickest point of the deposition tongue.
    max_deposition_mm: float | None = Field(default=None, ge=0.0)
    # The DEEPEST scour magnitude - the most-negative node of the same field.
    # Reported beside the deposition maximum so BOTH limbs of the signed field
    # the diverging ramp paints are narratable. ``None`` on a supply-limited
    # run, where nothing erodes.
    max_scour_mm: float | None = Field(default=None, ge=0.0)
    grain_size_um: float | None = Field(default=None, gt=0.0)
    sediment_type: str | None = Field(default=None)


class TelemacWaveLayerURI(LayerURI):
    """The significant-wave-height field of a spectral wave run.
    Phase-AVERAGED: it answers how big the sea gets, not where a single wave
    crest is.
    """

    #: The strongest sea the storm builds, and the mean over the wet domain.
    hs_max_m: float = Field(ge=0.0)
    hs_mean_m: float | None = Field(default=None, ge=0.0)
    #: Sampled near the upwind and downwind shores - the DISCRIMINATING pair:
    #: one storm, opposite shores, and downwind must dominate.
    hs_upwind_m: float | None = Field(default=None, ge=0.0)
    hs_downwind_m: float | None = Field(default=None, ge=0.0)
    peak_period_max_s: float | None = Field(default=None, ge=0.0)
    #: Which question class the field answers.
    wave_mode: str | None = Field(default=None)
    #: The forcing the run was driven with, narrated rather than invented.
    wind_speed_mps: float | None = Field(default=None, ge=0.0)
    #: The granularity the solve used - the visible resolution lever.
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_node_estimate: int | None = Field(default=None, ge=0)
    mesh_resolution_label: str | None = Field(default=None)
    #: The ALONG-FETCH growth curve the run itself MEASURED, paired
    #: index-for-index. A chart plots this rather than resampling the raster, so
    #: the chart and the narrated numbers are the same measurement.
    fetch_curve_km: list[float] | None = Field(default=None)
    fetch_curve_hs_m: list[float] | None = Field(default=None)


class ArtemisAgitationLayerURI(LayerURI):
    """The harbour-agitation field: a dimensionless amplification ratio.
    The phase-RESOLVING complement to the spectral wave layer - it carries
    diffraction, refraction and partial reflection, not a wave height.
    """

    #: The strongest amplification anywhere - a resonant antinode or a focus.
    kd_max: float = Field(ge=0.0)
    hs_max_m: float | None = Field(default=None, ge=0.0)
    #: Mean amplification in the lee of a structure against the exposed
    #: approach - the DISCRIMINATING pair: sheltered must fall well below
    #: exposed, or the structure shelters nothing.
    kd_sheltered: float | None = Field(default=None, ge=0.0)
    kd_exposed: float | None = Field(default=None, ge=0.0)
    #: The harbour's resonant period, and the in-harbour response AT and OFF
    #: it - the resonance pair.
    resonant_period_s: float | None = Field(default=None, ge=0.0)
    response_at_resonance: float | None = Field(default=None, ge=0.0)
    response_off_resonance: float | None = Field(default=None, ge=0.0)
    #: The question class, and the incident period the field was forced with.
    wave_mode: str | None = Field(default=None)
    wave_period_s: float | None = Field(default=None, ge=0.0)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_resolution_label: str | None = Field(default=None)
    #: The curve the run MEASURED across the field, paired index-for-index,
    #: with the kind naming what the axis IS - a transect in metres, or a period
    #: sweep. A chart plots this rather than resampling the raster, so the chart
    #: and the sheltered/exposed pair above are one measurement.
    agitation_curve_m: list[float] | None = Field(default=None)
    agitation_curve_kd: list[float] | None = Field(default=None)
    agitation_curve_kind: str | None = Field(default=None)
    #: The mesh topology's own liquid-boundary sentence: how many liquid
    #: boundaries this domain names and how they are numbered, or that the whole
    #: boundary is solid wall.
    boundary_states: str | None = Field(default=None)


class Telemac3dLayerURI(LayerURI):
    """A surface- or bottom-layer field from a 3D baroclinic run.
    The vertical structure a 2D depth-average cannot resolve, so what makes the
    layer worth having is the scalars below, not the map.
    """

    #: The headline DISCRIMINATING magnitude, whichever quantity the mode makes
    #: it: a top-to-bottom difference, a surface-minus-bottom velocity, a front
    #: speed. Nonzero IS the 3D structure a 2D model misses.
    stratification_metric: float = Field(ge=0.0)
    #: The VERTICAL profile the run measured - sigma (0 at the bed, 1 at the
    #: surface) against the field value, final and initial, paired
    #: index-for-index. This is the 3D answer in the only form that shows it:
    #: what the column looked like at the start and what survived. A surface map
    #: carries no depth at all, so a chart plots these rather than resampling it.
    profile_sigma: list[float] | None = Field(default=None)
    profile_values: list[float] | None = Field(default=None)
    profile_values_initial: list[float] | None = Field(default=None)
    #: The question class, and what the rasterized variable IS.
    flow_mode: str | None = Field(default=None)
    variable_label: str | None = Field(default=None)
    variable_units: str | None = Field(default=None)
    #: The persisting top-to-bottom temperature difference: thermocline strength.
    stratification_dt: float | None = Field(default=None)
    #: The vertical velocity structure - surface downwind, bottom upwind, and a
    #: depth-average near zero, which is the whole two-layer gyre a 2D model
    #: returns as nothing everywhere.
    u_surface: float | None = Field(default=None)
    u_bottom: float | None = Field(default=None)
    depth_avg_u: float | None = Field(default=None)
    #: Measured against analytic gravity-current front speed.
    front_speed_mps: float | None = Field(default=None, ge=0.0)
    benjamin_speed_mps: float | None = Field(default=None, ge=0.0)
    surface_value_mean: float | None = Field(default=None)
    bottom_value_mean: float | None = Field(default=None)
    #: The number of sigma planes - the 3D degree of freedom - and whether the
    #: non-hydrostatic solver ran.
    nplan: int | None = Field(default=None, ge=0)
    non_hydrostatic: bool | None = Field(default=None)
    wind_speed_mps: float | None = Field(default=None, ge=0.0)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_resolution_label: str | None = Field(default=None)
    #: The depth-weighted column mean's own fractional move over the run: the
    #: numerical ERROR BAR on the mixing, since a run that exchanges no heat
    #: should conserve that mean exactly.
    column_heat_drift_frac: float | None = Field(default=None)
    #: The column's difference and depth-weighted mean at the start and at the
    #: end - what the two numbers above are measured against - and the depth the
    #: profile was taken from.
    stratification_dt_init: float | None = Field(default=None)
    column_heat_mean_init_c: float | None = Field(default=None)
    column_heat_mean_final_c: float | None = Field(default=None)
    column_depth_m: float | None = Field(default=None)


class TelemacCoastalLayerURI(LayerURI):
    """A coastal tidal or surge DEPTH field, carried by TWO separate layers:
    peak depth over land DRY at t0, and total peak depth. One raster carrying
    both would paint a submerged bay floor as inundation that never happened."""

    # Both layers carry the same scalars, because the scalars describe the RUN.
    #: Peak depth anywhere over the run - the whole water column, permanent
    #: water included.
    peak_depth_m: float = Field(ge=0.0)
    #: Newly inundated LAND: cells dry at t0 but wet at peak stage. THE
    #: discriminant - a surge floods far more land than a calm tide over the
    #: same domain.
    flooded_land_km2: float = Field(ge=0.0)
    #: The deepest INUNDATION, over initially dry land only, which is what the
    #: primary raster paints. Lower than the peak depth wherever permanent water
    #: is deeper than the flooding.
    inundation_peak_depth_m: float | None = Field(default=None, ge=0.0)
    #: HOW dry-at-t0 was decided. A reader is entitled to check which land was
    #: excluded, so the basis travels rather than being assumed.
    inundation_basis: str | None = Field(default=None)
    wet_area_km2: float | None = Field(default=None, ge=0.0)
    #: The crest stage reached over wet nodes.
    peak_wl_m: float | None = Field(default=None)
    #: The peak boundary forcing the run was DRIVEN with - narrated, never
    #: invented.
    sl_peak_m: float | None = Field(default=None)
    #: Which series drove the boundary: an observed surge or an astronomical
    #: prediction. This is the A/B question class.
    series_type: str | None = Field(default=None)
    #: The series' own vertical datum and the LABELED offset applied to
    #: reconcile it with the terrain datum. Always surfaced, never inferred.
    series_datum: str | None = Field(default=None)
    datum_offset_m: float | None = Field(default=None)
    station_id: str | None = Field(default=None)
    station_name: str | None = Field(default=None)
    #: Which edge of the domain carried the seaward boundary.
    ocean_edge: str | None = Field(default=None)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_resolution_label: str | None = Field(default=None)


class TelemacRainOnGridLayerURI(TelemacWseLayerURI):
    """The peak flood-depth raster of a RAIN-ON-GRID run, plus its hydrograph.
    Two questions at once: WHERE the water stood, which the raster paints, and
    HOW MUCH left the basin, which the outlet series below carries."""

    #: The delineated basin area upstream of the outlet. Every volume below is
    #: only readable against it.
    catchment_area_km2: float | None = Field(default=None, ge=0.0)
    #: The hydrograph crest and when it arrived.
    peak_discharge_m3s: float | None = Field(default=None)
    peak_discharge_time_s: float | None = Field(default=None, ge=0.0)
    #: True when that crest is the LAST sample - the outflow was still rising
    #: when the window closed. The peak, the runoff volume and the coefficient
    #: are then FLOORS on the storm's answer, not measurements of it.
    peak_is_window_truncated: bool | None = Field(default=None)
    #: What fell on the catchment and what left through the outlet, over the
    #: simulated window, and their ratio. The ratio is ``None`` when no rain fell.
    rainfall_volume_m3: float | None = Field(default=None, ge=0.0)
    runoff_volume_m3: float | None = Field(default=None)
    runoff_coefficient: float | None = Field(default=None)
    #: The deepest and fastest the overland sheet got anywhere, at any time.
    max_depth_peak_m: float | None = Field(default=None, ge=0.0)
    #: The 99th-percentile peak depth, published BESIDE the maximum: a single
    #: terrain pit ponding to its rim sets the maximum while the sheet the storm
    #: produced is orders of magnitude shallower. One is the extreme, the other
    #: is the field, and a reader needs both.
    max_depth_p99_m: float | None = Field(default=None, ge=0.0)
    max_velocity_peak_ms: float | None = Field(default=None, ge=0.0)
    #: The solver's own mass-balance residual. A run whose volumes do not close
    #: is not a run whose hydrograph means anything, so it is PUBLISHED rather
    #: than checked in private.
    continuity_rel_error: float | None = Field(default=None)
    #: Which infiltration path ran, under which antecedent-moisture condition,
    #: and the constant design rate when one drove the run.
    runoff_path: str | None = Field(default=None)
    amc_condition: int | None = Field(default=None, ge=1, le=3)
    rain_intensity_mm_per_hr: float | None = Field(default=None, ge=0.0)
    #: The outlet discharge series the chart is built from.
    outlet_hydrograph_t_s: list[float] | None = Field(default=None)
    outlet_hydrograph_q_m3s: list[float] | None = Field(default=None)
    #: What the catchment was discretized as, so resolution stays a visible,
    #: narratable lever.
    mesh_node_count: int | None = Field(default=None, ge=0)
    mesh_element_count: int | None = Field(default=None, ge=0)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_resolution_label: str | None = Field(default=None)
    #: Whether the mesh was GENERATED for this run or SUPPLIED on invocation.
    catchment_provenance: str | None = Field(default=None)
    #: What the modelled basin is CALLED, so a chart titles itself with the
    #: catchment rather than with whichever raster is the map anchor.
    catchment_name: str | None = Field(default=None)
    #: The extent actually MODELLED - the mesh's own node bounds, not the
    #: analysis window it was delineated inside. A basin is a fraction of its
    #: search buffer, so the two are different answers to "where is this".
    domain_bbox: list[float] | None = Field(default=None)
