"""The TELEMAC style rows, and the open-water result layers - one ``LayerURI``
subclass per postprocessed product.

Each subclass adds the scalars a narration CITES rather than invents, so a number
that reaches a reader came off the field. Every one is a raster anchor: the
animation rides the engine's NATIVE time-stepped mesh beside it."""

from __future__ import annotations

from pydantic import Field

from .execution import LayerURI

__all__ = [
    "TELEMAC_DYE_STYLE",
    "TELEMAC_SEDIMENT_CONCENTRATION_STYLE",
    "TELEMAC_BED_EVOLUTION_STYLE",
    "TELEMAC_WSE_STYLE",
    "TELEMAC_DO_STYLE",
    "TELEMAC_WAVE_STYLE",
    "TELEMAC_AGITATION_STYLE",
    "TELEMAC3D_STRATIFICATION_STYLE",
    "TELEMAC_COASTAL_DEPTH_STYLE",
    "TELEMAC_MAX_DEPTH_STYLE",
    "TelemacWseLayerURI",
    "TelemacWaveLayerURI",
    "TelemacCoastalLayerURI",
]

# The product contract's own style rows.
#
# Each row says which of the four preset shapes draws the product and what the
# quantity contributes to it - its ramp, its units, its legend title. Nothing
# here is a preset NAME, so nothing here can drift from one: the dye and the
# suspended-sediment fields differ because their PARAMETERS differ, which is
# what a reader sees on the canvas.

#: The coastal tidal/surge PEAK-INUNDATION-DEPTH raster. The rising-tide
#: animation rides the result SELAFIN as a ``layer_type="mesh"`` layer.
TELEMAC_COASTAL_DEPTH_STYLE: dict = {
    "kind": "continuous", "ramp": "ylgnbu", "units": "m",
    "label": "Peak inundation depth"}

#: A TELEMAC-3D plane of a strictly positive field - temperature C, salinity
#: psu - on a sequential ramp; the caption names the variable.
TELEMAC3D_STRATIFICATION_STYLE: dict = {"kind": "continuous", "ramp": "viridis"}

#: The ARTEMIS agitation coefficient Kd = Hs/H0 - a dimensionless amplification
#: ratio, not a wave height.
TELEMAC_AGITATION_STYLE: dict = {"kind": "continuous"}

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

#: The GAIA bed-evolution raster, in the metres the module writes: deposition
#: positive, erosion negative, so the ramp diverges about zero and the legend is
#: ranged symmetrically about that centre.
TELEMAC_BED_EVOLUTION_STYLE: dict = {
    "kind": "continuous", "ramp": "rdbu", "units": "m", "label": "Bed evolution",
    "center": 0.0}

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
