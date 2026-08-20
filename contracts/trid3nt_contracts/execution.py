"""Solver-execution shapes: ModelSetup, RunResult, ExecutionHandle,
LayerURI.

These are the return types of the model-setup/execution tool chain:
- ``build_sfincs_model(...)  -> ModelSetup``
- ``run_solver(...)          -> ExecutionHandle``
- ``wait_for_completion(...) -> RunResult``
- ``postprocess_flood(...)   -> list[LayerURI]``

Invariants this module is responsible for:
- **8. Cancellation is first-class.** ``ExecutionHandle`` carries the Cloud
  Workflows execution identifier as a first-class field
  (``workflows_execution_id``) so ``agent`` calls Workflows ``terminate``
  without string-parsing. There is one handle type; no per-backend variants.
- **``LayerURI`` aligns field-for-field with ``map-command load-layer`` args**
  (``layer_id``, ``style_preset``, optional ``temporal``) and with
  ``ResultLayer`` so postprocess output flows to the map without translation.
  Output formats are fixed: rasters COG, vectors FlatGeobuf/GeoParquet.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import FallbackActivation, GraceModel, SyntheticInput, ULIDStr, UTCDatetime
from .envelope import TemporalConfig

__all__ = [
    "ComputeClass",
    "ModelSetup",
    "ExecutionHandle",
    "RunResult",
    "LegendClass",
    "LegendKey",
    "LayerURI",
    "HighWaterMarksLayerURI",
    "FaultSourcesResult",
    "FloodExtentObservationResult",
    "LandcoverResult",
    "DemLayerURI",
    "TopobathyResult",
    "StormTracksLayerURI",
    "GOESSatelliteLayerURI",
    "NWMStreamflowLayerURI",
    "LivingAtlasLayerURI",
    "LAYER_RESULT_MODELS",
]


# Open enum: compute classes a solver may request. Engine/infra extend as
# backends are added; the handle shape does not change per backend.
ComputeClass = Literal["small", "standard", "large", "gpu"]


# --------------------------------------------------------------------------- #
# Data-driven render legend (the colormap KEY that comes from the data)
# --------------------------------------------------------------------------- #


class LegendClass(GraceModel):
    """One class swatch in a CATEGORICAL ``LegendKey`` (NLCD class, drought
    D0-D4, Pelicun damage state, etc.).

    A class addresses the data it colors in one of two ways; populate exactly
    one form per class:

    - ``value`` -- a single discrete value the swatch matches (the GDAL color
      table entry, the NLCD class code, the ``"D2"`` drought label). May be a
      number or a string.
    - ``value_min`` / ``value_max`` -- a half-open / closed numeric bin the
      swatch covers (graduated buckets, e.g. damage-state mean ``0.5..1.5``).

    ``color`` is an ``#rrggbb`` hex string; ``label`` is the human-readable
    swatch caption the frontend renders verbatim.
    """

    value: float | int | str | None = None
    value_min: float | None = None
    value_max: float | None = None
    color: str  # "#rrggbb"
    label: str


class LegendKey(GraceModel):
    """The DATA-DRIVEN render key for a layer -- the colormap/legend the
    frontend draws and the raster/vector colors are driven by.

    The principle (NATE): the gradient/key comes FROM THE DATA at fetch time,
    so it MEANS something rather than being a retroactive hardcoded guess. The
    producer (``publish_layer``) emits a ``LegendKey`` from values it already
    computed; the frontend renders ANY key generically, so a new tool that
    emits a ``LegendKey`` needs ZERO web changes.

    Two split of responsibility for the range:

    - The colormap CHOICE stays the semantic per-variable decision (drought
      ramps tan->dark-red, temperature ``rdylbu``, seismic PGA ``reds``, ...).
    - The RANGE (``vmin`` / ``vmax``) is the REAL data range by default -- the
      p2/p98 percentile read ``publish_layer`` already computes -- UNLESS a
      variable has a canonical fixed scale (seismic PGA 0-1, temperature K),
      which a tool/preset may pin. The legend and the raster render MUST agree
      on the same range, so the legacy hardcoded ``"0,3"``-style guesses are
      retired as the source of truth (kept only as the canonical-fixed-scale
      override or the no-data fallback).

    Additive + optional everywhere (``legend=None`` => legacy ``style_preset``
    rendering: the existing preset + URL-rescale + preset-fallback path stays
    as the fallback, so legacy layers render exactly as before).

    Fields:

    ``kind``
        ``"continuous"`` for rasters + graduated vectors (a ramp over a numeric
        range); ``"categorical"`` for discrete classes (NLCD, drought, damage
        states).
    ``colormap`` (continuous)
        Either a named ramp the frontend resolves to stops (e.g. ``"reds"`` /
        ``"viridis"``) OR explicit stops as ``[[stop_0to1, "#rrggbb"], ...]``
        (each stop a float in ``[0, 1]``). ``None`` for purely categorical
        keys that carry ``classes`` instead.
    ``vmin`` / ``vmax`` (continuous)
        The REAL data range the colormap spans (the percentile read by
        default; a canonical fixed scale when a variable pins one). ``None``
        when unknown / not applicable.
    ``classes`` (categorical)
        The ordered list of ``LegendClass`` swatches. ``None`` for continuous
        keys.
    ``value_field``
        For VECTOR layers: the GeoJSON feature property the color is driven by
        (e.g. ``"ds_mean"`` on a Pelicun choropleth). ``None`` for rasters
        (the raster band IS the value).
    ``units``
        The data units the legend annotates (e.g. ``"meters"``, ``"mg/L"``).
        ``None`` for unitless / categorical.
    ``label``
        Optional human-readable legend title (e.g. ``"Flood depth"``).
    """

    kind: Literal["continuous", "categorical"]

    # continuous (rasters + graduated vectors)
    colormap: str | list[tuple[float, str]] | None = None
    vmin: float | None = None
    vmax: float | None = None

    # categorical (NLCD classes, drought D0-D4, damage states)
    classes: list[LegendClass] | None = None

    # both
    value_field: str | None = None  # VECTOR: the GeoJSON property the color is driven by
    units: str | None = None
    label: str | None = None


class ModelSetup(GraceModel):
    """Returned by ``build_sfincs_model`` (HydroMT). A staged, ready-to-run model.

    The built model artifacts live in GCS; ``setup_uri`` points at them.
    ``parameters`` is solver-specific staging metadata (grid, forcing, options)
    validated at the engine layer.
    """

    schema_version: Literal["v1"] = "v1"

    setup_id: ULIDStr
    solver: str  # e.g., "sfincs"
    setup_uri: str  # gs://... staged model inputs
    grid_resolution_m: float = Field(gt=0.0)
    bbox: tuple[float, float, float, float]
    parameters: dict = Field(default_factory=dict)  # solver-specific staging
    created_at: UTCDatetime


class ExecutionHandle(GraceModel):
    """Returned by ``run_solver``. The cancellation contract (invariant 8).

    ``workflows_execution_id`` is the Cloud Workflows execution identifier —
    the pinned cancellation seam. ``agent`` calls Workflows ``terminate`` with
    it on cancel; ``infra`` provisions the workflow definitions it names. All
    three cite this same handle (orchestrator "Solver cancellation chain").
    """

    schema_version: Literal["v1"] = "v1"

    handle_id: ULIDStr
    run_id: ULIDStr  # the runs._id / solver_run_id this execution backs
    solver: str
    compute_class: ComputeClass

    # --- Cancellation seam
    workflows_execution_id: str  # Cloud Workflows execution identifier
    workflow_name: str  # the Cloud Workflows definition name
    workflow_location: str  # GCP region of the workflow execution

    submitted_at: UTCDatetime


class RunResult(GraceModel):
    """Returned by ``wait_for_completion``. Terminal outcome of an execution.

    ``status`` mirrors the ``runs`` lifecycle; ``cancelled`` is distinct from
    ``failed`` (invariant 8). ``output_uri`` points at the raw solver output in
    GCS, which ``postprocess_flood`` consumes to produce ``LayerURI`` objects.
    """

    schema_version: Literal["v1"] = "v1"

    run_id: ULIDStr
    handle_id: ULIDStr
    status: Literal["complete", "failed", "cancelled"]
    output_uri: str | None = None  # gs://... raw solver output (None if not complete)
    started_at: UTCDatetime | None = None
    completed_at: UTCDatetime | None = None
    duration_seconds: float | None = None

    # Failure details (status == "failed")
    error_code: str | None = None
    error_message: str | None = None

    # Cancellation details (status == "cancelled")
    cancellation_reason: str | None = None

    # AWS Batch compute metadata (task-153 — solve-time inference). Best-effort
    # capture of the Spot instance + timing breakdown the run landed on, so the
    # adaptive perf model can later infer completion time from real (instance,
    # problem-size) measurements. Populated ONLY on the aws-batch terminal paths
    # (SUCCEEDED / FAILED); ``None`` on the local/in-process paths and on any
    # AWS-describe failure (the capture is wrapped + swallows all exceptions).
    # Shape (all keys optional): ``{instance_type, instance_lifecycle, az,
    # vcpus, memory_mib, created_at_ms, started_at_ms, stopped_at_ms,
    # queue_provision_secs, compute_secs, total_secs}``.
    batch_compute_meta: dict | None = None


class LayerURI(GraceModel):
    """Returned by ``postprocess_flood`` (one per output layer).

    Aligned field-for-field with ``map-command load-layer`` args and with
    ``ResultLayer`` so postprocess output maps onto the visualization seam with
    no translation. ``uri`` is a COG (raster) or FlatGeobuf/GeoParquet (vector).

    ``bbox`` is optional: when present the pipeline emitter emits a
    ``map-command(zoom-to)`` after ``add_loaded_layer`` so the client camera
    flies to the layer's geographic extent. Format: ``(min_lon, min_lat,
    max_lon, max_lat)`` in EPSG:4326.

    ``legend`` is the DATA-DRIVEN render key (see ``LegendKey``): the colormap
    is the semantic per-variable choice, the range is the REAL data range the
    producer already computed. Additive + optional -- ``legend=None`` means
    legacy ``style_preset`` rendering (the existing preset + URL-rescale +
    preset-fallback path), so layers without a legend render exactly as before.
    """

    layer_id: str  # stable id; flows into map-command load-layer args
    name: str
    # ``mesh`` (SCHISM landing): an unstructured/UGRID solver mesh the
    # plugin opens via MDAL (``QgsMeshLayer``) -- the ONE format the live
    # materializer STAGES rather than streams. SCHISM's out2d UGRID +
    # (retroactively) the SFINCS quadtree map are the mesh producers.
    layer_type: Literal["raster", "vector", "mesh"]
    uri: str  # gs://... COG / FlatGeobuf / GeoParquet / UGRID netCDF (mesh)
    style_preset: str  # references the QML preset library
    temporal: TemporalConfig | None = None  # present iff time-varying
    role: Literal["primary", "context", "input"] = "primary"
    units: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # (min_lon, min_lat, max_lon, max_lat); triggers zoom-to
    legend: LegendKey | None = None  # data-driven render key; None => legacy style_preset rendering
    # Cross-source fallback honesty marker (2026-07-13, DEM 3DEP->GLO-30
    # ladder): set ONLY when a tool substituted a fallback data source for the
    # requested/default primary (e.g. ``fetch_dem`` with USGS 3DEP down returns
    # Copernicus GLO-30 instead). Carries a human-readable note naming BOTH
    # sources so the LLM/user can never mistake fallback data for the primary
    # (honesty floor). ``None`` => the layer is exactly the requested source.
    # Additive + optional per the GraceModel forward-compat rule.
    fallback_note: str | None = None
    # Structured half of the same honesty: which rungs of a DECLARED fallback
    # ladder served this layer, with the coverage share each painted. A mosaic
    # several rungs built together carries one row per rung; an undegraded layer
    # carries at most the ``primary`` row. ADDITIVE + default-empty -- ``[]``
    # means no ladder governs this fetch, never "nothing was substituted".
    fallbacks: list[FallbackActivation] = Field(default_factory=list)
    # Structured input provenance (provenance-chain wave): the physical model
    # inputs this layer was built from, each tagged with WHERE it came from
    # (fetched / user / default_demo / derived / prompt_interpreted). ADDITIVE +
    # default-empty -- a template populates it incrementally; ``[]`` means the
    # template has not declared its input provenance yet, NOT "all real". The
    # narration seam (``summarize_tool_result``) renders it into one compact
    # assumptions line so the agent narrates which quantities are demo defaults
    # vs site-derived, and never mistakes a baked constant for measured data.
    synthetic_inputs: list[SyntheticInput] = Field(default_factory=list)
    # Explicit CRS authority id for a ``layer_type="mesh"`` row.
    # MDAL reports an EMPTY crs() for a SCHISM out2d UGRID / a SFINCS quadtree
    # grid, so the plugin's ``_add_mesh`` applies this string via
    # ``QgsMeshLayer.setCrs(QgsCoordinateReferenceSystem(crs_authid))`` (0116).
    # ``None`` for a raster/vector row (their CRS rides in the object bytes).
    # Additive + optional per the GraceModel forward-compat rule.
    crs_authid: str | None = None


# --------------------------------------------------------------------------- #
# LayerURI SUBCLASS result models (the router's envelope-hook seam).
#
# A source whose result is a ``LayerURI`` SUBCLASS carrying business fields
# computed POST-serialize from the produced bytes declares the subclass by name
# (``output.result_model``) in its ``source.yaml``; the router constructs it from
# the base ``LayerURI`` plus the pure ``hooks.envelope`` field dict. The subclass
# lives HERE (not in a fetcher module) so the spec-driven surface has no coded
# twin. ``LAYER_RESULT_MODELS`` is the name -> class table the router resolves.
# --------------------------------------------------------------------------- #


class HighWaterMarksLayerURI(LayerURI):
    """The USGS STN HWM point ``LayerURI`` plus the survey-quality envelope.

    Extra fields beyond ``LayerURI`` (the ``fetch_high_water_marks`` envelope,
    computed post-serialize from the FGB by ``hooks.usgs_stn_hwm.envelope``):

    - ``n_marks`` -- HWM count in the AOI.
    - ``event`` -- resolved flood-event name (or None for a state-scoped fetch).
    - ``quality_breakdown`` -- ``{quality_label: count}`` (surveyor accuracy).
    - ``type_breakdown`` -- ``{hwm_type: count}`` (seed/debris/stain/mud line).
    - ``datum_summary`` -- ``{vertical_datum: count}``.
    - ``observed_quantity`` -- the physical quantity ``elev_ft`` carries:
      ``"water_surface_elevation"`` (a WSE above the stated vertical datum, NOT a
      depth-above-ground), so ``extract_model_at_observations`` never silently
      pairs this WSE against a model DEPTH raster.
    - ``caveats`` -- honest usage caveats (quality spread, datum, point-peak).
    - ``notes`` -- provenance detail.
    """

    n_marks: int = 0
    event: str | None = None
    quality_breakdown: dict[str, int] = {}
    type_breakdown: dict[str, int] = {}
    datum_summary: dict[str, int] = {}
    observed_quantity: str = "water_surface_elevation"
    caveats: list[str] = []
    notes: list[str] = []


class FaultSourcesResult(LayerURI):
    """The GEM active-fault trace ``LayerURI`` plus the kinematic source records.

    Extra fields beyond ``LayerURI`` (the ``fetch_fault_sources`` envelope,
    computed post-serialize from the produced FGB by ``hooks.fault_sources.envelope``):

    - ``catalog`` -- the source catalog ("gem").
    - ``fault_count`` -- number of fault-source records (== ``len(faults)``).
    - ``faults`` -- the kinematic source records (geometry trace + slip rate +
      dip / rake / seismogenic-depth band) the OpenQuake deck builder turns into
      ``simpleFaultSource`` sources.
    - ``source`` -- provenance string.
    - ``note`` -- always ``None`` on this (non-empty, rendered) path; the empty
      AOI degrade returns a plain dict with a populated ``note`` instead (the
      ``output.variant_by_emptiness`` switch).
    """

    catalog: str = "gem"
    fault_count: int = 0
    faults: list[dict] = []
    source: str = "GEM Global Active Faults (harmonized)"
    note: str | None = None


class FloodExtentObservationResult(LayerURI):
    """The observed (MODIS MCDWD) flood-extent ``LayerURI`` plus the observation envelope.

    Extra fields beyond ``LayerURI`` (the ``fetch_flood_extent_observation``
    envelope, computed post-serialize from the produced categorical COG by
    ``hooks.flood_extent_observation.envelope``):

    - ``product`` -- the LANCE product id (``MCDWD_L3_F3_NRT``).
    - ``observation_date`` -- the resolved 3-day-composite date (ISO).
    - ``class_breakdown`` -- ``{class_label: pixel_count}`` (nodata excluded).
    - ``flood_pixel_count`` / ``flood_area_km2`` -- classes 2 + 3.
    - ``caveats`` -- SAR/optical detection-limit + 250 m resolution + NRT-provisional.
    - ``notes`` -- provenance detail.
    """

    product: str = "MCDWD_L3_F3_NRT"
    observation_date: str | None = None
    class_breakdown: dict[str, int] = {}
    flood_pixel_count: int = 0
    flood_area_km2: float | None = None
    caveats: list[str] = []
    notes: list[str] = []


class LandcoverResult(LayerURI):
    """The NLCD landcover ``LayerURI`` plus the Manning's-validation sidecar.

    Extra fields beyond ``LayerURI`` (the ``fetch_landcover`` sidecar, computed by
    ``hooks.landcover.envelope``). ``LayerURI`` is a FROZEN ``extra="forbid"``
    contract, so the NLCD vintage year cannot live on it -- the twin returned a
    ``{"layer": LayerURI, "nlcd_vintage_year": ...}`` dict for exactly this reason;
    the subclass carries the sidecar directly instead, and the SFINCS builder reads
    ``.uri`` + ``.nlcd_vintage_year`` off it (Invariant 7: no silent wrong answers).

    - ``nlcd_vintage_year`` -- the vintage year the SFINCS setup validates the
      Manning's-roughness mapping CSV against (None for ESA WorldCover).
    - ``dataset`` -- echo of the resolved dataset string ("nlcd_2021").
    - ``source`` -- provenance ("mrlc-wcs").
    - ``effective_resolution_m`` -- the pixel spacing actually delivered.
    - ``native_resolution_m`` -- NLCD native (30 m).
    - ``downsampled`` -- True when coarsened above native for a large AOI.
    - ``downsampling_note`` -- honest coarsening caveat (None at native res).
    """

    nlcd_vintage_year: int | None = None
    dataset: str = "nlcd_2021"
    source: str = "mrlc-wcs"
    effective_resolution_m: int = 30
    native_resolution_m: int = 30
    downsampled: bool = False
    downsampling_note: str | None = None


class DemLayerURI(LayerURI):
    """A NO-FIELD ``LayerURI`` subclass for the ``fetch_dem`` fold.

    ``fetch_dem`` carries NO business fields beyond the base ``LayerURI`` -- the
    twin returned a plain ``LayerURI``. But the router's ONLY seam to override the
    emitted ``layer_id`` / ``name`` (the twin's ``dem-{lon}-{lat}-{Nm}`` id and
    ``USGS 3DEP DEM (Nm)`` name with the pixel-budget auto-coarsen stamp) is the
    ``hooks.envelope`` post-emit hook, which ``registration._validate_hooks``
    REQUIRES be declared TOGETHER with ``output.result_model`` (a name in this
    table). This zero-field subclass satisfies that pairing with the LEAST
    invasive change (no relaxation of the shared envelope/result_model validator,
    which every other envelope spec depends on): it serializes field-for-field
    like the base ``LayerURI``, so a consumer that expects a ``LayerURI`` sees an
    identical shape (``isinstance`` holds).
    """


class TopobathyResult(LayerURI):
    """A merged coastal topo-bathymetry ``LayerURI`` plus its fetch-time provenance.

    The ``fetch_topobathy`` fold's result model. The four extra fields
    are FETCH-TIME PROVENANCE -- which of the composite's heterogeneous legs (CUDEM
    1/9" tiles, NCEI regional 1 m tiles, the ETOPO 2022 global fallback, 3DEP land)
    actually painted the merge -- and are NOT recoverable from the final single-band
    COG. They are carried from the delegate to here by the provenance channel
    (``topobathy.read`` records them; ``topobathy.envelope`` reads them back), so
    they survive a cache hit that never re-runs the fetch. A cache object written
    before the channel (no sidecar) yields ``None`` provenance, and these declared
    DEFAULTS then hold -- byte-identical to the twin's own cache-hit behaviour.

    - ``bathymetry_present`` -- True when CUDEM, the regional fine DEM, OR the ETOPO
      global fallback contributed a real below-waterline bed; False on the
      3DEP-land-only degrade.
    - ``fallback_warning`` -- an honest human-readable warning when the surface
      degraded (bathymetry absent; global ETOPO fallback bathy; the land leg was
      absent). ``None`` on the clean CUDEM/regional path. NEVER a fabricated success.
    - ``cudem_tile_count`` -- number of CUDEM 1/9" tiles merged (0 off the CUDEM path).
    - ``regional_tile_count`` -- number of NCEI regional fine (~1 m) tiles merged.
    """

    bathymetry_present: bool = True
    fallback_warning: str | None = None
    cudem_tile_count: int = 0
    regional_tile_count: int = 0


class StormTracksLayerURI(LayerURI):
    """The hurricane / tropical-cyclone track ``LayerURI`` plus its fetch-time mode provenance.

    The ``fetch_storm_tracks`` fold's result model. The twin returned a
    plain ``LayerURI``; the router's only seam to override the emitted ``layer_id`` /
    ``name`` (the twin's ``storm-tracks-{seed}`` id + ``Storm tracks - <mode> (<scope>)``
    name) is the ``hooks.envelope`` post-emit hook, which pairs with a result model.
    The extra fields are FETCH-TIME PROVENANCE (which mode served + which storms were
    attributed) -- carried from the delegate to here by the provenance channel
    (``storm_tracks.read`` records them; ``storm_tracks.envelope`` reads them back), so
    they survive a cache hit that never re-runs the fetch. A pre-channel cache object
    (no sidecar) yields ``None`` provenance and these declared DEFAULTS hold.

    - ``mode`` -- ``"active"`` (NHC live) or ``"historical"`` (IBTrACS archive).
    - ``storm_count`` -- number of distinct storms in the returned layer.
    - ``storm_names`` -- the attributed storm names (or SIDs) in the layer.
    """

    mode: str = "historical"
    storm_count: int = 0
    storm_names: list[str] = []


class GOESSatelliteLayerURI(LayerURI):
    """The single-band GOES ABI imagery ``LayerURI`` plus its fetch-time scan provenance.

    The ``fetch_goes_satellite`` fold's result model. The twin returned a
    plain ``LayerURI`` with an em-dash in its ``name``; the router's only seam to
    reproduce that exact ``name`` is the ``hooks.envelope`` post-emit hook, which pairs
    with a result model. The extra fields are FETCH-TIME PROVENANCE (which bird + which
    scan actually served) carried by the provenance channel (``goes_satellite.read``
    records them; ``goes_satellite.envelope`` reads them back) -- the ``scan_time`` in
    particular is UNRECOVERABLE from the produced COG on a cache hit (the twin's own
    cache hit lost which scan served), so the channel is what makes it durable.

    - ``satellite`` -- the canonical bird token that served (``"goes-19"``).
    - ``band`` -- the ABI band emitted (``"visible"`` / ``"ir_window"`` / ``"water_vapor"``).
    - ``scan_time`` -- the ISO start-time of the chosen MCMIPC scan (``None`` on a
      pre-channel cache object).
    """

    satellite: str = "goes-19"
    band: str = "visible"
    scan_time: str | None = None


class NWMStreamflowLayerURI(LayerURI):
    """The NOAA NWM point-streamflow ``LayerURI`` plus its fetch-time cycle provenance.

    The ``fetch_noaa_nwm_streamflow`` fold's result model (the fetcher-finale
    endgame -- the LAST coded data-fetcher). The twin returned a plain ``LayerURI``; the
    router's only seam to reproduce its exact ``layer_id`` /
    ``name`` (``nwm-streamflow-{product}-{seed}`` + ``NWM streamflow -- <product>
    (<latest|valid_time>[ +fNNN])``) is the ``hooks.envelope`` post-emit hook, which pairs
    with a result model. The extra fields are FETCH-TIME PROVENANCE for a MULTI-SOURCE
    COMPOSITE (which NWM cycle served + how many reaches joined + how many NLDI COMIDs the
    5x5 bbox sample discovered) -- carried from the delegate to here by the provenance
    channel (``nwm_streamflow.read`` records them; ``nwm_streamflow.envelope`` reads them
    back), so they survive a cache hit that never re-runs the composite fetch. The
    ``reference_time`` in particular is UNRECOVERABLE from the produced FGB's per-feature
    ``valid_time`` attribute on a pre-channel cache object, so the channel is what makes it
    durable. A pre-channel cache object (no sidecar) yields ``None`` provenance and these
    declared DEFAULTS hold.

    - ``product`` -- the NWM configuration that served (``"analysis_assim"`` / ``"short_range"``).
    - ``reference_time`` -- the ISO valid/reference time of the resolved NWM cycle
      (``None`` on a pre-channel cache object).
    - ``reach_count`` -- number of joined NHDPlus reach points in the emitted layer.
    - ``nldi_comids_discovered`` -- COMIDs the NLDI 5x5 bbox sample snapped (>= reach_count).
    """

    product: str = "analysis_assim"
    reference_time: str | None = None
    reach_count: int = 0
    nldi_comids_discovered: int = 0


class LivingAtlasLayerURI(LayerURI):
    """A discovered ESRI Living Atlas layer ``LayerURI`` plus its curation envelope.

    Built by ``fetch_living_atlas_layer`` (not a spec envelope hook -- the tool is
    hand-written) around the router's produced ``LayerURI``. Carries NATE's
    two-pool curation label so the agent/user can never mistake community-curated
    content for authoritative ESRI content.

    Extra fields beyond ``LayerURI``:
    - ``curation`` -- "authoritative" (ESRI ``contentStatus`` badge) or "community".
    - ``item_id`` -- the Living Atlas ArcGIS item id.
    - ``service_type`` -- "Image Service" | "Feature Service" | "Map Service".
    - ``provenance`` -- item id, curation, service type/url, owner, source string.
    """

    curation: Literal["authoritative", "community"] = "community"
    item_id: str = ""
    service_type: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


#: name -> LayerURI-subclass. A spec's ``output.result_model`` string resolves
#: here; the router builds the named subclass from the base LayerURI + the
#: envelope hook's field dict. Empty of a name -> the plain LayerURI (no-op).
LAYER_RESULT_MODELS: dict[str, type[LayerURI]] = {
    "HighWaterMarksLayerURI": HighWaterMarksLayerURI,
    "FaultSourcesResult": FaultSourcesResult,
    "FloodExtentObservationResult": FloodExtentObservationResult,
    "LandcoverResult": LandcoverResult,
    "DemLayerURI": DemLayerURI,
    "TopobathyResult": TopobathyResult,
    "StormTracksLayerURI": StormTracksLayerURI,
    "GOESSatelliteLayerURI": GOESSatelliteLayerURI,
    "NWMStreamflowLayerURI": NWMStreamflowLayerURI,
    "LivingAtlasLayerURI": LivingAtlasLayerURI,
}


# --------------------------------------------------------------------------- #
# Resolve the envelope-side ``LegendKey`` forward reference.
# --------------------------------------------------------------------------- #
# ``ResultLayer`` (envelope.py) mirrors ``LayerURI.legend`` but cannot import
# ``LegendKey`` at module scope: execution.py imports envelope.py (for
# ``TemporalConfig``), so the reverse import would be circular. ``ResultLayer``
# therefore carries a STRING forward-ref ``"LegendKey | None"``. envelope.py is
# fully loaded by the time execution.py reaches this point, so we rebuild the
# envelope models that reference ``LegendKey`` here, injecting it into the
# types namespace. ``AssessmentEnvelope`` embeds ``ResultLayer`` and so must be
# rebuilt too. Idempotent; ``raise_errors=False`` keeps any unrelated still-open
# forward ref from breaking the package import.
from . import envelope as _envelope  # noqa: E402  (deferred to break the import cycle)

_envelope.ResultLayer.model_rebuild(
    _types_namespace={**vars(_envelope), "LegendKey": LegendKey, "LegendClass": LegendClass}
)
_envelope.AssessmentEnvelope.model_rebuild(
    _types_namespace={**vars(_envelope), "LegendKey": LegendKey, "LegendClass": LegendClass},
    force=True,
)
