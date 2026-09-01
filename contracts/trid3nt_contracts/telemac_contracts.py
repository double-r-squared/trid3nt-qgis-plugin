"""TELEMAC-2D river-dye surface-tracer engine contracts (river-dye North Star).

The TELEMAC analogue of ``geoclaw_contracts.py``. TELEMAC-2D solves the 2D
shallow-water equations with an advected TRACER over a real river reach; the
river-dye archetype releases a FINITE dye pulse at a mid-reach point source and
watches the plume travel downstream and dilute. The deliverable differs from the
flood engines in ONE deliberate way: the primary artifact is the engine's NATIVE
time-stepped mesh (a SELAFIN ``.slf`` MDAL reads directly, animating the dye
dataset group with zero new render infra), so the postprocess emits ONE
peak-concentration COG as the map anchor + narration carrier and lets the mesh
sibling carry the animation (see ``export_case_to_qgis`` + ``postprocess_telemac``).

``TelemacDyeLayerURI`` extends ``LayerURI`` field-for-field (so it still maps onto
``map-command load-layer`` with no translation) and adds the dye narration
scalars the agent cites rather than invents (invariant 1).
"""

from __future__ import annotations

from typing import NamedTuple

from pydantic import Field

from .execution import LayerURI

__all__ = [
    "TELEMAC_DYE_STYLE_PRESET",
    "TELEMAC_SEDIMENT_CONCENTRATION_STYLE_PRESET",
    "TELEMAC_SUBSTANCE_PRODUCTS",
    "SubstanceProduct",
    "TELEMAC_BED_EVOLUTION_STYLE_PRESET",
    "TELEMAC_WSE_STYLE_PRESET",
    "TELEMAC_DO_STYLE_PRESET",
    "TELEMAC_WAVE_STYLE_PRESET",
    "TELEMAC_AGITATION_STYLE_PRESET",
    "TELEMAC3D_STRATIFICATION_STYLE_PRESET",
    "TELEMAC_COASTAL_DEPTH_STYLE_PRESET",
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

#: Style preset for the coastal tidal/surge PEAK-INUNDATION-DEPTH raster (ADR
#: 0259). A DISTINCT continuous key for the peak-depth raster (never the
#: dye/WSE/wave/agitation/3D presets). The rising-tide animation rides the result
#: SELAFIN, published as a ``layer_type="mesh"`` layer by the emit-on-solve seam
#: (ADR 0283). The layer carries a data-driven ``legend`` so the real depth renders
#: (additive / legend-drives-render, same as the other TELEMAC layers).
TELEMAC_COASTAL_DEPTH_STYLE_PRESET: str = "continuous_coastal_inundation_depth"

#: Style preset for the TELEMAC-3D stratified / 3D-hydrodynamics surface (or
#: bottom) field raster. A DISTINCT continuous key (never the dye/WSE/DO/wave/
#: agitation presets) so the peak raster styles cleanly. The COG variable differs
#: by mode
#: (temperature C / velocity m/s / salinity psu), so the layer ALWAYS carries a
#: data-driven ``legend`` and the preset is purely the mesh-sibling routing key
#: (additive / legend-drives-render, same as the other TELEMAC layers).
TELEMAC3D_STRATIFICATION_STYLE_PRESET: str = "continuous_stratified_flow"

#: Style preset for the ARTEMIS agitation-coefficient (Kd = Hs/H0) raster (ADR
#: 0237). A DISTINCT continuous key so it never collides with the TOMAWAC Hs
#: preset for the mesh-sibling animation map; the layer carries a data-driven
#: ``legend`` so the 0..~2.5 Kd range renders regardless of QML preset coverage.
TELEMAC_AGITATION_STYLE_PRESET: str = "continuous_wave_agitation"

#: Style preset for the TOMAWAC significant-wave-height (Hs) raster. A
#: DISTINCT continuous key (never the dye/WSE/DO presets) so the peak raster styles
#: cleanly. The layer always carries a data-driven
#: ``legend`` so it renders regardless of QML preset-library coverage.
TELEMAC_WAVE_STYLE_PRESET: str = "continuous_significant_wave_height"

#: Style preset for the dye-concentration raster. A DISTINCT key (not the flood
#: ``continuous_flood_depth`` nor the MODFLOW ``continuous_plume_concentration``)
#: so the peak raster styles cleanly; the dye animation rides the result SELAFIN,
#: published as a ``layer_type="mesh"`` layer by the emit-on-solve seam (ADR 0283).
#: The layer always carries a data-driven ``legend`` so it renders regardless of
#: the QML
#: preset library's coverage of this key (additive, legend-drives-render design).
TELEMAC_DYE_STYLE_PRESET: str = "continuous_dye_concentration"

#: Style preset for the GAIA SUSPENDED-SEDIMENT concentration raster - a grain
#: load in mg/L, on its own ramp so it never reads as the dissolved dye field a
#: sediment run also carries a bed-evolution map beside.
TELEMAC_SEDIMENT_CONCENTRATION_STYLE_PRESET: str = "continuous_suspended_sediment"


class SubstanceProduct(NamedTuple):
    """What ONE substance class publishes as its transported-field raster.

    The product's NAME must not assert more than the field carries: an oil run
    advects the same passive tracer a dye run does (the slick physics rides the
    drogues track, not this raster) and a sediment run's tracer is a suspended
    grain load, so each class names its own COG, declares its own QUANTITY for
    the styling seam, and carries its own preset and noun.

        cog: the basename the COG is uploaded under, per run.
        quantity: the quantity row the style contract resolves this raster by.
        style_preset: the preset that quantity carries.
        noun: what the layer name and the legend call the field.
    """

    cog: str
    quantity: str
    style_preset: str
    noun: str


#: substance class -> its transported-field product. A class absent here takes
#: the dye row: a tracer whose class declared no chemistry of its own IS dye.
TELEMAC_SUBSTANCE_PRODUCTS: dict[str, SubstanceProduct] = {
    "tracer": SubstanceProduct("telemac_dye_peak.tif", "dye_concentration",
                               TELEMAC_DYE_STYLE_PRESET, "dye"),
    "decay": SubstanceProduct("telemac_dye_peak.tif", "dye_concentration",
                              TELEMAC_DYE_STYLE_PRESET, "dye"),
    "oil": SubstanceProduct("telemac_oil_tracer_peak.tif",
                            "oil_tracer_concentration",
                            TELEMAC_DYE_STYLE_PRESET, "oil tracer"),
    "sediment": SubstanceProduct("telemac_sediment_peak.tif",
                                 "suspended_sediment_concentration",
                                 TELEMAC_SEDIMENT_CONCENTRATION_STYLE_PRESET,
                                 "suspended sediment"),
}

#: Style preset for the GAIA sediment BED-EVOLUTION (deposition) raster. A DISTINCT
#: diverging key (mirrors the ``diverging_river_seepage`` pattern) so
#: ``publish_layer._resolve_titiler_style_params`` renders it on a diverging rdbu
#: ramp centered on 0 (deposition positive / erosion negative), never colliding
#: with the dye preset. The
#: layer carries a data-driven ``legend`` so the mm-scale range renders (a fixed
#: registry range would wash out mm deposition), additive/legend-drives-render.
TELEMAC_BED_EVOLUTION_STYLE_PRESET: str = "diverging_bed_evolution"

#: Style preset for the MAX FREE-SURFACE ELEVATION (WSE) raster -- the validation
#: deliverable for the dam-break / river archetype (Malpasset). A DISTINCT
#: continuous key so it renders on a sequential ramp (WSE decreases down-valley)
#: and never collides with the dye/sediment presets. The layer carries a
#: data-driven ``legend`` so the real WSE range renders (additive /
#: legend-drives-render, same as the other TELEMAC layers).
TELEMAC_WSE_STYLE_PRESET: str = "continuous_water_surface_elevation"

#: Style preset for the DISSOLVED-OXYGEN (DO) field raster from a WAQTEL O2 sag
#: run. A DISTINCT continuous key (never the dye/WSE/sediment presets) so
#: ``publish_layer._resolve_titiler_style_params`` renders it on a REVERSED
#: sequential ramp (low DO = the hazard = the hot end), never colliding with
#: another engine's preset. The layer carries
#: a data-driven ``legend`` so the mg/L range renders (additive /
#: legend-drives-render, same as the other TELEMAC layers).
TELEMAC_DO_STYLE_PRESET: str = "continuous_dissolved_oxygen"


class TelemacWseLayerURI(LayerURI):
    """A ``LayerURI`` for a TELEMAC-2D peak (max-over-time) FREE-SURFACE raster.

    The validation-case analogue of ``TelemacDyeLayerURI``: for a dam-break /
    river solve, the deliverable that pairs against surveyed high-water marks is
    the MAX water-surface ELEVATION reached at each wet node over the run (the
    ``FREE SURFACE`` variable, masked to cells that were ever wet so dry terrain
    is never mistaken for a water surface). Extends ``LayerURI`` field-for-field
    (so it still maps onto ``map-command load-layer``) and adds the WSE scalars +
    the honesty metadata a like-for-like WSE-vs-HWM pairing needs (Invariant 1):

        wse_max_m: peak free-surface elevation anywhere/anytime over the run
            (metres in the mesh's vertical datum, ``ge`` unconstrained since an
            elevation may be negative for a below-datum bed) -- the headline
            crest.
        wse_peak_time_s: OPTIONAL simulated time (s from t0) of that peak
            (``>= 0``). ``None`` when unavailable.
        n_frames: OPTIONAL number of output frames the peak was taken over --
            surfaced because a coarse-cadence result (e.g. the 2-frame TELEMAC
            reference solution) can UNDER-estimate the transient crest; the agent
            cites this so a low-cadence run is never read as a full peak envelope.
        quantity: the physical quantity the raster carries -- always
            ``"water_surface_elevation"`` (an ELEVATION above the vertical datum,
            NOT a depth above ground). Also stamped as a raster TAG so
            ``extract_model_at_observations`` resolves the model quantity from the
            tag and pairs it like-for-like against a WSE observation.
        vertical_datum: OPTIONAL vertical datum label for ``wse``/the raster (e.g.
            ``"NGF"`` for Malpasset, ``"EGM2008"`` for a Copernicus-bed river
            solve). Carried explicitly because ``LayerURI`` has no datum field and
            NOTHING auto-derives it; a WSE-vs-HWM pairing needs the caller to know
            it (else the pairing tool silently assumes a matching datum).
        mesh_epsg: OPTIONAL EPSG the raster is written in (the mesh CRS). For a
            bundled local-frame validation mesh this is a PLACEHOLDER projected
            EPSG the coordinates are stamped with so both this raster and the
            observation layer share one identical CRS (pairing is then an exact
            identity, no reprojection distortion); ``fallback_note`` records the
            local-frame caveat.

    ``layer_type`` is ``"raster"`` (the peak-WSE COG). The raster uses the
    ``continuous_water_surface_elevation`` style preset + a data-driven ``legend``.
    """

    wse_max_m: float
    wse_peak_time_s: float | None = Field(default=None, ge=0.0)
    n_frames: int | None = Field(default=None, ge=0)
    quantity: str = "water_surface_elevation"
    vertical_datum: str | None = Field(default=None)
    mesh_epsg: int | None = Field(default=None, gt=0)


class TelemacDyeLayerURI(LayerURI):
    """A ``LayerURI`` for a TELEMAC-2D peak dye-concentration layer + scalars.

    Extends ``LayerURI`` field-for-field (same as every other layer). Adds the
    structured numbers the agent narrates about the tracer plume so the LLM cites
    typed fields, never invents them (invariant 1):

        dye_cmax_mgl: peak dye concentration anywhere/anytime in the reach, mg/L
            (>= 0) -- the strength of the spill signal.
        dye_peak_time_s: OPTIONAL simulated time (s from t0) at which that peak
            concentration occurred (>= 0). ``None`` when unavailable.
        plume_reach_m: OPTIONAL along-reach distance (m, >= 0) the plume centroid
            travelled from the release point to its farthest downstream position
            -- how far the dye moved. ``None`` when unavailable.
        active_frames: OPTIONAL number of output frames in which the plume was
            present in-reach (>= 0) -- how long the dye lingered before it passed.
            ``None`` when unavailable.
        mesh_size_m: OPTIONAL target gmsh edge length (m, > 0) the mesh was built
            at -- the GRANULARITY the solve actually used (BK-3c). The agent cites
            this so mesh resolution is a visible, narratable lever, never hidden.
        mesh_node_estimate: OPTIONAL estimated node count for that resolution
            (>= 0) -- the size/cost signal the approve-mesh gate surfaces.
        mesh_resolution_label: OPTIONAL human label for how the resolution was
            chosen ("auto (medium)", "fine", "custom 8 m", ...). ``None`` when
            unavailable.

    ``layer_type`` is ``"raster"`` (the peak-concentration COG); the animation is
    played from the SELAFIN mesh sibling, not per-frame COGs. The raster uses the
    ``continuous_dye_concentration`` style preset + a data-driven ``legend``.
    """

    dye_cmax_mgl: float = Field(ge=0.0)
    dye_peak_time_s: float | None = Field(default=None, ge=0.0)
    plume_reach_m: float | None = Field(default=None, ge=0.0)
    active_frames: int | None = Field(default=None, ge=0)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_node_estimate: int | None = Field(default=None, ge=0)
    mesh_resolution_label: str | None = Field(default=None)
    # GAIA v1 sediment scalars (OPTIONAL; only populated for a sediment run so the
    # returned peak-CONCENTRATION layer ALSO carries the deposition numbers the
    # agent narrates - Invariant 1). ``None`` for every non-sediment run so dye /
    # oil / decay layers are byte-identical.
    deposited_mass_kg: float | None = Field(default=None, ge=0.0)
    deposit_fraction: float | None = Field(default=None, ge=0.0)
    max_deposition_mm: float | None = Field(default=None, ge=0.0)
    # GAIA v2 erodible-bed only: deepest bed SCOUR magnitude (mm, >= 0). None on
    # the v1 supply-limited path and every non-sediment run.
    max_scour_mm: float | None = Field(default=None, ge=0.0)
    # GAIA v3 multi-class graded-sediment SORTING scalars: the spread
    # of the SURFACE mean grain size (um) after the bed sorts - the bed armors in
    # scour zones (D50 up) and fines in deposits (D50 down). Populated ONLY for a
    # multi-class (>= 2 grain classes) run; None on single-class / non-sediment.
    # A single class is uniform by construction (range 0), so a nonzero range IS
    # the sorting signature the agent narrates (Invariant 1 - from the field).
    sediment_n_classes: int | None = Field(default=None, ge=2)
    sediment_surface_d50_min_um: float | None = Field(default=None, ge=0.0)
    sediment_surface_d50_max_um: float | None = Field(default=None, ge=0.0)
    sediment_surface_d50_range_um: float | None = Field(default=None, ge=0.0)


class TelemacDoLayerURI(LayerURI):
    """A ``LayerURI`` for a TELEMAC-2D WAQTEL dissolved-oxygen (DO) sag layer.

    The WAQTEL O2 (WATER QUALITY PROCESS = 2) analogue of ``TelemacDyeLayerURI``:
    below a permitted discharge, downstream CBOD decay consumes oxygen and
    reaeration recovers it - the classic Streeter-Phelps sag. The published raster
    is the steady-state DISSOLVED O2 field (mg/L); the along-reach DO-vs-distance
    sag curve rides in ``sag_curve_*`` for the dock chart. Extends ``LayerURI``
    field-for-field and adds the typed scalars the agent narrates rather than
    invents (Invariant 1):

        do_min_mgl: the SAG minimum - lowest dissolved oxygen anywhere in the
            reach, mg/L (>= 0). The headline: how low DO bottoms out.
        do_min_distance_m: along-reach distance (m, >= 0) from the discharge to
            that sag minimum - WHERE the critical point sits.
        do_upstream_mgl: DO carried in at the top of the reach (the fully-mixed
            discharge DO), mg/L - the pre-sag reference.
        do_saturation_mgl: the O2 saturation Cs (mg/L) the deficit is measured
            against (temperature-dependent) - the recovery ceiling.
        do_standard_mgl: the water-quality DO standard (mg/L) the sag is judged
            against (e.g. 5 mg/L warm-water aquatic-life) - chart reference only.
        do_violates_standard: True when ``do_min_mgl`` falls below
            ``do_standard_mgl`` (the sag violates the standard) - the permit answer.
        bod_upstream_mgl: the fully-mixed ultimate CBOD (mg/L) loaded at the top
            of the reach - the driver of the sag.
        sag_curve_distance_m / sag_curve_do_mgl / sag_curve_bod_mgl: OPTIONAL
            equal-length arrays of the centerline DO-sag curve (downstream
            distance, DO, CBOD) the dock chart plots against the standard line.
        mesh_size_m / mesh_node_estimate / mesh_resolution_label: the granularity
            the solve used (the visible, narratable resolution lever).

    ``layer_type`` is ``"raster"`` (the steady-state DO COG); the time animation
    plays from the SELAFIN mesh sibling. The raster uses the
    ``continuous_dissolved_oxygen`` style preset + a data-driven ``legend``.
    """

    do_min_mgl: float = Field(ge=0.0)
    do_min_distance_m: float | None = Field(default=None, ge=0.0)
    do_upstream_mgl: float | None = Field(default=None, ge=0.0)
    do_saturation_mgl: float | None = Field(default=None, ge=0.0)
    do_standard_mgl: float | None = Field(default=None, ge=0.0)
    do_violates_standard: bool | None = Field(default=None)
    bod_upstream_mgl: float | None = Field(default=None, ge=0.0)
    sag_curve_distance_m: list[float] | None = Field(default=None)
    sag_curve_do_mgl: list[float] | None = Field(default=None)
    sag_curve_bod_mgl: list[float] | None = Field(default=None)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_node_estimate: int | None = Field(default=None, ge=0)
    mesh_resolution_label: str | None = Field(default=None)
    #: The solver run this layer came out of - i.e. its object-store prefix. The
    #: run's own chart spec + metrics are written there, so a reader can pull the
    #: product's chart instead of rebuilding one from the scalars above.
    run_id: str | None = Field(default=None)


class TelemacSedimentLayerURI(LayerURI):
    """A ``LayerURI`` for the GAIA sediment BED-EVOLUTION (deposition) raster.

    The SECOND COG a GAIA sediment run emits beside the peak suspended-sediment
    concentration ribbon: the final CUMUL BED EVOL field (deposition, in mm) read
    from ``gaia_river.slf`` and rendered on the diverging
    ``TELEMAC_BED_EVOLUTION_STYLE_PRESET`` ramp. Extends ``LayerURI`` field-for-
    field (so it still maps onto ``map-command load-layer``) and adds the sediment
    scalars the agent cites rather than invents (Invariant 1):

        deposited_mass_kg: NET sediment mass left on the bed over the run (kg, >= 0)
            - from GAIA's own listing mass balance (CUMULATED BED EVOLUTIONS, the
            net deposition-minus-erosion closure), clamped >= 0. The SAME net
            quantity the final-frame bed-evolution map and deposit_fraction
            integrate; NEVER the gross CUMULATED DEPOSITION, which can cancel
            against re-suspension erosion and contradict the (empty) map.
        deposit_fraction: fraction of the injected sediment mass that settled to
            the bed (0..1) - net bed mass / injected mass, the "how much stayed"
            headline. ``None`` when the injected mass is unknown.
        max_deposition_mm: peak bed-elevation gain anywhere in the reach (mm,
            >= 0) - the thickest point of the deposition tongue.

    ``layer_type`` is ``"raster"`` (the deposition COG); the time animation plays
    from the ``gaia_river.slf`` SELAFIN mesh sibling that ``export_case_to_qgis``
    discovers via ``TELEMAC_BED_EVOLUTION_STYLE_PRESET``.
    """

    deposited_mass_kg: float | None = Field(default=None, ge=0.0)
    deposit_fraction: float | None = Field(default=None, ge=0.0)
    max_deposition_mm: float | None = Field(default=None, ge=0.0)
    # GAIA v2 erodible-bed morphodynamics only: the magnitude (mm, >= 0) of the
    # DEEPEST bed SCOUR anywhere in the reach - the most-negative CUMUL BED EVOL
    # node. None on the v1 supply-limited path (nothing erodes). Reported beside
    # max_deposition_mm so the agent narrates both limbs of the signed
    # scour/deposition field it renders on the diverging ramp (Invariant 1).
    max_scour_mm: float | None = Field(default=None, ge=0.0)
    grain_size_um: float | None = Field(default=None, gt=0.0)
    sediment_type: str | None = Field(default=None)


class TelemacWaveLayerURI(LayerURI):
    """A ``LayerURI`` for a TOMAWAC significant-wave-height (Hs) field.

    The spectral-wave analogue of ``TelemacDyeLayerURI``: TOMAWAC solves the
    wave-action balance (wind-wave generation, shoaling/breaking, wave-current
    interaction, bottom friction) over a real-lake or idealized basin, and the
    primary artifact is the significant wave height Hs field. Extends ``LayerURI``
    field-for-field and adds the wave scalars the agent cites rather than invents
    (invariant 1):

        hs_max_m: peak significant wave height anywhere in the domain, m (>= 0)
            -- the strongest sea the storm builds.
        hs_mean_m: OPTIONAL mean Hs over the wet domain, m (>= 0).
        hs_upwind_m / hs_downwind_m: OPTIONAL Hs sampled near the upwind vs
            downwind shore (m, >= 0) -- the proof-norm-#9 discriminating pair for
            a fetch run (same storm, opposite shores; downwind >> upwind).
        peak_period_max_s: OPTIONAL peak wave period at the strongest sea, s
            (>= 0).
        wave_mode: which question class the field answers (fetch_growth /
            shoaling / bottom_friction / wave_current).
        wind_speed_mps: OPTIONAL sustained wind speed the run was forced with
            (m/s, >= 0) -- the forcing the agent narrates, never invents.
        mesh_size_m: OPTIONAL grid node spacing (m, > 0) the solve used -- the
            visible granularity lever.
        mesh_node_estimate: OPTIONAL node count for that resolution (>= 0).
        mesh_resolution_label: OPTIONAL human label for the resolution choice.
        fetch_curve_km / fetch_curve_hs_m: OPTIONAL the ALONG-FETCH growth curve
            the worker measured -- downwind distance (km) against Hs (m), the two
            arrays paired index-for-index. This is the run's own record of the
            growth the fetch produced; the chart plots it rather than resampling
            the raster, which is what keeps the chart and the narrated numbers the
            SAME measurement.

    ``layer_type`` is ``"raster"`` (the Hs COG); the time evolution plays from the
    TOMAWAC result SELAFIN mesh sibling that ``export_case_to_qgis`` discovers via
    ``TELEMAC_WAVE_STYLE_PRESET``. The raster carries a data-driven ``legend``.
    """

    hs_max_m: float = Field(ge=0.0)
    hs_mean_m: float | None = Field(default=None, ge=0.0)
    hs_upwind_m: float | None = Field(default=None, ge=0.0)
    hs_downwind_m: float | None = Field(default=None, ge=0.0)
    peak_period_max_s: float | None = Field(default=None, ge=0.0)
    wave_mode: str | None = Field(default=None)
    wind_speed_mps: float | None = Field(default=None, ge=0.0)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_node_estimate: int | None = Field(default=None, ge=0)
    mesh_resolution_label: str | None = Field(default=None)
    fetch_curve_km: list[float] | None = Field(default=None)
    fetch_curve_hs_m: list[float] | None = Field(default=None)


class ArtemisAgitationLayerURI(LayerURI):
    """A ``LayerURI`` for an ARTEMIS harbour-agitation field.

    The phase-RESOLVING complement to ``TelemacWaveLayerURI`` (TOMAWAC's
    phase-averaged spectral tier): ARTEMIS solves the elliptic mild-slope
    (Berkhoff) equation for steady-state diffraction / refraction / partial
    reflection inside harbours and around structures. The primary artifact is the
    dimensionless agitation coefficient Kd = Hs/H0 (how much the incident wave is
    amplified or sheltered). Extends ``LayerURI`` field-for-field and adds the
    agitation scalars the agent cites rather than invents (invariant 1):

        kd_max: peak agitation coefficient anywhere in the domain (>= 0) -- the
            strongest amplification (a resonant antinode or a focus caustic).
        hs_max_m: peak significant wave height, m (>= 0).
        kd_sheltered / kd_exposed: OPTIONAL mean Kd in the lee of a breakwater vs
            the exposed approach (>= 0) -- the diffraction proof-norm-#9 pair
            (sheltered << exposed proves the structure shelters).
        resonant_period_s: OPTIONAL the harbour resonant (seiche) period, s (>= 0).
        response_at_resonance / response_off_resonance: OPTIONAL in-harbour mean
            Hs/H0 amplification AT vs OFF resonance (>= 0) -- the resonance pair.
        wave_mode: the question class (diffraction / resonance / shoal).
        wave_period_s: incident wave period the field was forced with (s, >= 0).
        mesh_size_m: grid node spacing (m, > 0) the solve used.
        mesh_resolution_label: OPTIONAL human label for the resolution choice.
        agitation_curve_m / agitation_curve_kd / agitation_curve_kind: OPTIONAL the
            curve the worker measured across the field - a transect in metres
            against Kd for a diffraction run, a period sweep for a resonance run -
            paired index-for-index, with the kind naming what the axis IS. The
            chart plots this rather than resampling the raster, so the chart and
            the narrated kd_sheltered / kd_exposed are the SAME measurement.

    ``layer_type`` is ``"raster"`` (the Kd COG); the phase field plays from the
    ARTEMIS result SELAFIN mesh sibling discovered via
    ``TELEMAC_AGITATION_STYLE_PRESET``. The raster carries a data-driven ``legend``.
    """

    kd_max: float = Field(ge=0.0)
    hs_max_m: float | None = Field(default=None, ge=0.0)
    kd_sheltered: float | None = Field(default=None, ge=0.0)
    kd_exposed: float | None = Field(default=None, ge=0.0)
    resonant_period_s: float | None = Field(default=None, ge=0.0)
    response_at_resonance: float | None = Field(default=None, ge=0.0)
    response_off_resonance: float | None = Field(default=None, ge=0.0)
    wave_mode: str | None = Field(default=None)
    wave_period_s: float | None = Field(default=None, ge=0.0)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_resolution_label: str | None = Field(default=None)
    agitation_curve_m: list[float] | None = Field(default=None)
    agitation_curve_kd: list[float] | None = Field(default=None)
    agitation_curve_kind: str | None = Field(default=None)


class Telemac3dLayerURI(LayerURI):
    """A ``LayerURI`` for a TELEMAC-3D stratified / 3D-hydrodynamics field.

    The three-dimensional baroclinic analogue of the 2D TELEMAC layers: TELEMAC-3D
    solves the 3D (hydrostatic / non-hydrostatic) Navier-Stokes equations with
    active-tracer (temperature / salinity) density coupling over sigma layers - the
    vertical structure a 2D depth-averaging cannot resolve. The primary artifact is
    a SURFACE-layer field COG (temperature / velocity / salinity by mode) plus a
    BOTTOM-layer companion; the discriminating 3D signature is carried in the scalar
    fields the agent cites rather than invents (invariant 1):

        stratification_metric: the headline discriminating magnitude (>= 0) -
            top-to-bottom temperature difference (stratification), surface-minus-
            bottom velocity magnitude (wind_circulation), or gravity-current front
            speed (salt_wedge). Nonzero == the 3D structure a 2D model misses.
        flow_mode: the question class (stratification / wind_circulation /
            salt_wedge).
        variable_label / variable_units: what the surface/bottom COG shows.
        stratification_dt: OPTIONAL persisting top-to-bottom temperature diff, C
            (stratification mode) - the thermocline strength.
        u_surface / u_bottom / depth_avg_u: OPTIONAL vertical-velocity structure,
            m/s (wind_circulation) - surface downwind (+), bottom upwind (-),
            depth-average ~0 (the two-layer wind gyre a 2D model returns as ~0
            everywhere).
        front_speed_mps / benjamin_speed_mps: OPTIONAL measured vs analytic
            gravity-current front speed, m/s (salt_wedge).
        surface_value_mean / bottom_value_mean: OPTIONAL mean of the primary
            variable at the surface vs bottom layer.
        nplan: OPTIONAL number of sigma planes (the 3D degree of freedom).
        non_hydrostatic: OPTIONAL whether the non-hydrostatic solver was used.
        wind_speed_mps: OPTIONAL sustained wind speed the run was forced with
            (m/s, >= 0).
        mesh_size_m: OPTIONAL horizontal grid node spacing (m, > 0).
        mesh_resolution_label: OPTIONAL human label for the resolution choice.
        profile_sigma / profile_values / profile_values_initial: OPTIONAL the
            VERTICAL profile the worker measured - sigma (0 = bed, 1 = surface)
            against the field value, final and initial, paired index-for-index.
            This is the 3D answer in the only form that shows it: what the column
            looked like when the run started and what survived. The chart plots
            it rather than resampling the surface map, which carries no depth at
            all.

    ``layer_type`` is ``"raster"`` (the surface-field COG); the full-column
    evolution plays from the TELEMAC-3D result SELAFIN mesh sibling that
    ``export_case_to_qgis`` discovers via ``TELEMAC3D_STRATIFICATION_STYLE_PRESET``.
    The raster carries a data-driven ``legend``.
    """

    stratification_metric: float = Field(ge=0.0)
    profile_sigma: list[float] | None = Field(default=None)
    profile_values: list[float] | None = Field(default=None)
    profile_values_initial: list[float] | None = Field(default=None)
    flow_mode: str | None = Field(default=None)
    variable_label: str | None = Field(default=None)
    variable_units: str | None = Field(default=None)
    stratification_dt: float | None = Field(default=None)
    u_surface: float | None = Field(default=None)
    u_bottom: float | None = Field(default=None)
    depth_avg_u: float | None = Field(default=None)
    front_speed_mps: float | None = Field(default=None, ge=0.0)
    benjamin_speed_mps: float | None = Field(default=None, ge=0.0)
    surface_value_mean: float | None = Field(default=None)
    bottom_value_mean: float | None = Field(default=None)
    nplan: int | None = Field(default=None, ge=0)
    non_hydrostatic: bool | None = Field(default=None)
    wind_speed_mps: float | None = Field(default=None, ge=0.0)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_resolution_label: str | None = Field(default=None)


class TelemacCoastalLayerURI(LayerURI):
    """A ``LayerURI`` for a TELEMAC-2D coastal tidal/surge depth field.

    TWO layers carry this type, and they answer different questions. The PRIMARY
    is peak depth over land that was DRY at t=0 - the planning quantity, the same
    discrimination ``flooded_land_km2`` counts. Beside it, ``role="context"``, the
    TOTAL peak water depth including the permanently submerged bay: where the
    water is, rather than where the tide went. The two must stay separate
    rasters: one raster carrying both paints a permanently submerged bay floor
    in an "inundation depth" ramp, which reads as flooding that never happened.

    The storm-tide analogue of the other TELEMAC layers: an open-water coastal
    domain (real NOAA DEM_all topobathy) with ONE seaward liquid boundary driven
    in time by a NOAA CO-OPS / GTSM water-level series through the LIQUID
    BOUNDARIES FILE (SL(1)); SAINT-VENANT + TIDAL FLATS wetting/drying floods the
    low coast as the boundary stage rises. Extends ``LayerURI`` field-for-field
    and adds the storm-tide scalars the agent cites rather than invents
    (invariant 1). Both layers carry the SAME scalars - they describe the run, not
    the raster:

        peak_depth_m: peak water depth anywhere in the domain over the run, m
            (>= 0) -- the whole water column, permanent water included.
        flooded_land_km2: newly-inundated LAND area, km^2 (>= 0) -- mesh cells
            dry at t0 (bed above the initial water line) but wet at peak stage.
            This is THE discriminant: a surge series floods far more land than the
            calm astronomical tide over the SAME domain.
        inundation_peak_depth_m: OPTIONAL deepest INUNDATION, m (>= 0) -- the peak
            depth over initially-dry land only, which is what the primary raster
            paints. Lower than ``peak_depth_m`` wherever permanent water is deeper
            than the flooding.
        inundation_basis: OPTIONAL label for HOW dry-at-t0 was decided (the bed
            against the datum-corrected initial water line, or frame 0 of WATER
            DEPTH) -- the reader is entitled to check which land was excluded.
        wet_area_km2: OPTIONAL total wetted area at peak stage, km^2 (>= 0).
        peak_wl_m: OPTIONAL peak free-surface water level over wet nodes, m
            (the crest stage reached).
        sl_peak_m: OPTIONAL peak boundary forcing SL(1) the run was driven with,
            m -- the ocean water level the agent narrates, never invents.
        series_type: which series drove the boundary (``observed`` storm surge /
            ``prediction`` astronomical tide) -- the A/B question class.
        series_datum / datum_offset_m: OPTIONAL tide-series vertical datum label
            (e.g. ``"MLLW"``) + the labeled offset applied to reconcile it with
            the DEM (sea-level) datum -- never invented, always surfaced.
        station_id / station_name: OPTIONAL CO-OPS station the series came from.
        ocean_edge: OPTIONAL which bbox edge carried the seaward boundary.
        mesh_size_m: OPTIONAL grid node spacing (m, > 0) the solve used.
        mesh_resolution_label: OPTIONAL human label for the resolution choice.

    ``layer_type`` is ``"raster"`` on both; the rising-tide animation plays from
    the coastal result SELAFIN mesh sibling that ``export_case_to_qgis`` discovers
    via ``TELEMAC_COASTAL_DEPTH_STYLE_PRESET``. Both rasters carry a data-driven
    ``legend``, each over its OWN range.
    """

    peak_depth_m: float = Field(ge=0.0)
    flooded_land_km2: float = Field(ge=0.0)
    inundation_peak_depth_m: float | None = Field(default=None, ge=0.0)
    inundation_basis: str | None = Field(default=None)
    wet_area_km2: float | None = Field(default=None, ge=0.0)
    peak_wl_m: float | None = Field(default=None)
    sl_peak_m: float | None = Field(default=None)
    series_type: str | None = Field(default=None)
    series_datum: str | None = Field(default=None)
    datum_offset_m: float | None = Field(default=None)
    station_id: str | None = Field(default=None)
    station_name: str | None = Field(default=None)
    ocean_edge: str | None = Field(default=None)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_resolution_label: str | None = Field(default=None)


class TelemacRainOnGridLayerURI(TelemacWseLayerURI):
    """A ``LayerURI`` for a TELEMAC-2D RAIN-ON-GRID peak flood-depth raster.

    A rain-on-grid run answers two questions at once, and the deliverable carries
    both: WHERE the water stood (the peak max-over-time depth raster this layer
    points at) and HOW MUCH left the basin (the outlet hydrograph, whose series
    rides here so the chart dock draws the run's own numbers rather than a
    rederivation). It extends the peak-depth layer field-for-field and adds the
    catchment scalars the agent narrates rather than invents (Invariant 1):

        catchment_area_km2: the delineated basin area upstream of the outlet.
            Every volume below is only readable against it.
        peak_discharge_m3s / peak_discharge_time_s: the hydrograph crest and when
            it arrived - the flash-flood headline.
        peak_is_window_truncated: True when that crest is the LAST sample in the
            series, i.e. the outflow was still rising when the simulated window
            closed. The peak, the runoff volume and the runoff coefficient are
            then FLOORS on the storm's answer, not measurements of it.
        rainfall_volume_m3 / runoff_volume_m3: what fell on the catchment and what
            left through the outlet over the simulated window.
        runoff_coefficient: runoff / rainfall, the dimensionless summary of how
            much the infiltration surface absorbed. ``None`` when no rain fell.
        max_depth_peak_m / max_velocity_peak_ms: the deepest and fastest the
            overland sheet got anywhere, at any time.
        max_depth_p99_m: the 99th-percentile peak depth over the wet catchment -
            published BESIDE the maximum because a single terrain pit ponding to
            its rim sets ``max_depth_peak_m`` while the sheet the storm actually
            produced is two orders of magnitude shallower. One number is the
            extreme, the other is the field; a reader needs both.
        continuity_rel_error: the solver's own mass-balance residual. A run whose
            volumes do not close is not a run whose hydrograph means anything, so
            the number is published rather than checked in private.
        runoff_path: which infiltration path ran - ``"native"`` (constant design
            storm through the engine's SCS-CN) or ``"native_hyetograph"`` (a real
            time-varying hyetograph driving the same SCS-CN per timestep).
        amc_condition: the SCS antecedent-moisture condition (1 dry, 2 normal,
            3 wet) the curve numbers were converted under.
        rain_intensity_mm_per_hr: the constant design-storm rate, when one drove
            the run.
        outlet_hydrograph_t_s / outlet_hydrograph_q_m3s: the outlet discharge
            series the chart spec is built from.
        mesh_node_count / mesh_element_count / mesh_size_m /
        mesh_resolution_label: what the catchment was discretized as, so mesh
        resolution stays a visible, narratable lever.
        catchment_provenance: whether the mesh was GENERATED for this run or
            SUPPLIED on the invocation.
        domain_bbox: the EPSG:4326 extent the run actually modelled - the mesh's
            own node bounds, not the analysis window it was delineated inside. The
            basin is a fraction of the search buffer, so the two are different
            answers to "where is this".
        catchment_name: what the modelled basin is CALLED - the chart titles
            itself with the catchment rather than with the raster that happens to
            be the map anchor.

    ``layer_type`` is ``"raster"``; the animation plays from the native
    ``r2d_rog.slf`` mesh sibling the results seam publishes beside it.
    """

    catchment_area_km2: float | None = Field(default=None, ge=0.0)
    peak_discharge_m3s: float | None = Field(default=None)
    peak_discharge_time_s: float | None = Field(default=None, ge=0.0)
    peak_is_window_truncated: bool | None = Field(default=None)
    rainfall_volume_m3: float | None = Field(default=None, ge=0.0)
    runoff_volume_m3: float | None = Field(default=None)
    runoff_coefficient: float | None = Field(default=None)
    max_depth_peak_m: float | None = Field(default=None, ge=0.0)
    max_depth_p99_m: float | None = Field(default=None, ge=0.0)
    max_velocity_peak_ms: float | None = Field(default=None, ge=0.0)
    continuity_rel_error: float | None = Field(default=None)
    runoff_path: str | None = Field(default=None)
    amc_condition: int | None = Field(default=None, ge=1, le=3)
    rain_intensity_mm_per_hr: float | None = Field(default=None, ge=0.0)
    outlet_hydrograph_t_s: list[float] | None = Field(default=None)
    outlet_hydrograph_q_m3s: list[float] | None = Field(default=None)
    mesh_node_count: int | None = Field(default=None, ge=0)
    mesh_element_count: int | None = Field(default=None, ge=0)
    mesh_size_m: float | None = Field(default=None, gt=0.0)
    mesh_resolution_label: str | None = Field(default=None)
    catchment_provenance: str | None = Field(default=None)
    catchment_name: str | None = Field(default=None)
    domain_bbox: list[float] | None = Field(default=None)
