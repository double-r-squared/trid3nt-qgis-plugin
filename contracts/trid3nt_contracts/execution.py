"""The solver-execution shapes: handle, result, layer.

``LayerURI`` aligns field-for-field with the ``load-layer`` map command, so a
produced layer reaches the map without translation; rasters are COG, vectors FlatGeobuf or GeoParquet. There is ONE handle type -
cancellation reads a first-class field on it rather than parsing a string.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .common import ContractModel, SyntheticInput, ULIDStr, UTCDatetime

__all__ = [
    "ExecutionHandle",
    "RunResult",
    "LegendKey",
    "LayerURI",
    "HighWaterMarksLayerURI",
    "FaultSourcesResult",
    "FloodExtentObservationResult",
    "LandcoverResult",
    "DemLayerURI",
    "BlueTopoResult",
    "StormTracksLayerURI",
    "NWMStreamflowLayerURI",
    "LAYER_RESULT_MODELS",
]


class LegendKey(ContractModel):
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
    #: What the quantity is measured in - the suffix on every legend tick.
    units: str | None = None
    #: Where the field STOPS BEING DRAWN, when it has such an edge. ``vmin`` IS
    #: this value on a floored field; a renderer reading the key masks below it
    #: rather than painting the ramp's bottom over an absent region.
    floor: float | None = None
    #: The resolved preset as a QGIS ``.qml`` document - the ONE record of how
    #: the layer is painted, swatches and class breaks included, and what the
    #: map loads. ``None`` for a layer whose file already carries its colours:
    #: nothing may override the colours such a file already has.
    qml: str | None = None


class ExecutionHandle(ContractModel):
    """A submitted execution, and the CANCELLATION contract.
    The execution identifier is a first-class field, so a cancel terminates by
    it instead of parsing one out of a string. One handle, every backend."""

    schema_version: Literal["v1"] = "v1"

    handle_id: ULIDStr
    run_id: ULIDStr  # the run this execution backs
    solver: str
    #: The partition this execution runs on - the SAME number the engine's own
    #: input file states, so the launcher and the deck describe one solve.
    cores: int = Field(ge=1)

    # --- Cancellation seam: the identifier a terminate is issued against, and
    # --- the definition and region it names.
    workflows_execution_id: str
    workflow_name: str
    workflow_location: str

    submitted_at: UTCDatetime


class RunResult(ContractModel):
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


class LayerURI(ContractModel):
    """ONE produced output layer, aligned field-for-field with the
    ``load-layer`` map command so it reaches the map untranslated. The producer
    DECLARES a style; the publish path RESOLVES it into ``legend``."""

    layer_id: str  # stable id; flows into the load-layer args
    name: str
    # ``mesh`` is an unstructured solver mesh, the ONE format a client STAGES
    # rather than streams.
    layer_type: Literal["raster", "vector", "mesh"]
    uri: str  # COG / FlatGeobuf / GeoParquet / UGRID netCDF
    #: The DECLARED style row - ``{kind, ramp, units, scale, classes, geometry,
    #: color, floor, range}``. ``None`` = the kind's bare default.
    style: dict[str, Any] | None = None
    #: The PHYSICAL QUANTITY this layer carries, as its producer names it. A
    #: title is prose and may be rewritten; the quantity is the layer's
    #: identity, and it is what a still, a frame and an animation of one field
    #: are held to a single scale by.
    quantity: str | None = None
    #: WHICH tracer of the run this layer carries, counted from 1 in the order
    #: the deck declares them. A tracer's NAME is the run's own - a named
    #: release renames it, and the quantity moves with it - so the position is
    #: the only stable way to ask for one. ``None`` on a row that is not a tracer.
    tracer: int | None = None
    role: Literal["primary", "context", "input"] = "primary"
    #: WHERE the layer came from, read by a person. ``user`` is a file the user
    #: pushed in themselves; ``None`` is the system saying nothing. Nothing
    #: branches on it - a reader overrides with confidence, or does not.
    origin: Literal["user"] | None = None
    units: str | None = None
    # ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. When present, the
    # camera flies to it once the layer is added.
    bbox: tuple[float, float, float, float] | None = None
    legend: LegendKey | None = None  # the resolved style; None until published
    # Every sentence the producer and the run state about this layer, one per
    # entry, read by the person and the model alike. A substitution of a
    # fallback source names BOTH sources here, so fallback data can never be
    # mistaken for the primary.
    notes: list[str] = Field(default_factory=list)
    # The physical model inputs this layer was built from, each tagged with
    # WHERE it came from. ``[]`` means no provenance has been declared yet, NOT
    # "all real" - which is why a narration renders these rather than assuming a
    # baked constant was measured.
    synthetic_inputs: list[SyntheticInput] = Field(default_factory=list)
    # The MDAL dataset files a ``layer_type="mesh"`` row carries beside its own
    # groups: a derived group the run wrote next to the mesh it was measured
    # over, loaded onto the layer before the declared group is bound. ``[]`` for
    # every other row, and for a mesh whose file already carries what it paints.
    dataset_uris: list[str] = Field(default_factory=list)
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
    #: The ZERO this layer's elevations are counted from, as its own source
    #: states it. Two surfaces are on one axis only when this agrees, so a
    #: consumer that places one over another reads it HERE - on the layer it was
    #: handed - rather than looking the producer's row up by name. ``None`` is
    #: the layer saying nothing, which is never the same as NAVD88.
    vertical_datum: str | None = None
    #: The shift the SOURCE publishes about its own zero: ``datum_offset_m``
    #: added to a value on ``vertical_datum`` reads it on ``datum_offset_frame``.
    #: The two ride together - metres onto no named frame are a shift nothing
    #: can check - and ``None`` is the layer publishing none.
    datum_offset_m: float | None = None
    datum_offset_frame: str | None = None

    @classmethod
    def published(cls, prefix: str, *, seed: str, **fields: Any) -> "LayerURI":
        """A layer THIS RUN produced, its id minted as ``<prefix>-<seed>``.

        The seed is the caller's because the artifact's file carries it too: one
        stem names the record and the bytes, so a reader holding either can find
        the other."""
        return cls(layer_id=f"{prefix}-{seed}", **fields)


def layer_seed() -> str:
    """A fresh stem for one produced layer - its id and its file share it."""
    import uuid

    return uuid.uuid4().hex[:8]


# LayerURI SUBCLASS result models.
#
# A source whose result carries business fields computed POST-serialize from the
# produced bytes names its subclass in its ``source.yaml``; the subclass is built
# from the base layer plus a pure envelope hook's field dict. The subclasses live
# HERE rather than in a fetcher module, so the declarative surface needs no code
# of its own. ``LAYER_RESULT_MODELS`` is the name -> class table.


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


#: name -> subclass. A row's ``output.result_model`` resolves here; a name
#: absent from this table is a registration error, not a fallback.
LAYER_RESULT_MODELS: dict[str, type[LayerURI]] = {
    "HighWaterMarksLayerURI": HighWaterMarksLayerURI,
    "FaultSourcesResult": FaultSourcesResult,
    "FloodExtentObservationResult": FloodExtentObservationResult,
    "LandcoverResult": LandcoverResult,
    "DemLayerURI": DemLayerURI,
    "BlueTopoResult": BlueTopoResult,
    "StormTracksLayerURI": StormTracksLayerURI,
    "NWMStreamflowLayerURI": NWMStreamflowLayerURI,
}
