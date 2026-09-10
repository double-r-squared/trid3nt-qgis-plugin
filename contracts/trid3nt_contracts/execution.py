"""The solver-execution shapes: setup, handle, result, layer.

``LayerURI`` aligns field-for-field with the ``load-layer`` map command and
with ``ResultLayer``, so a produced layer reaches the map without translation;
rasters are COG, vectors FlatGeobuf or GeoParquet. There is ONE handle type -
cancellation reads a first-class field on it rather than parsing a string.
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
    "LegendKey",
    "LayerURI",
    "HighWaterMarksLayerURI",
    "FaultSourcesResult",
    "FloodExtentObservationResult",
    "LandcoverResult",
    "DemLayerURI",
    "TopobathyResult",
    "BlueTopoResult",
    "StormTracksLayerURI",
    "GOESSatelliteLayerURI",
    "NWMStreamflowLayerURI",
    "LivingAtlasLayerURI",
    "LAYER_RESULT_MODELS",
]


# Open enum: the compute classes a solver may request. The handle shape does
# NOT change per backend, so a new backend adds a class and nothing else.
ComputeClass = Literal["small", "standard", "large", "gpu"]


# --------------------------------------------------------------------------- #
# The render legend - the colormap key that comes from the data
# --------------------------------------------------------------------------- #


class LegendKey(GraceModel):
    """A layer's RESOLVED style: what the reader is looking at, and its scale.
    The colours and the range here are the SAME ones the render uses, because
    both come from this one resolution - there is no second range to drift.
    """

    #: Which preset shape drew it: ``continuous`` (a ramp over a range),
    #: ``classed`` (declared breaks or an embedded class table), ``reference``
    #: (drawn, not measured), ``mesh`` (a dataset group).
    kind: Literal["continuous", "classed", "reference", "mesh"]

    #: The ramp name, or explicit stops as ``[[stop_0to1, "#rrggbb"], ...]``.
    colormap: str | list[tuple[float, str]] | None = None
    #: The ONE range the layer is read on. ``None`` for a reference layer.
    vmin: float | None = None
    vmax: float | None = None
    #: What the quantity is measured in, and what the legend calls it.
    units: str | None = None
    label: str | None = None
    #: The resolved preset as a QGIS ``.qml`` document - the ONE record of how
    #: the layer is painted, swatches and class breaks included, and what the
    #: map loads. ``None`` for a layer whose file already carries its colours:
    #: nothing may override the colours such a file already has.
    qml: str | None = None


class ModelSetup(GraceModel):
    """A staged, ready-to-run model. ``setup_uri`` points at the built
    artifacts, and ``parameters`` is solver-specific staging metadata validated
    at the engine layer rather than here."""

    schema_version: Literal["v1"] = "v1"

    setup_id: ULIDStr
    solver: str
    setup_uri: str  # the staged model inputs
    grid_resolution_m: float = Field(gt=0.0)
    bbox: tuple[float, float, float, float]
    parameters: dict = Field(default_factory=dict)
    created_at: UTCDatetime


class ExecutionHandle(GraceModel):
    """A submitted execution, and the CANCELLATION contract.
    The execution identifier is a first-class field, so a cancel terminates by
    it instead of parsing one out of a string. One handle, every backend."""

    schema_version: Literal["v1"] = "v1"

    handle_id: ULIDStr
    run_id: ULIDStr  # the run this execution backs
    solver: str
    compute_class: ComputeClass

    # --- Cancellation seam: the identifier a terminate is issued against, and
    # --- the definition and region it names.
    workflows_execution_id: str
    workflow_name: str
    workflow_location: str

    submitted_at: UTCDatetime


class RunResult(GraceModel):
    """The TERMINAL outcome of an execution.
    ``cancelled`` is a distinct status from ``failed``: a stopped run is a
    finished run, not a broken one."""

    schema_version: Literal["v1"] = "v1"

    run_id: ULIDStr
    handle_id: ULIDStr
    status: Literal["complete", "failed", "cancelled"]
    output_uri: str | None = None  # the raw solver output; None until complete
    started_at: UTCDatetime | None = None
    completed_at: UTCDatetime | None = None
    duration_seconds: float | None = None

    # Failure details (status == "failed")
    error_code: str | None = None
    error_message: str | None = None

    # Cancellation details (status == "cancelled")
    cancellation_reason: str | None = None

    # BEST-EFFORT capture of the instance and timing breakdown the run landed
    # on, so a completion-time model can later be inferred from real
    # measurements rather than guessed. ``None`` on an in-process run and on any
    # describe failure - the capture swallows its own exceptions, because a
    # missing measurement must never fail a finished run. Every key optional:
    # ``{instance_type, instance_lifecycle, az, vcpus, memory_mib,
    # created_at_ms, started_at_ms, stopped_at_ms, queue_provision_secs,
    # compute_secs, total_secs}``.
    batch_compute_meta: dict | None = None


class LayerURI(GraceModel):
    """ONE produced output layer, aligned field-for-field with the
    ``load-layer`` map command so it reaches the map untranslated. The producer
    DECLARES a style; the publish path RESOLVES it into ``legend``."""

    layer_id: str  # stable id; flows into the load-layer args
    name: str
    # ``mesh`` is an unstructured solver mesh, the ONE format a client STAGES
    # rather than streams.
    layer_type: Literal["raster", "vector", "mesh"]
    uri: str  # COG / FlatGeobuf / GeoParquet / UGRID netCDF
    #: The DECLARED style row - ``{kind, ramp, units, label, scale, classes,
    #: geometry, color}``. ``None`` = the kind's bare default.
    style: dict[str, Any] | None = None
    #: The PHYSICAL QUANTITY this layer carries, as its producer names it. A
    #: title is prose and may be rewritten; the quantity is the layer's
    #: identity, and it is what a still, a frame and an animation of one field
    #: are held to a single scale by.
    quantity: str | None = None
    temporal: TemporalConfig | None = None  # present iff time-varying
    role: Literal["primary", "context", "input"] = "primary"
    units: str | None = None
    # ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. When present, the
    # camera flies to it once the layer is added.
    bbox: tuple[float, float, float, float] | None = None
    legend: LegendKey | None = None  # the resolved style; None until published
    # Set ONLY when a fallback data source was substituted for the requested
    # primary. It names BOTH sources, so fallback data can never be mistaken for
    # the primary. ``None`` means the layer is exactly the requested source.
    fallback_note: str | None = None
    # The structured half of the same honesty: which rungs of a DECLARED ladder
    # served this layer, and the share each painted. A mosaic several rungs built
    # carries one row per rung. ``[]`` means no ladder governs this fetch, NEVER
    # "nothing was substituted".
    fallbacks: list[FallbackActivation] = Field(default_factory=list)
    # The physical model inputs this layer was built from, each tagged with
    # WHERE it came from. ``[]`` means no provenance has been declared yet, NOT
    # "all real" - which is why a narration renders these rather than assuming a
    # baked constant was measured.
    synthetic_inputs: list[SyntheticInput] = Field(default_factory=list)
    # The CRS authority id for a ``layer_type="mesh"`` row: a mesh reader
    # reports an empty CRS for these formats, so the run has to state it.
    # ``None`` for a raster or vector row, whose CRS rides in the bytes.
    crs_authid: str | None = None
    # The temporal twin of ``crs_authid``: the instant a mesh's dataset times
    # are counted from. A SELAFIN records no origin, so without this a scrubber
    # reads the run's first step as 1900. ``None`` for a raster or vector row.
    reference_time: str | None = None
    # The window this layer is valid for, as ISO-8601 UTC instants. One frame of
    # an ordered sequence states its own ``[valid_from, valid_to)`` and the map
    # stamps it straight on. The producer holds the instant already; a time
    # spelled into a display NAME and parsed back out is the same fact in the
    # wrong data class. ``None`` for a layer that is not one frame.
    valid_from: str | None = None
    valid_to: str | None = None


# --------------------------------------------------------------------------- #
# LayerURI SUBCLASS result models.
#
# A source whose result carries business fields computed POST-serialize from the
# produced bytes names its subclass in its ``source.yaml``; the subclass is built
# from the base layer plus a pure envelope hook's field dict. The subclasses live
# HERE rather than in a fetcher module, so the declarative surface needs no code
# of its own. ``LAYER_RESULT_MODELS`` is the name -> class table.
# --------------------------------------------------------------------------- #


class HighWaterMarksLayerURI(LayerURI):
    """A high-water-mark point layer plus its survey-quality envelope."""


    n_marks: int = 0
    #: The resolved flood-event name; ``None`` for a state-scoped fetch.
    event: str | None = None
    #: ``{label: count}`` over surveyor accuracy, mark type and vertical datum.
    quality_breakdown: dict[str, int] = {}
    type_breakdown: dict[str, int] = {}
    datum_summary: dict[str, int] = {}
    #: The PHYSICAL QUANTITY the elevation column carries: a water-surface
    #: elevation above the stated datum, NOT a depth above ground. Stated so a
    #: comparison never silently pairs this against a model depth raster.
    observed_quantity: str = "water_surface_elevation"
    caveats: list[str] = []
    notes: list[str] = []


class FaultSourcesResult(LayerURI):
    """An active-fault trace layer plus the kinematic source records."""


    catalog: str = "gem"
    #: Always equals ``len(faults)``.
    fault_count: int = 0
    #: Geometry trace, slip rate, dip, rake and seismogenic-depth band per
    #: record - what a deck builder turns into fault sources.
    faults: list[dict] = []
    source: str = "GEM Global Active Faults (harmonized)"
    #: Always ``None`` here: an empty AOI returns a bare record instead of a
    #: layer, so a populated note never rides a rendered result.
    note: str | None = None


class FloodExtentObservationResult(LayerURI):
    """An OBSERVED flood-extent layer plus its observation envelope."""


    product: str = "MCDWD_L3_F3_NRT"
    #: The resolved composite date, ISO.
    observation_date: str | None = None
    #: ``{class_label: pixel_count}``, nodata excluded.
    class_breakdown: dict[str, int] = {}
    #: The flood classes only, not every wet class.
    flood_pixel_count: int = 0
    flood_area_km2: float | None = None
    #: The detection limits and the provisional status, stated on the result.
    caveats: list[str] = []
    notes: list[str] = []


class LandcoverResult(LayerURI):
    """A landcover layer plus the sidecar a roughness mapping is validated on.
    The base layer is a frozen ``extra="forbid"`` contract, so carrying the
    vintage here keeps it typed rather than wrapped in a dict beside it."""


    #: The vintage the roughness mapping is validated against. ``None`` for a
    #: dataset that has no such mapping.
    nlcd_vintage_year: int | None = None
    dataset: str = "nlcd_2021"
    source: str = "mrlc-wcs"
    #: The pixel spacing actually DELIVERED, against the dataset's native.
    effective_resolution_m: int = 30
    native_resolution_m: int = 30
    #: True when coarsened above native for a large AOI, with the honest
    #: caveat beside it. The note is ``None`` at native resolution.
    downsampled: bool = False
    downsampling_note: str | None = None


class DemLayerURI(LayerURI):
    """A NO-FIELD subclass, carrying nothing beyond the base layer and
    serializing identically. It exists only because the seam that overrides an
    emitted layer id and name must be declared together with a result model."""


class TopobathyResult(LayerURI):
    """A merged coastal topo-bathymetry layer plus its FETCH-TIME provenance.
    Every field reports what actually PAINTED the merge, never what was selected
    for it: a tile can drop in between, and a selection-keyed claim would lie."""

    # The provenance travels through the recorder channel, so it survives a
    # cache hit that never re-runs the fetch. A cache object written before the
    # channel yields no sidecar, and these declared DEFAULTS then hold.

    #: True when a real below-waterline bed painted, from any bathymetric leg;
    #: False on the land-only degrade.
    bathymetry_present: bool = True
    #: An honest warning when the surface degraded - bathymetry absent, a global
    #: fallback bed, a missing land leg. ``None`` on the clean path, and NEVER a
    #: fabricated success.
    fallback_warning: str | None = None
    #: How many tiles of each fine leg painted; 0 when that leg did not run.
    cudem_tile_count: int = 0
    regional_tile_count: int = 0
    #: The MEASURED share each ladder rung's source painted, by rung name, so an
    #: activation row reports measured paint rather than a footprint promise.
    #: ``None`` when nothing measurable ran.
    rung_coverage: dict[str, float] | None = None


class BlueTopoResult(LayerURI):
    """A bathymetric surface layer plus its fetch-time provenance.
    Every field reports what the tile scheme said and what actually painted,
    never what was asked for.
    """


    #: The datum read off the tiles that painted, VERBATIM. It is orthometric
    #: rather than tidal, so the surface merges with a matching land DEM with no
    #: conversion step. Stated rather than assumed: a bed whose datum nobody
    #: carried is a bed nobody can merge.
    vertical_datum: str = "NAVD88"
    tile_count: int = 0
    #: The tile scheme's own resolution labels; a merge may span tiers.
    resolution_tiers: list[str] = Field(default_factory=list)
    #: The share of the requested AOI the selected footprints cover, measured
    #: against the tile scheme's geometry. This source is bathymetry ONLY and
    #: concentrates on navigable water, so a shore-spanning AOI is partially
    #: covered BY CONSTRUCTION - this number reports that, it is not a failure.
    coverage_fraction: float = 0.0
    #: The MEASURED share each ladder rung's source painted, by rung name.
    rung_coverage: dict[str, float] | None = None


class StormTracksLayerURI(LayerURI):
    """A storm-track layer plus the fetch-time provenance of which mode served.
    The provenance travels the recorder channel, so it survives a cache hit that
    never re-runs the fetch; without a sidecar these DEFAULTS hold."""


    #: ``"active"`` (a live feed) or ``"historical"`` (an archive).
    mode: str = "historical"
    #: Distinct storms in the layer, and the names or ids attributed to them.
    storm_count: int = 0
    storm_names: list[str] = []


class GOESSatelliteLayerURI(LayerURI):
    """A single-band satellite imagery layer plus its scan provenance.
    ``scan_time`` is UNRECOVERABLE from the produced file, so without the
    recorder channel a cache hit would lose which scan served.
    """


    #: The canonical bird token that served, and the band emitted.
    satellite: str = "goes-19"
    band: str = "visible"
    #: The chosen scan's ISO start time. ``None`` on a pre-channel cache object.
    scan_time: str | None = None


class NWMStreamflowLayerURI(LayerURI):
    """A point-streamflow layer plus the fetch-time provenance of a COMPOSITE.
    ``reference_time`` is unrecoverable from the produced file, so the recorder
    channel is what makes it survive a cache hit."""


    #: The model configuration that served.
    product: str = "analysis_assim"
    #: The resolved cycle's ISO valid time. ``None`` on a pre-channel object.
    reference_time: str | None = None
    #: Joined reach points in the layer, against the reach ids the bbox sample
    #: discovered. The second is always >= the first.
    reach_count: int = 0
    nldi_comids_discovered: int = 0


class LivingAtlasLayerURI(LayerURI):
    """A discovered third-party layer plus its CURATION label.
    The label exists so community-curated content can never be mistaken for
    authoritative content by a reader or a model.
    """


    curation: Literal["authoritative", "community"] = "community"
    item_id: str = ""
    #: "Image Service" | "Feature Service" | "Map Service".
    service_type: str = ""
    #: Item id, curation, service type and url, owner, source string.
    provenance: dict[str, Any] = Field(default_factory=dict)


#: name -> subclass. A spec's ``output.result_model`` resolves here; a name
#: absent from this table is a registration error, not a fallback.
LAYER_RESULT_MODELS: dict[str, type[LayerURI]] = {
    "HighWaterMarksLayerURI": HighWaterMarksLayerURI,
    "FaultSourcesResult": FaultSourcesResult,
    "FloodExtentObservationResult": FloodExtentObservationResult,
    "LandcoverResult": LandcoverResult,
    "DemLayerURI": DemLayerURI,
    "TopobathyResult": TopobathyResult,
    "BlueTopoResult": BlueTopoResult,
    "StormTracksLayerURI": StormTracksLayerURI,
    "GOESSatelliteLayerURI": GOESSatelliteLayerURI,
    "NWMStreamflowLayerURI": NWMStreamflowLayerURI,
    "LivingAtlasLayerURI": LivingAtlasLayerURI,
}


# --------------------------------------------------------------------------- #
# Resolve the envelope-side ``LegendKey`` forward reference.
# --------------------------------------------------------------------------- #
# ``ResultLayer`` mirrors ``LayerURI.legend`` but cannot import ``LegendKey`` at
# module scope: this module imports the envelope one, so the reverse would be
# circular. It therefore carries a STRING forward-ref, and the models that use
# it are rebuilt here, where the envelope module is fully loaded. The envelope
# embeds ``ResultLayer``, so it is rebuilt too. Idempotent.
from . import envelope as _envelope  # noqa: E402  (deferred to break the import cycle)

_envelope.ResultLayer.model_rebuild(
    _types_namespace={**vars(_envelope), "LegendKey": LegendKey}
)
_envelope.AssessmentEnvelope.model_rebuild(
    _types_namespace={**vars(_envelope), "LegendKey": LegendKey},
    force=True,
)
